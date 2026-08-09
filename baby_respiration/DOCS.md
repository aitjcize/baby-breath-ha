# Baby Respiration Monitor

An experimental, local-only detector that estimates respiration-like chest/abdomen motion from an RTSP camera and publishes results through Home Assistant MQTT discovery.

> **This is an experimental secondary monitor, not a medical device or a life-safety system. Never use it as the primary or only way to monitor an infant. A valid video signal does not prove an infant is safe, and missing video motion does not prove apnea. Do not build life-safety automations on top of this add-on.**

## Installation

1. Add this repository to the add-on store: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, then paste the repository URL.
2. Install **Baby Respiration Monitor** and start it.
3. For Home Assistant entities, also install and start the **Mosquitto broker** add-on (plus the MQTT integration). The monitor finds it automatically — no broker settings needed.

## Onboarding

Open the **Baby Respiration** panel in the sidebar. The wizard walks you through:

1. **Safety acknowledgement** — please read it for real.
2. **Camera** — paste your camera's RTSP URL (found in the camera app under RTSP/ONVIF/local streaming) and test the connection; a preview frame confirms you have the right camera. No camera handy? Enter `demo://breathing` to explore with a synthetic scene.
3. **Breathing region** — press **Auto-detect breathing region**: the add-on watches ~30 seconds of video for periodic motion in the breathing band and suggests the region to measure. Keep the room still during the scan. You can always drag the box manually; keep it snug around the chest and tummy, away from crib rails and loose blankets.

**Keep the box small.** The signal is the *median* motion of every pixel in the box, so a box dominated by static bedding averages the breathing away to nothing — a whole-bed box produces a Signal RMS an order of magnitude below the detection gate. Aim for the torso only (well under 25% of the image; the panel warns when you exceed that).

**Co-sleeping warning.** Never include another person in the box. If an adult shares the bed, the detector can report reassuring "breathing" from *their* motion regardless of the baby's state — a false sense of safety that is worse than no reading. Draw the box around the baby's torso only, and double-check where the auto-detect suggestion lands before accepting it.

Settings persist across restarts and updates. Use **Re-detect region** or **Camera…** on the dashboard to change them later.

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
- After a pickup-shaped disturbance with no signal, the add-on runs full-frame breathing scans: breathing anywhere → *present* (if it is outside your configured region, the panel suggests re-detecting); two clean empty scans → *absent*, monitoring pauses.
- Another disturbance (baby returned) triggers re-verification, and monitoring resumes on its own.

Presence has an honest `UNKNOWN` state (startup, prolonged verification failure). Known limitation: if a caregiver rearranges bedding such that breathing becomes unmeasurable *anywhere* in the frame, absence can be inferred wrongly — no camera-based method (ML included) can see through that; consider a gentle notification on prolonged `UNKNOWN`/`ABSENT` during expected sleep hours. Disable the feature with the `presence_detection` option to always treat the crib as occupied.

## Entities

A single device is created with:

- `sensor.baby_respiration_rate` — breaths per minute
- `sensor.baby_respiration_confidence` — signal quality 0–100 (**not** a medical probability)
- `binary_sensor.baby_breathing_detected`
- `binary_sensor.baby_respiration_measurement_valid`
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
```

## Options

| Option | Purpose |
| --- | --- |
| `processing_fps` | Analyzed frames per second. 5 is enough up to 120 BPM. |
| `min_bpm` / `max_bpm` | The breathing band. Newborns commonly breathe 30–60 BPM. |
| `minimum_confidence` | Quality score required before motion counts as breathing. |
| `no_breath_timeout` | Seconds of missing signal (while calibrated and observable) before `NO_BREATHING_SIGNAL`. |
| `presence_detection` | Pause monitoring when the crib is confirmed empty. Off = always treat the crib as occupied. |
| `minimum_signal_rms` | Band-passed motion amplitude (px RMS) required before the signal counts as observable. Camera-distance dependent: lower it if the panel shows a rhythmic waveform with good SNR but says "too faint to trust". |
| `mqtt_custom_broker` + `mqtt_*` | Only needed for a broker other than the one Home Assistant provides. |

The camera URL and measurement region are set in the panel, not in the options, and are stored in the add-on's private data.

## Tips and limitations

- Prefer a fixed, stable camera mount; set the camera stream to a modest resolution (the add-on processes at 320 px wide anyway).
- Bedding, caregiver motion, camera vibration, IR mode switches, autofocus, and video compression can all dominate chest motion.
- The algorithm measures image motion, not airflow or oxygenation.
- Thresholds are camera- and scene-specific: validate across lighting modes, sleep positions, and clothing before trusting trends.
- `NO_BREATHING_SIGNAL` is deliberately hard to reach: the region must first calibrate with sustained good signal, and the view must remain observable.
