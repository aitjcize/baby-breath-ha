# Baby Respiration Monitor

An experimental, local-only detector that estimates respiration-like chest/abdomen motion from an RTSP camera and publishes results through Home Assistant MQTT discovery.

> **This is an experimental secondary monitor, not a medical device or a life-safety system. Never use it as the primary or only way to monitor an infant. A valid video signal does not prove an infant is safe, and missing video motion does not prove apnea. Do not build life-safety automations on top of this add-on.**

## Installation

1. Add this repository to the add-on store: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, then paste the repository URL.
2. Install **Baby Respiration Monitor** and start it.
3. For Home Assistant entities, also install and start the **Mosquitto broker** add-on (plus the MQTT integration). The monitor finds it automatically — no broker settings needed. Running your broker elsewhere? Point the panel at it in the **Home Assistant** step of onboarding.

## Onboarding

Open the **Baby Respiration** panel in the sidebar. The wizard walks you through:

1. **Safety acknowledgement** — please read it for real.
2. **Camera** — paste your camera's RTSP URL (found in the camera app under RTSP/ONVIF/local streaming) and test the connection; a preview frame confirms you have the right camera. No camera handy? Enter `demo://breathing` to explore with a synthetic scene.
3. **Breathing region** — drag a box over where your baby sleeps. Draw it generously: the monitor finds and follows the breathing rhythm within the box, so it keeps working when your baby moves during the night.

**The box is a search area.** Inside it, the monitor scores small blocks for breathing-band rhythm every window and measures from the block that currently carries it — the dashboard shows that active sub-region as a dashed mint box. This means the box may be drawn generously to allow the baby to move during sleep; static bedding inside it does not dilute the signal.

**Co-sleeping warning.** Precisely because the monitor locks onto the strongest breathing inside the box, it must **never** be able to include another person. A box reaching a co-sleeping adult can report reassuring "breathing" from *them* regardless of the baby's state — worse than no reading. Cover the area the baby can occupy, and nothing an adult can occupy.

4. **Home Assistant** — choose how readings reach Home Assistant: the broker Home Assistant provides (default, zero setup), a **custom MQTT broker** running anywhere on your network (host, port, credentials), or no MQTT at all. The panel shows live connection status while you save.

Settings persist across restarts and updates. Use **Edit region**, **Camera**, or **MQTT** on the dashboard to change them later.

## What the states mean

| State | Meaning |
| --- | --- |
| `BREATHING` | Video quality is adequate and periodic in-range motion is present. |
| `NO_BREATHING_SIGNAL` | A previously calibrated, still-observable region has lost periodic motion for the configured timeout — **and the baby is confirmed present**. It means only that *this detector sees no signal*. |
| `MEASUREMENT_INVALID` | The stream, image quality, motion stability, or signal strength is insufficient to say anything. |
| `CRIB_EMPTY` | Presence detection confirmed the crib is empty; monitoring is paused and resumes automatically. |

Low signal-to-noise, a frozen stream, excess movement, and startup without calibration all fail toward `MEASUREMENT_INVALID`. While invalid, the rate and breathing entities are marked *unavailable* rather than reporting zero BPM or `OFF`.

## Presence detection

The camera is always on, but the baby is not always in the crib. Rather than an ML person detector (unreliable on IR night vision and swaddled infants — and a missed detection would silently suppress alerts), presence uses a physical invariant: **a baby cannot enter or leave the crib without a caregiver**, i.e. without a large sustained motion event.

- A confirmed breathing signal marks the baby **present** at any time.
- A sudden loss of breathing **without** a preceding pickup-shaped disturbance keeps presence at *present* — apnea does not look like a pickup, so it can never be mistaken for absence, and the no-breathing alert path stays fully armed.
- After a pickup-shaped disturbance with no signal, the add-on runs full-frame breathing scans: breathing anywhere → *present* (if it is outside your configured region, the panel suggests enlarging or moving your box); two clean empty scans → *absent*, monitoring pauses.
- Another disturbance (baby returned) triggers re-verification, and monitoring resumes on its own.

Presence has an honest `UNKNOWN` state (startup, prolonged verification failure). Known limitation: if a caregiver rearranges bedding such that breathing becomes unmeasurable *anywhere* in the frame, absence can be inferred wrongly — no camera-based method (ML included) can see through that; consider a gentle notification on prolonged `UNKNOWN`/`ABSENT` during expected sleep hours. Disable the feature with the `presence_detection` option to always treat the crib as occupied.

## Entities

A single device is created with:

- `sensor.baby_respiration_rate` — breaths per minute
- `sensor.baby_respiration_confidence` — signal quality 0–100 (**not** a medical probability)
- `binary_sensor.baby_breathing_detected`
- `binary_sensor.baby_respiration_measurement_valid`
- `binary_sensor.baby_breathing_rate_low` — on while the measured rate sits below your configured threshold (device class *problem*; unavailable while not measuring)
- `sensor.baby_presence` — `PRESENT` / `ABSENT` / `CHECKING` / `UNKNOWN`
- `binary_sensor.baby_in_crib` — occupancy; *unavailable* while presence is undecided
- Diagnostics: detector state, signal RMS, SNR, video FPS, analysis window, stream status, reason, and excessive motion.

**Automations must gate any use of `baby_breathing_detected` on `baby_respiration_measurement_valid`, and should gate notifications on presence.** The recommended notification condition (still never as a life-safety mechanism):

```yaml
condition:
  - condition: state
    entity_id: binary_sensor.baby_in_crib
    state: "on"
  - condition: or
    conditions:
      - condition: state           # clear video, no breathing rhythm
        entity_id: sensor.baby_respiration_state
        state: "NO_BREATHING_SIGNAL"
      - condition: state           # cannot measure for a sustained period
        entity_id: binary_sensor.baby_respiration_measurement_valid
        state: "off"
        for: "00:05:00"
      - condition: state           # measured rate below your threshold
        entity_id: binary_sensor.baby_breathing_rate_low
        state: "on"
        for: "00:00:30"
```

## Options

| Option | Purpose |
| --- | --- |
| `processing_fps` | Analyzed frames per second. 5 is enough up to 120 BPM. |
| `min_bpm` / `max_bpm` | The breathing band. Newborns commonly breathe 30–60 BPM. |
| `minimum_confidence` | Quality score required before motion counts as breathing. |
| `no_breath_timeout` | Seconds of missing signal (while calibrated and observable) before `NO_BREATHING_SIGNAL`. |
| `presence_detection` | Pause monitoring when the crib is confirmed empty. Off = always treat the crib as occupied. |
| `low_rate_threshold_bpm` | Flags `baby_breathing_rate_low` when the measured rate is below this (0 disables). Must exceed `min_bpm` — slower rates are unmeasurable and surface as signal loss instead. Small hysteresis (+2 BPM to clear) prevents flapping; add a `for:` duration in your automation. |
| `minimum_signal_rms` | Band-passed motion amplitude (px RMS) required before the signal counts as observable. A clearly concentrated spectral peak with strong SNR overrides this down to 30% of the value — unambiguous rhythm is not vetoed for being small. |
| `mqtt_base_topic` / `mqtt_discovery_prefix` | Topic naming for power users. The broker itself is chosen in the panel's Home Assistant step. |

The camera URL and measurement region are set in the panel, not in the options, and are stored in the add-on's private data.

## Tips and limitations

- Prefer a fixed, stable camera mount; set the camera stream to a modest resolution (the add-on processes at 320 px wide anyway).
- Bedding, caregiver motion, camera vibration, IR mode switches, autofocus, and video compression can all dominate chest motion.
- The algorithm measures image motion, not airflow or oxygenation.
- Thresholds are camera- and scene-specific: validate across lighting modes, sleep positions, and clothing before trusting trends.
- `NO_BREATHING_SIGNAL` is deliberately hard to reach: the region must first calibrate with sustained good signal, and the view must remain observable.
