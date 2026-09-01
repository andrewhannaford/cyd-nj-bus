"""
NJ Transit bus arrivals bridge: calls NJT's official BUSDATA (BUSDV2) API
for one or more stop IDs and serves a small JSON snapshot for the CYD
firmware to poll.

Config comes from environment variables (see njt-bridge.env.example and
.env.njt): STOP_IDS, POLL_INTERVAL, PORT, NJT_USERNAME, NJT_PASSWORD.

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
NJT_USERNAME = os.environ["NJT_USERNAME"]
NJT_PASSWORD = os.environ["NJT_PASSWORD"]
NJT_TIMEOUT_S = 10

API_BASE = "https://pcsdata.njtransit.com/api/BUSDV2"
# NJT says tokens are typically valid ~24h but reserves the right to change
# that - refresh well before the documented window, and reactively on any
# "invalid token" response regardless of this timer.
TOKEN_MAX_AGE_S = 20 * 3600
TZ = ZoneInfo("America/New_York")

DELAYED_SEC_LATE = 300  # API gives no seconds-late figure, just a remarks string - pin past the firmware's critical threshold
ETA_MIN_RE = re.compile(r"(\d+)\s*min", re.I)
SCHED_TIME_FMT = "%m/%d/%Y %I:%M:%S %p"
# NJT uses the literal strings "EMPTY" and "no data" as null placeholders
# across DVTrip fields instead of omitting them - normalize those away.
_NULL_SENTINELS = {"", "empty", "no data", "n/a"}


def _sentinel(value):
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NULL_SENTINELS else text


# NJT's own app shows a 3-figure occupancy icon (n of 3 filled); passload is
# a coarse text label, and only present when a vehicle is actively assigned.
def _occupancy_level(passload):
    passload = _sentinel(passload)
    if not passload:
        return 1  # no data reported - default to low so the icon always shows
    label = passload.strip().lower()
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

_token_lock = threading.Lock()
_token = None
_token_fetched_at = 0.0
_stop_names = {}  # stop_id -> friendly name, fetched once and cached


class TokenInvalid(Exception):
    pass


def _authenticate():
    global _token, _token_fetched_at
    resp = _session.post(
        f"{API_BASE}/authenticateUser",
        data={"username": NJT_USERNAME, "password": NJT_PASSWORD},
        timeout=NJT_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("Authenticated") != "True" or not data.get("UserToken"):
        raise RuntimeError("NJT auth failed - check NJT_USERNAME/NJT_PASSWORD")
    with _token_lock:
        _token = data["UserToken"]
        _token_fetched_at = time.time()
    return _token


def _get_token(force=False):
    with _token_lock:
        stale = _token is None or (time.time() - _token_fetched_at) > TOKEN_MAX_AGE_S
    if force or stale:
        return _authenticate()
    return _token


def _api_call(endpoint, **fields):
    # A token can go bad before our own freshness timer says so (NJT-side
    # revocation, clock drift) - retry once with a forced re-auth before
    # giving up, same shape as the old scraper's Cloudflare-retry pattern.
    for attempt in range(2):
        token = _get_token(force=(attempt == 1))
        resp = _session.post(
            f"{API_BASE}/{endpoint}",
            data={"token": token, **fields},
            timeout=NJT_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "token" in data.get("errorMessage", "").lower():
            continue
        if isinstance(data, dict) and "errorMessage" in data:
            raise RuntimeError(f"{endpoint}: {data['errorMessage']}")
        return data
    raise RuntimeError(f"{endpoint}: token kept getting rejected")


def _get_stop_name(stop_id):
    if stop_id not in _stop_names:
        data = _api_call("getStopName", stopnum=stop_id)
        _stop_names[stop_id] = data.get("stopName", "")
    return _stop_names[stop_id]


def _fetch_stop(stop_id, now):
    stop_name = _get_stop_name(stop_id)
    data = _api_call("getBusDV", stop=stop_id, direction="", route="", IP="")
    trips = data.get("DVTrip") or []

    buses = []
    for trip in trips:
        route = (trip.get("public_route") or "").strip()
        dest = re.sub(r"\s+", " ", (trip.get("header") or "")).strip()
        # "158 NEW YORK VIA RIVER ROAD" -> "NEW YORK VIA RIVER ROAD"; the
        # board shows the route on its own badge, so the prefix just eats width.
        dest = re.sub(r"^" + re.escape(route) + r"[A-Z]?\s+", "", dest)

        remarks = _sentinel(trip.get("remarks")) or ""
        # "departurestatus" (e.g. "in 13 mins") only appears once a vehicle
        # is actively tracked; otherwise it just repeats "departuretime",
        # the scheduled clock time, which sched_dep_time below is parsed from.
        status = _sentinel(trip.get("departurestatus")) or ""
        delayed = "delay" in remarks.lower() or "delay" in status.lower()

        eta_min = None
        sched_raw = _sentinel(trip.get("sched_dep_time"))
        if sched_raw:
            try:
                sched_dt = datetime.strptime(sched_raw, SCHED_TIME_FMT).replace(tzinfo=TZ)
                eta_min = round((sched_dt - now).total_seconds() / 60)
            except ValueError:
                eta_min = None

        m = ETA_MIN_RE.search(status)
        if m:
            eta_min = int(m.group(1))
        elif "due" in status.lower() and eta_min is None:
            eta_min = 0

        if eta_min is None:
            # No live countdown and an unparseable schedule time - skip this
            # one trip rather than fail the whole stop over a single bad entry.
            continue
        # Clock skew/rounding can put an on-time trip a few seconds negative
        # right at departure; only a real delay should ever go negative.
        if eta_min < 0 and not delayed:
            eta_min = 0

        # %-I (no leading zero) is a glibc extension, not portable - format
        # normally and strip a leading zero instead.
        eta_time = (now + timedelta(minutes=eta_min)).strftime("%I:%M %p").lstrip("0")

        vehicle_id = _sentinel(trip.get("vehicle_id"))
        buses.append(
            {
                "route": route,
                "header": dest,
                "eta_min": eta_min,
                "eta_time": eta_time,
                "sec_late": DELAYED_SEC_LATE if delayed else 0,
                "realtime": vehicle_id is not None,
                "vehicle_id": vehicle_id,
                "occupancy": _occupancy_level(trip.get("passload")),
            }
        )
    return stop_name, buses


def _poll_once():
    all_buses = []
    stop_name = None
    errors = []
    # Each stop is independent - one down direction failing shouldn't take
    # the whole board (including the OTHER direction's good data) with it.
    for stop_id in STOP_IDS:
        try:
            name, buses = _fetch_stop(stop_id, datetime.now(TZ))
        except Exception as exc:
            errors.append(f"{stop_id}: {exc}")
            continue
        stop_name = stop_name or name
        all_buses.extend(buses)

    if not all_buses and errors:
        raise RuntimeError("; ".join(errors))

    all_buses.sort(key=lambda b: b["eta_min"] if b["eta_min"] >= 0 else 10**9)

    new_state = {
        "ok": True,
        "updated": int(time.time()),
        "stop_name": stop_name or "",
        "buses": all_buses[:8],
    }
    if errors:  # partial success - some stops failed, but at least one had data
        new_state["error"] = "; ".join(errors)
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


# Started at import time, not just under __main__ - gunicorn imports this
# module rather than executing it directly, so a __main__-only start never runs.
threading.Thread(target=_poll_loop, daemon=True).start()

if __name__ == "__main__":
    # threaded=True: unauthenticated and reachable from the public internet,
    # with multiple gift units polling it - don't serialize their requests.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
