/*
 * FocusFlow UI — TFT display driver for FocusFlow Lite v2.0
 * Hardware: Arduino UNO Q (STM32U585) + ILI9341V 240x320 TFT (portrait)
 * Software SPI, tested at 10MHz
 */

#ifndef FOCUSFLOW_UI_H
#define FOCUSFLOW_UI_H

#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include "focus_chinese_font.h"

// ── Screen (portrait orientation) ──
#define SCREEN_W  240
#define SCREEN_H  320

// ── Layout constants (portrait: 240×320) ──
#define HEADER_H   40              // title bar
#define HEADER_Y   0
#define MARGIN_X   10              // card side margin
#define ROW_H      50              // standard card height
#define BAR_ROW_H  62              // focus-bar row (taller)
#define GAP        12              // vertical gap between cards

// Derived: first card Y, bottom hint Y
#define CONTENT_Y   (HEADER_H + 12)                 // 52
#define BOTTOM_Y    304                               // thin bottom bar
#define BOTTOM_H    16

// ── Colors (RGB565) ──
#define C_BG             0x0841   // #0B132B  dark navy background
#define C_HEADER_FOCUS   0x1A6E   // #1E6F95  focus header bar
#define C_HEADER_ALERT   0xA104   // #A32020  alert header bar
#define C_ALERT          0xD904   // #D03333  vivid red, alert text
#define C_HEADER_BREAK   0x2D04   // #2E8068  break header bar
#define C_CARD_BG        0x0C63   // #0E1A3A  card/row background
#define C_TEXT           0xFFFF   // white
#define C_TEXT_DIM       0x8C71   // dim gray
#define C_ACCENT         0x57FD   // #5FC3F7  light blue accent
#define C_FOCUS_HIGH     0x2784   // #26C050  green (≥70%)
#define C_FOCUS_MID      0xFD08   // #F5A820  yellow (40-69%)
#define C_FOCUS_LOW      0xD904   // #D04020  red (<40%)
#define C_WARN           0xFD08   // yellow warning
#define C_BREAK          0x36A2   // #36B0A0  teal
#define C_DIV            0x18A3   // divider line (unused, reserved)
#define C_TOMATO         0xD925   // tomato red icon
#define C_OK             0x2784   // green checkmark
#define C_BOTTOM_BG      0x0841   // bottom bar bg (unused, reserved)

// ── Screen IDs ──
enum ScreenID {
    SCREEN_NONE = 0,
    SCREEN_FOCUS,
    SCREEN_ALERT,
    SCREEN_BREAK
};

class FocusFlowUI {
public:
    /*
     * Constructor — 6-param software SPI.
     * cs=D10, dc=D9, mosi=ICSP4, sck=ICSP3, rst=D8, led=D7, miso=D12 (unused)
     */
    FocusFlowUI(uint8_t cs,  uint8_t dc,  uint8_t mosi,
                uint8_t sck, uint8_t rst, uint8_t led,
                uint8_t miso = 255);

    void begin();
    void setBacklight(uint8_t brightness); // 0 (off) ~ 255 (max)

    // ── Three core UI entry points ──

    /*
     * Focus / studying screen.
     *   focusLevel  0-100  — attention percentage
     *   elapsedSec         — seconds studied so far
     *   totalSec           — planned session length in seconds
     *   screenLabel        — current app/window name (UTF-8)
     *   statusText         — focus level description, default "高度专注"
     */
    void showFocusScreen(uint8_t  focusLevel,
                         uint32_t elapsedSec,
                         uint32_t totalSec,
                         const char* screenLabel,
                         const char* statusText = "高度专注");

    /*
     * Distraction alert screen — covers full display immediately.
     *   screenLabel  — the distracting app name (e.g. "B站")
     */
    void showAlertScreen(const char* screenLabel);

    /*
     * Break screen.
     *   breakRemainingSec  — seconds left in break
     *   nextSessionSec     — next study session length in seconds
     */
    void showBreakScreen(uint32_t breakRemainingSec,
                         uint32_t nextSessionSec);

private:
    Adafruit_ILI9341 tft;
    uint8_t _led;
    uint8_t _curScreen;

    // ── Drawing primitives ──
    void drawHeader(const char* title, uint16_t barColor);
    void drawBottom(const char* hint, uint16_t barColor);
    void drawCard(int y, int h, uint16_t color);

    // label:value row inside content area
    void drawDataRow(int y, const char* label,
                     const char* valueStr,
                     uint16_t labelColor, uint16_t valueColor);

    // progress bar + percentage
    void drawFocusBar(int y, uint8_t pct);

    // small pixel icons
    void drawTomato(int x, int y);
    void drawCheckmark(int x, int y);
    void drawWarningBig(int x, int y);

    // ── Chinese text helpers ──
    void drawCN(int x, int y, uint8_t idx,
                uint16_t fg, uint16_t bg);
    void drawCNString(int x, int y, const char* utf8,
                      uint16_t fg, uint16_t bg);
    int8_t findCN(const char* utf8);

    // ── Utility ──
    void fmtTime(char* buf, uint32_t seconds); // "MM:SS"
    uint16_t focusPctColor(uint8_t pct);
};

#endif // FOCUSFLOW_UI_H
