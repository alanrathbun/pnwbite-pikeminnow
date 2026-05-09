#!/usr/bin/env python3
"""
fishing_report.py  —  Pikeminnow Sport-Reward 3-Day Fishing Conditions Report
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates a self-contained HTML report covering all 21 NPSRP check-in
stations from Cathlamet, WA upstream to Vernita Bridge / Clarkston.

Per station, for the next 3 days:
  • NWS hourly + daily forecast (per-station lat/lon)
  • Flow: linear extrapolation of recent FPC daily history (controlling dam)
  • Water temp: USGS IV where reported; else "—"
  • Hourly table: Time | Flow | Wind | Water Temp | Ambient Temp

Written atomically to OUTPUT_HTML (default: report.html beside this script;
override with FISHING_HTML).
"""

import json
import math
import os
import socket
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────

# DATA_DIR: state location. /data on Railway; project dir locally.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_HTML = DATA_DIR / "report.html"
OUTPUT_HTML  = Path(os.environ.get("FISHING_HTML", str(_DEFAULT_HTML)))
NWS_GRID_CACHE     = DATA_DIR / ".nws_grid_cache.json"
CPUE_CACHE         = DATA_DIR / ".cpue_2025_cache.json"
TEMP_HIST_CACHE    = DATA_DIR / ".dart_temp_history_cache.json"

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
LOOKAHEAD_DAYS = 6  # today + next 6 days = 7 days total (NWS forecast ceiling)
TREND_DAYS = 10  # days of FPC history used to fit the linear trend

FLOW_GREAT, FLOW_GOOD = 100_000, 150_000   # cfs (kcfs * 1000)
WIND_OK, WIND_ROUGH   = 15, 20             # mph

# ── STATIONS ──────────────────────────────────────────────────────────────────
# Each station maps to a "controlling dam" whose outflow drives flow at that
# stretch of river; the same dam key drives the DART forebay temp lookup.
#
#   dam : key into FPC_DAMS / DART loc[]
STATIONS = [
    # Lower Columbia (below Bonneville) — BON forebay temp is the closest proxy.
    ("cathlamet",   "Cathlamet Marina",         "Cathlamet, WA",     46.2032, -123.3791, "BON", "cathlamet-marina"),
    ("willowgrove", "Willow Grove Park",        "Longview, WA",      46.1700, -123.0606, "BON", "willow-grove-park"),
    ("rainier",     "Rainier Marina",           "Rainier, OR",       46.0918, -122.9419, "BON", "rainier-marina"),
    ("kalama",      "Kalama Marina",            "Kalama, WA",        46.0190, -122.8489, "BON", "kalama-marina"),
    ("ridgefield",  "Ridgefield Marina",        "Ridgefield, WA",    45.7951, -122.7457, "BON", "ridgefield"),
    # Portland metro Columbia
    ("gleason",     "M. James Gleason Ramp",    "Portland, OR",      45.6024, -122.6755, "BON", "m-james-gleason-boat-ramp"),
    ("chinook",     "Chinook Landing",          "Fairview, OR",      45.5479, -122.4144, "BON", "chinook-landing"),
    ("washougal",   "Washougal Boat Ramp",      "Washougal, WA",     45.5747, -122.3760, "BON", "washougal-boat-ramp"),
    # Bonneville pool
    ("cascade",     "Cascade Locks Ramp",       "Cascade Locks, OR", 45.6720, -121.8939, "TDA", "cascade-locks-boat-ramp"),
    ("stevenson",   "Stevenson Boat Launch",    "Stevenson, WA",     45.6957, -121.8848, "TDA", "stevenson-boat-launch"),
    ("bingen",      "Bingen Marina",            "Bingen, WA",        45.7257, -121.4647, "TDA", "bingen-marina"),
    # The Dalles pool
    ("tdalles",     "The Dalles Boat Basin",    "The Dalles, OR",    45.6112, -121.1830, "JDA", "the-dalles-boat-basin"),
    ("giles",       "Giles French",             "Rufus, OR",         45.6951, -120.7434, "JDA", "giles-french"),
    # John Day pool
    ("umatilla",    "Umatilla Boat Ramp",       "Umatilla, OR",      45.9223, -119.3422, "MCN", "umatilla-boat-ramp"),
    # McNary pool / Hanford reach
    ("richland",    "Columbia Point Park",      "Richland, WA",      46.2667, -119.2698, "PRD", "columbia-point-park"),
    ("vernita",     "Vernita Bridge Rest Area", "Mattawa, WA",       46.6388, -119.8895, "PRD", "vernita-bridge-rest-area"),
    # Snake — Lake Wallula / Ice Harbor outflow
    ("hood",        "Hood Park",                "Burbank, WA",       46.2096, -118.9013, "IHR", "hood-park"),
    # Snake — Lower Monumental outflow
    ("windust",     "Windust Park",             "Pasco, WA",         46.5598, -118.5417, "LMN", "windust-park"),
    # Snake — Lower Granite outflow
    ("boyer",       "Boyer Park",               "Colfax, WA",        46.6669, -117.7374, "LWG", "boyer-park"),
    # Snake — Lower Granite inflow (above-dam)
    ("greenbelt",   "Greenbelt",                "Clarkston, WA",     46.4172, -117.0570, "LWG", "greenbelt"),
    ("swallows",    "Swallows Park",            "Clarkston, WA",     46.3970, -117.0498, "LWG", "swallows-park"),
]

STATION_KEYS = [s[0] for s in STATIONS]

# FPC sections + column index of (flow, spill) for each dam used above.
# Section header text is matched verbatim against the FPC daily file.
FPC_DAMS = {
    "BON": {"name": "Bonneville",       "section": "Lower Columbia Projects",  "flow": 6, "spill": 7},
    "TDA": {"name": "The Dalles",       "section": "Lower Columbia Projects",  "flow": 4, "spill": 5},
    "JDA": {"name": "John Day",         "section": "Lower Columbia Projects",  "flow": 2, "spill": 3},
    "MCN": {"name": "McNary",           "section": "Lower Columbia Projects",  "flow": 0, "spill": 1},
    "PRD": {"name": "Priest Rapids",    "section": "Mid-Columbia Projects",    "flow": 12, "spill": 13},
    "IHR": {"name": "Ice Harbor",       "section": "Snake Basin Projects",     "flow": 10, "spill": 11},
    "LMN": {"name": "Lower Monumental", "section": "Snake Basin Projects",     "flow": 8,  "spill": 9},
    "LWG": {"name": "Lower Granite",    "section": "Snake Basin Projects",     "flow": 4,  "spill": 5},
}

def _utc_offset_hours(d):
    """UTC offset in hours for the given date (handles DST)."""
    dt = datetime(d.year, d.month, d.day, 12, tzinfo=LOCAL_TZ)
    return dt.utcoffset().total_seconds() / 3600

# ── HTTP FETCH WITH RETRY ─────────────────────────────────────────────────────

def fetch(url, accept=None, retries=3, delay=3, timeout=15, data=None):
    headers = {"User-Agent": "fishing-report-bot/3.0 (local)"}
    if accept:
        headers["Accept"] = accept
    if data is not None and isinstance(data, dict):
        data = urlencode(data, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers, data=data)
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode(errors="replace")
        except (HTTPError, URLError, TimeoutError, socket.timeout) as e:
            code = getattr(e, "code", type(e).__name__)
            print(f"  [{attempt+1}/{retries}] {url[:80]}  →  {code}", file=sys.stderr)
            if attempt < retries - 1:
                # Exponential backoff: delay, 2*delay, 4*delay…
                time.sleep(delay * (2 ** attempt))
    return None

# ── SUNRISE / SUNSET (Meeus, per station lat/lon) ────────────────────────────

def sun_times(d, lat, lon):
    rad = math.pi / 180
    tz_offset = _utc_offset_hours(d)
    y, m, day = d.year, d.month, d.day
    if m <= 2:
        y -= 1; m += 12
    A  = int(y / 100); B = 2 - A + int(A / 4)
    JD = int(365.25*(y+4716)) + int(30.6001*(m+1)) + day + B - 1524.5

    n   = JD - 2451545.0
    L0  = (280.46646 + 0.9856474 * n) % 360
    M   = (357.52911 + 0.9856003 * n) % 360
    C   = ((1.914602 - 0.004817 * n / 36525) * math.sin(M * rad)
           + 0.019993 * math.sin(2 * M * rad)
           + 0.000289 * math.sin(3 * M * rad))
    sl  = L0 + C
    eps = 23.439 - 0.00000036 * n
    dec = math.asin(math.sin(eps*rad) * math.sin(sl*rad)) / rad

    cosH = ((math.cos(90.833 * rad) - math.sin(lat*rad) * math.sin(dec*rad))
            / (math.cos(lat*rad) * math.cos(dec*rad)))
    if abs(cosH) > 1:
        return None
    H   = math.acos(cosH) / rad
    y2  = eps / 2 * rad
    eot = 4 * (math.tan(y2)**2 * math.sin(2*L0*rad)
               - 2 * 0.016708634 * math.sin(M*rad)
               + 4 * 0.016708634 * math.tan(y2)**2 * math.sin(M*rad) * math.cos(2*L0*rad)
               - 0.5 * math.tan(y2)**4 * math.sin(4*L0*rad)
               - 1.25 * 0.016708634**2 * math.sin(2*M*rad))
    eot_min = eot * 4 / rad
    noon    = 720 - 4 * lon - eot_min

    def to_local(mins):
        mins = (mins + tz_offset * 60) % 1440
        hh   = int(mins // 60); mm = int(mins % 60)
        ap   = "AM" if hh < 12 else "PM"
        return f"{hh % 12 or 12}:{mm:02d} {ap}", hh + (mins % 60) / 60

    rs, rh = to_local(noon - H * 4)
    ss, sh = to_local(noon + H * 4)
    return {"rise": rs, "set": ss, "riseH": rh, "setH": sh}

# ── FPC PARSER (multi-dam) ────────────────────────────────────────────────────

_FPC_HEADER_KEYWORDS = {"Date", "Flow", "Spill", "Inflow", "Outflow",
                        "Dworshak", "Hells", "McNary", "Grand", "Chief",
                        "Wells", "Rocky", "Reach", "Rock", "Island",
                        "Wanapum", "Priest", "Rapids", "Coulee", "Joseph",
                        "Lower", "Granite", "Goose", "Monumental", "Ice",
                        "Harbor", "John", "Day", "Dalles", "Bonneville",
                        "Brownlee", "Canyon", "PH1", "PH2"}

def parse_fpc_section(text, header, flow_col, spill_col):
    lines = text.splitlines()
    in_section = past_hdrs = False
    rows = []
    for line in lines:
        if header in line:
            in_section, past_hdrs = True, False
            continue
        if not in_section:
            continue
        s = line.strip()
        if not past_hdrs:
            if s.startswith("=") or s == "" or any(k in line for k in _FPC_HEADER_KEYWORDS):
                continue
            past_hdrs = True
        if s.startswith("="):
            break
        if not s or s.startswith("*") or s.startswith("---"):
            continue
        parts = s.split()
        if not parts or "/" not in parts[0]:
            continue
        nums = []
        for p in parts[1:]:
            try: nums.append(float(p))
            except ValueError: nums.append(None)
        max_col = max(flow_col, spill_col)
        if len(nums) > max_col and nums[flow_col] is not None:
            rows.append({
                "date":  parts[0],
                "flow":  nums[flow_col],
                "spill": nums[spill_col] if nums[spill_col] is not None else 0.0,
            })
    return rows

# ── LINEAR TREND EXTRAPOLATION ────────────────────────────────────────────────

def linear_fit(points):
    """Least-squares linear fit. points = [(x, y), ...]. Returns (a, b) for y=a+bx."""
    n = len(points)
    if n < 2:
        return (points[0][1] if n == 1 else 0.0, 0.0)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x*x for x, _ in points)
    sxy = sum(x*y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return (sy / n, 0.0)
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return (a, b)

def extrapolate_at(history, target_dt):
    """
    history = list of {"date_obj": date, "flow": kcfs} (or {"value":...}).
    target_dt = datetime to predict at.
    Returns extrapolated value (kcfs) or None.
    """
    if not history:
        return None
    base = history[0]["date_obj"]
    pts = [((h["date_obj"] - base).days, h["value"]) for h in history]
    a, b = linear_fit(pts)
    # target as days from base (with sub-day fraction)
    delta = (target_dt.date() - base).days + (target_dt.hour + target_dt.minute / 60) / 24
    val = a + b * delta
    return max(val, 0.0)  # flow & temp can't go negative

# ── WIND PARSER ───────────────────────────────────────────────────────────────

def parse_wind(s):
    try:
        nums = [float(x) for x in str(s).replace("mph", "").split()
                if x.replace(".", "").isdigit()]
        return int(max(nums)) if nums else 0
    except Exception:
        return 0

# ── FEEDING WINDOW (used internally only — drives row coloring) ──────────────

def feeding_window(hour_f, rise_h, set_h):
    pre_start, pre_end   = rise_h - 1.5, rise_h + 0.75
    dusk_start, dusk_end = set_h - 0.75, set_h + 1.25
    if pre_start <= hour_f <= pre_end:   return "dawn"
    if dusk_start <= hour_f <= dusk_end: return "dusk"
    if hour_f >= dusk_end or hour_f < pre_start: return "night"
    return "day"

# ── NWS GRID LOOKUP (cached on disk) ──────────────────────────────────────────

_GRID_CACHE = NWS_GRID_CACHE

def _load_grid_cache():
    if _GRID_CACHE.exists():
        try: return json.loads(_GRID_CACHE.read_text())
        except Exception: return {}
    return {}

def _save_grid_cache(c):
    try: _GRID_CACHE.write_text(json.dumps(c, indent=2))
    except OSError: pass

def nws_grid_for(lat, lon, cache):
    key = f"{lat:.4f},{lon:.4f}"
    if key in cache:
        return cache[key]
    raw = fetch(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                accept="application/geo+json")
    if not raw:
        return None
    try:
        p = json.loads(raw)["properties"]
        info = {"office": p["gridId"], "x": p["gridX"], "y": p["gridY"]}
        cache[key] = info
        return info
    except (KeyError, json.JSONDecodeError):
        return None

# ── CPUE (last-year catch-per-unit-effort) ────────────────────────────────────
# 2025 data is frozen (last week ended 10/12/2025), so we cache forever.
# Cache shape: { "<slug>": [["4/20", 0.6], ["4/27", 5.8], ...], ... }

_CPUE_CACHE_FILE = CPUE_CACHE
CPUE_YEAR = 2025

def _load_cpue_cache():
    if _CPUE_CACHE_FILE.exists():
        try: return json.loads(_CPUE_CACHE_FILE.read_text())
        except Exception: return {}
    return {}

def _save_cpue_cache(c):
    try: _CPUE_CACHE_FILE.write_text(json.dumps(c, indent=2))
    except OSError: pass

import re as _re
_TABLE_RE = _re.compile(r"<table[^>]*>.*?</table>", _re.S)
_TAG_RE   = _re.compile(r"<[^>]+>")

def parse_cpue_year_table(html_text, year):
    """Extract [(week_ending_str, cpue_float), ...] for the given year, or []."""
    needle = f": {year}"
    for m in _TABLE_RE.finditer(html_text):
        tbl = m.group(0)
        if needle not in tbl:
            continue
        # Convert <td>X</td> -> "|X|"
        body = _TAG_RE.sub("|", tbl)
        body = _re.sub(r"\|+", "|", body).strip("|")
        cells = [c.strip() for c in body.split("|")]
        try:
            we_idx = cells.index("Week Ending:")
            tot_idx = cells.index("Total", we_idx)
        except ValueError:
            continue
        weeks = cells[we_idx + 1: tot_idx]
        # Find CPUE row (label may be "CPUE*" or "CPUE")
        cp_idx = None
        for i, c in enumerate(cells):
            if c.startswith("CPUE"):
                cp_idx = i; break
        if cp_idx is None:
            continue
        vals = cells[cp_idx + 1: cp_idx + 1 + len(weeks)]
        out = []
        for wk, v in zip(weeks, vals):
            v = v.replace(",", "").strip()
            try:
                out.append((wk, float(v)))
            except ValueError:
                continue
        if out:
            return out
    return []

def fetch_cpue_for_station(slug):
    """Returns [(week_ending_str, cpue), ...] for CPUE_YEAR, or []."""
    url = f"https://www.pikeminnow.org/catch-data/catch-data-by-station/catch-data-{slug}/"
    raw = fetch(url, timeout=20, retries=2)
    if not raw:
        return []
    return parse_cpue_year_table(raw, CPUE_YEAR)

def cpue_lookup(entries, target_d):
    """Pick the entry whose week-ending (M/D in CPUE_YEAR) is the smallest one
    on or after target_d's M/D. Returns (week_str, cpue) or (None, None)."""
    if not entries:
        return None, None
    target_md = (target_d.month, target_d.day)
    best = None
    for wk, v in entries:
        try:
            mm, dd = (int(x) for x in wk.split("/"))
        except ValueError:
            continue
        if (mm, dd) >= target_md:
            if best is None or (mm, dd) < best[0]:
                best = ((mm, dd), wk, v)
    if best:
        return best[1], best[2]
    # off-season (e.g. winter): fall back to last entry of the season
    return entries[-1]

# ── DART FOREBAY WATER TEMP ───────────────────────────────────────────────────
# Columbia River DART (Columbia Basin Research, Univ. of Washington) publishes
# daily-mean forebay temp keyed by dam code (BON, TDA, JDA, MCN, PRD, IHR, LMN,
# LWG). One POST per dam-year fetches a full calendar year of daily means.
#
# Forecast strategy: build a climatology from the previous CLIMATOLOGY_YEARS,
# then for each future date predict climatology[date] + (today_actual -
# climatology[today]). The offset anchors the climate curve to current state,
# so a year that's running 2°F warm stays 2°F warm in the forecast.

CLIMATOLOGY_YEARS = 10
_TEMP_HIST_CACHE = TEMP_HIST_CACHE

def _load_temp_hist_cache():
    if _TEMP_HIST_CACHE.exists():
        try: return json.loads(_TEMP_HIST_CACHE.read_text())
        except Exception: return {}
    return {}

def _save_temp_hist_cache(c):
    try: _TEMP_HIST_CACHE.write_text(json.dumps(c))
    except OSError: pass

def fetch_dart_year(dam_code, year):
    """Return {'mm-dd': celsius} for a full calendar year, or {}."""
    if not dam_code:
        return {}
    params = {
        "sc": "1",
        "mgconfig": "river",
        "outputFormat": "csvSingle",
        "year[]": str(year),
        "loc[]": dam_code,
        "data[]": "Temp (WQM)",
        "startdate": "1/1",
        "enddate":   "12/31",
        "avgyear": "0",
        "consolidate": "1",
        "grid": "1",
    }
    raw = fetch("https://www.cbr.washington.edu/dart/cs/php/rpt/mg.php",
                data=params, timeout=30, retries=2)
    if not raw:
        return {}
    out = {}
    for line in raw.splitlines():
        parts = line.split(",")
        if len(parts) < 7 or parts[0] == "year" or parts[6] == "NA":
            continue
        try:
            c = float(parts[6])
            if c < -5 or c > 40:
                continue
            out[parts[1]] = c
        except (ValueError, IndexError):
            continue
    return out

def build_dam_temps(dam_code, cache):
    """Returns (climatology_by_mmdd_celsius, current_year_by_mmdd_celsius).

    climatology  – mean across the last CLIMATOLOGY_YEARS completed years
    current_year – {'mm-dd': celsius} for the in-progress year (always fresh)
    """
    today = date.today()
    by_mmdd = {}  # (mm, dd) → [celsius values]
    for y in range(today.year - CLIMATOLOGY_YEARS, today.year):
        key = f"{dam_code}_{y}"
        if key not in cache:
            cache[key] = fetch_dart_year(dam_code, y)
        for mmdd, c in cache[key].items():
            try:
                mm, dd = mmdd.split("-")
                by_mmdd.setdefault((int(mm), int(dd)), []).append(c)
            except (ValueError, AttributeError):
                continue
    climatology = {k: sum(v) / len(v) for k, v in by_mmdd.items() if v}
    current = fetch_dart_year(dam_code, today.year)
    return climatology, current

def latest_reading(current_by_mmdd, today, lookback_days=7):
    """Return (date, celsius) of the most recent reading within lookback_days."""
    if not current_by_mmdd:
        return None, None
    for i in range(lookback_days):
        d = today - timedelta(days=i)
        mmdd = f"{d.month}-{d.day}"
        if mmdd in current_by_mmdd:
            return d, current_by_mmdd[mmdd]
    return None, None

# ── DATA PIPELINE ─────────────────────────────────────────────────────────────

def parse_fpc_history(text, dam_key, days):
    """Returns list of {date_obj, value} (kcfs) for given dam key."""
    info = FPC_DAMS[dam_key]
    rows = parse_fpc_section(text, info["section"], info["flow"], info["spill"])
    rows = rows[-days:]
    out = []
    for r in rows:
        try:
            mm, dd, yy = r["date"].split("/")
            d_ = date(int(yy), int(mm), int(dd))
        except (ValueError, IndexError):
            continue
        out.append({"date_obj": d_, "value": r["flow"]})
    return out

def build_station_data(skey, lat, lon, dam_key, cpue_entries,
                        target_dates, fpc_text, grid_cache,
                        climatology, current_temps):
    """Build per-station forecast dict. Returns None on hard failure."""
    flow_hist = parse_fpc_history(fpc_text, dam_key, TREND_DAYS)

    # Anchor the climatology to today's actual reading so a warm year stays warm.
    today = date.today()
    anchor_date, anchor_c = latest_reading(current_temps, today)
    offset_c = 0.0
    if anchor_c is not None and climatology:
        clim_anchor = climatology.get((anchor_date.month, anchor_date.day))
        if clim_anchor is not None:
            offset_c = anchor_c - clim_anchor
    wtemp_now_f = anchor_c * 9/5 + 32 if anchor_c is not None else None

    def temp_for(d_):
        clim_c = climatology.get((d_.month, d_.day)) if climatology else None
        if clim_c is None:
            return None
        return (clim_c + offset_c) * 9/5 + 32

    # NWS grid + hourly + daily
    grid = nws_grid_for(lat, lon, grid_cache)
    hourly_all = daily_all = []
    if grid:
        hraw = fetch(
            f"https://api.weather.gov/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast/hourly",
            accept="application/geo+json")
        if hraw:
            try: hourly_all = json.loads(hraw)["properties"]["periods"]
            except (KeyError, json.JSONDecodeError): pass
        draw = fetch(
            f"https://api.weather.gov/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast",
            accept="application/geo+json")
        if draw:
            try: daily_all = json.loads(draw)["properties"]["periods"]
            except (KeyError, json.JSONDecodeError): pass

    # Build per-day
    days = []
    for td in target_dates:
        d_  = date.fromisoformat(td)
        sun = sun_times(d_, lat, lon)
        h_periods = [p for p in hourly_all if p.get("startTime", "")[:10] == td]
        d_periods = [p for p in daily_all
                     if p.get("name") == d_.strftime("%A")
                     or p.get("name") == d_.strftime("%A") + " Night"]

        # Day-level extrapolated flow (linear); water temp from climatology.
        noon = datetime.combine(d_, datetime.min.time()).replace(hour=12)
        flow_kcfs = extrapolate_at(flow_hist, noon)
        wtemp_f   = temp_for(d_)

        rows = []
        for p in h_periods:
            try:
                dt = datetime.fromisoformat(p["startTime"])
                hf = dt.hour + dt.minute / 60
            except Exception:
                continue
            wind_str = p.get("windSpeed") or "0 mph"
            wind = parse_wind(wind_str)
            wcss = feeding_window(hf, sun["riseH"], sun["setH"]) if sun else "day"

            # Flow varies hour-to-hour with dam ops; water temp barely moves
            # within a day, so use the day's climatology value for every row.
            row_flow = extrapolate_at(flow_hist, dt)
            row_temp = wtemp_f

            rows.append({
                "time_str": dt.strftime("%-I:%M %p"),
                "flow":     row_flow,
                "wind":     wind,
                "wind_str": wind_str,
                "wind_dir": p.get("windDirection", ""),
                "wtemp":    row_temp,
                "atemp":    p.get("temperature"),
                "fw_css":   wcss,
                "wind_ok":  wind <= WIND_OK,
            })

        cpue_wk, cpue_val = cpue_lookup(cpue_entries, d_)
        days.append({
            "date":      td,
            "long_name": d_.strftime("%A, %B %-d"),
            "sun":       sun,
            "rows":      rows,
            "flow_day":  flow_kcfs,
            "wtemp_day": wtemp_f,
            "cpue":      cpue_val,
            "cpue_week": cpue_wk,
            "day_fc":    next((p for p in d_periods if "Night" not in p.get("name","")), None),
            "night_fc":  next((p for p in d_periods if "Night" in p.get("name","")), None),
        })

    # Top-line CPUE for the *current* week (today), shown in the now-strip.
    cpue_now_wk, cpue_now = cpue_lookup(cpue_entries, date.today())

    return {
        "key":       skey,
        "dam_key":   dam_key,
        "dam_name":  FPC_DAMS[dam_key]["name"],
        "has_temp":  wtemp_now_f is not None,
        "has_cpue":  bool(cpue_entries),
        "flow_now":  flow_hist[-1]["value"] if flow_hist else None,
        "wtemp_now": wtemp_now_f,
        "wtemp_now_date": anchor_date.isoformat() if anchor_date else None,
        "cpue_now":  cpue_now,
        "cpue_now_week": cpue_now_wk,
        "days":      days,
    }

def build_data():
    today = date.today()
    target_dates = [(today + timedelta(days=i)).isoformat()
                    for i in range(0, LOOKAHEAD_DAYS + 1)]
    print(f"Fetching FPC + per-station NWS for {target_dates[0]} → {target_dates[-1]}…")

    fpc_text = fetch("https://www.fpc.org/currentdaily/flowspil.txt")
    if not fpc_text:
        print("ERROR: FPC fetch failed", file=sys.stderr)
        return None

    grid_cache = _load_grid_cache()
    cpue_cache = _load_cpue_cache()
    # Build a 10-year climatology and the current year's daily readings per dam.
    # Past years are cached forever (frozen); current year refreshes each run.
    temp_hist_cache = _load_temp_hist_cache()
    climatology = {}     # dam → {(month, day): mean_celsius}
    current_temps = {}   # dam → {'mm-dd': celsius}
    # Preserve dam ordering from STATIONS so the top-of-page strip flows
    # downstream → upstream as the station list does.
    seen = set()
    ordered_dams = [s[5] for s in STATIONS if not (s[5] in seen or seen.add(s[5]))]
    for dam in ordered_dams:
        climatology[dam], current_temps[dam] = build_dam_temps(dam, temp_hist_cache)
    _save_temp_hist_cache(temp_hist_cache)

    stations = []
    for skey, sname, scity, lat, lon, dam, slug in STATIONS:
        # CPUE: cache 2025 data forever (frozen).
        if slug in cpue_cache and cpue_cache[slug]:
            cpue_entries = [(wk, v) for wk, v in cpue_cache[slug]]
        else:
            cpue_entries = fetch_cpue_for_station(slug)
            if cpue_entries:
                cpue_cache[slug] = cpue_entries

        print(f"  · {skey:11s} {sname}  (cpue: {len(cpue_entries):2d} weeks)")
        try:
            sd = build_station_data(skey, lat, lon, dam, cpue_entries,
                                    target_dates, fpc_text, grid_cache,
                                    climatology.get(dam, {}),
                                    current_temps.get(dam, {}))
        except Exception as e:
            print(f"  ! {skey} build failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        sd["name"] = sname
        sd["city"] = scity
        sd["lat"], sd["lon"] = lat, lon
        sd["slug"] = slug
        stations.append(sd)
    _save_grid_cache(grid_cache)
    _save_cpue_cache(cpue_cache)

    # Top-of-page "now" strip: most recent reading at each unique dam pool.
    now_temps = []
    for dam in ordered_dams:
        d_, c = latest_reading(current_temps.get(dam, {}), today)
        if c is not None:
            now_temps.append({
                "dam": dam,
                "dam_name": FPC_DAMS[dam]["name"],
                "temp_f": c * 9/5 + 32,
                "date": d_.strftime("%b %-d"),
            })

    return {
        "generated":    datetime.now(LOCAL_TZ).strftime("%A %B %-d, %Y at %-I:%M %p"),
        "target_range": f"{date.fromisoformat(target_dates[0]).strftime('%b %-d')} – "
                        f"{date.fromisoformat(target_dates[-1]).strftime('%b %-d, %Y')}",
        "stations":     stations,
        "now_temps":    now_temps,
    }

# ── BADGES ────────────────────────────────────────────────────────────────────

def flow_badge(cfs):
    if cfs is None:           return "dim",  "—"
    if cfs <= FLOW_GREAT:     return "great","GREAT"
    if cfs <= FLOW_GOOD:      return "good", "FISHABLE"
    return "high", "HIGH"

def wind_badge(mph):
    if mph <= WIND_OK:    return "wgood",  f"{mph}"
    if mph <= WIND_ROUGH: return "wmarg",  f"{mph}"
    return "wrough", f"{mph}"

def overall_verdict(station):
    f_kcfs = station["days"][0]["flow_day"]
    if f_kcfs is None:
        return "DATA UNAVAILABLE", "verdict-warn", "Could not retrieve flow data."
    cfs = f_kcfs * 1000
    flow_ok = cfs < FLOW_GOOD
    has_calm_prime = any(
        r["wind_ok"]
        for day in station["days"]
        for r in day["rows"]
    )
    if flow_ok and has_calm_prime:
        return "GO FISH", "verdict-go", "Flows and wind both favorable."
    if flow_ok:
        return "MARGINAL", "verdict-marg", "Flows OK but wind may be rough. Target dawn."
    if has_calm_prime:
        return "TOUGH — HIGH FLOWS", "verdict-tough", "Focus on deep eddies away from main current."
    return "STAY HOME", "verdict-no", "High flows AND rough wind. Check again tomorrow."

# ── HTML BUILDER ──────────────────────────────────────────────────────────────

def fmt_flow(kcfs):
    return "—" if kcfs is None else f"{kcfs:.1f}"

def fmt_temp(f):
    return "—" if f is None else f"{f:.0f}°"

def render_station_section(s):
    verdict, vcss, vdetail = overall_verdict(s)
    flow_now = fmt_flow(s["flow_now"])
    fc_now,  fl_now  = flow_badge(s["flow_now"] * 1000 if s["flow_now"] else None)

    day_html = ""
    for day in s["days"]:
        sun_str = (f"☀ {day['sun']['rise']} → {day['sun']['set']}"
                   if day["sun"] else "☀ n/a")
        flow_d  = fmt_flow(day["flow_day"])
        fcd, _  = flow_badge(day["flow_day"] * 1000 if day["flow_day"] else None)
        wtemp_d = fmt_temp(day["wtemp_day"])
        cpue_d  = "—" if day["cpue"] is None else f'{day["cpue"]:.1f}'
        cpue_wk = day["cpue_week"] or ""

        rows_html = ""
        for r in day["rows"]:
            wcss, wval = wind_badge(r["wind"])
            rows_html += f"""
              <tr class="fw-{r['fw_css']}{'' if r['wind_ok'] else ' dim'}">
                <td>{r['time_str']}</td>
                <td>{fmt_flow(r['flow'])}</td>
                <td class="{wcss}">{wval}</td>
                <td>{fmt_temp(r['wtemp'])}</td>
                <td>{fmt_temp(r['atemp']) if r['atemp'] is not None else '—'}</td>
              </tr>"""
        if not rows_html:
            rows_html = '<tr><td colspan="5" class="dim">No hourly forecast available.</td></tr>'

        df, nf = day["day_fc"], day["night_fc"]
        def fc_card(p, label):
            if not p: return f'<div class="fc-card dim">{label}: no data</div>'
            w = parse_wind(p.get("windSpeed", "0"))
            wc, _ = wind_badge(w)
            return f"""<div class="fc-card">
              <div class="fc-label">{label}</div>
              <div class="fc-temp">{p.get('temperature','—')}°{p.get('temperatureUnit','F')}</div>
              <div class="fc-wind {wc}">{p.get('windSpeed','')} {p.get('windDirection','')}</div>
              <div class="fc-sky">{p.get('shortForecast','')}</div>
            </div>"""

        day_html += f"""
        <section class="day-section">
          <div class="day-header">
            <span class="day-name">{day['long_name']}</span>
            <span class="day-sun">{sun_str}</span>
          </div>
          <div class="day-stats">
            <div class="day-stat"><span class="ds-label">Flow</span>
              <span class="ds-value {fcd}">{flow_d} <small>kcfs</small></span></div>
            <div class="day-stat"><span class="ds-label">Water</span>
              <span class="ds-value">{wtemp_d}</span></div>
            <div class="day-stat"><span class="ds-label">'25 CPUE</span>
              <span class="ds-value">{cpue_d}</span>
              <span class="ds-sub">{('wk ' + cpue_wk) if cpue_wk else ''}</span></div>
          </div>
          <div class="fc-row">
            {fc_card(df, 'DAY')}
            {fc_card(nf, 'NIGHT')}
          </div>
          <div class="hourly-wrap">
            <div class="section-label">Hourly</div>
            <table class="hourly-table">
              <thead>
                <tr><th>Time</th><th>Flow</th><th>Wind</th><th>Water</th><th>Air</th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </div>
        </section>"""

    temp_status = "" if s["has_temp"] else (
        '<span class="no-temp">no live water-temp gauge for this reach</span>')
    cpue_now    = "—" if s["cpue_now"] is None else f"{s['cpue_now']:.1f}"
    cpue_now_wk = s["cpue_now_week"] or ""

    return f"""
    <div class="station" data-station="{s['key']}" hidden>
      <div class="station-meta">
        <div class="station-name">{s['name']}</div>
        <div class="station-city">{s['city']} &nbsp;·&nbsp; controlled by {s['dam_name']} outflow</div>
      </div>
      <div class="verdict-box {vcss}">
        <span class="verdict-label">{verdict}</span>
        <span class="verdict-detail">{vdetail}</span>
      </div>
      <div class="now-strip">
        <div class="now-cell"><span class="ds-label">Flow now</span>
          <span class="ds-value {fc_now}">{flow_now} <small>kcfs</small></span></div>
        <div class="now-cell"><span class="ds-label">Water now</span>
          <span class="ds-value">{fmt_temp(s['wtemp_now'])}</span> {temp_status}</div>
        <div class="now-cell"><span class="ds-label">2025 CPUE this week</span>
          <span class="ds-value">{cpue_now}</span>
          <span class="ds-sub">{('wk ending ' + cpue_now_wk) if cpue_now_wk else 'no 2025 data'}</span></div>
      </div>
      {day_html}
    </div>"""

def render_html(data):
    options_html = "\n".join(
        f'<option value="{s["key"]}">{s["name"]} ({s["city"]})</option>'
        for s in data["stations"]
    )
    sections_html = "\n".join(render_station_section(s) for s in data["stations"])
    first_key = data["stations"][0]["key"] if data["stations"] else ""

    # Top-of-page water-temp-now strip (one cell per dam pool).
    now_temps = data.get("now_temps", [])
    if now_temps:
        as_of = now_temps[0]["date"]  # all readings within the same lookback window
        cells = "".join(
            f'<div class="ntm-cell">'
            f'<span class="ntm-dam">{n["dam"]}</span>'
            f'<span class="ntm-val">{n["temp_f"]:.0f}°</span>'
            f'<span class="ntm-name">{n["dam_name"]}</span>'
            f'</div>' for n in now_temps
        )
        now_temps_html = (
            f'<div class="now-temps-strip">'
            f'<div class="ntm-label">Water Temp Now <small>· as of {as_of}</small></div>'
            f'<div class="ntm-row">{cells}</div>'
            f'</div>'
        )
    else:
        now_temps_html = ""

    # Best 2025 CPUE for the current week, across all stations
    best = max((s for s in data["stations"] if s.get("cpue_now") is not None),
               key=lambda s: s["cpue_now"], default=None)
    if best:
        best_html = (f'Best 2025 CPUE: <a href="#{best["key"]}">'
                     f'{best["name"]}</a> ({best["cpue_now"]:.1f}, '
                     f'wk ending {best["cpue_now_week"]})')
    else:
        best_html = "Best 2025 CPUE: no data"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NPSRP Fishing Conditions — {data['target_range']}</title>
<meta property="og:title" content="Pikeminnow Sport-Reward Report" />
<meta property="og:description" content="Daily fishing forecast for 21 NPSRP check-in stations on the Columbia River." />
<meta property="og:url" content="https://pikeminnow.pnwbite.com/" />
<meta property="og:type" content="website" />
<meta name="twitter:card" content="summary" />
<meta name="description" content="Daily Northern Pikeminnow Sport-Reward Program fishing forecast across 21 check-in stations on the Columbia River." />
<style>
  :root {{
    --bg:#0a0e12; --bg2:#0d1620; --bg3:#0a1018; --bdr:#111d28; --bdr2:#1a2e3a;
    --muted:#2e6a7a; --dim:#4a6a7a; --txt:#c8d6e0; --acc:#4dd9ac;
    --mono:'Courier New','Lucida Console',monospace;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:var(--mono); background:var(--bg); color:var(--txt);
         font-size:13px; line-height:1.5; }}

  .header {{ background:linear-gradient(180deg,#0f1923,var(--bg));
             border-bottom:1px solid #1e3a4a; padding:16px 20px; }}
  .header-title {{ font-size:13px; letter-spacing:.22em; color:var(--acc);
                   font-weight:bold; text-transform:uppercase; }}
  .header-sub {{ font-size:10px; letter-spacing:.12em; color:var(--dim); margin-top:3px; }}
  .header-meta {{ font-size:9px; color:var(--muted); margin-top:8px; }}

  .picker {{ padding:14px 20px; background:var(--bg2);
             border-bottom:1px solid var(--bdr); display:flex; gap:10px;
             align-items:center; flex-wrap:wrap; }}
  .picker label {{ font-size:9px; letter-spacing:.22em; color:var(--muted);
                   text-transform:uppercase; }}
  .picker select {{ flex:1; min-width:240px; background:var(--bg3); color:var(--txt);
                    border:1px solid var(--bdr2); padding:8px 10px;
                    font-family:var(--mono); font-size:13px; border-radius:2px; }}

  .station-meta {{ padding:12px 20px; background:var(--bg2);
                   border-bottom:1px solid var(--bdr); }}
  .station-name {{ font-size:14px; letter-spacing:.15em; color:var(--acc);
                   font-weight:bold; text-transform:uppercase; }}
  .station-city {{ font-size:10px; color:var(--dim); margin-top:2px; }}

  .verdict-go    {{ background:#4dd9ac11; border-left:3px solid #4dd9ac; --vc:#4dd9ac; }}
  .verdict-marg  {{ background:#facc1511; border-left:3px solid #facc15; --vc:#facc15; }}
  .verdict-tough {{ background:#fb923c11; border-left:3px solid #fb923c; --vc:#fb923c; }}
  .verdict-no    {{ background:#f8717111; border-left:3px solid #f87171; --vc:#f87171; }}
  .verdict-warn  {{ background:#94a3b811; border-left:3px solid #94a3b8; --vc:#94a3b8; }}
  .verdict-box {{ padding:10px 16px; margin:1px 0;
                  border-left:3px solid var(--vc); }}
  .verdict-label {{ font-size:12px; letter-spacing:.2em; color:var(--vc); font-weight:bold; }}
  .verdict-detail {{ font-size:11px; color:#8aa0ae; margin-left:14px; }}

  .now-temps-strip {{ background:var(--bg2); padding:14px 20px;
                      border-bottom:1px solid var(--bdr); }}
  .ntm-label {{ font-size:9px; letter-spacing:.22em; color:var(--muted);
                text-transform:uppercase; margin-bottom:10px; }}
  .ntm-label small {{ color:var(--dim); font-weight:normal; letter-spacing:.06em; }}
  .ntm-row {{ display:grid; grid-template-columns:repeat(8,1fr); gap:1px;
              background:var(--bdr); }}
  .ntm-cell {{ background:var(--bg3); padding:10px 8px; text-align:center;
               display:flex; flex-direction:column; gap:2px; }}
  .ntm-dam {{ font-size:9px; letter-spacing:.18em; color:var(--muted); }}
  .ntm-val {{ font-size:20px; font-weight:bold; color:var(--acc); }}
  .ntm-name {{ font-size:8px; color:var(--dim); }}
  @media (max-width:600px) {{ .ntm-row {{ grid-template-columns:repeat(4,1fr); }} }}

  .now-strip {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
                background:var(--bdr); margin:1px 0; }}
  .now-cell {{ background:var(--bg2); padding:14px 20px; }}
  .ds-label {{ font-size:9px; letter-spacing:.22em; color:var(--muted);
               text-transform:uppercase; display:block; margin-bottom:4px; }}
  .ds-value {{ font-size:22px; font-weight:bold; }}
  .ds-value small {{ font-size:11px; color:var(--dim); font-weight:normal; margin-left:4px; }}
  .ds-sub {{ font-size:9px; color:var(--dim); margin-left:6px; letter-spacing:.06em; }}
  .no-temp {{ font-size:9px; color:var(--dim); margin-left:8px; }}

  .day-section {{ background:var(--bg2); border-bottom:2px solid var(--bdr); }}
  .day-header  {{ display:flex; justify-content:space-between; align-items:center;
                  padding:12px 20px; background:#0c1822;
                  border-bottom:1px solid var(--bdr); flex-wrap:wrap; gap:6px; }}
  .day-name {{ font-size:13px; letter-spacing:.15em; color:var(--acc); font-weight:bold; }}
  .day-sun  {{ font-size:10px; color:var(--muted); }}
  .day-stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--bdr); }}
  .day-stat {{ background:var(--bg3); padding:10px 16px; }}
  .day-stat .ds-value {{ font-size:18px; }}

  .great {{ color:#4dd9ac; }} .good {{ color:#a3e635; }} .high {{ color:#f87171; }}
  .dim   {{ color:var(--dim); }}
  .wgood {{ color:#4dd9ac; }} .wmarg {{ color:#facc15; }} .wrough {{ color:#f87171; }}

  .fc-row  {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--bdr); }}
  .fc-card {{ background:var(--bg3); padding:14px 16px; }}
  .fc-label  {{ font-size:9px; letter-spacing:.18em; color:var(--muted);
                text-transform:uppercase; margin-bottom:6px; }}
  .fc-temp   {{ font-size:24px; font-weight:bold; line-height:1; }}
  .fc-wind   {{ font-size:10px; margin-top:6px; }}
  .fc-sky    {{ font-size:10px; color:var(--dim); margin-top:3px; }}

  .hourly-wrap {{ padding:14px 20px; }}
  .section-label {{ font-size:9px; letter-spacing:.22em; color:var(--muted);
                    text-transform:uppercase; margin-bottom:8px; }}
  .hourly-table {{ width:100%; border-collapse:collapse; font-size:11px; }}
  .hourly-table th {{ font-size:8px; letter-spacing:.15em; color:var(--muted);
                      text-transform:uppercase; text-align:left;
                      padding:0 8px 6px 0; border-bottom:1px solid var(--bdr); }}
  .hourly-table td {{ padding:5px 8px 5px 0; border-bottom:1px solid #0c1620;
                      vertical-align:middle; }}
  .hourly-table tr.dim {{ opacity:.45; }}
  /* time-of-day cue: thin colored left border on the time cell */
  .hourly-table tr.fw-dawn  td:first-child {{ border-left:3px solid #f59e0b; padding-left:6px; color:#f59e0b; }}
  .hourly-table tr.fw-dusk  td:first-child {{ border-left:3px solid #f97316; padding-left:6px; color:#f97316; }}
  .hourly-table tr.fw-night td:first-child {{ border-left:3px solid #818cf8; padding-left:6px; color:#818cf8; }}
  .hourly-table tr.fw-day   td:first-child {{ border-left:3px solid transparent; padding-left:6px; }}

  .footer {{ background:var(--bg2); padding:14px 20px; font-size:10px;
             border-top:1px solid var(--bdr); }}
  .footer a {{ color:var(--muted); text-decoration:none; margin-right:20px; }}
  .footer a:hover {{ color:var(--acc); }}
  .footer-gen {{ color:var(--dim); margin-bottom:8px; }}

  @media (max-width:600px) {{
    .fc-row, .day-stats, .now-strip {{ grid-template-columns:1fr; }}
    .verdict-detail {{ margin-left:0; display:block; margin-top:4px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-title">◈ NPSRP Fishing Conditions</div>
  <div class="header-sub">Northern Pikeminnow Sport-Reward Program — 21 stations</div>
  <div class="header-meta">Forecast: {data['target_range']} · Flow trend + {CLIMATOLOGY_YEARS}-yr climatology water temp · {best_html}</div>
</div>

{now_temps_html}

<div class="picker">
  <label for="station-select">Station</label>
  <select id="station-select">{options_html}</select>
</div>

<div id="stations">{sections_html}</div>

<div class="footer">
  <div class="footer-gen">Generated: {data['generated']} · Auto-refreshes daily at 05:30</div>
  <div>
    <a href="https://www.pikeminnow.org/" target="_blank">NPSRP ↗</a>
    <a href="https://www.fpc.org/currentdaily/flowspil.txt" target="_blank">FPC Flows ↗</a>
    <a href="https://www.cbr.washington.edu/dart" target="_blank">DART ↗</a>
    <a href="https://forecast.weather.gov/" target="_blank">NWS ↗</a>
  </div>
</div>

<script>
(function() {{
  const sel = document.getElementById('station-select');
  const all = document.querySelectorAll('.station');
  function show(key) {{
    all.forEach(el => {{ el.hidden = (el.dataset.station !== key); }});
    try {{ localStorage.setItem('npsrp.station', key); }} catch (e) {{}}
    if (location.hash !== '#' + key) {{
      history.replaceState(null, '', '#' + key);
    }}
  }}
  sel.addEventListener('change', () => show(sel.value));
  window.addEventListener('hashchange', () => {{
    const key = location.hash.slice(1);
    if (key && document.querySelector('[data-station="' + key + '"]')) {{
      sel.value = key; show(key);
    }}
  }});
  let initial = location.hash.slice(1);
  if (!initial) {{
    try {{ initial = localStorage.getItem('npsrp.station') || ''; }} catch (e) {{}}
  }}
  if (!initial || !document.querySelector('[data-station="' + initial + '"]')) {{
    initial = '{first_key}';
  }}
  sel.value = initial;
  show(initial);
}})();
</script>

</body>
</html>"""

# ── MAIN ──────────────────────────────────────────────────────────────────────

def write_atomic(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report-", suffix=".html.tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def main():
    """Generate the report. Importable by the scheduler."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Generating fishing report…")
    data = build_data()
    if data is None:
        print("Aborting — data fetch failed.", file=sys.stderr)
        sys.exit(1)

    html = render_html(data)
    write_atomic(OUTPUT_HTML, html)
    n_temp = sum(1 for s in data["stations"] if s["has_temp"])
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Report written → {OUTPUT_HTML}  ({len(html):,} bytes)")
    print(f"  Stations: {len(data['stations'])} ({n_temp} with live water-temp)")


if __name__ == "__main__":
    main()
