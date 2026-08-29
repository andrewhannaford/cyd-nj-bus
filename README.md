# NJ Transit CYD Bus Board

Shows next-arrival times for a bus stop on a Cheap Yellow Display
(ESP32-2432S028R).

Architecture: `bridge/` polls NJ Transit's Bus Data Exchange
(`getNextTripsXML`) for one or more stop IDs and serves a small
`/stats` JSON endpoint. `firmware/` is the ESP32 firmware that polls
that endpoint every 20s and renders it. The CYD talks plain HTTP to
the bridge, never directly to NJ Transit, since XML parsing + form-auth
is easier to get right and debug on a real machine than on the ESP32.

**Status:** waiting on NJ Transit to approve the developer account
(registered at developer.njtransit.com, pending). Everything below is
ready to go once credentials land — just fill in the env/secrets files.

## 1. NJ Transit developer account

Registered at https://developer.njtransit.com/registration/ — pending
approval. Once approved you'll have a username/password for the Bus
Data Exchange (`busdata.njtransit.com`). Free tier: 40,000 requests/day
for real-time data, more than enough at a 20-30s poll interval
(~3,000-4,000/day for one stop).

You'll also need your stop's `stop_id`. Once the account is live,
either:
- Download the static GTFS bus feed from the developer portal and look
  up your stop in `stops.txt` (by name or lat/lon), or
- Call `getBusDVXML` for a nearby terminal/location to explore, or ask
  NJ Transit support for the stop_id at a specific street corner.

A physical stop pole usually has its own `stop_id` per direction —
`STOP_IDS` in the bridge config takes a comma-separated list so you can
merge both directions (or a couple of nearby poles) into one board.

## 2. Deploy the bridge

Same pattern as [cyd-ms01-dashboard](../cyd-ms01-dashboard) — a small
Flask container, e.g. on `docker-svc` (10.20.0.193) alongside your
other containers.

1. Copy `bridge/` to the target machine, e.g.:
   ```
   scp -r bridge/ user@docker-svc:/opt/njt-bus-bridge
   ```
2. On the target machine:
   ```
   cd /opt/njt-bus-bridge
   cp njt-bridge.env.example njt-bridge.env
   ```
   Fill in `NJT_USERNAME` / `NJT_PASSWORD` / `STOP_IDS`. `chmod 600` it
   since it holds a password.
3. Build and start it:
   ```
   docker compose up -d --build
   ```
4. Check it: `curl http://<host>:8001/stats` should return JSON with
   `"ok": true` and a `buses` array. `docker compose logs -f
   njt-bus-bridge` if it isn't.

Note the bridge listens on container port 8000, mapped to host port
**8001** in `docker-compose.yml` so it doesn't collide with the
ms01-bridge (which already uses 8000 on `docker-svc`).

## 3. Flash the CYD

Requires [PlatformIO](https://platformio.org/) (CLI or the VS Code
extension).

1. `cd firmware`
2. `cp src/secrets.h.example src/secrets.h` and fill in your WiFi
   SSID/password and `BRIDGE_URL` (point it at the bridge, e.g.
   `http://192.168.0.50:8001/stats`).

   `src/secrets.h` is gitignored — it holds a plaintext WiFi password.
   Confirm `git status` shows it untracked before committing anything
   else in `firmware/`.
3. Connect the CYD via USB and run:
   ```
   pio run -t upload
   pio device monitor
   ```
4. The display shows a departure-board list: route badge, destination,
   and ETA (color-coded — green on time, amber a few minutes late, red
   significantly late, blue running early). No touch or paging, one
   fixed stop.

## Notes

- **Display driver is unverified for this physical unit.** This is a
  third CYD (distinct from `claude-buddy-cyd`'s ST7789 unit and
  `cyd-ms01-dashboard`'s ILI9341_2 unit with broken touch) — defaults
  here match `cyd-ms01-dashboard`'s config (`ILI9341_2_DRIVER` +
  `tft.invertDisplay(true)`) as a starting point, but this unit hasn't
  been flashed yet to confirm. If colors look inverted or the image is
  garbled/offset, try `ILI9341_DRIVER` or the ST7789 flags from
  `claude-buddy-cyd`'s `platformio.ini` next, and toggle
  `invertDisplay()`.
- **Windows build issue:** if `pio run` fails with a cryptic "no such
  file or directory" partway through compiling, that's the same
  PlatformIO/SCons argv-mangling bug documented in
  `cyd-ms01-dashboard`'s README — already worked around here via
  `firmware/extra_script.py` (wired in via `extra_scripts =
  pre:extra_script.py` in `platformio.ini`), no action needed.
- **Time handling:** NJ Transit's API returns bare `HH:MM:SS` times
  with no date or timezone. The bridge anchors them to "today" in
  `America/New_York` explicitly (via Python's `zoneinfo`), regardless
  of the host machine's own timezone, and rolls forward a day if a
  time lands more than 5 minutes in the past (handles trips just after
  midnight).
- The bridge caches NJT responses in memory and refreshes every
  `POLL_INTERVAL` seconds independent of how often the display polls
  it, so the ESP32 always gets an instant response.
- If `docker-svc` or NJT's API becomes unreachable, the bridge keeps
  serving its last-known-good `buses` data with `"ok": false` and an
  `"error"` field rather than blanking the display — the CYD shows a
  "STALE" badge in that case instead of freezing silently.

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

- `/stats` has **no authentication** — deliberately LAN-only (the
  ESP32 can't do TLS/auth). The payload is just public bus arrival
  times, low sensitivity, but still don't port-forward it to the
  internet.
- `njt-bridge.env` holds your NJT developer password in plaintext
  (`chmod 600`'d). Blast radius is low (read-only transit data
  access), but it's still a live credential on a shared box.
