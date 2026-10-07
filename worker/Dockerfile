FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 ASHAD_WIDTH=1280 ASHAD_HEIGHT=720 \
    ASHAD_WORKER_DATA=/data ASHAD_EXECUTABLE_ROOTS=/game:/opt/runtime \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/player LIBGL_ALWAYS_SOFTWARE=1

RUN dpkg --add-architecture i386 \
    && sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && echo steam steam/question select "I AGREE" | debconf-set-selections \
    && echo steam steam/license note '' | debconf-set-selections \
    && apt-get install -y --no-install-recommends xvfb openbox xdotool x11vnc novnc websockify ffmpeg \
       x11-utils xauth fonts-dejavu-core dbus-x11 gosu procps ca-certificates \
       steam-installer libgl1-mesa-dri libgl1 libvulkan1 mesa-vulkan-drivers \
       libasound2 libpulse0 libxrandr2 libxcursor1 libxinerama1 libxi6 libnss3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash player

WORKDIR /opt/ashad
COPY requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/worker-requirements.txt
COPY . /opt/ashad/worker
RUN sed -i 's/\r$//' /opt/ashad/worker/entrypoint.sh && chmod +x /opt/ashad/worker/entrypoint.sh

EXPOSE 8080 6080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD python -c "import pathlib,urllib.request; t=pathlib.Path('/run/secrets/worker_token').read_text().strip(); urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/health',headers={'Authorization':'Bearer '+t}),timeout=3)"
ENTRYPOINT ["/opt/ashad/worker/entrypoint.sh"]
