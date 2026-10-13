# Sailing log

Days on the water, hours by role, miles and routes around San Francisco Bay.
Live at https://tulerpetontidae.github.io/sailing/

## How it's put together

- `trips.json` is the hand-written log: one entry per day on the water, with
  the things no watch knows — boat, role, who was aboard, sail trim, notes.
- `tracks/<id>.json` holds each sail's track from Strava, compressed to one
  point a minute.
- `build.py` merges the two, adds conditions and writes `data.json`:
  - wind from the [Open-Meteo](https://open-meteo.com/) historical forecast
    archive, at the sail's midpoint, averaged over the hours sailed;
  - tidal current from [NOAA](https://tidesandcurrents.noaa.gov/) predictions
    at the nearest station, sampled every ten minutes along the track.
  Both are modelled, not measured on the boat. Responses are cached in
  `conditions.json`, so rebuilding doesn't hit the network again.
- `index.html` reads `data.json` and draws everything; there is no build step
  for the page itself.

## Adding a sail

1. Add an entry to `trips.json`. If it has a Strava track, set `"strava": <id>`
   and drop the track in `tracks/`.
2. `python3 build.py`
3. Commit and push.

Roles are skipper, mate, crew and student. Set `"anchored": true` on a day
spent at anchor and the build finds the spot from the track.
