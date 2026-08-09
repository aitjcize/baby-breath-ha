#!/usr/bin/with-contenv bashio
set -e

# Prefer the broker the Supervisor provides (e.g. the Mosquitto add-on) unless
# the user configured an explicit broker in the add-on options.
if bashio::services.available 'mqtt'; then
    BABY_MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    BABY_MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    BABY_MQTT_USERNAME="$(bashio::services 'mqtt' 'username')"
    BABY_MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
    export BABY_MQTT_HOST BABY_MQTT_PORT BABY_MQTT_USERNAME BABY_MQTT_PASSWORD
    bashio::log.info "Using the Home Assistant MQTT service at ${BABY_MQTT_HOST}:${BABY_MQTT_PORT}."
else
    bashio::log.notice "No MQTT broker found. Install the Mosquitto broker add-on, or point the panel (Home Assistant step) at a custom broker; the web panel works either way."
fi

# RTSP over TCP avoids UDP packet loss artifacts that corrupt optical flow.
export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"

cd /usr/src
exec python3 -m app --addon
