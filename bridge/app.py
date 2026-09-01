"""
NJ Transit bus arrivals bridge: merges NJT's official BUSDATA (BUSDV2) API
with the GTFS-RT (GTFSG2) real-time feed for one or more stop IDs, and
serves a small JSON snapshot for the CYD firmware to poll.

BUSDV2's getBusDV gives route/destination/occupancy but sometimes omits
imminent trips outright; GTFS-RT's TripUpdates feed has fresher/more
complete arrival predictions (and real seconds-late) but no route/
destination text of its own, so a background-refreshed lookup from GTFS-RT's
static trips.txt fills that in for trips BUSDV2 doesn't know about at all.

Config comes from environment variables (see njt-bridge.env.example and
.env.njt): STOP_IDS, POLL_INTERVAL, PORT, NJT_USERNAME, NJT_PASSWORD (the
same credentials are used for both APIs).

Data provided by NJ TRANSIT, sole owner of the data. This app is not
endorsed by, affiliated with, or sponsored by NJ TRANSIT.
"""
import csv
import io
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify
from google.transit import gtfs_realtime_pb2

# A physical stop pole gets its own stop_id per direction - list more than
# one to merge both directions (or multiple nearby poles) into one board.
STOP_IDS = [s.strip() for s in os.environ["STOP_IDS"].split(",") if s.strip()]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
PORT = int(os.environ.get("PORT", "8000"))
NJT_USERNAME = os.environ["NJT_USERNAME"]
NJT_PASSWORD = os.environ["NJT_PASSWORD"]
NJT_TIMEOUT_S = 10
GTFSRT_TIMEOUT_S = 30
GTFS_STATIC_TIMEOUT_S = 180  # getGTFS is an ~80MB zip, not a quick call

BUSDV2_BASE = "https://pcsdata.njtransit.com/api/BUSDV2"
GTFSG2_BASE = "https://pcsdata.njtransit.com/api/GTFSG2"
# NJT says tokens are typically valid ~24h but reserves the right to change
# that - refresh well before the documented window, and reactively on any
# "invalid token" response regardless of this timer.
TOKEN_MAX_AGE_S = 20 * 3600
TRIP_LOOKUP_REFRESH_S = 24 * 3600
TZ = ZoneInfo("America/New_York")

DELAYED_SEC_LATE = 300  # BUSDV2 gives no seconds-late figure, just a remarks string - pin past the firmware's critical threshold
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
        return 0  # no data reported - firmware skips the icon entirely for 0
    label = passload.strip().lower()
    if "full" in label or "crowd" in label or "heavy" in label:
        return 3
    if "medium" in label or "moderate" in label:
        return 2
    if "light" in label or "empty" in label:
        return 1
    return 0


# GTFS-RT VehiclePosition.OccupancyStatus enum (gtfs-realtime.proto) -> our
# 3-level scale. BUSDV2's passload is essentially never populated in
# practice (always "no data"/"EMPTY" observed); this is the real source.
_OCCUPANCY_STATUS_LEVEL = {
    0: 1,  # EMPTY
    1: 1,  # MANY_SEATS_AVAILABLE
    2: 2,  # FEW_SEATS_AVAILABLE
    3: 3,  # STANDING_ROOM_ONLY
    4: 3,  # CRUSHED_STANDING_ROOM_ONLY
    5: 3,  # FULL
    6: 3,  # NOT_ACCEPTING_PASSENGERS
    # 7 NO_DATA_AVAILABLE, 8 NOT_BOARDABLE - no usable reading, fall through
}


app = Flask(__name__)
_state_lock = threading.Lock()
_state = {"ok": False, "error": "not polled yet"}
_session = requests.Session()
_stop_names = {}  # stop_id -> friendly name, fetched once and cached
# trip_id -> {"route": ..., "header": ...}, from GTFS-RT's static trips.txt.
# Reassigned wholesale (never mutated in place) so reads need no lock.
_trip_lookup = {}


def _make_api_client(api_base):
    # BUSDV2 and GTFSG2 are separate token namespaces on the same account -
    # each needs its own cached token and its own retry-on-invalid-token loop.
    token_lock = threading.Lock()
    token_state = {"token": None, "fetched_at": 0.0}

    def authenticate():
        resp = _session.post(
            f"{api_base}/authenticateUser",
            data={"username": NJT_USERNAME, "password": NJT_PASSWORD},
            timeout=NJT_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("Authenticated") != "True" or not data.get("UserToken"):
            raise RuntimeError(f"NJT auth failed for {api_base} - check NJT_USERNAME/NJT_PASSWORD")
        with token_lock:
            token_state["token"] = data["UserToken"]
            token_state["fetched_at"] = time.time()
        return token_state["token"]

    def get_token(force=False):
        with token_lock:
            stale = token_state["token"] is None or (time.time() - token_state["fetched_at"]) > TOKEN_MAX_AGE_S
        if force or stale:
            return authenticate()
        return token_state["token"]

    def call(endpoint, timeout=NJT_TIMEOUT_S, binary=False, **fields):
        # A token can go bad before our own freshness timer says so (NJT-side
        # revocation, clock drift) - retry once with a forced re-auth before
        # giving up.
        for attempt in range(2):
            token = get_token(force=(attempt == 1))
            resp = _session.post(f"{api_base}/{endpoint}", data={"token": token, **fields}, timeout=timeout)
            resp.raise_for_status()
            if binary:
                # Binary endpoints (protobuf/zip) still return a small JSON
                # body on error instead of the expected content type.
                if resp.headers.get("content-type", "").startswith("application/json"):
                    err = resp.json().get("errorMessage", "")
                    if "token" in err.lower():
                        continue
                    raise RuntimeError(f"{endpoint}: {err or resp.text[:200]}")
                return resp.content
            data = resp.json()
            if isinstance(data, dict) and "token" in data.get("errorMessage", "").lower():
                continue
            if isinstance(data, dict) and "errorMessage" in data:
                raise RuntimeError(f"{endpoint}: {data['errorMessage']}")
            return data
        raise RuntimeError(f"{endpoint}: token kept getting rejected")

    return call


_busdv2 = _make_api_client(BUSDV2_BASE)
_gtfsg2 = _make_api_client(GTFSG2_BASE)


def _get_stop_name(stop_id):
    if stop_id not in _stop_names:
        data = _busdv2("getStopName", stopnum=stop_id)
        _stop_names[stop_id] = data.get("stopName", "")
    return _stop_names[stop_id]


def _refresh_trip_lookup():
    global _trip_lookup
    content = _gtfsg2("getGTFS", timeout=GTFS_STATIC_TIMEOUT_S, binary=True)
    lookup = {}
    with zipfile.ZipFile(io.BytesIO(content)) as zf, zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            lookup[row["trip_id"]] = {
                "route": row["route_id"].strip(),
                "header": re.sub(r"\s+", " ", row["trip_headsign"]).strip(),
            }
    _trip_lookup = lookup  # atomic reference swap - no lock needed for readers


def _trip_lookup_loop():
    while True:
        try:
            _refresh_trip_lookup()
        except Exception as exc:  # keep serving with the last-good (or empty) cache
            print(f"[trip_lookup] refresh failed, keeping previous cache: {exc}", flush=True)
        time.sleep(TRIP_LOOKUP_REFRESH_S)


def _fetch_realtime_updates():
    """Live arrival predictions for our configured stops, keyed by trip_id.

    GTFS-RT is the authoritative real-time source: it includes imminent
    trips BUSDV2's own getBusDV sometimes omits entirely, and carries a real
    seconds-late figure instead of BUSDV2's boolean-ish delay signal.
    """
    content = _gtfsg2("getTripUpdates", timeout=GTFSRT_TIMEOUT_S, binary=True)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)

    now = datetime.now(TZ)
    stop_id_set = set(STOP_IDS)
    by_stop = {stop_id: {} for stop_id in STOP_IDS}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_id = entity.trip_update.trip.trip_id
        for stu in entity.trip_update.stop_time_update:
            if stu.stop_id not in stop_id_set:
                continue
            event = stu.arrival if stu.HasField("arrival") else (stu.departure if stu.HasField("departure") else None)
            if event is None or not event.time:
                continue  # no real-time prediction for this stop on this trip
            eta_dt = datetime.fromtimestamp(event.time, TZ)
            # Floor, not round - transit apps (including NJT's own) never
            # show a bigger number than the true remaining time.
            eta_min = int((eta_dt - now).total_seconds() // 60)
            sec_late = max(event.delay, 0) if event.HasField("delay") else 0
            # Clock skew/rounding can put an on-time prediction a few seconds
            # negative; a real delay is already reflected in eta_dt itself.
            if eta_min < 0 and sec_late == 0:
                eta_min = 0
            by_stop[stu.stop_id][trip_id] = {
                "eta_min": eta_min,
                "eta_time": eta_dt.strftime("%I:%M %p").lstrip("0"),
                "sec_late": sec_late,
            }
    return by_stop


def _fetch_vehicle_occupancy():
    """trip_id -> our 1-3 occupancy level, from GTFS-RT's VehiclePositions
    feed. System-wide (not stop-scoped) - trip_id is the join key against
    whatever _fetch_stop already has, same as trip updates.
    """
    content = _gtfsg2("getVehiclePositions", timeout=GTFSRT_TIMEOUT_S, binary=True)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)

    levels = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("occupancy_status"):
            continue
        level = _OCCUPANCY_STATUS_LEVEL.get(v.occupancy_status)
        if level is not None and v.trip.trip_id:
            levels[v.trip.trip_id] = level
    return levels


def _fetch_stop(stop_id, now, rt_updates, vehicle_occupancy):
    stop_name = _get_stop_name(stop_id)
    data = _busdv2("getBusDV", stop=stop_id, direction="", route="", IP="")
    trips = data.get("DVTrip") or []

    buses_by_trip = {}
    for trip in trips:
        route = (trip.get("public_route") or "").strip()
        dest = re.sub(r"\s+", " ", (trip.get("header") or "")).strip()
        # "159R NEW YORK VIA RIVER ROAD" -> "NEW YORK VIA RIVER ROAD"; the
        # board shows the route on its own badge, so the prefix just eats
        # width. public_route omits branch-variant suffixes like the "R" in
        # "159R" (matches the app) - recover it from the header before
        # stripping, or the variant gets lost outright instead of relocated.
        variant_m = re.match(r"^" + re.escape(route) + r"([A-Z]?)\s+", dest)
        if variant_m and variant_m.group(1):
            route += variant_m.group(1)
        dest = re.sub(r"^" + re.escape(route) + r"\s+", "", dest)

        remarks = _sentinel(trip.get("remarks")) or ""
        # "departurestatus" (e.g. "in 13 mins") only appears once a vehicle
        # is actively tracked; otherwise it just repeats "departuretime",
        # the scheduled clock time, which sched_dep_time below is parsed from.
        status = _sentinel(trip.get("departurestatus")) or ""
        delayed = "delay" in remarks.lower() or "delay" in status.lower()

        eta_min = None
        sched_dt = None
        sched_raw = _sentinel(trip.get("sched_dep_time"))
        if sched_raw:
            try:
                sched_dt = datetime.strptime(sched_raw, SCHED_TIME_FMT).replace(tzinfo=TZ)
                # Floor, not round - see the matching comment in
                # _fetch_realtime_updates.
                eta_min = int((sched_dt - now).total_seconds() // 60)
            except ValueError:
                sched_dt = None
                eta_min = None

        # A live-formatted "in N mins"/"due" countdown isn't on its own proof
        # of real tracking - BUSDV2 can show one from schedule-pattern
        # estimation with no vehicle actually assigned (confirmed: a trip
        # with vehicle_id "EMPTY" AND a GTFS-RT NO_DATA relationship for this
        # exact stop still got a live-formatted countdown here). A real
        # vehicle_id is the trustworthy signal on the BUSDV2 side; GTFS-RT
        # confirmation (below, in the merge step) is the other, independent
        # path for promoting a trip BUSDV2 itself has no vehicle for yet.
        vehicle_id = _sentinel(trip.get("vehicle_id"))
        live_tracked = vehicle_id is not None
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

        # The app shows NJT's own scheduled departure time here, not a time
        # re-derived from the live countdown - the two can legitimately
        # differ (that's what the countdown is for), and re-deriving it from
        # now+eta_min drifts further every poll instead of matching NJT's
        # own reference value. %-I (no leading zero) is a glibc extension,
        # not portable - format normally and strip a leading zero instead.
        if sched_dt is not None:
            eta_time = sched_dt.strftime("%I:%M %p").lstrip("0")
        else:
            eta_time = (now + timedelta(minutes=eta_min)).strftime("%I:%M %p").lstrip("0")

        trip_id = _sentinel(trip.get("internal_trip_number"))
        # GTFS-RT VehiclePositions has real crowding data; BUSDV2's passload
        # is essentially never populated in practice, so it's just a fallback.
        occupancy = vehicle_occupancy.get(trip_id) or _occupancy_level(trip.get("passload"))
        bus = {
            "route": route,
            "header": dest,
            "eta_min": eta_min,
            "eta_time": eta_time,
            "sec_late": DELAYED_SEC_LATE if delayed else 0,
            "realtime": live_tracked,
            "vehicle_id": vehicle_id,
            "occupancy": occupancy,
        }
        buses_by_trip[trip_id or id(trip)] = bus

    # GTFS-RT overrides eta/delay for trips BUSDV2 already knows about (it's
    # the fresher, more complete source), and adds trips BUSDV2 omitted
    # entirely - route/destination for those comes from the static trip
    # lookup instead, since GTFS-RT's own trip_update carries neither.
    for trip_id, rt in rt_updates.items():
        if trip_id in buses_by_trip:
            bus = buses_by_trip[trip_id]
            bus["eta_min"] = rt["eta_min"]
            bus["sec_late"] = rt["sec_late"]
            bus["realtime"] = True
            # eta_time deliberately left alone - it's already NJT's own
            # sched_dep_time (matches what the app shows there), and GTFS-RT's
            # own predicted time is a different, delay-adjusted value that
            # would silently drift it away from the app's displayed time.
            continue
        lookup = _trip_lookup.get(trip_id)
        if not lookup:
            continue  # no cached static metadata for this trip yet - skip rather than show a blank route
        route = lookup["route"]
        header = lookup["header"]
        # Recover a branch-variant suffix (e.g. "159R") the same way as the
        # BUSDV2 path above - route_id alone omits it.
        variant_m = re.match(r"^" + re.escape(route) + r"([A-Z]?)\s+", header)
        if variant_m and variant_m.group(1):
            route += variant_m.group(1)
        header = re.sub(r"^" + re.escape(route) + r"\s+", "", header)
        buses_by_trip[trip_id] = {
            "route": route,
            "header": header,
            "eta_min": rt["eta_min"],
            "eta_time": rt["eta_time"],
            "sec_late": rt["sec_late"],
            "realtime": True,
            "vehicle_id": None,
            "occupancy": vehicle_occupancy.get(trip_id, 0),
        }

    # Schedule-only trips (no live vehicle/prediction) stay on the board -
    # the firmware labels them "Scheduled" rather than hiding them, since
    # NJT's own app shows them too. Still drop trips more than a minute in
    # the past - a "delayed" bus can run a little behind its predicted
    # time, but a large negative eta_min is a stale prediction for a trip
    # that's already come and gone, not a real upcoming arrival (seen
    # repeatedly in GTFS-RT for trips that haven't aged out of the feed yet).
    buses = [b for b in buses_by_trip.values() if b["eta_min"] >= -1]
    return stop_name, buses


def _poll_once():
    now = datetime.now(TZ)
    try:
        rt_by_stop = _fetch_realtime_updates()
    except Exception as exc:
        # Not fatal - BUSDV2 alone still gives a usable, if less complete, board.
        print(f"[gtfs-rt] fetch failed, falling back to BUSDV2-only: {exc}", flush=True)
        rt_by_stop = {}
    try:
        vehicle_occupancy = _fetch_vehicle_occupancy()
    except Exception as exc:
        # Not fatal - occupancy just falls back to the default/BUSDV2 value.
        print(f"[gtfs-rt] vehicle positions fetch failed: {exc}", flush=True)
        vehicle_occupancy = {}

    all_buses = []
    stop_name = None
    errors = []
    # Each stop is independent - one down direction failing shouldn't take
    # the whole board (including the OTHER direction's good data) with it.
    for stop_id in STOP_IDS:
        try:
            name, buses = _fetch_stop(stop_id, now, rt_by_stop.get(stop_id, {}), vehicle_occupancy)
        except Exception as exc:
            errors.append(f"{stop_id}: {exc}")
            continue
        stop_name = stop_name or name
        all_buses.extend(buses)

    if not all_buses and errors:
        raise RuntimeError("; ".join(errors))

    # eta_min is already floored at -1 by _fetch_stop's filter (nothing more
    # stale survives), so a plain ascending sort is correct - a -1 means
    # "due/arriving now" and belongs near the top, not pushed to the bottom.
    all_buses.sort(key=lambda b: b["eta_min"])

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
threading.Thread(target=_trip_lookup_loop, daemon=True).start()

if __name__ == "__main__":
    # threaded=True: unauthenticated and reachable from the public internet,
    # with multiple gift units polling it - don't serialize their requests.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
