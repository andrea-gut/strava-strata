# Strava trail map

Pulls every activity from your Strava account (rides, hikes, runs, walks, ...) and
builds a single HTML map of all your routes, with a frequency heatmap for the places
you go most. Everything runs locally; no Strava export download needed.

## One-time setup (about 3 minutes)

Strava only lets apps read your data through an "API application" you own, so you
create one and hand its keys to the script.

1. Go to <https://www.strava.com/settings/api> (signed in) and fill in the form:
   - **Application Name:** anything, e.g. `Trail Map`
   - **Category:** Visualizer
   - **Website:** `http://localhost`
   - **Authorization Callback Domain:** `localhost`  ← this one matters
   - Upload any small image as the app icon when asked (Strava requires one).
2. The page then shows a **Client ID** and **Client Secret**.
3. In this folder, copy `.env.example` to `.env` and paste those two values in:
   ```bash
   cp .env.example .env && open -e .env
   ```
4. Run the sync. Your browser opens Strava's "Authorize" page; click **Authorize**
   (keep "View data about your private activities" checked so private activities show).
   ```bash
   .venv/bin/python strava_sync.py
   ```

That downloads your whole activity list, writes `map.html`, and opens it in your
browser. Later runs reuse the saved login and only fetch new activities.

## Easiest way to run it

Double-click **`Sync Trail Map.command`** in Finder. It opens a Terminal window,
pulls anything new from Strava, rebuilds `map.html`, and opens the map in your browser.
(First time only: if macOS asks, right-click the file → Open.)

## Options

```bash
.venv/bin/python strava_sync.py            # incremental sync, rebuild map.html, open it
.venv/bin/python strava_sync.py --no-open  # same, without opening the browser
.venv/bin/python strava_sync.py --full     # re-download the whole history
.venv/bin/python strava_sync.py --streams  # full-resolution GPS instead of Strava's summary line
.venv/bin/python strava_sync.py --no-fetch # just rebuild the map from the local cache
```

By default routes come from Strava's *summary polyline* (one API call per 200
activities, fast). `--streams` fetches the raw GPS track per activity, which is
sharper on tight switchbacks but costs one API call each; Strava allows roughly
100 calls per 15 minutes, and the script waits automatically when it hits that.

## What the map does

- **Routes** view: one colored line per activity (orange rides, green hikes, blue runs,
  purple walks, white ski). Hover for name / date / distance, click for a link to Strava.
- **Heatmap** view: brighter where you've been more often. The intensity slider sets
  the blur radius. **Both** overlays faint routes on the heat.
- Filter by activity type and year range; base maps (Esri): Dark, Topo, Streets, Satellite.

## Files

- `strava_sync.py` — Strava login, activity fetch, cache, map build.
- `build_map.py` — track simplification + HTML rendering. Also works standalone on a
  Strava bulk-export zip (`build_map.py export.zip`) if you ever want that route.
- `template.html` — the Leaflet map UI. Edit this to change colors, layers or layout.
- `.env`, `tokens.json`, `activities_cache.json` — your credentials, login token and
  downloaded activities. Private, owner-read-only, never leave this folder.
- `sample_map.html` — demo built from **fake** data, just to show the UI.
- `.venv/` — Python env. Recreate with
  `uv venv .venv && uv pip install --python .venv/bin/python fitdecode`.
