# NJ Transit CYD Bus Board

Shows next-arrival times for a bus stop on a Cheap Yellow Display
(ESP32-2432S028R). **Live and deployed** — see Status below.

## Architecture

`bridge/` (Flask) scrapes NJ Transit's public, unauthenticated "MyBus"
wireless site (`mybusnow.njtransit.com`) every `POLL_INTERVAL` seconds
and serves a small `/stats` JSON snapshot. `firmware/` (ESP32) polls
that endpoint every 20s and renders it. The CYD never talks to NJT
directly.

**Why scraping instead of NJT's official developer API:** the
`developer.njtransit.com` account never got approved (registered,
pending indefinitely). `mybusnow.njtransit.com`'s "wireless" (text-only,
pre-smartphone-era) pages need no API key or login — the bridge warms a
session against `home.jsp` for cookies, then GETs `eta.jsp?...&id=<stop_id>`
and regex-parses the HTML prediction blocks. This is inherently fragile
(NJT could change the markup any time with no notice) and occasionally
403s behind Cloudflare's bot challenge under load — the bridge retries
once by re-warming the session, and the firmware shows a "STALE" pill
rather than blanking when a poll fails. If the developer account is ever
approved, `git log` has the original `getNextTripsXML` implementation to
revert to.

## Status: LIVE

- **Bridge**: deployed as `njt-bus-bridge` on `docker-svc` (10.20.0.193),
  `/opt/docker/services/njt-bus-bridge/`. Reachable two ways:
  - LAN: `http://docker-svc.home:8001/stats`
  - Public (for gift units off-network): `https://njtbus.lanarchy.net/stats`
    — routed through the home Traefik proxy, same unauthenticated
    `crowdsec-bouncer + rate-limit + secure-headers` chain as the
    portfolio sites (no Pocket ID — an ESP32 can't do OIDC login, and
    bus arrival times aren't sensitive).
- **Firmware**: flashed and running on one physical unit, stop 21923
  (Port Imperial Blvd at Riverwalk Place, routes 158/159 to NYC).
- Redeploy the bridge after any `bridge/app.py` change:
  ```
  scp bridge/app.py docker-svc@10.20.0.193:/opt/docker/services/njt-bus-bridge/app.py
  ssh docker-svc@10.20.0.193 "cd /opt/docker/services/njt-bus-bridge && docker compose up -d --build"
  ```

## WiFi setup — no compiled-in credentials

Firmware uses `WiFiManager` (captive portal), not a `secrets.h` file.
On first boot (or after a long outage — see below), the unit opens its
own AP `NJ-Bus-Setup` and shows on-screen instructions; connect a phone
to it and pick the real WiFi network from the popup. Credentials persist
in ESP32 NVS after that. This is what makes the device giftable to
neighbors — you never need their WiFi password.

If a unit goes fully offline for more than 10 minutes (saved WiFi
stopped working — router replaced, password changed), it automatically
reopens the setup portal rather than getting stuck forever needing a
manual power cycle.

## Two PlatformIO environments — same firmware, different bridge URL

```
pio run -e lan -t upload      # points at http://docker-svc.home:8001/stats (your own units)
pio run -e public -t upload   # points at https://njtbus.lanarchy.net/stats (gift units)
```

No `secrets.h` needed at all — `BRIDGE_URL` is a build-time flag in
`platformio.ini`, WiFi is handled entirely by the captive portal.

## UI

Direct visual match to NJ Transit's own mobile app (colors sampled
from `njtransit.com` and a phone screenshot), adapted for a 320×240
screen — not a literal port, several iterations of trimming happened:

- Dark background (`#0d0d0d`) with rows alternating navy (`#00416d`,
  NJT's own blue) — zebra striping only on rows that actually have a
  bus, never on empty trailing slots.
- Route number: plain bold text, no badge (matches the app).
- Middle column: destination, then arrival clock time + a 3-figure
  congestion icon (green/grey person silhouettes, drawn large — an
  earlier attempt at tiny icons was unreadable) on the same line.
- Right: big bold ETA countdown ("7 min" / "Due" / "Delayed"),
  color-coded amber/red when late.
- **Dropped along the way** (real UX feedback, not first-draft
  guesses): the absolute-time-as-its-own-line, the vehicle-number
  text, and a "live GPS vs scheduled" icon — all judged as either
  redundant with other info or too small to read at a glance on this
  screen. `git log` has the fuller 3-line/vehicle-number version if a
  reason ever comes up to want it back.
- Status pill (LIVE/STALE/NO NET) is a **solid filled badge**, not
  colored text on the blue header — colored text directly on `#004f99`
  blue had too little contrast to read.
- Rows render into an off-screen sprite and push in one shot; only
  rows that actually changed since the last poll get redrawn, so
  there's no visible flicker on an unrelated field updating.

## Notes / gotchas hit building this

- **A "buses disappeared then came back a few minutes later" report
  turned out to be real NJT behavior, not a scraper bug** — verified
  by hitting NJT's own `eta.jsp` directly at the same moment and
  getting the identical "No arrival times available." Before assuming
  the scraper broke, check NJT's page directly for the same stop_id.
- The bridge has **no historical logging** — it only ever serves the
  latest snapshot, so a "what did it show 5 minutes ago" question
  can't be answered retroactively. Deliberately kept simple; add
  logging only if this becomes a recurring need.
- **Passenger occupancy is intermittent by nature**, not a bug when
  it's missing — NJT's page only includes `(Passengers: ...)` when a
  vehicle happens to be reporting it; same for `(Vehicle #####)`
  (real-time tracking indicator) and the derived arrival clock time
  (computed by the bridge from `eta_min`, not present on NJT's page
  at all).
- Timezone math (`America/New_York` via `zoneinfo`) is correct —
  double-check by comparing against local EDT/EST time, not a raw UTC
  timestamp, before suspecting a bug there.
- **Display driver** for this physical unit: `ILI9341_2_DRIVER` +
  `tft.invertDisplay(true)` (same as `cyd-ms01-dashboard`) — confirmed
  working, no longer unverified.
- **Windows PlatformIO build fix** already wired in via
  `firmware/extra_script.py` (SCons argv-mangling workaround,
  documented in `cyd-ms01-dashboard`'s README) — no action needed.

## Legal

Per NJ Transit's developer terms, any app using this data must display:

> Data provided by NJ TRANSIT, which is the sole owner of the Data.
> This "App" is not endorsed by, directly affiliated with, maintained,
> authorized, or sponsored by NJ TRANSIT. All product and company
> names are the registered trademarks of their original owners. The
> use of any trade name or trademark is for identification and
> reference purposes only and does not imply any association with the
> trademark owner.

## Security notes

- `/stats` is **intentionally public and unauthenticated** — the data
  is just public bus arrival times (low sensitivity), and gift units
  on other people's networks need to reach it. Protected only by the
  home Traefik proxy's standard `public` entrypoint chain
  (CrowdSec + rate limit), same as the portfolio sites.
- No credentials anywhere in this project anymore — no NJT developer
  password, no WiFi password baked into firmware. `bridge/njt-bridge.env`
  (gitignored) holds only `STOP_IDS`/`POLL_INTERVAL`/`PORT`.
