# Changelog

## 0.3.12

Anti-flap round from live logs (real baby, rate drifting 24-30 BPM within a minute):

- Rhythm stability no longer penalizes genuine rate drift: while locked, a peak within 15% of the previously locked rate counts as stable even when the window halves disagree (REM transitions tripped the ±20% split-half tolerance, now ±25%). Cold lock-on still requires half-agreement, so the empty-bed defense is unchanged.
- One drop is now one transition in HA history: availabilities going offline publish before the state JSON, eliminating the momentary Off between On and Unavailable.
- Logs: every per-second line now carries the gate values (rms/snr/concentration/selected block), and state transitions log an explicit STATE CHANGE line with the full gate snapshot — flap diagnosis no longer needs guesswork.

## 0.3.11

- Detection hold: once calibrated breathing is established, brief dropouts (stream hiccups, twitch-corrupted windows, marginal seconds) keep reporting `BREATHING` with the last rate for `detection_hold_seconds` (default 10, option) instead of flapping to unavailable. The hold is a reporting overlay only: the no-breathing countdown and calibration decay run from the true moment of loss, so `NO_BREATHING_SIGNAL` fires at the same absolute time and punches through the hold immediately, as does `CRIB_EMPTY`.

## 0.3.10

- Pausing monitoring now disconnects the RTSP stream entirely. Decoding the camera's full-rate H.264 feed is the dominant CPU cost — bigger than the analysis — and it previously kept running while paused. The panel shows a still preview refreshed every ~30 s over a brief reconnect; resuming reconnects the stream. Paused CPU now drops to near zero.
- Docs tip: use the camera's low-resolution substream if available; decode cost dwarfs analysis cost.

## 0.3.9

- Camera-card buttons (Edit region / Camera / MQTT / Pause) fit on a single row: compact styling and shorter Pause/Resume labels.

## 0.3.8

- New `switch.baby_monitoring` (MQTT-controllable) plus a panel button: turn analysis on only while the baby is asleep. While off, optical flow is skipped entirely (near-zero CPU), no alerts can fire, measurement/presence entities go unavailable, the state shows `MONITORING_OFF`, and live video stays in the panel. Re-enabling starts a fresh calibration. The choice persists across restarts.
- CPU tuning: `processing_fps` may now go down to 4 (safe with the default 90 BPM band — Nyquist keeps a 33% margin, and per-frame displacement actually grows), and `target_processing_width` is exposed as an option (CPU scales with its square; 320→256 saves ~36%). Both together roughly halve analysis CPU.

## 0.3.7

- The waveform no longer disguises noise as signal: it shows its real amplitude range in pixels (flagging "noise floor" below 0.004 px) and the trace dims to gray whenever breathing is not actually detected. Auto-scaling previously made a 0.0005 px empty-bed noise floor fill the plot exactly like genuine 0.01+ px breathing.

## 0.3.6

Empty-crib false positives fixed (observed: "in crib" and a 44 BPM "detection" on an empty bed from airflow rippling the blankets):

- Presence now requires **sustained** breathing (~8 s continuous) before marking the crib occupied. A momentary false blip used to stick forever, because an empty bed never produces the pickup disturbance that triggers re-verification.
- Block selection requires **spatial coherence**: the chosen block must have an adjacent block moving in phase (correlation ≥ 0.5 with meaningful amplitude) plus a periodicity floor. A chest moves a multi-block area together; a fluttering blanket corner or cherry-picked noise does not.
- When per-block data exists and no block shows coherent breathing, detection is vetoed outright (`no_coherent_breathing_region`) instead of falling back to the whole-box median — a chance spectral fluke in a 24 s noise window can no longer register as breathing.

## 0.3.5

- Removed the user-facing auto-detect region feature: with block-adaptive measurement, drawing a generous box by hand is simpler and more predictable than the scan's suggestions. The wizard step is now draw-only, and the dashboard button is renamed **Edit region**. (Full-frame scans remain internal to presence verification, which they were designed for.)

## 0.3.4

Large boxes are now first-class — drawn generously so a moving baby stays covered:

- Block-adaptive measurement: the region is a search area. Every analysis window scores ~20 px blocks inside it for breathing-band periodicity and measures from the block that carries the rhythm, so static bedding no longer dilutes the median. The dashboard shows the active sub-region as a dashed mint box. Sticky selection avoids hopping between neighbouring blocks.
- Spectral evidence beats the amplitude threshold: a clearly concentrated peak with SNR ≥ minimum+5 dB lowers the RMS floor to 30% of the configured value. Frequency-domain checks (Welch spectrum peak, concentration, SNR) were already the core of detection; the absolute amplitude floor no longer vetoes an unambiguous rhythm.
- Safety wording updated everywhere: because the monitor locks onto the strongest breathing inside the box, a big box must never be able to include a co-sleeping adult.

## 0.3.3

- New `binary_sensor.baby_breathing_rate_low` (device class *problem*): turns on while the measured rate sits below the configurable `low_rate_threshold_bpm` option (default 20, 0 disables), with a +2 BPM clear hysteresis. Build notifications on it in Home Assistant with a `for:` duration. Rates below `min_bpm` remain unmeasurable by design and surface as signal loss / `NO_BREATHING_SIGNAL` instead.
- Dashboard: shows "slower than your threshold" under the rate while flagged; camera-card buttons cleaned up (consistent styling, region readout on its own line).

## 0.3.2

Stability fixes from overnight real-camera use, plus broker choice in the panel:

- Transient rejection: limb twitches (large but sub-excessive motion) no longer poison the whole 24 s analysis window; the masked samples drop out and detection recovers in seconds instead of drifting unavailable after every twitch.
- Gate hysteresis: once breathing is locked, marginal dips in SNR/confidence no longer flap the state; cold lock-on thresholds are unchanged.
- New onboarding step **Home Assistant**: choose the HA-provided broker (default, zero setup), a custom MQTT broker anywhere on the network, or no MQTT — with live connection feedback. Also reachable from the dashboard (**MQTT…**). The `mqtt_custom_broker`/`mqtt_host`/… add-on options moved into the panel; `mqtt_base_topic` and `mqtt_discovery_prefix` remain options.

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
