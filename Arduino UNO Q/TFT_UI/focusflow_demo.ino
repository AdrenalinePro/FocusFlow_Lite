/*
 * FocusFlow Lite v2.0 — Demo Sketch
 * Hardware: Arduino UNO Q (STM32U585) + ILI9341V 240x320 TFT (portrait)
 *
 * Two modes:
 *   1. Standalone demo — cycles through three screens with default params
 *   2. Serial-driven — parse JSON commands from Linux side via USB Serial
 *
 * ── JSON protocol (one JSON object per line, newline-terminated) ──
 *
 *   Focus screen:
 *     {"cmd":"focus","pct":82,"elapsed":1122,"total":1500,"screen":"专注工作","status":"高度专注"}
 *
 *   Alert screen:
 *     {"cmd":"alert","screen":"B站"}
 *
 *   Break screen:
 *     {"cmd":"break","remain":154,"next":1500}
 *
 *   All fields except "cmd" are optional — defaults used when missing.
 */

#include "focusflow_ui.h"

// ── Pin definitions (verified software SPI wiring) ──
#define PIN_CS    10
#define PIN_DC     9
#define PIN_MOSI  11    // ICSP pin 4
#define PIN_SCK   13    // ICSP pin 3
#define PIN_RST    8    // dedicated GPIO
#define PIN_LED    7    // PWM backlight
#define PIN_MISO  12    // ICSP pin 1 (unused)

// ── Default parameters (standalone demo) ──
#define DEMO_FOCUS_PCT       82
#define DEMO_ELAPSED_SEC     1122
#define DEMO_TOTAL_SEC       1500
#define DEMO_SCREEN_LABEL    "VS Code"
#define DEMO_STATUS_TEXT     "高度专注"
#define DEMO_ALERT_LABEL     "B站"
#define DEMO_BREAK_REMAIN    154
#define DEMO_NEXT_SESSION    1500

#define DEMO_INTERVAL_MS     5000
#define CMD_BUF_SIZE         256

// ── Global objects ──
FocusFlowUI ui(PIN_CS, PIN_DC, PIN_MOSI, PIN_SCK, PIN_RST, PIN_LED, PIN_MISO);

char    cmdBuf[CMD_BUF_SIZE];
uint8_t cmdIdx = 0;

// ────────────────────────────────────────────────────────────
//  JSON helpers (minimal, no library dependency)
// ────────────────────────────────────────────────────────────

/* Extract string value: "key":"value" → returns pointer to value copy */
static const char* jsonGetStr(const char* json, const char* key) {
    // Build search pattern: "key":"
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);
    const char* p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    const char* end = p;
    while (*end && *end != '"') end++;
    // Copy out
    static char buf[64];
    size_t len = end - p;
    if (len >= sizeof(buf)) len = sizeof(buf) - 1;
    memcpy(buf, p, len);
    buf[len] = '\0';
    return buf;
}

/* Extract integer value: "key":123 → returns value, or defVal if not found */
static long jsonGetLong(const char* json, const char* key, long defVal) {
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\":", key);
    const char* p = strstr(json, pattern);
    if (!p) return defVal;
    p += strlen(pattern);
    return atol(p);
}

// ────────────────────────────────────────────────────────────
//  Setup
// ────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(500);

    Serial.println("{\"status\":\"ready\",\"device\":\"FocusFlow TFT\"}");

    ui.begin();
    delay(200);
    ui.showFocusScreen(DEMO_FOCUS_PCT, DEMO_ELAPSED_SEC,
                       DEMO_TOTAL_SEC, DEMO_SCREEN_LABEL, DEMO_STATUS_TEXT);
}

// ────────────────────────────────────────────────────────────
//  Main loop
// ────────────────────────────────────────────────────────────

static unsigned long lastDemoSwitch = 0;
static uint8_t      demoPhase      = 0;
static bool          serialActive   = false;

void loop() {
    // ── Read serial line ──
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (cmdIdx > 0) {
                cmdBuf[cmdIdx] = '\0';
                handleCommand(cmdBuf);
                cmdIdx = 0;
                serialActive = true;
                lastDemoSwitch = millis();
            }
        } else if (cmdIdx < CMD_BUF_SIZE - 1) {
            cmdBuf[cmdIdx++] = c;
        }
    }

    // ── Demo cycle (when no serial traffic for >30s) ──
    unsigned long now = millis();
    if (serialActive && (now - lastDemoSwitch > 30000)) {
        serialActive = false;      // resume demo
        lastDemoSwitch = now;
    }
    if (!serialActive && (now - lastDemoSwitch > DEMO_INTERVAL_MS)) {
        lastDemoSwitch = now;
        demoPhase = (demoPhase + 1) % 3;
        switch (demoPhase) {
            case 0:
                ui.showFocusScreen(DEMO_FOCUS_PCT, DEMO_ELAPSED_SEC,
                                   DEMO_TOTAL_SEC, DEMO_SCREEN_LABEL,
                                   DEMO_STATUS_TEXT);
                break;
            case 1:
                ui.showAlertScreen(DEMO_ALERT_LABEL);
                break;
            case 2:
                ui.showBreakScreen(DEMO_BREAK_REMAIN, DEMO_NEXT_SESSION);
                break;
        }
    }
}

// ────────────────────────────────────────────────────────────
//  Command dispatcher
// ────────────────────────────────────────────────────────────

void handleCommand(const char* json) {
    const char* cmd = jsonGetStr(json, "cmd");
    if (!cmd) return;   // malformed

    if (strcmp(cmd, "focus") == 0) {
        uint8_t  pct     = (uint8_t) jsonGetLong(json, "pct",      DEMO_FOCUS_PCT);
        uint32_t elapsed = (uint32_t) jsonGetLong(json, "elapsed", DEMO_ELAPSED_SEC);
        uint32_t total   = (uint32_t) jsonGetLong(json, "total",   DEMO_TOTAL_SEC);
        const char* scr  = jsonGetStr(json, "screen");
        const char* st   = jsonGetStr(json, "status");

        if (pct > 100) pct = 100;

        ui.showFocusScreen(pct, elapsed, total,
                           scr   ? scr   : DEMO_SCREEN_LABEL,
                           st    ? st    : DEMO_STATUS_TEXT);

    } else if (strcmp(cmd, "alert") == 0) {
        const char* scr = jsonGetStr(json, "screen");
        ui.showAlertScreen(scr ? scr : DEMO_ALERT_LABEL);

    } else if (strcmp(cmd, "break") == 0) {
        uint32_t remain = (uint32_t) jsonGetLong(json, "remain", DEMO_BREAK_REMAIN);
        uint32_t next   = (uint32_t) jsonGetLong(json, "next",   DEMO_NEXT_SESSION);
        ui.showBreakScreen(remain, next);

    } else if (strcmp(cmd, "ping") == 0) {
        Serial.println("{\"status\":\"pong\"}");
    }
}
