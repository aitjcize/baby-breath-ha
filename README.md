# Baby Respiration Monitor for Home Assistant

An experimental, local-only detector that estimates respiration-like chest/abdomen motion from an RTSP camera and publishes results to Home Assistant — packaged as a Home Assistant add-on with a guided onboarding flow.

> **This is an experimental secondary monitor, not a medical device or a life-safety system. Never use it as the primary or only way to monitor an infant. A valid video signal does not prove an infant is safe, and missing video motion does not prove apnea.**

## Install as a Home Assistant add-on (recommended)

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add this repository's URL.
2. Install **Baby Respiration Monitor** and start it. For entities, also install the **Mosquitto broker** add-on — the monitor picks it up automatically.
3. Open the **Baby Respiration** panel in the sidebar. The onboarding wizard takes it from there:
   - **Safety acknowledgement** — the honest talk about what this can and cannot do.
   - **Camera** — paste the RTSP URL, press *Test connection*, and confirm the preview shows the crib. No camera handy? `demo://breathing` starts a synthetic scene.
   - **Breathing region** — drag a generous box over where your baby sleeps; the monitor scores small blocks inside it for breathing-band rhythm and measures from whichever block currently carries it, following the baby as they move.

Everything entered in the wizard persists in the add-on's data and applies without a restart. The dashboard offers **Edit region**, **Camera**, **MQTT**, **Pause**, and **Tuning** to change things later — all configuration lives in the panel; there is no add-on Configuration tab.

The add-on details live in [`baby_respiration/`](baby_respiration/): [DOCS.md](baby_respiration/DOCS.md) covers entities, options, and limitations; [CHANGELOG.md](baby_respiration/CHANGELOG.md) tracks releases.

## How it works

The service keeps only the latest RTSP frame, downsizes it, computes dense Farnebäck optical flow, subtracts median whole-frame motion, and takes the median residual vertical flow inside the selected region. Irregular observations are resampled onto a uniform timebase, band-pass filtered to 15–90 breaths/min, and a Welch spectrum estimates the dominant frequency. Confidence blends spectral peak concentration, SNR, amplitude, completeness, and motion stability — a signal-quality score, **not a medical probability**.

The conservative state machine distinguishes:

| State | Meaning |
| --- | --- |
| `BREATHING` | Signal quality is adequate and periodic in-range motion is present. |
| `NO_BREATHING_SIGNAL` | A previously calibrated, still-observable region lost periodic motion for the configured timeout. Means only that *this detector sees no signal*. |
| `MEASUREMENT_INVALID` | Stream, image, stability, completeness, amplitude, or SNR is insufficient. |

While invalid, MQTT marks the rate entities *unavailable* rather than reporting zero BPM. The `baby_respiration_state` and `baby_presence` enum sensors are the single source of truth for automations — and no automation built on them may ever be life-safety.

The same per-block periodicity scoring runs in two more places: inside the user's box every analysis window (to pick the block currently carrying breathing), and across the full frame after pickup-shaped disturbances (to verify crib occupancy for presence detection). Because it looks for the signal itself rather than a baby-shaped object, it works under IR night vision and blankets where object detectors struggle.

## Example automations

Entity IDs below use the device-name prefix Home Assistant generates (`baby_respiration_detector_…`); check yours under **Settings → Devices → Baby Respiration Detector**.

**Bed-exit warning** — for a baby sleeping on an open bed: `binary_sensor.…_baby_left_region` turns on within seconds when a motion trail *originating inside your monitored box* crosses out and stays out (a caregiver's reach or a co-sleeping adult's movement originates outside the box and can never fire it). It clears automatically when breathing is found inside the box again.

```yaml
alias: Baby left the bed area
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.baby_respiration_detector_baby_left_region
    to: "on"
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: "🚼 Baby left the monitored area"
      message: "Motion from inside the sleep area crossed the boundary — check the bed now."
      data:
        push:
          sound:
            name: default
            critical: 1
            volume: 1.0
```

**Breathing alert** — the full recipe (no-breathing state, sustained unmeasurable, low rate, all gated on presence) lives in [DOCS.md](baby_respiration/DOCS.md#entities).

As with everything in this project: these are prompts to go look, never a life-safety mechanism.

## Development

```sh
uv sync
uv run pytest -q                                  # test suite
uv run python -m app --config config.yaml          # native run, web UI on 127.0.0.1:8080
uv run python -m app --config config.yaml --run-seconds 10   # smoke run
```

Runtime settings (camera URL, region) persist in `./data/` (override with `BABY_DATA_DIR`). Enter `demo://breathing` in the wizard for a synthetic breathing scene — the full pipeline runs against it, including the region scan.

Docker Compose (standalone, outside Home Assistant):

```sh
docker compose up -d --build
docker compose logs -f baby-respiration
```

Compose publishes the web UI only on `127.0.0.1`. In standalone mode, configure MQTT in [`config.yaml`](config.yaml) (`${BABY_MQTT_USERNAME}` / `${BABY_MQTT_PASSWORD}` env expansion supported).

To test on a real Home Assistant box, `./deploy-dev.sh [root@homeassistant.local]` installs a parallel **Baby Respiration Monitor (Dev)** add-on (slug `local_baby_respiration_dev`): it rsyncs the add-on over the SSH add-on, and after the one-time on-device install it builds the image locally and loads it into the HAOS Docker via debug SSH on port 22222 — no slow on-device rebuilds.

Repository layout:

- `baby_respiration/` — the Home Assistant add-on (manifest, Dockerfile, `run.sh`, and the `app/` Python package with the web UI in `app/static/`)
- `tests/` — pytest suite (signal recovery, scanner, state machine, web API, settings, add-on config)
- `scripts/make_icons.py` — regenerates the add-on icon/logo
- Root `Dockerfile` + `docker-compose.yml` — standalone (non-add-on) deployment

## Tuning and limitations

- A ~24 s analysis window trades startup/response latency for frequency resolution and noise rejection.
- Bedding, caregiver motion, camera vibration, IR illumination changes, autofocus, and compression can dominate chest motion.
- The algorithm measures image motion, not airflow, oxygenation, or clinical breathing.
- Global-motion subtraction assumes most of the image is stationary; a tight view filled by the infant weakens that.
- Thresholds are camera/scene-specific — validate on the real stream (lighting modes, sleep positions, clothing) before trusting trends.
- `NO_BREATHING_SIGNAL` is deliberately hard to reach: it requires prior sustained high-quality calibration and a still-observable view.
