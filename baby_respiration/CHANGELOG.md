# Changelog

## 0.3.22

- Entity consolidation: the derived binaries `breathing_detected` (= state `BREATHING`), `measurement_valid` (= state `BREATHING`/`NO_BREATHING_SIGNAL`), and `baby_in_crib` (= presence `PRESENT`) are removed — the two enum sensors are the single source of truth, and discovery cleanup messages delete the old entities from Home Assistant automatically. `breathing_rate_low` stays (its threshold logic is not derivable from state). The notification recipe in DOCS now uses the enums directly — update your automation accordingly.
- Baby presence wears `mdi:teddy-bear`.

## 0.3.21

- Entity organization: **Detector state** moves from the diagnostics section to the main sensors (it is the system's primary summary and the alert trigger), and both it and **Baby presence** declare `device_class: enum` with their option lists. Numeric diagnostics (signal RMS, SNR, video FPS, analysis window) gain `state_class: measurement` so Home Assistant keeps long-term statistics for trend tuning. No entities added or removed — the derived binary/sensor pairs (breathing⇄state, in-crib⇄presence, valid⇄state) are intentional automation ergonomics, not duplicates.

## 0.3.20

- The add-on Configuration tab is gone — the panel is the single source of configuration. Processing FPS, processing width, and log level join the Tuning card (FPS/width changes briefly recalibrate; the rest applies instantly), and the MQTT base topic / discovery prefix move to the MQTT screen. Built-in defaults apply wherever the panel has no override; legacy add-on option values are still read as defaults if present.

## 0.3.19

- Detection tuning moved into the panel: a **Tuning** card on the dashboard adjusts the breathing band, minimum confidence, low-rate alert threshold, no-breath timeout, detection hold, minimum amplitude, and presence detection — applied live with no restart and no recalibration. Add-on options remain as defaults; blank fields inherit them, and **Use defaults** clears overrides.
- Dashboard edit buttons (Edit region / Camera / MQTT) now open standalone: no more being routed through the whole onboarding flow — the step rail hides, Back becomes Cancel, and saving returns straight to the dashboard.

## 0.3.18

Morning churn diagnosis (baby still, ~544 entity transitions overnight): shallow breathing swings the measured amplitude ~10× with position and covering, and at the faint end (RMS ~0.001–0.003) the empty-bed hardening rejected genuine breathing (`no_coherent_breathing_region` at conf 42, RMS 0.0014 in the captured breakdown).

- The block-hardening floors are now Schmitt-triggered like everything else: while recently breathing, periodicity 0.6→0.5, background contrast 3×→2×, neighbour correlation 0.5→0.35. Cold lock-on keeps full hardening — an empty bed is never "recently breathing", so the noise defenses are unchanged.
- Per-second CSV telemetry in `/data/telemetry-YYYYMMDD.csv` (3-day retention): every gate value, every second, immune to the ~30-minute supervisor log rotation. Threshold tuning can finally be done from a full night of evidence.

## 0.3.17

User report: unavailable gaps with the baby demonstrably still — the movement theory doesn't cover them all, and the window-level reason (`incomplete_motion_data`) hides *why* frames were discarded.

- Per-frame invalid breakdown: the estimate, status, and STATE CHANGE log lines now carry a count of discarded frames by cause (`duplicate_frame`, `excessive_motion`, `low_contrast`, `bad_exposure`, stream gaps…) — the next gap names its culprit directly.
- Duplicated frames (go2rtc relay stalls repeating the last frame) are excluded as missing samples immediately instead of being analyzed: previously up to 2 s of exact repeats passed through as zero-motion samples that flattened the rhythm before the frozen-video threshold tripped, and sustained stalls burned CPU computing optical flow on identical images.

## 0.3.16

- A moving baby is a breathing baby: when the measurement fails because of gross body movement (stirring, being resettled), the reporting hold refreshes instead of expiring — `breathing_detected` stays ON with a "baby is moving" note, bounded at 5 minutes. Post-deploy logs showed movement episodes were the last source of 20–30 s unavailable gaps; movement is stronger vitality evidence than a rhythm, so reporting it as "unavailable" was wrong. Stream gaps and quiet signal losses keep the strict 15 s hold, and alarm timers remain completely unaffected.

## 0.3.15

- Faster detection of dead camera sessions: the frame read timeout drops from 8 s to 4 s. Combined with the 0.5 s reconnect and the 15 s hold, a routine CuboAi session drop (confirmed camera-side behavior) should now be fully invisible: ~4 s to notice + ~1 s to reconnect + ~5 s window refill fits inside the hold. The wizard's connection test keeps the longer 8 s budget for slow first handshakes.

## 0.3.14

From gate-level logs of a full night (drops still surfacing as rhythm_not_stable at conf 50-55, plus 1 s aftershock blips while recalibrating):

- "Recently breathing" is now a time window aligned with the detection hold, not a single-window boolean: one rough second no longer resets the relaxed re-entry thresholds and the rate-continuity escape, so irregular-breathing episodes re-lock at hysteresis floors (conf ≥47) instead of failing cold (≥55) for the whole phase.
- The reporting hold applies to uncalibrated locks too: raw locks already pass the contrast/coherence/stability hardening, and requiring calibration made every 1 s hiccup visible during the 10 s post-drop rebuild.

## 0.3.13

Stream-drop resilience — live logs showed the camera killing its RTSP session every few minutes ("RTSP frame read failed"), and reconnect gaps of 13–33 s were the remaining source of unavailable flaps:

- Reconnects start after 0.5 s (doubling toward the configured `reconnect_interval` only on repeated failures) instead of a fixed 5 s wait.
- `detection_hold_seconds` default 10 → 15: a typical reconnect-plus-refill gap now fits inside the hold, so brief stream drops no longer reach Home Assistant.
- `measurement_invalid_timeout` default 8 → 20 s: calibration (no-breathing alarm arming) survives a reconnect instead of resetting on every gap.

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
