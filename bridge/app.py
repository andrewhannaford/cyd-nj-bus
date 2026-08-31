"""
NJ Transit bus arrivals bridge: scrapes NJT's public MyBus site
(mybusnow.njtransit.com) for one or more stop IDs and serves a small JSON
snapshot for the CYD firmware to poll.

Uses the same unauthenticated "wireless" (text-only) rider-facing pages
the mobile site itself uses - no developer API key required. NJT could
change this markup at any time; switch back to getNextTripsXML (see git
history) once a developer account is approved.

Config comes from environment variables (see njt-bridge.env.example):
  STOP_IDS, POLL_INTERVAL, PORT

Data provided by NJ TRANSIT, sole owner of the data. This app is not
endorsed by, affiliated with, or sponsored by NJ TRANSIT.
"""
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

# A physical stop pole gets its own stop_id per direction - list more than
# one to merge both directions (or multiple nearby poles) into one board.
STOP_IDS = [s.strip() for s in os.environ["STOP_IDS"].split(",") if s.strip()]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "8000"))
NJT_TIMEOUT_S = 10

MYBUS_HOME_URL = "https://mybusnow.njtransit.com/bustime/wireless/html/home.jsp"
MYBUS_ETA_URL = "https://mybusnow.njtransit.com/bustime/wireless/html/eta.jsp"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TZ = ZoneInfo("America/New_York")

# Each prediction block on eta.jsp looks like:
#   <strong class="larger">#158&nbsp;</strong> ... To ... 158 NEW YORK VIA RIVER ROAD
#   ... <strong class="larger">7&nbsp;MIN</strong>  (or "DELAYED", or "&lt; 1 MIN")
#   <span class="smaller"> ... (Vehicle 20872) ... (Passengers: Light) </span>
# The (Vehicle #) line only appears when a real bus is actively GPS-tracked
# for that trip; its absence is NJT's own signal for a schedule-based
# estimate with no live vehicle assigned yet (there's no separate flag).
BLOCK_RE = re.compile(
    r'<strong class="larger">#(?P<route>\S+?)&nbsp;</strong>.*?'
    r'To\s*(?P<dest>.*?)\s*'
    r'<strong class="larger">(?P<eta>.*?)</strong>'
    r'(?P<tail>.*?)(?=<hr)',
    re.S,
)
STOP_NAME_RE = re.compile(r"Selected Stop:\s*(.+?)\s*<")
VEHICLE_RE = re.compile(r"\(Vehicle\s*(\d+)\)")
PASSENGERS_RE = re.compile(r"\(Passengers:\s*([^)]+)\)")
DELAYED_SEC_LATE = 300  # scrape only gives a boolean "DELAYED" flag, no seconds - pin it past the firmware's critical threshold

# NJT's own app shows a 3-figure occupancy icon (n of 3 filled); the wireless
# page only gives a text label, and only when a vehicle happens to report
# one, so this is a best-effort mapping onto the same 3-level scale.
def _occupancy_level(tail):
    m = PASSENGERS_RE.search(tail)
    if not m:
        return 0
    label = m.group(1).strip().lower()
    if "full" in label or "crowd" in label or "heavy" in label:
        return 3
    if "medium" in label or "moderate" in label:
        return 2
    if "light" in label or "empty" in label:
        return 1
    return 0

app = Flask(__name__)
_state_lock = threading.Lock()
_state = {"ok": False, "error": "not polled yet"}
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _fetch_stop(stop_id, now):
    # eta.jsp 500s without a valid session cookie (from Cloudflare's bot
    # check) - re-warm the session and retry once before giving up.
    for attempt in range(2):
        resp = _session.get(
            MYBUS_ETA_URL,
            params={
                "route": "---",
                "direction": "---",
                "displaydirection": "---",
                "stop": "---",
                "findstop": "on",
                "selectedRtpiFeeds": "",
                "id": stop_id,
            },
            timeout=NJT_TIMEOUT_S,
        )
        if resp.ok and "Error processing request" not in resp.text:
            break
        _session.get(MYBUS_HOME_URL, timeout=NJT_TIMEOUT_S)
    resp.raise_for_status()
    html = resp.text

    stop_name = None
    m = STOP_NAME_RE.search(html)
    if m:
        stop_name = m.group(1).strip()

    buses = []
    for m in BLOCK_RE.finditer(html):
        eta_raw = m.group("eta").replace("&nbsp;", " ").strip()
        route = m.group("route").strip()
        dest = re.sub(r"\s+", " ", m.group("dest").replace("&nbsp;", " ")).strip()
        # "158 NEW YORK VIA RIVER ROAD" -> "NEW YORK VIA RIVER ROAD"; the board
        # shows the route on its own badge, so the prefix just eats width.
        dest = re.sub(r"^" + re.escape(route) + r"[A-Z]?\s+", "", dest)
        tail = m.group("tail")
        delayed = eta_raw.upper() == "DELAYED"
        digits = re.search(r"\d+", eta_raw)
        eta_min = int(digits.group()) if digits else -1

        vehicle_m = VEHICLE_RE.search(tail)
        # Absolute clock time, like the app's "6:01 PM" - not on this page at
        # all, so it's derived the same way a countdown implies a clock time.
        eta_time = None
        if eta_min >= 0:
            # %-I (no leading zero) is a glibc extension, not portable -
            # format normally and strip a leading zero instead.
            eta_time = (now + timedelta(minutes=eta_min)).strftime("%I:%M %p").lstrip("0")

        buses.append(
            {
                "route": route,
                "header": dest,
                "eta_min": eta_min,
                "eta_time": eta_time,
                "sec_late": DELAYED_SEC_LATE if delayed else 0,
                "realtime": vehicle_m is not None,
                "vehicle_id": vehicle_m.group(1) if vehicle_m else None,
                "occupancy": _occupancy_level(tail),
            }
        )
    return stop_name, buses


def _poll_once():
    now = datetime.now(TZ)
    all_buses = []
    stop_name = None
    for stop_id in STOP_IDS:
        name, buses = _fetch_stop(stop_id, now)
        stop_name = stop_name or name
        all_buses.extend(buses)

    all_buses.sort(key=lambda b: b["eta_min"] if b["eta_min"] >= 0 else 10**9)

    new_state = {
        "ok": True,
        "updated": int(time.time()),
        "stop_name": stop_name or "",
        "buses": all_buses[:8],
    }
    with _state_lock:
        _state.clear()
        _state.update(new_state)


def _poll_loop():
    while True:
        try:
            _poll_once()
        except Exception as exc:  # keep serving stale/last-good data on transient errors
            with _state_lock:
                _state["ok"] = False
                _state["error"] = str(exc)
        time.sleep(POLL_INTERVAL)


@app.route("/stats")
def stats():
    with _state_lock:
        return jsonify(dict(_state))


if __name__ == "__main__":
    threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
