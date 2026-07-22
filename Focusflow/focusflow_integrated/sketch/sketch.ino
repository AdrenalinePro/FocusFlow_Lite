/*
 * FocusFlow Integration — MCU Sketch (UNO Q STM32U585)
 *
 * Drives an ILI9341V 240x320 TFT and exposes two Bridge verbs to the
 * Linux MPU side:
 *
 *   tft_cmd(<json>)        — render the screen described by ``json``.
 *                            Same JSON shape that the original
 *                            focusflow_demo.ino accepted over Serial:
 *
 *                              {"cmd":"focus","pct":82,"elapsed":1122,
 *                               "total":1500,"screen":"VS Code",
 *                               "status":"高度专注"}
 *                              {"cmd":"alert","screen":"B站"}
 *                              {"cmd":"break","remain":154,"next":1500}
 *                              {"cmd":"ping"}
 *
 *                            Unknown / malformed JSON is silently ignored
 *                            (the MPU side decides whether to log).
 *
 *   tft_status()           — internal helper that returns the
 *                            current TFT health ("running" / "offline").
 *                            Exposed to the MPU through the periodic
 *                            Bridge.notify("tft_heartbeat", ...) push,
 *                            not as a Bridge.provide handler (the sketch
 *                            side of Bridge.provide cannot reply).
 *
 * The sketch also pushes Bridge.notify("tft_heartbeat", state) every
 * TFT_HEARTBEAT_MS milliseconds so the MPU can include the latest
 * state in the ``device_status`` it sends back to the laptop.
 *
 * JSON parsing helpers (jsonGetStr / jsonGetLong) are copied verbatim
 * from source_code/TFT_UI/focusflow_demo.ino — they are zero-dep,
 * pure strstr + atol.  See that file for the rationale.
 */

#include <Arduino_RouterBridge.h>
#include "focusflow_ui.h"

// ── Pin definitions (verified software SPI wiring on UNO Q) ──
#define PIN_CS    10
#define PIN_DC     9
#define PIN_MOSI  11    // ICSP pin 4
#define PIN_SCK   13    // ICSP pin 3
#define PIN_RST    8    // dedicated GPIO (NRST has no reset pulse)
#define PIN_LED    7    // PWM backlight
#define PIN_MISO  12    // ICSP pin 1 (unused, software SPI)

// ── Default parameters (boot splash) ──
#define DEFAULT_PCT         82
#define DEFAULT_ELAPSED     1122
#define DEFAULT_TOTAL       1500
#define DEFAULT_SCREEN      "VS Code"
#define DEFAULT_STATUS      "高度专注"
#define DEFAULT_ALERT       "B站"
#define DEFAULT_BREAK_REM   154
#define DEFAULT_NEXT_SESS   1500

// ── Buffer / heartbeat ──
#define CMD_BUF_SIZE        256
#define TFT_HEARTBEAT_MS    5000UL

// ── Global objects ──
FocusFlowUI ui(PIN_CS, PIN_DC, PIN_MOSI, PIN_SCK, PIN_RST, PIN_LED, PIN_MISO);

static char    cmdBuf[CMD_BUF_SIZE];
static uint8_t cmdIdx = 0;
static bool    tftReady  = false;
static bool    lastRenderOk = true;
static unsigned long lastHeartbeatAt = 0;

// ────────────────────────────────────────────────────────────
//  Minimal JSON helpers (no library dependency)
// ────────────────────────────────────────────────────────────

static const char* jsonGetStr(const char* json, const char* key) {
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);
    const char* p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    const char* end = p;
    while (*end && *end != '"') end++;
    static char buf[64];
    size_t len = end - p;
    if (len >= sizeof(buf)) len = sizeof(buf) - 1;
    memcpy(buf, p, len);
    buf[len] = '\0';
    return buf;
}

static long jsonGetLong(const char* json, const char* key, long defVal) {
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\":", key);
    const char* p = strstr(json, pattern);
    if (!p) return defVal;
    p += strlen(pattern);
    return atol(p);
}

// ────────────────────────────────────────────────────────────
//  TFT render dispatcher
// ────────────────────────────────────────────────────────────

static void renderFromJson(const char* json) {
    if (!json || !*json) return;
    if (!tftReady) return;

    const char* cmd = jsonGetStr(json, "cmd");
    if (!cmd) return;

    if (strcmp(cmd, "focus") == 0) {
        uint8_t  pct     = (uint8_t) jsonGetLong(json, "pct",     DEFAULT_PCT);
        uint32_t elapsed = (uint32_t)jsonGetLong(json, "elapsed", DEFAULT_ELAPSED);
        uint32_t total   = (uint32_t)jsonGetLong(json, "total",   DEFAULT_TOTAL);
        const char* scr  = jsonGetStr(json, "screen");
        const char* st   = jsonGetStr(json, "status");
        if (pct > 100) pct = 100;
        ui.showFocusScreen(pct, elapsed, total,
                           scr ? scr : DEFAULT_SCREEN,
                           st  ? st  : DEFAULT_STATUS);

    } else if (strcmp(cmd, "alert") == 0) {
        const char* scr = jsonGetStr(json, "screen");
        ui.showAlertScreen(scr ? scr : DEFAULT_ALERT);

    } else if (strcmp(cmd, "break") == 0) {
        uint32_t remain = (uint32_t)jsonGetLong(json, "remain", DEFAULT_BREAK_REM);
        uint32_t next   = (uint32_t)jsonGetLong(json, "next",   DEFAULT_NEXT_SESS);
        ui.showBreakScreen(remain, next);

    } else if (strcmp(cmd, "ping") == 0) {
        // Echo back through the MPU rather than Serial: a ``ping`` is
        // an MPU-level liveness probe and should not pollute USB.
        // We still acknowledge it by re-rendering the focus screen so
        // the MPU knows the TFT pipeline is healthy.
        ui.showFocusScreen(DEFAULT_PCT, DEFAULT_ELAPSED, DEFAULT_TOTAL,
                           DEFAULT_SCREEN, DEFAULT_STATUS);

    } else {
        // Unknown cmd — ignore.  MPU decides whether to log.
        return;
    }

    lastRenderOk = true;
}

// ────────────────────────────────────────────────────────────
//  Bridge handlers (MPU → MCU)
// ────────────────────────────────────────────────────────────

/*
 * tft_cmd(json_str)  — render the screen described by ``json_str``.
 *
 * Registered with Arduino_RouterBridge so the Linux MPU side can push
 * JSON-over-Bridge exactly the same way the original demo sketch
 * accepted JSON-over-USB-Serial.
 */
void tft_cmd(String json) {
    if (json.length() == 0) return;
    // Bound the copy: the Bridge transport already chunks long strings,
    // but CMD_BUF_SIZE is a safety net against malformed peers.
    size_t n = json.length();
    if (n >= CMD_BUF_SIZE) n = CMD_BUF_SIZE - 1;
    memcpy(cmdBuf, json.c_str(), n);
    cmdBuf[n] = '\0';
    renderFromJson(cmdBuf);
}

/*
 * tft_status()  — return the current TFT health as a String.
 *
 * Sketch-side Bridge.provide handlers are void and the wire protocol
 * has no return-value slot, so this helper is invoked from the
 * Bridge.notify("tft_heartbeat", ...) push in loop() rather than
 * exposed as a Bridge verb.  The MPU receives the value via its own
 * Bridge.provide("tft_heartbeat", ...) callback.
 */
String tft_status() {
    if (!tftReady) return String("offline");
    if (!lastRenderOk) return String("error");
    return String("running");
}

// ────────────────────────────────────────────────────────────
//  Setup / loop
// ────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(300);

    // LED on (active-low: LOW == ON)
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);

    // TFT — FocusFlowUI::begin() returns void, so we cannot use its
    // return value to detect a missing display.  Assume the SPI bus
    // and ILI9341 are wired correctly (matches focusflow_demo.ino);
    // tftReady stays true and the MPU reports tft_display="running"
    // until the operator notices a blank screen.  A future revision
    // could probe via Adafruit_ILI9341::begin() directly, but that
    // would require touching focusflow_ui.h's private ``tft`` member.
    ui.begin();
    tftReady = true;
    ui.showFocusScreen(DEFAULT_PCT, DEFAULT_ELAPSED, DEFAULT_TOTAL,
                       DEFAULT_SCREEN, DEFAULT_STATUS);

    // Bridge — bring up last so the handlers are registered before the
    // MPU side starts dispatching.
    Bridge.begin();
    Bridge.provide("tft_cmd", tft_cmd);
    // Bridge.provide("tft_status", ...) intentionally omitted:
    // handlers on the sketch side are void and cannot reply
    // synchronously.  Status flows via tft_heartbeat below.

    Serial.println("{\"status\":\"mcu_ready\"}");
}

void loop() {
    unsigned long now = millis();
    if (now - lastHeartbeatAt >= TFT_HEARTBEAT_MS) {
        lastHeartbeatAt = now;
        // Fire-and-forget: the MPU just updates its bookkeeping.
        Bridge.notify("tft_heartbeat", tft_status());
    }
}
