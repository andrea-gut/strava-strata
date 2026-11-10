# strava-strata

Pulls every map activity from my Strava account and builds a single HTML map of all my routes, with a heatmap for the places I go most. (I guess if you're reading this, in theory, it could create your heatmap too.)

## Setup

1. Make an API app at https://www.strava.com/settings/api with `localhost` as the Authorization Callback Domain.
2. Copy `.env.example` to `.env` and paste in the Client ID and Client Secret.
3. `uv venv .venv && uv pip install --python .venv/bin/python fitdecode`

## Run

```
.venv/bin/python strava_sync.py
```

First run opens Strava to authorize. After that it only grabs new activities, rebuilds `map.html`, and opens it. Or just double-click `Sync Trail Map.command`.

Flags: `--full` re-downloads everything, `--streams` fetches full-res GPS (slow), `--no-open` skips the browser.
