# Pikeminnow Fishing Report — Handoff

## Goal

A daily fishing-conditions HTML report covering all 21 NPSRP (Northern Pikeminnow Sport-Reward Program) check-in stations from Cathlamet, WA up to Vernita Bridge / Clarkston. Cron generates it every morning at 05:30; a small Python HTTP server exposes it on port 7070 over Tailscale (HTTPS via `tailscale serve`, *pending tailnet enablement* — see Known Issues).

User priorities (running list, latest first):
1. **Default forecast window: today + next 6 days** (was: tomorrow + next 2). 7 days total, no picker.
2. **Climatology-based water-temp forecast** (10-year DART daily-mean per dam, anchored to today's actual reading) instead of linear extrapolation.
3. **Per-pool "Water Temp Now" strip at top of page** showing current temp at all 8 dam pools.
4. **All 21 stations show live water temp** (was: 2/21). DART forebay temp keyed by dam code.
5. Reliable daily cron (was silently failing for days at a time).
6. Multi-station report, hourly table (Time | Flow | Wind | Water | Air), trend extrapolation for flow, last-year CPUE per station.

## Current Progress

All shipped and verified as of 2026-05-08:

- **DART forebay water temp** for all 21 stations via `https://www.cbr.washington.edu/dart/cs/php/rpt/mg.php` POST. One POST per unique dam (8 total), keyed by FPC dam code (BON/TDA/JDA/MCN/PRD/IHR/LMN/LWG). USGS lookups removed entirely.
- **10-year climatology forecast**: pulls DART history for current_year-10 through current_year-1 once per dam, computes mean by (month, day), anchors to today's actual reading. Forecast = `climatology[date] + (today_actual - climatology[today])`. Cached to `.dart_temp_history_cache.json` (~250 KB, 80 dam-years, frozen for past years).
- **"Water Temp Now" strip** below the header: 8 cells (BON/TDA/JDA/MCN/PRD/IHR/LMN/LWG) with current °F + dam name + as-of date. Most-recent-reading lookback walks back up to 7 days within the current year's DART data.
- **7-day window** (today + 6) — `LOOKAHEAD_DAYS = 6` and `target_dates = [today + i for i in range(0, LOOKAHEAD_DAYS+1)]`. Today's hourly populates from current hour forward (NWS doesn't return past hours).
- **Cron robustness**: `fetch()` now catches `TimeoutError` and `socket.timeout` (Python 3.12 doesn't wrap socket timeouts in `URLError`, which was the root cause of silent multi-day outages). Per-station `try/except` in `build_data()` so one network failure drops only that station.
- **Freshness banner** in `fishing_server.py`: injected into `<body>` on every request. Teal "Report generated …" if <26h old, red "STALE — last update …" otherwise.
- **POST support added to `fetch()`**: `fetch(url, data=dict)` urlencodes + posts with retry/backoff, sharing the same error handling as GETs.
- 21-station dropdown selector, JS-toggled visibility, URL hash + localStorage memory.
- 2025 CPUE per station (frozen cache); top-line "best-CPUE this week" link in the header.
- Atomic `tempfile`+`os.replace` write so the server never sees a half-finished file.

Last run: 21 stations, **978 KB HTML**, **21/21 with live water temp**, 4 s with cache warm.

## What Worked

- **DART POST endpoint** at `cbr.washington.edu/dart/cs/php/rpt/mg.php` with `outputFormat=csvSingle`, `year[]=YYYY` (array param!), and `mgconfig=river`. Returns clean CSV directly. **No browser automation needed** despite the prior handoff's claim — that was wrong (see "What Didn't Work").
- **Climatology + offset anchor** is much better than linear extrapolation for water temp: it follows the seasonal curve and tracks how warm/cool the current year is running. Past-year data is frozen, so caching forever is safe and brings the warm-cache run down to ~4 s.
- **One DART POST per dam-year**, cached forever for past years. Current year fetched fresh each run (8 POSTs). Total network for temps: 8 + 80-on-first-run = 88; subsequent: 8.
- **`fetch_dart_year(dam, year)` returns `{'mm-dd': celsius}`** — easy to merge across years for climatology, easy to look up "today" for the now-strip anchor.
- **Pre-render all 7 days, station-toggle in JS**: HTML is ~1 MB but only one station is laid out at a time. No perceptible browser slowdown on mobile. Don't try lazy-loading; it's premature.
- **Per-station `try/except` in `build_data()`**: a single timeout no longer takes down the whole report. Combined with the new `TimeoutError` catch in `fetch()`, defense-in-depth against cron silently producing no output.
- **Server reads from disk on every GET**: cron's atomic write means the server picks up new content with no restart. Verified via `/health` mtime.
- **NWS grid cache and CPUE cache** (forever-frozen) — both still valuable, untouched this session.

## What Didn't Work

- **TimeoutError silently killing the cron** (root cause of the user's "wednesday data on friday" complaint). In Python 3.12, `urlopen()` socket timeouts raise `TimeoutError` directly — they are *not* wrapped in `URLError`. The old `except (HTTPError, URLError):` clause missed them. Always include `TimeoutError` and `socket.timeout` in network-retry except clauses.
- **First DART probe with GET + scalar `year=2026`** got a 302 redirect to a Drupal CMS error page saying "No values for required parameter: Year." The fix is **POST** with `year[]=2026` (array). The prior handoff's note about DART needing browser automation was wrong; the issue was just the wrong parameter shape.
- **USGS site `14033500` (Umatilla R tributary)** was wired as the Umatilla station's temp source. It reads **~10°F warmer** than the Columbia mainstem in spring because tributaries warm faster. Don't substitute tributaries for mainstem temps — clear the field instead and let DART forebay take over.
- **USGS mainstem temp sensors are largely decommissioned** in McNary pool / Hanford reach / Snake reservoirs. `14019240` (below McNary), `14019200` (at McNary), `12472900` (Vernita), and most Snake gauges return no `00010` data despite the parameter being listed. DART forebay temp is the only reliable mainstem source — don't waste time hunting for live USGS sensors.
- **Beaver Army Terminal (`14246900`)** lost its temp sensor in Feb 2024. Lower-Columbia stations >75 km below Bonneville have no nearby active USGS temp gauge. Use BON forebay as the proxy via DART (already wired).
- **Linear extrapolation of water temp** ricochets off the seasonal curve when the curve bends — replaced with climatology. Don't reintroduce.
- **Earlier "DART requires browser automation" conclusion** in the prior handoff was a red herring caused by sending GET instead of POST and `year=` instead of `year[]=`. Never trust a "needs browser auth" claim without verifying the form-encoding.

## Known Issues

1. **Tailscale Serve enablement** — unresolved from prior session. User said "I figured it out" but never confirmed which path. Verify next time whether `https://alan-nucbox-evo-x2.tail1bbc24.ts.net/` is live.
2. **Day 7 hourly may be empty if NWS trims**: NWS hourly forecast covers ~6.5 days. If a refresh lands when their data has slipped under 7 days, the last day's hourly rows render as "—". Daily Day/Night cards still render. Acceptable; user has been told.
3. **DART current-year readings can lag a day**: the `latest_reading()` lookback walks back 7 days, so a missing today-row falls back to yesterday automatically. The "Water Temp Now" strip date label reflects the actual date used.
4. **Flow extrapolation past day 3-4** is shaky (linear fit on 10-day FPC history). Acceptable for the 7-day window because flow magnitudes don't matter much for the verdict; only `<150 kcfs` thresholds matter.
5. **Stations with short 2025 CPUE seasons** (Hood = 6 weeks, Vernita = 11) fall back to "last entry of season" if a report runs after Oct 12. Fine in spring.

## Key Files & Architecture

```
/home/alan/arath/fishing_reports/pikeminnow/
├── fishing_report.py                 # main report generator (cron target)
├── fishing_server.py                 # HTTP server on :7070
├── report.html                       # generated output (atomic-write target)
├── fishing.log                       # cron stdout/stderr
├── fishing_server.log                # server stdout/stderr
├── .nws_grid_cache.json              # lat/lon → NWS gridpoint
├── .cpue_2025_cache.json             # frozen 2025 catch data per station
├── .dart_temp_history_cache.json     # DART {dam}_{year} → {'mm-dd': celsius}
└── HANDOFF.md                        # this document (gitignored)
```

`fishing_report.py` structure (top-to-bottom):

- **CONFIG** — `LOOKAHEAD_DAYS=6`, `LOCAL_TZ`, thresholds, `CLIMATOLOGY_YEARS=10`.
- **STATIONS** — 21 tuples `(key, name, city, lat, lon, dam_key, slug)`. **Note:** the `usgs` column is gone; DART supersedes it. **Add new stations here.**
- **FPC_DAMS** — dict mapping 8 dam keys → FPC section + (flow, spill) column index. Same keys are used as DART `loc[]` codes.
- **`fetch(url, accept, retries, delay, timeout, data)`** — POST when `data` is a dict; catches `TimeoutError`/`socket.timeout`/`URLError`/`HTTPError` with exponential backoff.
- **DART block** — `_load_temp_hist_cache()`, `fetch_dart_year(dam, year)`, `build_dam_temps(dam, cache)` returns `(climatology, current_year)`, `latest_reading(current, today)`.
- **`build_station_data()`** — receives pre-built `climatology` dict and `current_temps` dict for its dam; computes `offset_c` once and uses `temp_for(d_)` for every forecast day. Hourly rows reuse the day's value (water temp doesn't move within a day).
- **`build_data()`** — fetches FPC once, computes climatology + current_temps per unique dam (saves cache), iterates stations, builds the `now_temps` list for the top strip.
- **`render_html()`** — single big f-string. Header subtitle now reads `Flow trend + 10-yr climatology water temp`. The `now-temps-strip` HTML sits between the header and the picker.
- **CSS additions** — `.now-temps-strip`, `.ntm-row` (8-column grid, collapses to 4-col on mobile), `.ntm-cell`, `.ntm-dam`, `.ntm-val`, `.ntm-name`.
- **`write_atomic()`** — temp-file + `os.replace`.

`fishing_server.py`:
- Reads report from disk on every GET.
- `_inject_freshness_banner(html, mtime)` inserts a fixed-top banner after `<body>`; teal if <26 h old, red "STALE" otherwise.
- `/health` returns JSON with size + mtime + now (used to verify the server is serving fresh content).

Crontab (user `alan`):
```
30 5 * * * /usr/bin/python3 /home/alan/arath/fishing_reports/pikeminnow/fishing_report.py >> /home/alan/arath/fishing_reports/pikeminnow/fishing.log 2>&1
@reboot sleep 10 && /usr/bin/python3 /home/alan/arath/fishing_reports/pikeminnow/fishing_server.py >> /home/alan/arath/fishing_reports/pikeminnow/fishing_server.log 2>&1
```

## Next Steps

Ordered by likely user priority. None are blocking; the report is fully functional.

1. **Confirm Tailscale HTTPS** — still pending from prior session. Ask user whether `https://alan-nucbox-evo-x2.tail1bbc24.ts.net/` works. If not: enable Serve at `https://login.tailscale.com/f/serve?node=nNwac5qYX411CNTRL`, then `tailscale serve --bg 7070`.
2. **Climatology confidence band** — the 10-year history exposes per-day stdev. Could render "57° (this date typ. 53–60°)" or a faint min/max band on the day-stat cell. User hasn't asked; only build if requested.
3. **Multi-year CPUE trend** — pikeminnow.org has 2016–2025 catch data. Could surface "5-yr median for this week" alongside the 2025 value. Same caching pattern as the existing CPUE cache. Speculative.
4. **Mobile layout review** — the now-temps strip collapses to 4-col on <600 px. Hasn't been visually tested on phone. If the 4-col cells are too narrow, drop the dam-name subtitle on mobile.
5. **Selectable date-range picker** — user explicitly declined ("No picker") in this session. If they change their mind, all 7 days are pre-rendered already; just add a JS filter strip. Don't allow >day-7 or <today (NWS data isn't available outside that range).
6. **Verdict tuning** — `overall_verdict()` thresholds (FLOW_GREAT=100k, FLOW_GOOD=150k, WIND_OK=15) are eyeballed. Expose as constants if user reports mismatches.
