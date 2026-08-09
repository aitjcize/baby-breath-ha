# Baby Respiration Detector for Home Assistant

An experimental, local-only video signal detector that estimates respiration-like chest/abdomen motion from an RTSP camera and publishes results through Home Assistant MQTT discovery.

> **This is an experimental secondary monitor, not a medical device or a life-safety system. Never use it as the primary or only way to monitor an infant. A valid video signal does not prove an infant is safe, and missing video motion does not prove apnea.**

## How it works

The service keeps only the latest RTSP frame, downsizes it, crops a normalized ROI, computes dense Farnebäck optical flow, subtracts median whole-frame motion, and takes the median residual vertical ROI flow. It resamples irregular observations onto a uniform timebase, applies a 15–90 breaths/min Butterworth band-pass filter, and uses a Welch spectrum to estimate the dominant frequency.

Confidence combines spectral peak concentration, SNR, filtered amplitude, data completeness, and motion stability. It is a signal-quality score from 0–100, **not a medical probability**.

The state machine deliberately distinguishes:

| State | Meaning |
| --- | --- |
| `BREATHING` | Video/signal quality is adequate and periodic in-range motion is present. |
| `NO_BREATHING_SIGNAL` | A previously calibrated, still-observable ROI has lost periodic motion for the configured timeout. This means only that this detector sees no signal. |
| `MEASUREMENT_INVALID` | The stream, image, motion stability, completeness, amplitude, or SNR is insufficient. |

Low SNR, a flat/frozen stream, excess motion, and startup without a learned good baseline fail toward `MEASUREMENT_INVALID`. While invalid, MQTT marks the rate and breathing entities unavailable, rather than reporting zero BPM or `OFF`.

## Configure

Edit [`config.yaml`](config.yaml). At minimum, provide:

```sh
export BABY_CAMERA_RTSP_URL='rtsp://USER:PASSWORD@CAMERA_IP:554/STREAM'
```

Use the debug page to adjust `camera.roi: [x, y, width, height]`. Values are normalized from 0 to 1, with the origin at the image's top-left. Keep the ROI tight around the chest/abdomen, avoid crib edges and moving blankets, and prefer a stable fixed camera.

To enable Home Assistant, edit the MQTT section:

```yaml
mqtt:
  enabled: true
  host: "192.168.1.10"  # use host.docker.internal for a broker on this Mac in Docker
  port: 1883
  username: "${BABY_MQTT_USERNAME}"
  password: "${BABY_MQTT_PASSWORD}"
  base_topic: "baby_respiration"
  discovery_prefix: "homeassistant"
```

Then export `BABY_MQTT_USERNAME` and `BABY_MQTT_PASSWORD`. Anonymous brokers may leave both empty. Keep credentials out of version control.

## Start and stop

Native mode has the least Docker/network friction on this Mac:

```sh
cd /Users/aitjcize/Work/baby-breath-ha
uv sync
uv run python -m app --config config.yaml
```

Stop with `Ctrl-C`.

Docker Compose:

```sh
cd /Users/aitjcize/Work/baby-breath-ha
docker compose up -d --build
docker compose logs -f baby-respiration
docker compose down
```

Compose publishes the debug port only on `127.0.0.1`; it is not exposed to the LAN. The container runs read-only as a non-root user with all Linux capabilities dropped.

## Debug UI

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). It shows:

- latest processed camera frame with ROI and quality overlay;
- filtered respiration waveform;
- BPM, confidence, SNR, FPS, stream status, validity, and classifier reason.

The native default binds only to loopback because the page contains sensitive camera imagery. If remote access is ever needed, use an authenticated reverse proxy rather than exposing this HTTP server directly.

## Home Assistant entities

After MQTT connects, discovery creates a single device with at least:

- `sensor.baby_respiration_rate`
- `sensor.baby_respiration_confidence`
- `binary_sensor.baby_breathing_detected`
- `binary_sensor.baby_respiration_measurement_valid`

Diagnostic entities also report detector state, signal RMS, SNR, estimated FPS, window length, stream status, classifier reason, and excessive motion. Home Assistant may append a suffix if an entity with the same ID already exists.

Automations must gate any interpretation of `baby_breathing_detected` on `baby_respiration_measurement_valid`. Do not create life-safety automations from this project.

## Tests

```sh
uv run pytest -q
```

Tests cover recovery of a noisy irregular 42 BPM synthetic waveform, invalid-window handling, RTSP reconnect, MQTT discovery JSON, and the conservative state machine.

For a short local smoke run without a configured camera or broker:

```sh
uv run python -m app --config config.yaml --run-seconds 10
```

It should remain stable, serve the debug page, log `MEASUREMENT_INVALID`, and exit cleanly.

## Tuning and limitations

- A 24-second window gives frequency resolution and noise rejection at the cost of startup/response latency.
- Bedding, caregiver motion, camera vibration, shadows, IR illumination changes, autofocus, video compression, and another person's motion can dominate chest motion.
- The algorithm measures image motion, not airflow, oxygenation, heart rate, or clinical breathing.
- The current global-motion subtraction assumes most of the image is stationary. A tight view filled by the infant can weaken that assumption.
- Median vertical flow is inexpensive and robust, but blankets or a camera angle with mostly horizontal apparent motion may reduce sensitivity.
- Thresholds are camera/scene-specific. Validate across lighting modes, sleep positions, clothing, and expected network failures.
- `NO_BREATHING_SIGNAL` is intentionally difficult to reach: the ROI first needs sustained high-quality breathing calibration, and current signal observability must remain adequate.

A phase/local-phase extractor is not included in the baseline. It should only be added after comparing the displayed waveform and SNR against a real camera stream; without real footage, choosing and tuning a more complex phase method would add CPU and false confidence without evidence.

## Verification on this Mac mini

The automated suite passes (7 tests). Native and Docker smoke runs both served a healthy debug endpoint, stayed conservatively invalid with the camera unset, and shut down cleanly. The no-camera container used about 102 MiB and 0.07% CPU; the native process used about 155 MiB RSS and sampled at 0.0% idle CPU. A separate 320×240 dense-flow benchmark processed 211 frames/s, corresponding to roughly 2.4% of one CPU core at 5 FPS. RTSP decode, real scene complexity, and MQTT overhead are not included in that estimate.

No camera URL or MQTT broker credentials were available during setup, so live-camera signal reliability and end-to-end broker delivery remain unverified. Use the debug waveform and diagnostics on the actual stream before deciding whether optical flow is sensitive enough to justify continued tuning or a phase-based extractor.
