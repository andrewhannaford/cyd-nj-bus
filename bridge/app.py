"""
NJ Transit bus arrivals bridge: polls NJT's Bus Data Exchange
(getNextTripsXML) for one or more stop IDs and serves a small JSON
snapshot for the CYD firmware to poll.

Config comes from environment variables (see njt-bridge.env.example):
  NJT_USERNAME, NJT_PASSWORD, STOP_IDS, POLL_INTERVAL, PORT

Data provided by NJ TRANSIT, sole owner of the data. This app is not
endorsed by, affiliated with, or sponsored by NJ TRANSIT.
"""
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

NJT_USERNAME = os.environ["NJT_USERNAME"]
NJT_PASSWORD = os.environ["NJT_PASSWORD"]
# A physical stop pole gets its own stop_id per direction - list more than
# one to merge both directions (or multiple nearby poles) into one board.
STOP_IDS = [s.strip() for s in os.environ["STOP_IDS"].split(",") if s.strip()]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "8000"))
NJT_TIMEOUT_S = 10

BUS_DATA_URL = "https://busdata.njtransit.com/NJTBusData.asmx/getNextTripsXML"
TZ = ZoneInfo("America/New_York")  # NJT times have no date/offset - assume Eastern regardless of host TZ

app = Flask(__name__)
_state_lock = threading.Lock()
_state = {"ok": False, "error": "not polled yet"}


def _parse_departure(time_str, now):
    # NJT gives "HH:MM:SS" with no date - anchor to today, then roll forward
    # a day if that lands more than a few minutes in the past (next-trip
    # queries near midnight can return times that are technically tomorrow).
    hh, mm, ss = (int(p) for p in time_str.split(":"))
    dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if dt < now - timedelta(minutes=5):
        dt += timedelta(days=1)
    return dt


def _fetch_stop(stop_id, now):
    resp = requests.post(
        BUS_DATA_URL,
        data={"username": NJT_USERNAME, "password": NJT_PASSWORD, "stopid": stop_id},
        timeout=NJT_TIMEOUT_S,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    buses = []
    stop_name = None
    for trip in root.iter("Trip"):
        dep_str = (trip.findtext("departure_time") or "").strip()
        if not dep_str:
            continue
        dep_dt = _parse_departure(dep_str, now)
        sec_late_raw = (trip.findtext("sec_late") or "0").strip()
        try:
            sec_late = int(sec_late_raw)
        except ValueError:
            sec_late = 0
        name = (trip.findtext("stop_name") or "").strip()
        if name:
            stop_name = name
        buses.append(
            {
                "route": (trip.findtext("route") or "?").strip(),
                "header": (trip.findtext("header") or "").strip(),
                "eta_min": max(0, round((dep_dt - now).total_seconds() / 60)),
                "sec_late": sec_late,
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

    all_buses.sort(key=lambda b: b["eta_min"])

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
