# Proposal: Google Weather minute forecast + weather map tiles in Home Assistant

Status: proposal (L1 — nothing here changes harness behavior; this is a review
and recommendation for the Home Assistant side of the homelab).

Execution status (2026-08-10): Track A is in flight — library PR submitted as
[tronikos/python-google-weather-api#11](https://github.com/tronikos/python-google-weather-api/pull/11);
the matching core change is pushed to the
[`google-weather-minute-forecast` branch of moellere/ha-core](https://github.com/moellere/ha-core/tree/google-weather-minute-forecast),
to be opened as a draft `home-assistant/core` PR once the library releases.
Track B lives in [moellere/hass-google-weather-maps](https://github.com/moellere/hass-google-weather-maps).

Google enabled two new Weather API endpoints on Google Maps Platform:

- [Minute forecast (Experimental)](https://developers.google.com/maps/documentation/weather/minute-forecast)
  — minute-by-minute precipitation nowcast for a point, 6-hour window.
- [Weather maps (Experimental)](https://developers.google.com/maps/documentation/weather/weather-map)
  — raster precipitation-nowcast map tiles in Web Mercator, overlayable on a map.

The question posed: build a custom integration, or extend the
[Google Maps integration](https://www.home-assistant.io/integrations/google_maps/)
in Home Assistant?

## TL;DR recommendation

**Neither endpoint belongs in `google_maps` — extend the official
[`google_weather`](https://www.home-assistant.io/integrations/google_weather/)
core integration instead.** It shipped in HA 2025.12, reached platinum quality
in 2026.5, already authenticates with the same Maps Platform API key against
`weather.googleapis.com/v1`, and has an active code owner (@tronikos) who also
owns the underlying client library. The work splits into:

1. Upstream two methods into `python-google-weather-api` (minute forecast +
   map tile fetch).
2. Add a minute-forecast coordinator, a few nowcast sensors, and a
   `get_minute_forecast` entity service to `google_weather` (mirroring
   `openweathermap.get_minute_forecast`).
3. Add a radar-style `camera` entity that stitches map tiles around each
   configured location (mirroring the `environment_canada` radar camera).

The two features should **travel different routes** (detail in
"Recommended track" below): the minute forecast goes upstream to core — it
has a merged precedent (`openweathermap.get_minute_forecast`), a small diff,
and one receptive maintainer controlling both repos — while the map-tile
camera starts life as a HACS custom integration, because the unresolved ToS
question, unpublished tile SKU pricing, and missing frame history would stall
a core PR indefinitely. An interim REST-sensor snippet for our own HA
instance is at the end.

## Why not `google_maps`

The HA `google_maps` integration is a `device_tracker` platform that scrapes
Google Maps *location sharing* using browser cookies (`locationsharinglib`).
It is unrelated to Google Maps Platform APIs:

- No API key, no `weather.googleapis.com` — it authenticates with a cookies
  file that users export from a browser session, a mechanism with chronic
  breakage (cookies expiring within 24h as recently as
  [Jan 2026](https://community.home-assistant.io/t/google-maps-device-tracker-cookies-expiring-within-24-hours/973312)).
- Wrong domain model: it produces person/device trackers, not weather data.
  HA's architecture review would reject weather platforms bolted onto it.
- `google_weather` already models exactly what these endpoints extend: an API
  object built from a Maps Platform key, with per-location config subentries
  (`CONF_LATITUDE`/`CONF_LONGITUDE`) and one coordinator per data type.

## What the new endpoints provide

### Minute forecast

```
GET https://weather.googleapis.com/v1/forecast/minutes:lookup
    ?key=API_KEY
    &location.latitude=LAT&location.longitude=LON
    &unitsSystem=METRIC&pageSize=N
```

Returns an `overallPredictionTimeframe` (~6 h), the location `timeZone`, and
per-minute `segments`, each carrying:

- `timeFrame` (start/end), precipitation `type` (e.g. `RAIN`),
- `probability` (percent), `qpf` (quantity + unit), `snowfallAmount`,
- `intensity` (e.g. `MODERATE`).

Coverage is populated areas in supported countries (no China/Cuba/Iran/North
Korea/Syria; Japan/Korea alerts-only; remote areas excluded).

### Weather map tiles

```
GET https://weather.googleapis.com/v1/mapTypes/{mapType}/mapTiles/{zoom}/{x}/{y}?key=API_KEY
```

Standard Web Mercator tiles, zoom 0–16. Currently documented map types are
precipitation nowcasts: `US_PRECIPITATION_CURRENT` and
`EU_PRECIPITATION_CURRENT`. Google's docs demonstrate overlay via
`google.maps.ImageMapType` in the Maps JS API; there is no timestamped frame
history — tiles are "current" only, so no animation loop like a classic radar
archive.

### Pricing / quota (shared budget with existing polling)

Weather API pricing is $0.15 / 1,000 calls with a 10,000-call/month free tier.
The core integration already spends, per configured location:

| Poll | Interval | Calls/month (~30.4 d) |
|---|---|---|
| Current conditions | 15 min | ~2,920 |
| Hourly forecast | 1 h | ~730 |
| Daily forecast | 1 h | ~730 |
| **Existing total** | | **~4,380** |

Additions must be quota-aware:

- Minute forecast at 5 min would be ~8,760/mo — untenable. At **15 min**
  it's ~2,920/mo, still doubling a location's spend. → default-disabled
  entities, 15-min coordinator that only runs while at least one nowcast
  entity is enabled, plus an on-demand service for automations.
- A 2×2 tile stitch refreshed every 15 min is ~11,700 tile calls/mo (tile SKU
  pricing not yet published for the experimental endpoint). → camera
  default-disabled, 15–30 min refresh, single-tile option.

## Design: extending `google_weather`

### Step 1 — library (`python-google-weather-api`)

HA platinum-quality rules require all API access to live in the library. Add:

- `async_get_minute_forecast(latitude, longitude, page_size=...) ->
  MinuteForecastResponse` — mashumaro models for
  `overallPredictionTimeframe`, `segments[]` (`type`, `probability`, `qpf`,
  `snowfallAmount`, `intensity`), following the existing
  `HourlyForecastResponse` shape in `model.py`.
- `async_get_map_tile(map_type, zoom, x, y) -> bytes` plus a
  `WeatherMapType` enum (`US_PRECIPITATION_CURRENT`, `EU_PRECIPITATION_CURRENT`),
  reusing the client session and auth/error mapping in `api.py`.

### Step 2 — minute forecast in the integration

- **Coordinator:** `GoogleWeatherMinuteForecastCoordinator` subclassing the
  existing `GoogleWeatherBaseCoordinator` generic (15-min interval), one per
  subentry, wired into `GoogleWeatherSubEntryRuntimeData`. Skip
  `async_config_entry_first_refresh` unless a consuming entity is enabled
  (same trick `environment_canada` uses for its radar coordinator).
- **Sensors** (per location, `entity_registry_enabled_default=False`):
  - `precipitation_start` / `precipitation_end` — `TIMESTAMP` device class;
    first/last segment in the window with `probability` over a threshold.
  - `minutes_to_precipitation` — duration until the first wet segment
    (`None` → dry window; great automation trigger for "close the awning").
  - `nowcast_precipitation_probability` — max probability in the next 60 min.
  - `nowcast_intensity` — `ENUM` device class over the API's intensity levels.
- **Binary sensor** (optional): `precipitation_expected` with a configurable
  lead-time option (default 15 min).
- **Entity service:** `google_weather.get_minute_forecast` registered on the
  weather entity, returning the full segment list as response data. This is a
  direct copy of the `openweathermap.get_minute_forecast` pattern
  (`SupportsResponse.ONLY`), so template/automation users get the raw
  6-hour nowcast without any polling entity enabled.

The HA `weather` entity itself cannot model this natively —
`WeatherEntityFeature` only defines `FORECAST_DAILY`, `FORECAST_HOURLY`, and
`FORECAST_TWICE_DAILY` — which is exactly why OWM chose the service + sensor
route. Proposing a `FORECAST_MINUTELY` architecture change upstream is a
larger, separate conversation; the service route works today.

### Step 3 — weather map camera

Follow the `environment_canada` radar precedent: a `Camera` entity per
location, default-disabled.

- Compute the Web Mercator tile containing the subentry's lat/lon at a
  configurable zoom (default ~8), fetch a 2×2 (option: 1×1 or 3×3)
  neighborhood, stitch with Pillow, composite over a static basemap or plain
  background, serve as PNG. Content is "current" only, so a static frame —
  no GIF loop until Google exposes timestamped frames.
- Pick `US_…` vs `EU_…` map type automatically from the location; error
  cleanly outside coverage.
- The camera proxies tiles server-side, so the API key never reaches the
  frontend (tile URLs embed the key — they must not be handed to the map
  card as a raw tile source).

### Config & UX

- Per-subentry options: enable nowcast (bool), nowcast poll interval,
  precipitation lead-time, enable map camera (bool), zoom, grid size.
- `strings.json`/`icons.json` additions; diagnostics already redact the key.
- Tests: extend the existing fixture set with recorded
  `minutes:lookup` JSON + 256×256 PNG tile fixtures, snapshot tests for the
  new entities, service-response tests mirroring OWM's.

## Risks and open questions

1. **Experimental status.** Both endpoints are pre-GA; Google may change
   shapes or SKUs. HA core reviewers may prefer to wait for GA — hence the
   two-track plan below. The integration's platinum `quality_scale` also
   raises the review bar for additions.
2. **Terms of service.** Maps Platform terms historically require Google map
   content to be displayed on Google maps; the weather-tiles docs only show
   overlay on Google's JS map. Rendering tiles into a camera image (or over
   HA's OSM-based map) needs a ToS read before a core PR — flag it in the
   upstream issue from day one.
3. **Quota.** Additions roughly double per-location spend when enabled;
   default-off entities and the on-demand service keep the free tier viable
   for a two-location household.
4. **Coverage gaps.** Minute forecast is populated-areas-only; the setup flow
   should surface a clean "not covered" error rather than a broken entity.

## Recommended track: split by feature

The two features have very different core-acceptance odds, so they take
different routes.

### Track A — minute forecast: upstream to core

| Phase | What | Where |
|---|---|---|
| A1 | Open feature issue referencing this design to gauge maintainer interest | `tronikos/python-google-weather-api` |
| A2 | Library PR: `async_get_minute_forecast` + `MinuteForecastResponse` models | `python-google-weather-api` |
| A3 | Core PR: coordinator + default-disabled nowcast sensors + `get_minute_forecast` service (one platform per PR — HA hard rule, so sensors and the service may land as separate PRs) | `home-assistant/core` |

Why core is the right bet here: a merged precedent exists
(`openweathermap.get_minute_forecast`), the diff is small (one coordinator
subclass, a handful of sensor descriptions, one service registration), the
response shape is simple enough to survive pre-GA churn inside the library,
and @tronikos maintains both the library and the integration — one receptive
person controls the whole path. Core also brings reauth, subentries,
diagnostics redaction, and translations for free. Expect a few weeks to a
couple of months of review for a non-codeowner PR.

### Track B — map-tile camera: HACS first

| Phase | What | Where |
|---|---|---|
| B1 | Custom integration with its own domain (e.g. `google_weather_maps`), own config entry + API key | new HACS repo |
| B2 | Iterate on stitching grid, zoom, refresh cadence, US/EU selection while the endpoint is experimental | same |
| B3 | Propose for core only if/when the endpoint goes GA, tile SKU pricing is published, frame history exists (real radar loops), and the ToS question has a clean answer | `home-assistant/core` |

Why not core first: the ToS question (Google tile imagery rendered outside a
Google map) reads as a blocker to core reviewers, not a footnote; the design
choices (grid, basemap compositing, cadence against an unpublished SKU) need
iteration speed core review doesn't allow; and the endpoint is the more
volatile of the two — when Google adds timestamped frames, the design changes
materially. Better to churn in HACS than in core's monthly release train.

**Domain trap:** do not name the custom component's domain `google_weather` —
a same-domain custom component *shadows* the core integration entirely,
turning it into a fork that must track every monthly core release. A separate
domain costs a second API-key entry and shared-quota awareness (keep the
camera default-off or at a 30-min refresh), which is the cheaper price.

The existing HACS integration
[safepay/ha_google_weather](https://github.com/safepay/ha_google_weather) is
an alternative contribution target for Track A if upstream stalls, but
core-first is the better bet given the official integration's momentum.

## Interim: use it in our HA today (no integration work)

A REST sensor gets the nowcast into automations immediately:

```yaml
rest:
  - resource: >-
      https://weather.googleapis.com/v1/forecast/minutes:lookup?key=!secret google_weather_key&location.latitude=XX.XXXX&location.longitude=-YY.YYYY&unitsSystem=METRIC
    scan_interval: 900
    sensor:
      - name: "Nowcast first precipitation"
        value_template: >-
          {% set wet = value_json.segments
             | selectattr('probability', 'ge', 50) | list %}
          {{ (wet | first).timeFrame.startTime if wet else 'none' }}
      - name: "Nowcast max probability 60m"
        unit_of_measurement: "%"
        value_template: >-
          {{ value_json.segments[:60] | map(attribute='probability')
             | max | default(0) }}
```

(`!secret` inside a URL requires assembling `resource` via `resource_template`
on older HA versions; 900 s keeps two such sensors within the free tier
alongside the official integration.)

## Sources

- [Get minute forecast (Experimental) — Google Weather API](https://developers.google.com/maps/documentation/weather/minute-forecast)
- [Get weather maps (Experimental) — Google Weather API](https://developers.google.com/maps/documentation/weather/weather-map)
- [Weather API overview](https://developers.google.com/maps/documentation/weather/overview) ·
  [usage & billing](https://developers.google.com/maps/documentation/weather/usage-and-billing) ·
  [coverage](https://developers.google.com/maps/documentation/weather/coverage) ·
  [v1 RPC reference](https://developers.google.com/maps/documentation/weather/reference/rpc/google.maps.weather.v1)
- [Google Weather — Home Assistant](https://www.home-assistant.io/integrations/google_weather/) ·
  [Google Maps — Home Assistant](https://www.home-assistant.io/integrations/google_maps/) ·
  [Weather entity — Home Assistant](https://www.home-assistant.io/integrations/weather/)
- [tronikos/python-google-weather-api](https://github.com/tronikos/python-google-weather-api)
- [safepay/ha_google_weather](https://github.com/safepay/ha_google_weather)
- HA core precedents: `homeassistant/components/openweathermap`
  (`get_minute_forecast` service), `homeassistant/components/environment_canada`
  (radar `camera`), `homeassistant/components/google_weather` (platinum
  baseline this proposal extends)
