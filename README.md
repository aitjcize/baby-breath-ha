# Baby Respiration Monitor for Home Assistant

An experimental, local-only detector that estimates respiration-like chest/abdomen motion from an RTSP camera and publishes results to Home Assistant — packaged as a Home Assistant add-on with a guided onboarding flow.

> **This is an experimental secondary monitor, not a medical device or a life-safety system. Never use it as the primary or only way to monitor an infant. A valid video signal does not prove an infant is safe, and missing video motion does not prove apnea.**

## Install as a Home Assistant add-on (recommended)

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add this repository's URL.
2. Install **Baby Respiration Monitor** and start it. For entities, also install the **Mosquitto broker** add-on — the monitor picks it up automatically.
3. Open the **Baby Respiration** panel in the sidebar. The onboarding wizard takes it from there:
   - **Safety acknowledgement** — the honest talk about what this can and cannot do.
   - **Camera** — paste the RTSP URL, press *Test connection*, and confirm the preview shows the crib. No camera handy? `demo://breathing` starts a synthetic scene.
   - **Breathing region** — press *Auto-detect breathing region*: a ~30 s optical-flow scan finds where periodic breathing-band motion lives (works in IR night vision; no ML model involved) and suggests the measurement box, which you can accept or drag to adjust.

Everything entered in the wizard persists in the add-on's data and applies without a restart. The dashboard offers **Re-detect region** and **Camera…** to change things later.

The add-on details live in [`baby_respiration/`](baby_respiration/): [DOCS.md](baby_respiration/DOCS.md) covers entities, options, and limitations; [CHANGELOG.md](baby_respiration/CHANGELOG.md) tracks releases.

## How it works

The service keeps only the latest RTSP frame, downsizes it, computes dense Farnebäck optical flow, subtracts median whole-frame motion, and takes the median residual vertical flow inside the selected region. Irregular observations are resampled onto a uniform timebase, band-pass filtered to 15–90 breaths/min, and a Welch spectrum estimates the dominant frequency. Confidence blends spectral peak concentration, SNR, amplitude, completeness, and motion stability — a signal-quality score, **not a medical probability**.

The conservative state machine distinguishes:

| State | Meaning |
| --- | --- |
| `BREATHING` | Signal quality is adequate and periodic in-range motion is present. |
| `NO_BREATHING_SIGNAL` | A previously calibrated, still-observable region lost periodic motion for the configured timeout. Means only that *this detector sees no signal*. |
| `MEASUREMENT_INVALID` | Stream, image, stability, completeness, amplitude, or SNR is insufficient. |

While invalid, MQTT marks the rate/breathing entities *unavailable* rather than reporting zero BPM or `OFF`. Automations must gate `baby_breathing_detected` on `baby_respiration_measurement_valid`, and must never be life-safety automations.

The breathing-region auto-detect reuses the same optical flow: every image block is scored for periodicity inside the breathing band over a ~30 s window, and the coherent cluster with the strongest periodic motion becomes the suggested region. Because it looks for the signal itself rather than a baby-shaped object, it works under IR night vision and blankets where object detectors struggle.

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
