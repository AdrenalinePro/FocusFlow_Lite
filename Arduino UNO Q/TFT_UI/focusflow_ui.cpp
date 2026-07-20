/*
 * FocusFlow UI — implementation
 * Target: Arduino UNO Q (STM32U585) + ILI9341V 240x320 TFT (portrait, sw SPI)
 */

#include "focusflow_ui.h"
#include <string.h>
#include <stdio.h>

// Forward declaration
static int cnStrLen(const char* utf8);
static int utf8PixelWidth(const char* utf8);

// ────────────────────────────────────────────────────────────
//  Constructor
// ────────────────────────────────────────────────────────────

FocusFlowUI::FocusFlowUI(uint8_t cs, uint8_t dc, uint8_t mosi,
                         uint8_t sck, uint8_t rst, uint8_t led, uint8_t miso)
    : tft(cs, dc, mosi, sck, rst, miso),
      _led(led),
      _curScreen(SCREEN_NONE)
{}

// ────────────────────────────────────────────────────────────
//  Public API
// ────────────────────────────────────────────────────────────

void FocusFlowUI::begin() {
    tft.begin(60000000);
    tft.setRotation(0);        // portrait: 240×320, connector at bottom
    tft.fillScreen(C_BG);
    pinMode(_led, OUTPUT);
    setBacklight(200);
    tft.setTextWrap(false);
}

void FocusFlowUI::setBacklight(uint8_t brightness) {
    analogWrite(_led, brightness);
}

// ────────────────────────────────────────────────────────────
//  Focus Screen
// ────────────────────────────────────────────────────────────

void FocusFlowUI::showFocusScreen(uint8_t focusLevel,
                                  uint32_t elapsedSec,
                                  uint32_t totalSec,
                                  const char* screenLabel,
                                  const char* statusText) {
    tft.fillScreen(C_BG);
    drawHeader("FocusFlow Lite v2.0", C_HEADER_FOCUS);

    int y = CONTENT_Y;

    // Card 1: 状态 label
    drawCard(y, ROW_H, C_CARD_BG);
    drawDataRow(y, "状态", statusText, C_TEXT_DIM, C_ACCENT);
    y += ROW_H + GAP;   // → 114

    // Card 2: 专注度 + progress bar
    drawCard(y, BAR_ROW_H, C_CARD_BG);
    {
        drawCNString(MARGIN_X + 6, y + 8, "专注度", C_TEXT_DIM, C_CARD_BG);
        drawFocusBar(y + 28, focusLevel);
    }
    y += BAR_ROW_H + GAP;   // → 188

    // Card 3: timer 学习中 MM:SS / MM:SS
    drawCard(y, ROW_H, C_CARD_BG);
    {
        drawTomato(MARGIN_X + 6, y + 14);
        drawCNString(MARGIN_X + 28, y + 14, "学习中", C_TEXT, C_CARD_BG);

        char timeBuf[8];
        fmtTime(timeBuf, elapsedSec);
        // right-align elapsed time
        tft.setCursor(SCREEN_W - MARGIN_X - 60, y + 12);
        tft.setTextColor(C_ACCENT, C_CARD_BG);
        tft.setTextSize(2);
        tft.print(timeBuf);

        // "/MM:SS" smaller below
        fmtTime(timeBuf, totalSec);
        tft.setTextSize(1);
        tft.setCursor(SCREEN_W - MARGIN_X - 58, y + 32);
        tft.setTextColor(C_TEXT_DIM, C_CARD_BG);
        tft.print("/");
        tft.print(timeBuf);
    }
    y += ROW_H + GAP;   // → 250

    // Card 4: 屏幕 label + checkmark
    drawCard(y, ROW_H, C_CARD_BG);
    drawDataRow(y, "屏幕", screenLabel, C_TEXT_DIM, C_OK);
    drawCheckmark(SCREEN_W - MARGIN_X - 24, y + 14);

    drawBottom("正常监测中", C_HEADER_FOCUS);
    _curScreen = SCREEN_FOCUS;
}

// ────────────────────────────────────────────────────────────
//  Alert Screen
// ────────────────────────────────────────────────────────────

void FocusFlowUI::showAlertScreen(const char* screenLabel) {
    tft.fillScreen(C_BG);
    drawHeader("摸鱼提醒", C_HEADER_ALERT);

    int y = CONTENT_Y + 8;   // 60

    // Big warning icon
    drawWarningBig(SCREEN_W / 2 - 16, y);
    y += 44;   // 104

    // Card: screen name + 摸鱼 tag
    drawCard(y, ROW_H, C_CARD_BG);
    {
        drawCNString(MARGIN_X + 6, y + 14, "屏幕", C_TEXT_DIM, C_CARD_BG);
        tft.setCursor(MARGIN_X + 42, y + 14);
        tft.setTextColor(C_TEXT_DIM, C_CARD_BG);
        tft.setTextSize(1);
        tft.print(":");

        // Label with fallback support for unknown chars
        int labelX = MARGIN_X + 52;
        drawCNString(labelX, y + 14, screenLabel, C_WARN, C_CARD_BG);
        int labelW = utf8PixelWidth(screenLabel);
        drawCNString(labelX + labelW + 6, y + 14, "摸鱼", C_ALERT, C_CARD_BG);
    }
    y += ROW_H + GAP;   // 166

    // Card: 状态 走神 + 请回到工作
    drawCard(y, ROW_H + 10, C_CARD_BG);
    {
        drawCNString(MARGIN_X + 6, y + 14, "状态", C_TEXT_DIM, C_CARD_BG);
        drawCNString(MARGIN_X + 42, y + 14, "走神", C_WARN, C_CARD_BG);
        drawCNString(MARGIN_X + 6, y + 36, "请回到工作", C_TEXT, C_CARD_BG);
    }
    y += ROW_H + 10 + GAP;   // 238

    // Card: 振动 已启动
    drawCard(y, ROW_H, C_CARD_BG);
    drawDataRow(y, "振动", "已启动", C_TEXT_DIM, C_WARN);

    drawBottom("请勿分心", C_HEADER_ALERT);
    _curScreen = SCREEN_ALERT;
}

// ────────────────────────────────────────────────────────────
//  Break Screen
// ────────────────────────────────────────────────────────────

void FocusFlowUI::showBreakScreen(uint32_t breakRemainingSec,
                                  uint32_t nextSessionSec) {
    // Full-screen image — replaces original break UI
    // Parameters retained for API compatibility, unused
    (void)breakRemainingSec;
    (void)nextSessionSec;

    tft.drawRGBBitmap(0, 0, break_image, BREAK_IMG_W, BREAK_IMG_H);
    _curScreen = SCREEN_BREAK;
}

// ────────────────────────────────────────────────────────────
//  Drawing primitives
// ────────────────────────────────────────────────────────────

void FocusFlowUI::drawHeader(const char* title, uint16_t barColor) {
    tft.fillRect(0, HEADER_Y, SCREEN_W, HEADER_H, barColor);
    // accent strip on left edge
    tft.fillRect(0, HEADER_Y, 4, HEADER_H, C_ACCENT);

    // Check ASCII vs Chinese
    bool pureAscii = true;
    for (const char* p = title; *p; p++) {
        if ((uint8_t)*p > 0x7F) { pureAscii = false; break; }
    }

    if (pureAscii) {
        // English title — centered horizontally, vertically centered
        size_t len = strlen(title);
        int tw = len * 12;   // text size 2 → ~12px per char
        int tx = (SCREEN_W - tw) / 2;
        if (tx < 4) tx = 4;
        tft.setCursor(tx, 10);
        tft.setTextColor(C_TEXT, barColor);
        tft.setTextSize(2);
        tft.print(title);
    } else {
        // Chinese title — centered
        int cnCount = 0;
        for (const char* p = title; *p; p++) {
            if ((uint8_t)*p >= 0xE0) { cnCount++; p += 2; }
        }
        int totalW = cnCount * (FONT_CHAR_W + 2);
        int startX = (SCREEN_W - totalW) / 2;
        drawCNString(startX, 10, title, C_TEXT, barColor);
    }
}

void FocusFlowUI::drawBottom(const char* hint, uint16_t barColor) {
    tft.fillRect(0, BOTTOM_Y, SCREEN_W, BOTTOM_H, barColor);

    // Small centered hint text
    int cnCount = 0;
    for (const char* p = hint; *p; p++) {
        if ((uint8_t)*p >= 0xE0) { cnCount++; p += 2; }
    }
    if (cnCount > 0) {
        int totalW = cnCount * (FONT_CHAR_W + 2);
        int startX = (SCREEN_W - totalW) / 2;
        tft.fillRect(0, BOTTOM_Y, SCREEN_W, BOTTOM_H, barColor);
        drawCNString(startX, BOTTOM_Y + 1, hint, C_TEXT_DIM, barColor);
    } else {
        int tw = strlen(hint) * 6;
        int tx = (SCREEN_W - tw) / 2;
        tft.setCursor(tx, BOTTOM_Y + 3);
        tft.setTextColor(C_TEXT_DIM, barColor);
        tft.setTextSize(1);
        tft.print(hint);
    }
}

void FocusFlowUI::drawCard(int y, int h, uint16_t color) {
    tft.fillRoundRect(MARGIN_X, y, SCREEN_W - 2 * MARGIN_X, h, 5, color);
}

void FocusFlowUI::drawDataRow(int y, const char* label,
                               const char* valueStr,
                               uint16_t labelColor, uint16_t valueColor) {
    int lx = MARGIN_X + 6;
    int ly = y + (ROW_H - FONT_CHAR_H) / 2;
    drawCNString(lx, ly, label, labelColor, C_CARD_BG);

    // colon
    tft.setCursor(lx + cnStrLen(label) * (FONT_CHAR_W + 2) + 2, ly + 2);
    tft.setTextColor(C_TEXT_DIM, C_CARD_BG);
    tft.setTextSize(1);
    tft.print(":");

    // value
    int valStart = lx + cnStrLen(label) * (FONT_CHAR_W + 2) + 16;
    bool valAscii = true;
    for (const char* p = valueStr; *p; p++) {
        if ((uint8_t)*p > 0x7F) { valAscii = false; break; }
    }
    if (valAscii) {
        tft.setCursor(valStart, ly + 2);
        tft.setTextColor(valueColor, C_CARD_BG);
        tft.setTextSize(1);
        tft.print(valueStr);
    } else {
        drawCNString(valStart, ly, valueStr, valueColor, C_CARD_BG);
    }
}

// ────────────────────────────────────────────────────────────
//  Focus progress bar
// ────────────────────────────────────────────────────────────

void FocusFlowUI::drawFocusBar(int y, uint8_t pct) {
    int barX = MARGIN_X + 6;
    int barW = SCREEN_W - 2 * MARGIN_X - 12;
    int barH = 16;
    uint16_t barColor = focusPctColor(pct);

    // Track
    tft.fillRoundRect(barX, y, barW, barH, 3, 0x2104);
    // Fill
    int fillW = (barW - 4) * pct / 100;
    if (fillW > 0) {
        tft.fillRoundRect(barX + 2, y + 2, fillW, barH - 4, 2, barColor);
    }
    // Percentage centered
    char pctStr[8];
    snprintf(pctStr, sizeof(pctStr), "%u%%", pct);
    tft.setCursor(barX + barW / 2 - 10, y + 1);
    tft.setTextColor(C_TEXT, barColor);
    tft.setTextSize(1);
    tft.print(pctStr);
}

uint16_t FocusFlowUI::focusPctColor(uint8_t pct) {
    if (pct >= 70) return C_FOCUS_HIGH;
    if (pct >= 40) return C_FOCUS_MID;
    return C_FOCUS_LOW;
}

// ────────────────────────────────────────────────────────────
//  Pixel-art icons
// ────────────────────────────────────────────────────────────

void FocusFlowUI::drawTomato(int x, int y) {
    tft.fillCircle(x + 7, y + 7, 6, C_TOMATO);
    tft.fillTriangle(x + 5, y, x + 10, y, x + 7, y + 4, 0x2D04);
}

void FocusFlowUI::drawCheckmark(int x, int y) {
    // Simple ✓ using lines
    tft.drawLine(x,     y + 8, x + 5,  y + 16, C_OK);
    tft.drawLine(x + 1, y + 8, x + 6,  y + 16, C_OK);
    tft.drawLine(x + 5, y + 15, x + 14, y + 3, C_OK);
    tft.drawLine(x + 5, y + 16, x + 15, y + 4, C_OK);
}

void FocusFlowUI::drawWarningBig(int x, int y) {
    tft.fillTriangle(x + 16, y, x, y + 32, x + 32, y + 32, C_WARN);
    tft.fillRect(x + 14, y + 12, 4, 10, C_BG);
    tft.fillRect(x + 14, y + 26, 4, 4, C_BG);
}

// ────────────────────────────────────────────────────────────
//  Chinese text rendering
// ────────────────────────────────────────────────────────────

int8_t FocusFlowUI::findCN(const char* utf8) {
    for (int i = 0; i < FONT_CHAR_COUNT; i++) {
        if (memcmp(utf8, chinese_char_str[i], 3) == 0)
            return i;
    }
    return -1;
}

void FocusFlowUI::drawCN(int x, int y, uint8_t idx,
                          uint16_t fg, uint16_t bg) {
    if (idx >= FONT_CHAR_COUNT) return;
    const uint8_t* bm = chinese_font[idx];
    for (int row = 0; row < FONT_CHAR_H; row++) {
        for (int col = 0; col < FONT_CHAR_W; col++) {
            uint8_t byteVal = bm[row * 2 + col / 8];
            if (byteVal & (0x80 >> (col % 8))) {
                tft.drawPixel(x + col, y + row, fg);
            } else {
                tft.drawPixel(x + col, y + row, bg);
            }
        }
    }
}

void FocusFlowUI::drawCNString(int x, int y, const char* utf8,
                                uint16_t fg, uint16_t bg) {
    int cx = x;
    while (*utf8) {
        uint8_t c = (uint8_t)*utf8;
        if (c < 0x80) {
            // ASCII — GFX built-in font
            tft.setCursor(cx, y + 2);
            tft.setTextColor(fg, bg);
            tft.setTextSize(1);
            tft.print(*utf8);
            cx += 8;
            utf8++;
        } else if (c >= 0xE0) {
            // 3-byte UTF-8 (Chinese)
            int8_t idx = findCN(utf8);
            if (idx >= 0) {
                drawCN(cx, y, (uint8_t)idx, fg, bg);
                cx += FONT_CHAR_W + 2;
            } else {
                // Unknown character → "?" fallback
                tft.setCursor(cx, y + 2);
                tft.setTextColor(C_WARN, bg);
                tft.setTextSize(1);
                tft.print("?");
                cx += 10;
            }
            utf8 += 3;
        } else {
            utf8++; // skip continuation bytes
        }
    }
}

// ────────────────────────────────────────────────────────────
//  Utility
// ────────────────────────────────────────────────────────────

static int cnStrLen(const char* utf8) {
    int count = 0;
    while (*utf8) {
        if ((uint8_t)*utf8 >= 0xE0) { count++; utf8 += 3; }
        else { utf8++; }
    }
    return count;
}

void FocusFlowUI::fmtTime(char* buf, uint32_t seconds) {
    uint32_t m = seconds / 60;
    uint32_t s = seconds % 60;
    snprintf(buf, 8, "%02lu:%02lu", m, s);
}

// ────────────────────────────────────────────────────────────
//  UTF-8 pixel width helper (for mixed CJK/ASCII layout)
// ────────────────────────────────────────────────────────────

static int utf8PixelWidth(const char* utf8) {
    int w = 0;
    while (*utf8) {
        uint8_t c = (uint8_t)*utf8;
        if (c < 0x80)       { w += 8;  utf8++; }
        else if (c >= 0xE0)  { w += FONT_CHAR_W + 2; utf8 += 3; }
        else                 { utf8++; }
    }
    return w;
}
