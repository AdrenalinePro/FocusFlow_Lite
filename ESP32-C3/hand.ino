#include <Arduino.h>
#include <NimBLEDevice.h>

#if __has_include("esp_arduino_version.h")
#include "esp_arduino_version.h"
#endif

/*
 * ESP32-C3 wristband
 *
 * BLE role:
 *   - This device is a GATT Client / Central.
 *   - The external Linux development board is a GATT Server / Peripheral.
 *   - Application data is sent only by the development board through a
 *     Notification characteristic.
 *
 * Command packet, exactly 3 bytes, little endian:
 *   byte 0: intensity, 0..100, mapped to PWM duty cycle
 *   byte 1: repeat count low byte
 *   byte 2: repeat count high byte
 *
 * One repeat is:
 *   output ON for 500 ms, then output OFF for 500 ms.
 *
 * GPIO0: vibration motor control PWM
 * GPIO1: front/user-notification LED control PWM
 * GPIO8: onboard status LED, active-low (LOW = on, HIGH = off)
 */

// ----------------------------- Pins and PWM -----------------------------

constexpr uint8_t MOTOR_PWM_PIN = 0;
constexpr uint8_t FRONT_LED_PWM_PIN = 1;
constexpr uint8_t STATUS_LED_PIN = 8;

constexpr uint32_t PWM_FREQUENCY_HZ = 5000;
constexpr uint8_t PWM_RESOLUTION_BITS = 8;
constexpr uint16_t PWM_MAX_DUTY = (1u << PWM_RESOLUTION_BITS) - 1u;

// Both pins share one LEDC channel, so the hardware guarantees the same
// duty cycle on GPIO0 and GPIO1.
constexpr uint8_t MOTION_PWM_CHANNEL = 0;

// ----------------------------- BLE protocol -----------------------------

// Change these UUIDs only if the Linux server uses different UUIDs.
constexpr char BLE_DEVICE_NAME[] = "ESP32-C3-Wristband";
constexpr char CONTROL_SERVICE_UUID[] =
    "7b3a0001-6a4f-4d91-9c10-123456789000";
constexpr char CONTROL_CHARACTERISTIC_UUID[] =
    "7b3a0002-6a4f-4d91-9c10-123456789000";

constexpr size_t COMMAND_PACKET_LENGTH = 3;
constexpr uint32_t SCAN_TIME_MS = 5000;

// ----------------------- Optional board address filter ------------------
// Both filters are intentionally disabled for the first prototype.
// The hand serial number is never checked: there is only one hand.

constexpr bool ENABLE_BOARD_ADDRESS_CHECK = false;
constexpr char EXPECTED_BOARD_ADDRESS[] = "AA:BB:CC:DD:EE:FF";

constexpr bool ENABLE_BOARD_WHITELIST = false;
constexpr const char *BOARD_ADDRESS_WHITELIST[] = {
    "AA:BB:CC:DD:EE:FF",
    // Add more board addresses here if the project later needs them.
};
constexpr size_t BOARD_ADDRESS_WHITELIST_SIZE =
    sizeof(BOARD_ADDRESS_WHITELIST) / sizeof(BOARD_ADDRESS_WHITELIST[0]);

// --------------------------- Application state --------------------------

struct MotionCommand {
  uint8_t intensity;
  uint16_t repeatCount;
};

enum class StatusMode : uint8_t {
  SEARCHING_FOR_BOARD,
  SERVICE_MATCHED,
};

static QueueHandle_t commandQueue = nullptr;
static NimBLEClient *bleClient = nullptr;
static const NimBLEAdvertisedDevice *advertisedBoard = nullptr;

static volatile bool connectRequested = false;
static volatile bool scanRequested = false;
static volatile bool serviceMatched = false;

static volatile StatusMode statusMode = StatusMode::SEARCHING_FOR_BOARD;
static StatusMode lastStatusMode = StatusMode::SEARCHING_FOR_BOARD;
static uint32_t statusModeStartedAt = 0;
static volatile bool stopMotionRequested = false;

static bool motionActive = false;
static bool motionPhaseOn = false;
static uint8_t motionDuty = 0;
static uint16_t remainingRepeats = 0;
static uint32_t motionPhaseStartedAt = 0;

// ----------------------------- PWM helpers -------------------------------

void setMotionDuty(uint8_t duty) {
  // Both outputs always receive the same duty. The output voltage amplitude
  // is not changed; only the PWM duty cycle changes.
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteChannel(MOTION_PWM_CHANNEL, duty);
#else
  ledcWrite(MOTION_PWM_CHANNEL, duty);
#endif
}

void stopMotion() {
  motionActive = false;
  motionPhaseOn = false;
  remainingRepeats = 0;
  setMotionDuty(0);
}

void startMotion(const MotionCommand &command) {
  if (command.intensity == 0 || command.repeatCount == 0) {
    stopMotion();
    return;
  }

  motionDuty = static_cast<uint8_t>(
      (static_cast<uint32_t>(command.intensity) * PWM_MAX_DUTY) / 100u);
  remainingRepeats = command.repeatCount;
  motionActive = true;
  motionPhaseOn = true;
  motionPhaseStartedAt = millis();
  setMotionDuty(motionDuty);
}

void updateMotion() {
  if (!motionActive) {
    return;
  }

  constexpr uint32_t PHASE_TIME_MS = 500;
  const uint32_t now = millis();

  if (now - motionPhaseStartedAt < PHASE_TIME_MS) {
    return;
  }

  motionPhaseStartedAt = now;

  if (motionPhaseOn) {
    // End of the 500 ms ON phase.
    motionPhaseOn = false;
    setMotionDuty(0);
    return;
  }

  // End of the 500 ms OFF phase: one complete repeat has finished.
  if (remainingRepeats > 0) {
    --remainingRepeats;
  }

  if (remainingRepeats == 0) {
    stopMotion();
    return;
  }

  motionPhaseOn = true;
  setMotionDuty(motionDuty);
}

void setupPwm() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttachChannel(MOTOR_PWM_PIN, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS,
                    MOTION_PWM_CHANNEL);
  ledcAttachChannel(FRONT_LED_PWM_PIN, PWM_FREQUENCY_HZ,
                    PWM_RESOLUTION_BITS, MOTION_PWM_CHANNEL);
#else
  ledcSetup(MOTION_PWM_CHANNEL, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  ledcAttachPin(MOTOR_PWM_PIN, MOTION_PWM_CHANNEL);
  ledcAttachPin(FRONT_LED_PWM_PIN, MOTION_PWM_CHANNEL);
#endif

  setMotionDuty(0);
}

// --------------------------- Status LED logic ----------------------------

void writeStatusLed(bool on) {
  // The onboard LED is active-low.
  digitalWrite(STATUS_LED_PIN, on ? LOW : HIGH);
}

void setStatusMode(StatusMode mode) {
  statusMode = mode;
  serviceMatched = (mode == StatusMode::SERVICE_MATCHED);
}

void updateStatusLed() {
  const uint32_t now = millis();

  if (statusMode != lastStatusMode) {
    lastStatusMode = statusMode;
    statusModeStartedAt = now;
  }

  const uint32_t elapsed = now - statusModeStartedAt;

  if (statusMode == StatusMode::SEARCHING_FOR_BOARD) {
    // Double flash every 1.8 s: ON 150 ms, OFF 150 ms, ON 150 ms, OFF.
    const uint32_t phase = elapsed % 1800u;
    const bool on = (phase < 150u) || (phase >= 300u && phase < 450u);
    writeStatusLed(on);
  } else {
    // One short flash every 3 seconds.
    writeStatusLed((elapsed % 3000u) < 120u);
  }
}

// --------------------------- Address filtering --------------------------

bool addressMatches(const NimBLEAddress &address, const char *expected) {
  const String actual(address.toString().c_str());
  return actual.equalsIgnoreCase(expected);
}

bool boardAddressAllowed(const NimBLEAdvertisedDevice *device) {
  const NimBLEAddress address = device->getAddress();

  if (ENABLE_BOARD_ADDRESS_CHECK) {
    if (EXPECTED_BOARD_ADDRESS[0] == '\0' ||
        !addressMatches(address, EXPECTED_BOARD_ADDRESS)) {
      return false;
    }
  }

  if (ENABLE_BOARD_WHITELIST) {
    bool found = false;
    for (size_t i = 0; i < BOARD_ADDRESS_WHITELIST_SIZE; ++i) {
      if (addressMatches(address, BOARD_ADDRESS_WHITELIST[i])) {
        found = true;
        break;
      }
    }
    if (!found) {
      return false;
    }
  }

  return true;
}

// ----------------------------- BLE callbacks ----------------------------

void notifyCallback(NimBLERemoteCharacteristic *characteristic,
                    uint8_t *data, size_t length, bool isNotify) {
  (void)characteristic;
  (void)isNotify;

  if (length != COMMAND_PACKET_LENGTH || commandQueue == nullptr) {
    Serial.printf("Ignored BLE packet with length %u\n",
                  static_cast<unsigned>(length));
    return;
  }

  const uint8_t intensity = data[0];
  if (intensity > 100) {
    Serial.printf("Ignored BLE packet: intensity=%u is out of range\n",
                  intensity);
    return;
  }

  MotionCommand command{};
  command.intensity = intensity;
  command.repeatCount = static_cast<uint16_t>(data[1]) |
                        (static_cast<uint16_t>(data[2]) << 8u);

  // Queue length is one: the newest command replaces an unfinished command.
  // This is suitable for the single-wristband prototype and prevents stale
  // motion commands from accumulating.
  xQueueOverwrite(commandQueue, &command);
}

class ClientCallbacks : public NimBLEClientCallbacks {
 public:
  void onConnect(NimBLEClient *client) override {
    Serial.printf("Connected to board: %s\n",
                  client->getPeerAddress().toString().c_str());
  }

  void onDisconnect(NimBLEClient *client, int reason) override {
    Serial.printf("Board disconnected, reason=%d\n", reason);
    serviceMatched = false;
    scanRequested = true;
    connectRequested = false;
    setStatusMode(StatusMode::SEARCHING_FOR_BOARD);
    stopMotionRequested = true;
  }
};

class ScanCallbacks : public NimBLEScanCallbacks {
 public:
  void onResult(const NimBLEAdvertisedDevice *device) override {
    if (!device->isAdvertisingService(NimBLEUUID(CONTROL_SERVICE_UUID))) {
      return;
    }

    if (!boardAddressAllowed(device)) {
      Serial.printf("Service matched but board address was rejected: %s\n",
                    device->getAddress().toString().c_str());
      return;
    }

    Serial.printf("Board service found at %s\n",
                  device->getAddress().toString().c_str());
    NimBLEDevice::getScan()->stop();
    advertisedBoard = device;
    connectRequested = true;
  }

  void onScanEnd(const NimBLEScanResults &results, int reason) override {
    Serial.printf("Scan ended, reason=%d, devices=%d\n", reason,
                  results.getCount());
    if (!connectRequested && !serviceMatched) {
      scanRequested = true;
    }
  }
};

static ClientCallbacks clientCallbacks;
static ScanCallbacks scanCallbacks;

void requestScan() {
  if (serviceMatched || connectRequested) {
    return;
  }

  scanRequested = false;
  setStatusMode(StatusMode::SEARCHING_FOR_BOARD);
  NimBLEDevice::getScan()->start(SCAN_TIME_MS, false, true);
}

bool connectToBoard() {
  if (advertisedBoard == nullptr) {
    return false;
  }

  if (bleClient == nullptr) {
    bleClient = NimBLEDevice::createClient();
    bleClient->setClientCallbacks(&clientCallbacks, false);
    bleClient->setConnectionParams(12, 24, 0, 150);
    bleClient->setConnectTimeout(5000);
  }

  if (!bleClient->connect(advertisedBoard)) {
    Serial.println("Could not connect to board");
    return false;
  }

  NimBLERemoteService *service =
      bleClient->getService(CONTROL_SERVICE_UUID);
  if (service == nullptr) {
    Serial.println("Control service not found after connection");
    bleClient->disconnect();
    return false;
  }

  NimBLERemoteCharacteristic *characteristic =
      service->getCharacteristic(CONTROL_CHARACTERISTIC_UUID);
  if (characteristic == nullptr || !characteristic->canNotify()) {
    Serial.println("Control characteristic is missing or not notify-capable");
    bleClient->disconnect();
    return false;
  }

  if (!characteristic->subscribe(true, notifyCallback)) {
    Serial.println("Could not subscribe to control notifications");
    bleClient->disconnect();
    return false;
  }

  serviceMatched = true;
  setStatusMode(StatusMode::SERVICE_MATCHED);
  Serial.println("Control service matched and notifications enabled");
  return true;
}

// -------------------------------- Arduino --------------------------------

void setup() {
  Serial.begin(115200);
  delay(100);

  pinMode(STATUS_LED_PIN, OUTPUT);
  writeStatusLed(false);
  setupPwm();

  commandQueue = xQueueCreate(1, sizeof(MotionCommand));
  if (commandQueue == nullptr) {
    Serial.println("ERROR: could not create command queue");
    while (true) {
      writeStatusLed(true);
      delay(100);
      writeStatusLed(false);
      delay(100);
    }
  }

  NimBLEDevice::init(BLE_DEVICE_NAME);
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setScanCallbacks(&scanCallbacks, false);
  scan->setActiveScan(true);
  scan->setInterval(45);
  scan->setWindow(15);

  setStatusMode(StatusMode::SEARCHING_FOR_BOARD);
  scanRequested = true;

  Serial.println("ESP32-C3 wristband started");
  Serial.printf("Control service: %s\n", CONTROL_SERVICE_UUID);
  Serial.printf("Control characteristic: %s\n",
                CONTROL_CHARACTERISTIC_UUID);
}

void loop() {
  updateStatusLed();

  if (scanRequested && !serviceMatched && !connectRequested) {
    requestScan();
  }

  if (connectRequested) {
    connectRequested = false;
    if (!connectToBoard()) {
      scanRequested = true;
      setStatusMode(StatusMode::SEARCHING_FOR_BOARD);
    }
  }

  if (stopMotionRequested) {
    stopMotionRequested = false;
    if (commandQueue != nullptr) {
      xQueueReset(commandQueue);
    }
    stopMotion();
  }

  MotionCommand command{};
  if (commandQueue != nullptr &&
      xQueueReceive(commandQueue, &command, 0) == pdTRUE) {
    Serial.printf("Command received: intensity=%u, repeats=%u\n",
                  command.intensity, command.repeatCount);
    startMotion(command);
  }

  updateMotion();
  delay(2);
}
