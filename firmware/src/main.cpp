// NJ Transit bus arrivals board for the "Cheap Yellow Display" (ESP32-2432S028R).
// Polls the bridge service's /stats JSON and renders next-arrival times for
// a stop on a single consolidated screen.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>

#ifndef BRIDGE_URL
#error "BRIDGE_URL not set - build with the 'lan' or 'public' PlatformIO environment (see platformio.ini)"
#endif

static const uint32_t POLL_INTERVAL_MS = 20000;
static const uint32_t WIFI_RETRY_MS = 15000; // give WiFi.begin() time to resolve before retrying
static const int MAX_BUSES = 3; // taller rows - the app's 3-line-per-row layout needs the room

TFT_eSPI tft = TFT_eSPI();
// Rows render into a sprite and get pushed in one shot, so a changed row
// never shows a clear-then-repaint flash.
TFT_eSprite rowSprite = TFT_eSprite(&tft);
bool spriteReady = false;
WiFiManager wifiManager;
WiFiClientSecure secureClient;
WiFiClient plainClient;

struct BusEntry {
  String route;
  String header;
  String etaTime;   // "6:15 PM" - computed by the bridge from eta_min, like the app shows
  String vehicleId; // "" when not GPS-tracked
  int etaMin;
  int secLate;
  int occupancy;    // 0 = unknown, 1..3 = light/medium/full (see bridge app.py)
  bool realtime;    // GPS-tracked vehicle vs a schedule-based estimate
};

struct BoardState {
  bool ok = false;
  String stopName;
  BusEntry buses[MAX_BUSES];
  int busCount = 0;
};

BoardState board;
BoardState lastDrawnBoard;
bool lastDrawnWifiConnected = false;

uint32_t lastPoll = 0;
uint32_t lastWifiAttempt = 0;

bool busEqual(const BusEntry &a, const BusEntry &b) {
  return a.route == b.route && a.header == b.header && a.etaMin == b.etaMin &&
         a.secLate == b.secLate && a.realtime == b.realtime &&
         a.etaTime == b.etaTime && a.vehicleId == b.vehicleId && a.occupancy == b.occupancy;
}

// ---------- palette: same app layout, dark mode ----------
// Same two-tone zebra and #004f99/#00416d NJT blues as the app screenshot,
// just with the "white" row swapped for near-black + white ink instead of
// white + navy ink - keeps the alternating-row readability, easier at night.
#define COL_PAGE     0x0861  // #0d0d0d - page plane / even-row background
#define COL_HEADER   0x0273  // #004f99 - header bar (NJT blue, unchanged)
#define COL_INK      0xFFFF  // #ffffff - ink on the near-black row
#define COL_ALT_BG   0x020D  // #00416d - odd-row (alternating) background, NJT navy
#define COL_ALT_INK  0xFFFF  // #ffffff - ink on the navy row
#define COL_MUTED    0x8C30  // #898781 - muted text (header eyebrow only)
#define COL_WARNING  0xFD83  // #fab219 - a few minutes late
#define COL_CRITICAL 0xD1C7  // #d03b3b - significantly late / delayed
#define COL_OCC_ON   0x0540  // #00aa00 - occupancy: filled figure
#define COL_OCC_OFF  0xBDF7  // #bdbdbd - occupancy: empty figure

// ---------- layout ----------
static const int SCREEN_W = 320;
static const int SCREEN_H = 240;
static const int HEADER_H = 44;
static const int ROW_H = (SCREEN_H - HEADER_H) / MAX_BUSES; // 65px - 3 text lines per row

// Left column is the route number as plain bold text (no badge box) -
// matches the app, which never puts it in a shape.
static const int ROUTE_X = 10;
static const int ROUTE_COL_W = 52; // route numbers right-align within this so they line up
static const int DEST_X = ROUTE_X + ROUTE_COL_W + 10;
static const int ETA_COL_W = 76; // reserved width for "Delayed"/"99 min" on the right
static const int ETA_RIGHT = SCREEN_W - 10;
static const int DEST_MAX_W = (ETA_RIGHT - ETA_COL_W) - DEST_X - 6;
static const int OCC_DOT_GAP = 8; // spacing between the 3 occupancy figures

static const int TITLE_X = 10;
static const int PILL_W = 62;
static const int PILL_H = 18;
static const int PILL_X = SCREEN_W - 10 - PILL_W;
static const int PILL_Y = (HEADER_H - PILL_H) / 2 - 1;
static const int TITLE_MAX_W = PILL_X - TITLE_X - 8; // stop short of the status pill

// Delay thresholds (seconds) for status color - mirrors typical rider tolerance:
// under a minute late reads as on-time, over 5 min late is a real problem.
static const int LATE_WARN_S = 60;
static const int LATE_CRIT_S = 300;

void drawSetupMessage() {
  tft.fillScreen(COL_PAGE);
  tft.drawRect(6, 6, SCREEN_W - 12, SCREEN_H - 12, COL_HEADER);

  tft.setFreeFont(&FreeSansBold12pt7b);
  tft.setTextColor(COL_HEADER, COL_PAGE);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("WIFI SETUP", SCREEN_W / 2, 74);

  tft.setFreeFont(&FreeSans9pt7b);
  tft.setTextColor(COL_MUTED, COL_PAGE);
  tft.drawString("Connect a phone to the network", SCREEN_W / 2, 116);

  tft.setFreeFont(&FreeSansBold12pt7b);
  tft.setTextColor(COL_INK, COL_PAGE);
  tft.drawString("NJ-Bus-Setup", SCREEN_W / 2, 148);

  tft.setFreeFont(NULL);
  tft.setTextSize(1);
  tft.setTextColor(COL_MUTED, COL_PAGE);
  tft.drawString("THEN PICK YOUR WIFI IN THE POPUP", SCREEN_W / 2, 182);
}

// First boot (or after a reset) with no working saved WiFi: this blocks,
// opening an AP + captive portal so a neighbor can pick their own network
// from their phone without ever telling us the password. Once WiFi.begin()
// has succeeded here once, ESP32 persists the credentials in NVS, so this
// call returns almost immediately on every later boot.
void connectWifi() {
  WiFi.mode(WIFI_STA);
  if (WiFi.SSID().length() == 0) {
    drawSetupMessage();
  }
  wifiManager.setConfigPortalTimeout(180);
  wifiManager.autoConnect("NJ-Bus-Setup");
}

// Normal ETA just uses the row's own ink color (the app doesn't have a
// special "on-time" color) - lateness overrides to amber/red regardless
// of which zebra row it's on, since both read fine on white or navy.
uint16_t statusColor(int secLate, uint16_t ink) {
  if (secLate >= LATE_CRIT_S) return COL_CRITICAL;
  if (secLate >= LATE_WARN_S) return COL_WARNING;
  return ink;
}

void drawHeader() {
  tft.fillRect(0, 0, SCREEN_W, HEADER_H, COL_HEADER);

  tft.setFreeFont(NULL);
  tft.setTextSize(1);
  tft.setTextColor(COL_ALT_INK, COL_HEADER);
  tft.setTextDatum(TL_DATUM);
  tft.drawString("NJ TRANSIT", TITLE_X, 6);

  tft.setFreeFont(&FreeSansBold9pt7b);
  tft.setTextColor(COL_ALT_INK, COL_HEADER);
  tft.setTextDatum(TL_DATUM);
  // Truncate by measured pixel width, not character count - a fixed
  // character cap left long/wide stop names overlapping the status pill.
  String title = board.stopName.length() ? board.stopName : "NJ BUS";
  if (tft.textWidth(title) > TITLE_MAX_W) {
    while (title.length() > 0 && tft.textWidth(title + "~") > TITLE_MAX_W) {
      title = title.substring(0, title.length() - 1);
    }
    title += "~";
  }
  tft.drawString(title, TITLE_X, 19);

  uint16_t pillColor;
  const char *label;
  if (WiFi.status() != WL_CONNECTED) {
    pillColor = COL_CRITICAL;
    label = "NO NET";
  } else if (board.ok) {
    pillColor = COL_OCC_ON; // reuse the app's occupancy green for "good"
    label = "LIVE";
  } else {
    pillColor = COL_WARNING;
    label = "STALE";
  }
  tft.drawRoundRect(PILL_X, PILL_Y, PILL_W, PILL_H, 4, pillColor);
  tft.setFreeFont(NULL);
  tft.setTextSize(1);
  tft.setTextColor(pillColor, COL_HEADER);
  tft.setTextDatum(MC_DATUM);
  tft.drawString(label, PILL_X + PILL_W / 2, PILL_Y + PILL_H / 2);
}

// Tiny person silhouette: round head + tapered body, built from primitives
// since there's no icon font available. x is the icon's horizontal center,
// cy roughly its vertical center.
void drawPersonIcon(TFT_eSPI &g, int x, int cy, uint16_t color) {
  g.fillCircle(x, cy - 3, 2, color);
  g.fillTriangle(x - 3, cy + 4, x + 3, cy + 4, x, cy - 1, color);
}

// n-of-3 figures, filled (green) vs empty (grey) - same on/off-count
// convention as the app's occupancy icon.
void drawOccupancy(TFT_eSPI &g, int x, int cy, int level) {
  for (int i = 0; i < 3; i++) {
    int iconX = x + i * OCC_DOT_GAP;
    drawPersonIcon(g, iconX, cy, i < level ? COL_OCC_ON : COL_OCC_OFF);
  }
}

// TFT_eSprite derives from TFT_eSPI, so the same code renders a row either
// into the off-screen sprite or straight to the panel as a fallback.
// Direct replica of the app's row: alternating white/navy background,
// route number plain bold on the left, destination + "Bus #### / time"
// stacked in the middle, big ETA on the right - no extra chrome.
void renderRow(TFT_eSPI &g, int top, const BusEntry &bus, bool alt) {
  uint16_t bg = alt ? COL_ALT_BG : COL_PAGE;
  uint16_t ink = alt ? COL_ALT_INK : COL_INK;
  g.fillRect(0, top, SCREEN_W, ROW_H, bg);

  int destY = top + 18;
  int subY = top + 38;
  int timeY = top + 54;
  int cy = top + ROW_H / 2;

  // Route number: plain bold text, no badge box - matches the app.
  g.setFreeFont(&FreeSansBold12pt7b);
  g.setTextColor(ink, bg);
  g.setTextDatum(ML_DATUM);
  g.drawString(bus.route, ROUTE_X, cy);

  g.setFreeFont(&FreeSans9pt7b);
  g.setTextColor(ink, bg);
  g.setTextDatum(ML_DATUM);
  String dest = bus.header;
  while (dest.length() > 0 && g.textWidth(dest) > DEST_MAX_W) {
    dest = dest.substring(0, dest.length() - 1);
  }
  g.drawString(dest, DEST_X, destY);

  // Sub-line: vehicle # (or "Scheduled") + occupancy dots, same as the
  // app's second line under the destination.
  g.setFreeFont(NULL);
  g.setTextSize(1);
  g.setTextColor(ink, bg);
  g.setTextDatum(ML_DATUM);
  String subLine = bus.realtime ? ("Bus #" + bus.vehicleId) : "Scheduled";
  g.drawString(subLine, DEST_X, subY);
  if (bus.occupancy > 0) {
    drawOccupancy(g, DEST_X + g.textWidth(subLine) + 10, subY, bus.occupancy);
  }

  // Third line: the arrival clock time, same as the app's own third line.
  if (bus.etaTime.length()) {
    g.setTextColor(ink, bg);
    g.drawString(bus.etaTime, DEST_X, timeY);
  }

  // ETA, right-aligned and vertically centered on the whole row.
  char etaBuf[10];
  if (bus.etaMin < 0) {
    snprintf(etaBuf, sizeof(etaBuf), "Delayed");
  } else if (bus.etaMin == 0) {
    snprintf(etaBuf, sizeof(etaBuf), "Due");
  } else {
    snprintf(etaBuf, sizeof(etaBuf), "%d min", bus.etaMin);
  }
  g.setFreeFont(&FreeSansBold12pt7b);
  g.setTextColor(statusColor(bus.secLate, ink), bg);
  g.setTextDatum(MR_DATUM);
  g.drawString(etaBuf, ETA_RIGHT, cy);
}

void drawBusRow(int rowY, const BusEntry &bus, bool alt) {
  if (spriteReady) {
    renderRow(rowSprite, 0, bus, alt);
    rowSprite.pushSprite(0, rowY);
  } else {
    renderRow(tft, rowY, bus, alt);
  }
}

void clearRow(int rowY, bool alt) {
  tft.fillRect(0, rowY, SCREEN_W, ROW_H, alt ? COL_ALT_BG : COL_PAGE);
}

void drawEmptyMessage() {
  tft.fillRect(0, HEADER_H, SCREEN_W, SCREEN_H - HEADER_H, COL_PAGE);
  int cy = HEADER_H + (SCREEN_H - HEADER_H) / 2;
  tft.setFreeFont(&FreeSansBold12pt7b);
  tft.setTextColor(COL_MUTED, COL_PAGE);
  tft.setTextDatum(MC_DATUM);
  const char *msg = board.ok ? "NO DEPARTURES" : "ACQUIRING FEED";
  tft.drawString(msg, SCREEN_W / 2, cy);
}

// Redraws only what actually changed since the last frame - a full
// fillScreen every poll caused a visible flash even when nothing on
// screen was different (most polls land between minute-boundary ETA
// ticks). Header, empty-state message, and each row are diffed
// independently against lastDrawnBoard/lastDrawnWifiConnected.
void updateDisplay(bool forceFull) {
  bool wifiConnected = (WiFi.status() == WL_CONNECTED);

  bool headerChanged = forceFull ||
      board.stopName != lastDrawnBoard.stopName ||
      board.ok != lastDrawnBoard.ok ||
      wifiConnected != lastDrawnWifiConnected;
  if (headerChanged) {
    drawHeader();
  }

  bool wasEmpty = !lastDrawnBoard.ok || lastDrawnBoard.busCount == 0;
  bool isEmpty = !board.ok || board.busCount == 0;

  if (isEmpty) {
    // Redraw the message if the content area might still show stale rows,
    // or the message text itself changed (waiting <-> no-upcoming-buses).
    if (forceFull || !wasEmpty || board.ok != lastDrawnBoard.ok) {
      drawEmptyMessage();
    }
  } else {
    if (forceFull || wasEmpty) {
      // Coming from the message state (or first paint): paint every row
      // fresh (each renderRow() fills its own zebra background, so no
      // separate whole-area clear is needed first).
      for (int i = 0; i < board.busCount; i++) {
        drawBusRow(HEADER_H + i * ROW_H, board.buses[i], i % 2 == 1);
      }
    } else {
      int maxRows = max(board.busCount, lastDrawnBoard.busCount);
      for (int i = 0; i < maxRows; i++) {
        int rowY = HEADER_H + i * ROW_H;
        bool alt = i % 2 == 1;
        if (i < board.busCount) {
          bool rowChanged = i >= lastDrawnBoard.busCount || !busEqual(board.buses[i], lastDrawnBoard.buses[i]);
          if (rowChanged) {
            drawBusRow(rowY, board.buses[i], alt); // repaints the full row, no separate clear
          }
        } else {
          // Fewer buses than last frame - blank the now-unused trailing row.
          clearRow(rowY, alt);
        }
      }
    }
  }

  lastDrawnBoard = board;
  lastDrawnWifiConnected = wifiConnected;
}

bool fetchStats() {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.setTimeout(4000);
  String url = BRIDGE_URL;
  if (url.startsWith("https")) {
    secureClient.setInsecure(); // public JSON, nothing sensitive - skip cert pinning
    http.begin(secureClient, url);
  } else {
    http.begin(plainClient, url);
  }
  int code = http.GET();
  if (code != 200) {
    http.end();
    board.ok = false;
    return false;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    board.ok = false;
    return false;
  }

  board.ok = doc["ok"] | false;
  // Even on a transient scrape error the bridge still serves the last-good
  // buses array (see app.py) - keep showing it (header pill goes STALE)
  // instead of blanking the whole board to "ACQUIRING FEED".
  board.stopName = String((const char *)(doc["stop_name"] | ""));

  JsonArray busArr = doc["buses"];
  board.busCount = 0;
  for (JsonObject b : busArr) {
    if (board.busCount >= MAX_BUSES) break;
    BusEntry &entry = board.buses[board.busCount];
    entry.route = String((const char *)(b["route"] | "?"));
    entry.header = String((const char *)(b["header"] | ""));
    entry.etaMin = b["eta_min"] | 0;
    entry.secLate = b["sec_late"] | 0;
    entry.realtime = b["realtime"] | true;
    entry.etaTime = String((const char *)(b["eta_time"] | ""));
    entry.vehicleId = String((const char *)(b["vehicle_id"] | ""));
    entry.occupancy = b["occupancy"] | 0;
    board.busCount++;
  }
  return true;
}

void setup() {
  Serial.begin(115200);
  tft.init();
  tft.invertDisplay(true); // ILI9341_2_DRIVER on most CYD units renders inverted otherwise - see README if colors look wrong
  tft.setRotation(1); // landscape, USB on the left
  tft.fillScreen(COL_PAGE);

  spriteReady = (rowSprite.createSprite(SCREEN_W, ROW_H) != nullptr);

  connectWifi();
  updateDisplay(true);
}

void loop() {
  uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED && now - lastWifiAttempt > WIFI_RETRY_MS) {
    lastWifiAttempt = now;
    // Reconnect with the saved credentials - NOT connectWifi(), which would
    // reopen the blocking captive portal on every transient drop (e.g. the
    // neighbor's router rebooting).
    WiFi.reconnect();
  }

  if (now - lastPoll > POLL_INTERVAL_MS || lastPoll == 0) {
    lastPoll = now;
    fetchStats();
    updateDisplay(false);
  }

  delay(200);
}
