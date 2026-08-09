ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Debian packages give prebuilt numpy/scipy/opencv (with ffmpeg) on every
# supported architecture; only pure-Python paho-mqtt comes from pip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-numpy \
        python3-scipy \
        python3-opencv \
        python3-yaml \
    && pip3 install --no-cache-dir --break-system-packages "paho-mqtt>=2.1,<3" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["/run.sh"]
