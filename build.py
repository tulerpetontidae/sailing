#!/usr/bin/env python3
"""Build data.json for the sailing log.

  python3 build.py

trips.json is the hand-written source: one entry per day on the water, with
the things no watch knows (boat, role, crew, notes). Entries that name a Strava
activity pick up their time, distance and track from tracks/<id>.json. Wind is
fetched once per trip from Open-Meteo's historical forecast archive and cached
in conditions.json, so rebuilding doesn't hit the network again.
"""
import json, math, datetime as dt, urllib.request, urllib.parse, os

KM_PER_NM = 1.852

# Rough named areas. Each track point goes to the nearest centre; one point is
# one minute on the water, so the totals read as time spent in each area.
AREAS = [
    ("Richardson Bay", 37.866, -122.490),
    ("Raccoon Strait", 37.868, -122.447),
    ("East of Tiburon", 37.890, -122.440),
    ("Central Bay",    37.832, -122.437),
    ("Golden Gate",    37.818, -122.478),
]

def decode(poly):
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(poly):
        for k in (0, 1):
            res = sh = 0
            while True:
                b = ord(poly[i]) - 63; i += 1
                res |= (b & 0x1f) << sh; sh += 5
                if b < 0x20: break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0: lat += d
            else: lon += d
        pts.append((lat / 1e5, lon / 1e5))
    return pts

def hms(s):
    h, m, sec = (int(x) for x in s.split(":")) if s.count(":") == 2 else (0, *map(int, s.split(":")))
    return h * 3600 + m * 60 + sec

def nearest_area(lat, lon):
    k = math.cos(math.radians(37.85))
    return min(AREAS, key=lambda a: (a[1] - lat) ** 2 + ((a[2] - lon) * k) ** 2)[0]

# NOAA tidal-current prediction points around where we sail (subordinate
# stations: NOAA publishes only slack and max-current times for these).
CURRENTS = [
    ("PCT0261", 1, "Fort Point, 0.3 nmi W",      37.8092, -122.4828),
    ("PCT0286", 1, "Fort Point, 0.5 nmi E",      37.8117, -122.4663),
    ("PCT0281", 1, "Golden Gate Bridge, 0.8 mi E", 37.8217, -122.4617),
    ("PCT0551", 1, "Yellow Bluff, 0.8 mi E",     37.8350, -122.4550),
    ("PCT0556", 1, "Point Cavallo, 1.3 mi E",    37.8317, -122.4433),
    ("PCT0291", 1, "Alcatraz, 0.2 mi W",         37.8278, -122.4303),
    ("PCT0561", 1, "Point Blunt, 0.5 nmi SW",    37.8455, -122.4230),
    ("PCT0606", 1, "Raccoon Strait, off Pt Stuart", 37.8612, -122.4520),
    ("PCT0621", 1, "Bluff Point, 0.1 mi E",      37.8833, -122.4350),
]

COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]

def wind_for(date, start, hours, lat, lon, cache, key):
    if key in cache: return cache[key]
    q = urllib.parse.urlencode({
        "latitude": round(lat, 3), "longitude": round(lon, 3),
        "start_date": date, "end_date": date,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        "wind_speed_unit": "kn", "timezone": "America/Los_Angeles"})
    url = "https://historical-forecast-api.open-meteo.com/v1/forecast?" + q
    h = json.load(urllib.request.urlopen(url, timeout=30))["hourly"]
    t0 = int(start[:2]); t1 = math.ceil(int(start[:2]) + int(start[3:]) / 60 + hours)
    idx = [i for i, t in enumerate(h["time"]) if t0 <= int(t[11:13]) <= t1]
    sp = [h["wind_speed_10m"][i] for i in idx]
    gu = [h["wind_gusts_10m"][i] for i in idx]
    di = [h["wind_direction_10m"][i] for i in idx]
    sx = sum(math.sin(math.radians(x)) for x in di); cx = sum(math.cos(math.radians(x)) for x in di)
    deg = (math.degrees(math.atan2(sx, cx)) + 360) % 360
    out = {"avg": round(sum(sp) / len(sp)), "gust": round(max(gu)), "deg": round(deg),
           "from": COMPASS[int((deg + 11.25) // 22.5) % 16], "source": "Open-Meteo historical forecast"}
    cache[key] = out
    return out

def compass(deg): return COMPASS[int((deg % 360 + 11.25) // 22.5) % 16]

def wind_at(when, lat, lon, cache, key):
    """Model wind at one moment, interpolated between the hourly values."""
    if key not in cache:
        q = urllib.parse.urlencode({
            "latitude": round(lat, 3), "longitude": round(lon, 3),
            "start_date": when.strftime("%Y-%m-%d"), "end_date": when.strftime("%Y-%m-%d"),
            "hourly": "wind_speed_10m,wind_direction_10m", "wind_speed_unit": "kn",
            "timezone": "America/Los_Angeles"})
        cache[key] = json.load(urllib.request.urlopen(
            "https://historical-forecast-api.open-meteo.com/v1/forecast?" + q, timeout=30))["hourly"]
    h = cache[key]; f = when.hour + when.minute / 60
    i = min(int(f), 22); w = f - i
    kn = h["wind_speed_10m"][i] * (1 - w) + h["wind_speed_10m"][i + 1] * w
    deg = h["wind_direction_10m"][i if w < .5 else i + 1]
    return {"kn": round(kn), "deg": round(deg), "from": compass(deg)}

def current_at(when, lat, lon, cache):
    """Tidal current at one moment and place, from the nearest NOAA point.

    Between successive slack/max events the speed follows the usual
    half-cosine curve. Positive is flood, negative ebb."""
    k = math.cos(math.radians(lat))
    sid, bin_, name, slat, slon = min(CURRENTS, key=lambda c: (c[3] - lat) ** 2 + ((c[4] - lon) * k) ** 2)
    ck = "cur:%s:%s" % (sid, when.strftime("%Y%m%d"))
    if ck not in cache:
        d0 = (when - dt.timedelta(days=1)).strftime("%Y%m%d"); d1 = (when + dt.timedelta(days=1)).strftime("%Y%m%d")
        q = urllib.parse.urlencode({"product": "currents_predictions", "station": sid, "bin": bin_,
            "begin_date": d0, "end_date": d1, "units": "english", "time_zone": "lst_ldt",
            "format": "json", "application": "sailing_log"})
        cache[ck] = json.load(urllib.request.urlopen(
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?" + q, timeout=30))["current_predictions"]["cp"]
    ev = [(dt.datetime.strptime(e["Time"], "%Y-%m-%d %H:%M"), float(e["Velocity_Major"]),
           e["meanFloodDir"], e["meanEbbDir"]) for e in cache[ck]]
    for (t0, v0, fd, ed), (t1, v1, _, _) in zip(ev, ev[1:]):
        if t0 <= when <= t1:
            f = (when - t0) / (t1 - t0)
            v = v0 + (v1 - v0) * (1 - math.cos(math.pi * f)) / 2
            phase = "slack" if abs(v) < 0.2 else ("flood" if v > 0 else "ebb")
            return {"kn": round(abs(v), 1), "toward": fd if v > 0 else ed, "phase": phase, "station": name}
    return None

def wind_trip(date, start, secs, mid_secs, lat, lon, cache):
    """All of a trip's wind from one model point and one hourly series, so the
    average, peak, gusts and the mid-sail value can't disagree with each other."""
    ck = "w:%s:%.3f:%.3f" % (date, lat, lon)
    if ck not in cache:
        q = urllib.parse.urlencode({
            "latitude": round(lat, 3), "longitude": round(lon, 3), "start_date": date, "end_date": date,
            "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m", "wind_speed_unit": "kn",
            "timezone": "America/Los_Angeles"})
        cache[ck] = json.load(urllib.request.urlopen(
            "https://historical-forecast-api.open-meteo.com/v1/forecast?" + q, timeout=30))["hourly"]
    h = cache[ck]
    t0 = int(start[:2]) + int(start[3:]) / 60
    span = [i for i in range(24) if t0 - 0.5 <= i <= t0 + secs / 3600 + 0.5]
    sp = [h["wind_speed_10m"][i] for i in span]; gu = [h["wind_gusts_10m"][i] for i in span]
    di = [h["wind_direction_10m"][i] for i in span]
    sx = sum(math.sin(math.radians(x)) for x in di); cx = sum(math.cos(math.radians(x)) for x in di)
    deg = (math.degrees(math.atan2(sx, cx)) + 360) % 360
    f = t0 + mid_secs / 3600; i = min(int(f), 22); w = f - i
    mkn = h["wind_speed_10m"][i] * (1 - w) + h["wind_speed_10m"][i + 1] * w
    mdeg = h["wind_direction_10m"][i if w < .5 else i + 1]
    return ({"avg": round(sum(sp) / len(sp)), "max": round(max(sp)), "gust": round(max(gu)),
             "deg": round(deg), "from": compass(deg), "source": "Open-Meteo historical forecast"},
            {"kn": round(mkn), "deg": round(mdeg), "from": compass(mdeg)})

def metres(a, b):
    k = math.cos(math.radians(37.85))
    return math.hypot((a[0] - b[0]) * 110540, (a[1] - b[1]) * 111320 * k)

def anchor_spot(pts, harbour):
    """Longest spell of five or more minutes within 60 m of one spot, at least
    500 m from the harbour - the time spent at anchor."""
    best, i = None, 0
    while i < len(pts):
        j = i
        while j + 1 < len(pts) and metres(pts[i], pts[j + 1]) < 60: j += 1
        if j - i >= 4 and (harbour is None or metres(pts[i], harbour) > 500):
            if best is None or j - i + 1 > best[0]: best = (j - i + 1, pts[(i + j) // 2])
        i = j + 1
    return best

def current_avg(samples, cache):
    """Average tidal current over a sail. samples: (datetime, lat, lon) every
    ten minutes. Speed is the mean of the speeds felt; direction comes from the
    vector sum; the phase is whichever of flood/ebb/slack held for 60%+ of the
    samples, otherwise "turning"."""
    sp, vx, vy, ph = [], 0.0, 0.0, {}
    for when, lat, lon in samples:
        c = current_at(when, lat, lon, cache)
        if not c: continue
        sp.append(c["kn"]); ph[c["phase"]] = ph.get(c["phase"], 0) + 1
        vx += c["kn"] * math.sin(math.radians(c["toward"])); vy += c["kn"] * math.cos(math.radians(c["toward"]))
    if not sp: return None
    n = len(sp); top = max(ph, key=ph.get)
    phase = top if ph[top] / n >= 0.6 else "turning"
    return {"kn": round(sum(sp) / n, 1), "toward": round((math.degrees(math.atan2(vx, vy)) + 360) % 360),
            "phase": phase, "share": {k: round(v / n, 2) for k, v in ph.items()}}

def main():
    trips = json.load(open("trips.json"))
    cache = json.load(open("conditions.json")) if os.path.exists("conditions.json") else {}
    out = []
    # the harbour: where the flagged sail's track comes back to the dock
    harbour = None
    for t in trips:
        if t.get("harbour") and t.get("strava"):
            hp = decode(json.load(open("tracks/%d.json" % t["strava"]))["poly"])[-1]
            harbour = {"name": "Sausalito", "lat": round(hp[0], 5), "lon": round(hp[1], 5)}
    for t in trips:
        r = dict(t)
        if t.get("strava"):
            tr = json.load(open("tracks/%d.json" % t["strava"]))
            start = dt.datetime.fromtimestamp(tr["raw"], dt.timezone.utc)   # local wall-clock time stored as UTC
            r["date"] = start.strftime("%Y-%m-%d"); r["start"] = start.strftime("%H:%M")
            r["hours"] = round(hms(tr["mov"]) / 3600, 2)
            r["nm"] = round(float(tr["km"]) / KM_PER_NM, 1)
            r["poly"] = tr["poly"]
            pts = decode(tr["poly"])
            areas = {}
            for la, lo in pts: a = nearest_area(la, lo); areas[a] = areas.get(a, 0) + 1
            r["areas"] = dict(sorted(areas.items(), key=lambda kv: -kv[1]))
            r["near"] = [round(sum(p[0] for p in pts) / len(pts), 4), round(sum(p[1] for p in pts) / len(pts), 4)]
            if t.get("anchored"):
                a = anchor_spot(pts, (harbour["lat"], harbour["lon"]) if harbour else None)
                if a: r["anchor"] = {"lat": round(a[1][0], 5), "lon": round(a[1][1], 5), "min": a[0]}
            # conditions at the middle of the sail, drawn on the trip thumbnails.
            # Points are one per minute of elapsed time, so the middle index is mid-time.
            mlat, mlon = pts[len(pts) // 2]
            mwhen = start.replace(tzinfo=None) + dt.timedelta(seconds=hms(tr["el"]) / 2)
            r["wind"], mw = wind_trip(r["date"], r["start"], hms(tr["el"]), hms(tr["el"]) / 2, mlat, mlon, cache)
            r["mid"] = {"lat": mlat, "lon": mlon, "time": mwhen.strftime("%H:%M"), "wind": mw,
                        "current": current_at(mwhen, mlat, mlon, cache)}
            r["cond"] = r["mid"]
            samples = [(start.replace(tzinfo=None) + dt.timedelta(minutes=i), pts[i][0], pts[i][1])
                       for i in range(0, len(pts), 10)]
            r["avg"] = {"wind": {"kn": r["wind"]["avg"], "deg": r["wind"]["deg"], "from": r["wind"]["from"]},
                        "current": current_avg(samples, cache)}
        if r.get("date") and r.get("near") and not r.get("wind"):     # untracked day with a known place
            r["wind"], mw = wind_trip(r["date"], r["start"], r["hours"] * 3600, r["hours"] * 1800,
                                      r["near"][0], r["near"][1], cache)
            mwhen = dt.datetime.strptime(r["date"] + " " + r["start"], "%Y-%m-%d %H:%M") + dt.timedelta(hours=r["hours"] / 2)
            r["cond"] = {"time": mwhen.strftime("%H:%M"), "wind": mw,
                         "current": current_at(mwhen, r["near"][0], r["near"][1], cache)}
            t0 = dt.datetime.strptime(r["date"] + " " + r["start"], "%Y-%m-%d %H:%M")
            samples = [(t0 + dt.timedelta(minutes=m), r["near"][0], r["near"][1]) for m in range(0, int(r["hours"] * 60) + 1, 10)]
            r["avg"] = {"wind": {"kn": r["wind"]["avg"], "deg": r["wind"]["deg"], "from": r["wind"]["from"]},
                        "current": current_avg(samples, cache)}
        out.append(r)
    out.sort(key=lambda r: (r.get("date") is None, r.get("date") or "", r["key"]))
    json.dump(cache, open("conditions.json", "w"), indent=1)
    data = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "areas": [{"name": a[0], "lat": a[1], "lon": a[2]} for a in AREAS], "harbour": harbour, "trips": out}
    # a rough shoreline for the header sketch (OpenStreetMap coastline, pre-simplified)
    if os.path.exists("coast.json"):
        data["coast"] = json.load(open("coast.json"))["lines"]
    json.dump(data, open("data.json", "w"), ensure_ascii=False, separators=(",", ":"))
    dated = [r for r in out if r.get("date") and r.get("hours")]
    print("%d days (%d with records), %.1f h, %.1f nm" % (len(out), len(dated),
          sum(r.get("hours") or 0 for r in dated), sum(r.get("nm") or 0 for r in dated)))
    for r in dated:
        m = r.get("avg") or {}; mw = m.get("wind") or {}; mc = m.get("current") or {}
        w = r.get("wind") or {}
        print("  %s %-8s %-8s reef %s jib %-4s | wind avg %s max %s gust %s from %s | mid %s kn | current %s %s" % (
            r["date"], r["role"], r.get("boat") or r["type"], r.get("reef"), r.get("jib"),
            w.get("avg"), w.get("max"), w.get("gust"), w.get("from"), mw.get("kn"), mc.get("phase"), mc.get("kn")))

main()
