#!/usr/bin/env python3
"""Build a self-contained HTML map from a Strava bulk export.

Usage:
    python build_map.py <export.zip | export_folder> [-o map.html]

The Strava export (Settings > My Account > Download or Delete Your Account >
Request Your Archive) contains ``activities.csv`` and an ``activities/`` folder
of .gpx / .fit.gz / .tcx.gz files. This script reads every GPS track, simplifies
it, and writes a single HTML file with all routes drawn on an interactive map,
plus a toggleable frequency heatmap.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import fitdecode
except ImportError:  # pragma: no cover
    fitdecode = None

SEMICIRCLE = 180.0 / 2**31
TRACK_EXTS = (".gpx", ".gpx.gz", ".fit", ".fit.gz", ".tcx", ".tcx.gz")


# --------------------------------------------------------------------------- #
# Reading the export
# --------------------------------------------------------------------------- #

def open_export(path: Path):
    """Yield (relative_name, bytes) for every file in a zip or folder."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                yield info.filename, zf.read(info)
    elif path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(path)), p.read_bytes()
    else:
        sys.exit(f"Not a zip file or folder: {path}")


def maybe_gunzip(name: str, data: bytes) -> tuple[str, bytes]:
    if name.lower().endswith(".gz"):
        return name[:-3], gzip.decompress(data)
    return name, data


def parse_activities_csv(data: bytes) -> dict[str, dict]:
    """Map filename (as listed in the CSV) -> metadata row."""
    text = data.decode("utf-8-sig", errors="replace")
    rows = {}
    for row in csv.DictReader(io.StringIO(text)):
        fn = (row.get("Filename") or "").strip()
        if fn:
            rows[fn] = row
            rows[Path(fn).name] = row
    return rows


def parse_strava_date(s: str) -> str | None:
    s = (s or "").strip()
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def to_float(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Track parsers -> list of (lat, lon)
# --------------------------------------------------------------------------- #

def parse_gpx(data: bytes) -> tuple[list, dict]:
    pts, meta = [], {}
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "trkpt":
            try:
                pts.append((float(el.get("lat")), float(el.get("lon"))))
            except (TypeError, ValueError):
                pass
            el.clear()
        elif tag == "type" and "type" not in meta and el.text:
            meta["type"] = el.text.strip()
        elif tag == "name" and "name" not in meta and el.text:
            meta["name"] = el.text.strip()
        elif tag == "time" and "date" not in meta and el.text:
            meta["date"] = el.text.strip()[:10]
    return pts, meta


def parse_tcx(data: bytes) -> tuple[list, dict]:
    pts, meta = [], {}
    lat = lon = None
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "LatitudeDegrees":
            lat = to_float(el.text)
        elif tag == "LongitudeDegrees":
            lon = to_float(el.text)
        elif tag == "Position":
            if lat is not None and lon is not None:
                pts.append((lat, lon))
            lat = lon = None
        elif tag == "Activity":
            meta.setdefault("type", el.get("Sport", ""))
        elif tag == "Id" and "date" not in meta and el.text:
            meta["date"] = el.text.strip()[:10]
    return pts, meta


def parse_fit(data: bytes) -> tuple[list, dict]:
    if fitdecode is None:
        raise RuntimeError("fitdecode is not installed (pip install fitdecode)")
    pts, meta = [], {}
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "record":
                lat = frame.get_value("position_lat", fallback=None)
                lon = frame.get_value("position_long", fallback=None)
                if lat is not None and lon is not None:
                    pts.append((lat * SEMICIRCLE, lon * SEMICIRCLE))
            elif frame.name in ("sport", "session") and "type" not in meta:
                sport = frame.get_value("sport", fallback=None)
                if sport:
                    meta["type"] = str(sport)
                if frame.name == "session":
                    ts = frame.get_value("start_time", fallback=None)
                    if ts is not None:
                        meta["date"] = str(ts)[:10]
    return pts, meta


def parse_track(name: str, data: bytes) -> tuple[list, dict]:
    lower = name.lower()
    if lower.endswith(".gpx"):
        return parse_gpx(data)
    if lower.endswith(".tcx"):
        return parse_tcx(data)
    if lower.endswith(".fit"):
        return parse_fit(data)
    return [], {}


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def haversine_km(a, b) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(d))


def track_length_km(pts) -> float:
    return sum(haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _perp_dist(p, a, b) -> float:
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def simplify(pts, tol=0.00004):
    """Iterative Douglas-Peucker. tol in degrees (~4-5 m)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e <= s + 1:
            continue
        best, idx = 0.0, -1
        for i in range(s + 1, e):
            d = _perp_dist(pts[i], pts[s], pts[e])
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, k in zip(pts, keep) if k]


def clean(pts, max_jump_km=5):
    """Drop obvious GPS garbage (0,0 points, impossible jumps). max_jump_km=None disables the jump filter."""
    out = []
    for p in pts:
        if abs(p[0]) < 0.01 and abs(p[1]) < 0.01:
            continue
        if not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180):
            continue
        if max_jump_km and out and haversine_km(out[-1], p) > max_jump_km:  # glitch
            continue
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def normalize_type(t: str) -> str:
    t = (t or "").strip()
    aliases = {
        "cycling": "Ride", "biking": "Ride", "Biking": "Ride", "ride": "Ride",
        "running": "Run", "run": "Run",
        "hiking": "Hike", "hike": "Hike",
        "walking": "Walk", "walk": "Walk",
        "generic": "Other", "": "Other",
    }
    t = aliases.get(t, aliases.get(t.lower(), t))
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)


def build(export: Path, out: Path, verbose=True) -> dict:
    csv_meta: dict[str, dict] = {}
    tracks: list[tuple[str, bytes]] = []

    for name, data in open_export(export):
        base = Path(name).name
        if base.lower() == "activities.csv":
            csv_meta = parse_activities_csv(data)
        elif name.lower().endswith(TRACK_EXTS):
            tracks.append((name, data))

    if verbose:
        print(f"Found {len(tracks)} track files, {len(csv_meta)//2 if csv_meta else 0} CSV rows")

    routes, skipped = [], 0
    for i, (name, data) in enumerate(tracks, 1):
        try:
            plain_name, raw = maybe_gunzip(name, data)
            pts, meta = parse_track(plain_name, raw)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  ! {name}: {e}")
            skipped += 1
            continue

        pts = clean(pts)
        if len(pts) < 2:
            skipped += 1
            continue

        row = csv_meta.get(name) or csv_meta.get(Path(name).name) or {}
        act_id = (row.get("Activity ID") or Path(plain_name).stem.split(".")[0]).strip()
        a_type = normalize_type(row.get("Activity Type") or meta.get("type"))
        date = parse_strava_date(row.get("Activity Date", "")) or meta.get("date")
        dist_km = to_float(row.get("Distance"))
        if dist_km is None or dist_km <= 0:
            dist_km = track_length_km(pts)
        # Strava export lists Distance in km already (floating), but the first
        # Distance column is sometimes meters in newer exports; sanity-correct.
        if dist_km > 2000:
            dist_km /= 1000.0

        simp = simplify(pts)
        routes.append({
            "id": act_id,
            "name": (row.get("Activity Name") or meta.get("name") or Path(plain_name).stem).strip(),
            "type": a_type,
            "date": date or "",
            "dist_km": round(dist_km, 2),
            "elev_m": round(to_float(row.get("Elevation Gain")) or 0),
            "time_s": round(to_float(row.get("Moving Time")) or to_float(row.get("Elapsed Time")) or 0),
            "coords": [[round(p[0], 5), round(p[1], 5)] for p in simp],
        })
        if verbose and (i % 50 == 0 or i == len(tracks)):
            print(f"  parsed {i}/{len(tracks)}")

    routes.sort(key=lambda r: r["date"])
    if verbose:
        n_pts = sum(len(r["coords"]) for r in routes)
        print(f"Kept {len(routes)} routes ({n_pts:,} points after simplification), skipped {skipped}")

    render(routes, out, verbose=verbose)
    return {"routes": len(routes), "skipped": skipped}


def render(routes: list[dict], out: Path, verbose=True) -> None:
    """Inject a list of route dicts into template.html and write ``out``."""
    template = (Path(__file__).parent / "template.html").read_text()
    html = template.replace("/*__ROUTES__*/[]", json.dumps(routes, separators=(",", ":")))
    out.write_text(html)
    if verbose:
        print(f"Wrote {out}  ({out.stat().st_size/1e6:.1f} MB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="Strava export .zip or extracted folder")
    ap.add_argument("-o", "--out", type=Path, default=Path("map.html"), help="output HTML (default map.html)")
    args = ap.parse_args()
    build(args.export, args.out)


if __name__ == "__main__":
    main()
