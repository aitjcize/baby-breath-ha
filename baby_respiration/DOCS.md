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

Settings persist across restarts and updates. Use **Edit region**, **Camera**, **MQTT**, or **Tuning** on the dashboard to change them later — each opens standalone and returns to the dashboard.

## What the states mean

| State | Meaning |
| --- | --- |
| `BREATHING` | Video quality is adequate and periodic in-range motion is present. |
| `NO_BREATHING_SIGNAL` | A previously calibrated, still-observable region has lost periodic motion for the configured timeout — **and the baby is confirmed present**. It means only that *this detector sees no signal*. |
| `MEASUREMENT_INVALID` | The stream, image quality, motion stability, or signal strength is insufficient to say anything. |
| `CRIB_EMPTY` | Presence detection confirmed the crib is empty; monitoring is paused and resumes automatically. |
| `MONITORING_OFF` | Monitoring switched off (panel button or `switch.baby_monitoring`); analysis is skipped and entities are unavailable. |

Low signal-to-noise, a frozen stream, excess movement, and startup without calibration all fail toward `MEASUREMENT_INVALID`. While invalid, the rate and breathing entities are marked *unavailable* rather than reporting zero BPM or `OFF`.

## Presence detection

The camera is always on, but the baby is not always in the crib. Rather than an ML person detector (unreliable on IR night vision and swaddled infants — and a missed detection would silently suppress alerts), presence uses a physical invariant: **a baby cannot enter or leave the crib without a caregiver**, i.e. without a large sustained motion event.

- A **sustained** breathing signal (several consecutive seconds, not a momentary blip) marks the baby **present** at any time.
- A sudden loss of breathing **without** a preceding pickup-shaped disturbance keeps presence at *present* — apnea does not look like a pickup, so it can never be mistaken for absence, and the no-breathing alert path stays fully armed.
- After a pickup-shaped disturbance with no signal, the add-on runs full-frame breathing scans: breathing anywhere → *present* (if it is outside your configured region, the panel suggests enlarging or moving your box); two clean empty scans → *absent*, monitoring pauses.
- Another disturbance (baby returned) triggers re-verification, and monitoring resumes on its own.

Presence has an honest `UNKNOWN` state (startup, prolonged verification failure). Known limitation: if a caregiver rearranges bedding such that breathing becomes unmeasurable *anywhere* in the frame, absence can be inferred wrongly — no camera-based method (ML included) can see through that; consider a gentle notification on prolonged `UNKNOWN`/`ABSENT` during expected sleep hours. Disable the feature in the panel's Tuning card to always treat the crib as occupied.

## Entities

A single device is created with:

- `sensor.baby_respiration_state` — the primary summary: `BREATHING` / `NO_BREATHING_SIGNAL` / `MEASUREMENT_INVALID` / `CRIB_EMPTY` / `MONITORING_OFF`
- `sensor.baby_respiration_rate` — breaths per minute (unavailable while not measuring)
- `sensor.baby_presence` — `PRESENT` / `ABSENT` / `CHECKING` / `UNKNOWN`
- `binary_sensor.baby_breathing_rate_low` — on while the measured rate sits below your configured threshold (device class *problem*; unavailable while not measuring)
- `switch.baby_monitoring` — turn analysis on/off (e.g. schedule it for sleep times); while off, CPU drops to near zero and no alerts can fire
- `sensor.baby_respiration_confidence` and diagnostics: signal RMS, SNR, video FPS, analysis window, stream status, reason, excessive motion.

Everything else is derivable from the two enum sensors — breathing detected is `state == BREATHING`, measurement valid is `state in (BREATHING, NO_BREATHING_SIGNAL)`, in-crib is `presence == PRESENT` — so no separate binary entities exist for them. The recommended notification automation (still never a life-safety mechanism):

```yaml
triggers:
  - trigger: state                 # clear video, no breathing rhythm
    entity_id: sensor.baby_respiration_state
    to: "NO_BREATHING_SIGNAL"
  - trigger: state                 # cannot measure for a sustained period
    entity_id: sensor.baby_respiration_state
    to: "MEASUREMENT_INVALID"
    for: "00:05:00"
  - trigger: state                 # measured rate below your threshold
    entity_id: binary_sensor.baby_breathing_rate_low
    to: "on"
    for: "00:00:30"
conditions:
  - condition: state
    entity_id: sensor.baby_presence
    state: "PRESENT"
  - condition: state
    entity_id: switch.baby_monitoring
    state: "on"
```

## Scheduling monitoring

Monitoring only makes sense while the baby is asleep. Automate `switch.baby_monitoring` — e.g. on at bedtime, off in the morning:

```yaml
- alias: Baby monitor on at bedtime
  trigger: [{platform: time, at: "19:30:00"}]
  action: [{service: switch.turn_on, target: {entity_id: switch.baby_monitoring}}]
```

While off, the camera stream is fully disconnected — decoding it is the largest CPU cost, bigger than the analysis itself — and the panel shows a still preview refreshed every ~30 s. Re-enabling reconnects and starts a fresh calibration.

## Configuration lives in the panel

There is no add-on Configuration tab — everything is set in the panel and persists in the add-on's private data:

- **Tuning card** (dashboard): breathing band, minimum confidence, low-rate alert threshold, no-breath timeout, detection hold, minimum amplitude, presence detection, processing FPS, processing width, log level. Detection changes apply instantly; FPS/width changes briefly recalibrate. Blank fields use the built-in defaults; **Use defaults** clears all overrides.
- **MQTT screen**: broker choice plus base topic and discovery prefix (blank = defaults). Note: changing the base topic leaves stale retained discovery messages on the old topic — remove the old device from the MQTT integration if you rename.

## Tips and limitations

- Prefer a fixed, stable camera mount; if your camera offers a low-resolution substream URL, use it — H.264 decode of the main stream is typically the largest CPU cost, ahead of the analysis (which processes at 320 px wide anyway).
- Bedding, caregiver motion, camera vibration, IR mode switches, autofocus, and video compression can all dominate chest motion.
- The algorithm measures image motion, not airflow or oxygenation.
- Thresholds are camera- and scene-specific: validate across lighting modes, sleep positions, and clothing before trusting trends.
- `NO_BREATHING_SIGNAL` is deliberately hard to reach: the region must first calibrate with sustained good signal, and the view must remain observable.
