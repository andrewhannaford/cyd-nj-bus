// NJ Transit bus arrivals board for the "Cheap Yellow Display" (ESP32-2432S028R).
// Polls the bridge service's /stats JSON and renders next-arrival times for
// a stop on a single consolidated screen.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>

#include "secrets.h"

static const uint32_t POLL_INTERVAL_MS = 20000;
static const uint32_t WIFI_RETRY_MS = 15000; // give WiFi.begin() time to resolve before retrying
static const int MAX_BUSES = 6;

TFT_eSPI tft = TFT_eSPI();

struct BusEntry {
  String route;
  String header;
  int etaMin;
  int secLate;
};

struct BoardState {
  bool ok = false;
  String stopName;
  BusEntry buses[MAX_BUSES];
  int busCount = 0;
};

BoardState board;

uint32_t lastPoll = 0;
uint32_t lastWifiAttempt = 0;

// ---------- palette (dataviz skill's dark-mode reference palette) ----------
#define COL_PAGE     0x0861  // #0d0d0d - page plane (screen background)
#define COL_CARD     0x18C3  // #1a1a19 - card/header surface
#define COL_TEXT     0xFFFF  // #ffffff - primary ink
#define COL_TEXT_SEC 0xC616  // #c3c2b7 - secondary ink
#define COL_MUTED    0x8C30  // #898781 - muted ink
#define COL_TRACK    0x2965  // #2c2c2a - row divider
#define COL_GOOD     0x0D01  // #0ca30c - on time
#define COL_WARNING  0xFD83  // #fab219 - a few minutes late
#define COL_CRITICAL 0xD1C7  // #d03b3b - significantly late
#define COL_ACCENT   0x3C3C  // #3987e5 - route badge / early

// ---------- layout ----------
static const int SCREEN_W = 320;
static const int SCREEN_H = 240;
static const int HEADER_H = 36;
static const int ROW_H = (SCREEN_H - HEADER_H) / MAX_BUSES; // 34px

static const int BADGE_X = 8;
static const int BADGE_W = 46;
static const int BADGE_H = 22;
static const int DEST_X = BADGE_X + BADGE_W + 10;
static const int DEST_MAX_W = 148;
static const int ETA_X = SCREEN_W - 12; // right-aligned

// Delay thresholds (seconds) for status color - mirrors typical rider tolerance:
// under a minute late reads as on-time, over 5 min late is a real problem.
static const int LATE_WARN_S = 60;
static const int LATE_CRIT_S = 300;

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

uint16_t statusColor(int secLate) {
  if (secLate >= LATE_CRIT_S) return COL_CRITICAL;
  if (secLate >= LATE_WARN_S) return COL_WARNING;
  if (secLate <= -LATE_WARN_S) return COL_ACCENT; // running early
  return COL_GOOD;
}

void drawHeader() {
  tft.fillRect(0, 0, SCREEN_W, HEADER_H, COL_CARD);
  tft.drawFastHLine(0, HEADER_H, SCREEN_W, COL_ACCENT);

  tft.setFreeFont(&FreeSansBold9pt7b);
  tft.setTextColor(COL_TEXT, COL_CARD);
  tft.setTextDatum(ML_DATUM);
  String title = board.stopName.length() ? board.stopName : "NJ BUS";
  if (title.length() > 22) title = title.substring(0, 21) + "~";
  tft.drawString(title, 8, HEADER_H / 2 + 1);

  uint16_t dotColor;
  const char *label;
  if (WiFi.status() != WL_CONNECTED) {
    dotColor = COL_CRITICAL;
    label = "NO WIFI";
  } else if (board.ok) {
    dotColor = COL_GOOD;
    label = "LIVE";
  } else {
    dotColor = COL_WARNING;
    label = "STALE";
  }
  int dotX = SCREEN_W - 62;
  tft.fillSmoothCircle(dotX, HEADER_H / 2, 4, dotColor, COL_CARD);
  tft.setFreeFont(NULL);
  tft.setTextSize(1);
  tft.setTextColor(COL_TEXT_SEC, COL_CARD);
  tft.setTextDatum(ML_DATUM);
  tft.drawString(label, dotX + 8, HEADER_H / 2 + 1);
}

void drawBusRow(int rowY, const BusEntry &bus) {
  int rowCenter = rowY + ROW_H / 2;

  // Route badge: filled rounded rect, bold route number centered.
  tft.fillRoundRect(BADGE_X, rowY + (ROW_H - BADGE_H) / 2, BADGE_W, BADGE_H, 4, COL_ACCENT);
  tft.setFreeFont(&FreeSansBold9pt7b);
  tft.setTextColor(COL_PAGE, COL_ACCENT);
  tft.setTextDatum(MC_DATUM);
  String routeLabel = bus.route;
  if (routeLabel.length() > 4) routeLabel = routeLabel.substring(0, 4);
  tft.drawString(routeLabel, BADGE_X + BADGE_W / 2, rowY + ROW_H / 2);

  // Destination, truncated to fit before the ETA column.
  tft.setFreeFont(&FreeSans9pt7b);
  tft.setTextColor(COL_TEXT, COL_PAGE);
  tft.setTextDatum(ML_DATUM);
  String dest = bus.header;
  while (dest.length() > 0 && tft.textWidth(dest) > DEST_MAX_W) {
    dest = dest.substring(0, dest.length() - 1);
  }
  tft.drawString(dest, DEST_X, rowCenter);

  // ETA, big and right-aligned, color-coded by lateness.
  char etaBuf[8];
  if (bus.etaMin <= 0) {
    snprintf(etaBuf, sizeof(etaBuf), "Due");
  } else {
    snprintf(etaBuf, sizeof(etaBuf), "%d min", bus.etaMin);
  }
  tft.setFreeFont(&FreeSansBold9pt7b);
  tft.setTextColor(statusColor(bus.secLate), COL_PAGE);
  tft.setTextDatum(MR_DATUM);
  tft.drawString(etaBuf, ETA_X, rowCenter);

  tft.drawFastHLine(0, rowY + ROW_H - 1, SCREEN_W, COL_TRACK);
}

void redraw() {
  drawHeader();
  tft.fillRect(0, HEADER_H, SCREEN_W, SCREEN_H - HEADER_H, COL_PAGE);

  if (!board.ok || board.busCount == 0) {
    tft.setFreeFont(&FreeSans9pt7b);
    tft.setTextColor(COL_MUTED, COL_PAGE);
    tft.setTextDatum(MC_DATUM);
    const char *msg = board.ok ? "No upcoming buses" : "Waiting for data...";
    tft.drawString(msg, SCREEN_W / 2, HEADER_H + (SCREEN_H - HEADER_H) / 2);
    return;
  }

  for (int i = 0; i < board.busCount; i++) {
    drawBusRow(HEADER_H + i * ROW_H, board.buses[i]);
  }
}

bool fetchStats() {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.setTimeout(4000);
  http.begin(BRIDGE_URL);
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
  if (!board.ok) return false;

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

  connectWifi();
  redraw();
}

void loop() {
  uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED && now - lastWifiAttempt > WIFI_RETRY_MS) {
    lastWifiAttempt = now;
    connectWifi();
  }

  if (now - lastPoll > POLL_INTERVAL_MS || lastPoll == 0) {
    lastPoll = now;
    fetchStats();
    redraw();
  }

  delay(200);
}
