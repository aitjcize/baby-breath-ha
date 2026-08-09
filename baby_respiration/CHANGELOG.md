# Changelog

## 0.3.1

Tuning from the first real-camera validation (wide-angle overhead cam, co-sleeping scene):

- Lowered the default amplitude gate `minimum_signal_rms` from 0.003 to 0.001 px and exposed it as an add-on option. The old value rejected genuine breathing (strong SNR, rhythmic waveform) from cameras where the baby is small in frame; spectral gates (SNR, peak concentration, confidence) remain the primary noise discriminators.
- Oversized-region warnings: the wizard and dashboard now warn when the box exceeds 25% of the image — the median-based signal gets drowned out by static pixels (a whole-bed box measured ~14× below the detection gate).
- Co-sleeping warnings in the wizard, dashboard, and docs: never include a second person in the box; the monitor could measure *their* breathing instead of the baby's.

## 0.3.0

- Crib presence detection (no ML): pickup-shaped disturbances followed by full-frame breathing scans decide `PRESENT`/`ABSENT`; sudden signal loss without a disturbance can never be classified as absence, so the no-breathing alert path stays armed.
- New entities: `sensor.baby_presence` and `binary_sensor.baby_in_crib` (occupancy, unavailable while undecided). `NO_BREATHING_SIGNAL` now requires confirmed presence; a confirmed-empty crib shows the new `CRIB_EMPTY` state and pauses monitoring until the baby returns.
- When a presence scan finds breathing outside the configured region, the dashboard suggests re-detecting (the region is never moved automatically).
- New `presence_detection` option (default on); off restores the previous always-occupied behavior.
- Recommended notification recipe documented: alert only when present AND (no breathing OR measurement invalid for a sustained period).

## 0.2.0

First release as a Home Assistant add-on.

- Guided onboarding in the ingress panel: safety acknowledgement, camera URL entry with live connection test and preview, and breathing-region selection.
- Breathing-region auto-detect: a ~30 s optical-flow scan scores every image block for periodic motion in the breathing band and suggests the measurement region — no ML model, works in IR night vision.
- New dashboard: breathing halo animated at the detected rate, live waveform, camera view with region overlay, and diagnostics.
- Camera URL and region apply at runtime (no restart) and persist in add-on data.
- Automatic MQTT configuration from the Home Assistant-provided broker; custom broker remains available in the options.
- `demo://breathing` synthetic camera for exploring the add-on without hardware.
- MQTT state payload slimmed (waveform stays in the panel only).

## 0.1.0

- Initial experimental detector: dense optical flow, band-passed spectral estimation, conservative three-state classifier, MQTT discovery, loopback debug page.
