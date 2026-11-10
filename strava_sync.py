#!/usr/bin/env python3
"""Pull every activity from your Strava account and build map.html.

First run:  opens Strava in your browser so you can click "Authorize" once.
Later runs: reuse the saved token, fetch only new activities, rebuild the map.

    python strava_sync.py            # incremental sync, rebuild map.html, open it
    python strava_sync.py --no-open  # same but don't open the browser
    python strava_sync.py --full     # re-download everything
    python strava_sync.py --streams  # also fetch full-resolution GPS for new activities
                                     # (1 API call per activity; slow for big histories)

Setup: create an API app at https://www.strava.com/settings/api and put its
Client ID / Client Secret in a .env file next to this script (see .env.example).
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from build_map import clean, normalize_type, render, simplify

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"
TOKEN_FILE = HERE / "tokens.json"
CACHE_FILE = HERE / "activities_cache.json"
PORT = 8723
REDIRECT = f"http://localhost:{PORT}/exchange_token"
SCOPE = "read,activity:read_all"
API = "https://www.strava.com/api/v3"


# --------------------------------------------------------------------------- #
# Config + tokens
# --------------------------------------------------------------------------- #

def load_env() -> tuple[str, str]:
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    cid = os.environ.get("STRAVA_CLIENT_ID") or vals.get("STRAVA_CLIENT_ID", "")
    sec = os.environ.get("STRAVA_CLIENT_SECRET") or vals.get("STRAVA_CLIENT_SECRET", "")
    if not cid or not sec or "your_" in sec:
        sys.exit(
            "Missing Strava API credentials.\n"
            f"  1. Create an app at https://www.strava.com/settings/api\n"
            f"     (Authorization Callback Domain: localhost)\n"
            f"  2. Copy .env.example to .env and fill in STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET\n"
            f"     ({ENV_FILE})"
        )
    return cid, sec


def save_private(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def post_token(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def authorize(cid: str, sec: str) -> dict:
    """Browser-based OAuth: open Strava, catch the redirect on localhost."""
    params = {
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT,
        "approval_prompt": "auto", "scope": SCOPE,
    }
    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode(params)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q:
                result["code"] = q["code"][0]
                result["scope"] = q.get("scope", [""])[0]
                msg = "<h2 style='font-family:sans-serif'>Connected to Strava. You can close this tab.</h2>"
            else:
                result["error"] = q.get("error", ["unknown"])[0]
                msg = f"<h2 style='font-family:sans-serif'>Authorization failed: {result['error']}</h2>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):  # silence
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print("Opening Strava in your browser to authorize this app...")
    print(f"If it doesn't open, visit:\n  {url}\n")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        sys.exit(f"Strava authorization failed: {result['error']}")
    if "activity:read_all" not in result.get("scope", ""):
        print("Warning: 'View data about your private activities' was not granted; private activities will be skipped.")

    tok = post_token({"client_id": cid, "client_secret": sec, "code": result["code"], "grant_type": "authorization_code"})
    athlete = tok.pop("athlete", {})
    tok["athlete_name"] = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    save_private(TOKEN_FILE, tok)
    print(f"Authorized as {tok['athlete_name'] or 'athlete'}.")
    return tok


def get_token(cid: str, sec: str) -> str:
    tok = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None
    if not tok:
        tok = authorize(cid, sec)
    if tok.get("expires_at", 0) < time.time() + 60:
        new = post_token({"client_id": cid, "client_secret": sec, "grant_type": "refresh_token",
                          "refresh_token": tok["refresh_token"]})
        tok.update(new)
        save_private(TOKEN_FILE, tok)
    return tok["access_token"]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def api_get(token: str, path: str, **params):
    url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 15-minute window exhausted; Strava resets at :00 :15 :30 :45
                wait = 900 - (int(time.time()) % 900) + 5
                print(f"  rate limited, sleeping {wait}s until the next 15-min window...")
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"gave up on {path}")


def fetch_activities(token: str, after: int | None) -> list[dict]:
    out, page = [], 1
    while True:
        params = {"per_page": 200, "page": page}
        if after:
            params["after"] = after
        batch = api_get(token, "/athlete/activities", **params)
        if not batch:
            break
        out.extend(batch)
        print(f"  fetched {len(out)} activities...")
        if len(batch) < 200:
            break
        page += 1
    return out


def fetch_stream(token: str, act_id: int) -> list | None:
    data = api_get(token, f"/activities/{act_id}/streams", keys="latlng", key_by_type="true")
    return data.get("latlng", {}).get("data") if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# Polyline decoding (Google encoded polyline, precision 5)
# --------------------------------------------------------------------------- #

def decode_polyline(s: str) -> list[tuple[float, float]]:
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        pts.append((lat / 1e5, lon / 1e5))
    return pts


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def to_route(a: dict, streams: dict) -> dict | None:
    raw = streams.get(str(a["id"]))
    if raw:
        pts = [(p[0], p[1]) for p in raw]
    else:
        poly = (a.get("map") or {}).get("summary_polyline") or ""
        if not poly:
            return None
        pts = decode_polyline(poly)
    pts = clean(pts, max_jump_km=5 if raw else None)  # summary polylines are already sparse
    if len(pts) < 2:
        return None
    return {
        "id": str(a["id"]),
        "name": a.get("name") or "Activity",
        "type": normalize_type(a.get("sport_type") or a.get("type") or ""),
        "date": (a.get("start_date_local") or "")[:10],
        "dist_km": round((a.get("distance") or 0) / 1000.0, 2),
        "elev_m": round(a.get("total_elevation_gain") or 0),
        "time_s": round(a.get("moving_time") or 0),
        "coords": [[round(p[0], 5), round(p[1], 5)] for p in simplify(pts)],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="ignore the cache and re-download all activities")
    ap.add_argument("--streams", action="store_true", help="fetch full-resolution GPS per activity (slow)")
    ap.add_argument("--no-fetch", action="store_true", help="just rebuild map.html from the cache")
    ap.add_argument("-o", "--out", type=Path, default=HERE / "map.html")
    ap.add_argument("--no-open", action="store_true", help="don't open the map in the browser afterwards")
    args = ap.parse_args()

    cache = {"activities": {}, "streams": {}}
    if CACHE_FILE.exists() and not args.full:
        cache = json.loads(CACHE_FILE.read_text())
        cache.setdefault("streams", {})

    if not args.no_fetch:
        cid, sec = load_env()
        token = get_token(cid, sec)

        after = None
        if cache["activities"]:
            latest = max(a["start_date"] for a in cache["activities"].values())
            after = int(datetime.fromisoformat(latest.replace("Z", "+00:00")).timestamp()) - 86400
        print("Fetching activities from Strava" + (f" since {latest[:10]}" if after else "") + "...")
        for a in fetch_activities(token, after):
            cache["activities"][str(a["id"])] = a
        save_private(CACHE_FILE, cache)

        if args.streams:
            todo = [aid for aid, a in cache["activities"].items()
                    if aid not in cache["streams"] and (a.get("map") or {}).get("summary_polyline")]
            print(f"Fetching full GPS streams for {len(todo)} activities (Strava allows ~100 calls per 15 min)...")
            for n, aid in enumerate(todo, 1):
                try:
                    cache["streams"][aid] = fetch_stream(token, int(aid)) or []
                except urllib.error.HTTPError as e:
                    print(f"  ! {aid}: HTTP {e.code}")
                    continue
                if n % 25 == 0 or n == len(todo):
                    save_private(CACHE_FILE, cache)
                    print(f"  {n}/{len(todo)}")

    routes = [r for r in (to_route(a, cache["streams"]) for a in cache["activities"].values()) if r]
    routes.sort(key=lambda r: r["date"])
    no_gps = len(cache["activities"]) - len(routes)
    print(f"{len(routes)} activities with GPS ({no_gps} without, e.g. indoor, skipped)")
    render(routes, args.out)
    if not args.no_open:
        print("Opening the map in your browser...")
        webbrowser.open(args.out.resolve().as_uri())


if __name__ == "__main__":
    main()
