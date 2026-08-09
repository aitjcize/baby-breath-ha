# Baby Respiration Monitor

Watches an RTSP camera for the gentle rise and fall of breathing motion and publishes what it sees to Home Assistant.

**Experimental secondary monitor only — not a medical device, never a life-safety system.**

- Guided onboarding in the ingress panel: paste the camera URL, test it, and let the breathing-region scan find where to measure (works in IR night vision).
- Conservative by design: anything the detector cannot verify becomes `MEASUREMENT_INVALID`, never a false "no breathing".
- Zero-configuration MQTT: uses the broker provided by Home Assistant (e.g. the Mosquitto add-on) automatically.
- Local only: video never leaves the machine; the panel is reachable only through authenticated Home Assistant ingress.
