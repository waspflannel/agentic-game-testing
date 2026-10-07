#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data /home/player /run/player
    chown player:player /data /home/player /run/player
    # Named volumes retain Steam authentication; run files live separately under /data/runs.
    exec gosu player "$0" "$@"
fi

# An abrupt container stop can leave X's lock and socket in the writable layer.
display_number=${DISPLAY#:}
case "$DISPLAY" in :*) ;; *) echo "Worker DISPLAY must be local: :number" >&2; exit 1 ;; esac
case "$display_number" in ''|*[!0-9]*) echo "Worker DISPLAY must be local: :number" >&2; exit 1 ;; esac
if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || pgrep -x 'Xvfb|Xorg|Xwayland|Xephyr|X' >/dev/null; then
    echo "Private X display is already in use; refusing to remove its lock or socket" >&2
    exit 1
fi
rm -f -- "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}"

export XDG_RUNTIME_DIR=/run/player
export PULSE_SERVER=127.0.0.1
Xvfb "$DISPLAY" -screen 0 "${ASHAD_WIDTH}x${ASHAD_HEIGHT}x24" -nolisten tcp -ac &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT INT TERM
count=0
until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
    count=$((count + 1))
    [ "$count" -lt 50 ] || exit 1
    sleep 0.1
done
dbus-daemon --session --fork --address=unix:path=/run/player/bus
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/player/bus
openbox >/dev/null 2>&1 &
python -m worker.independent_watchdog &
exec python -m uvicorn worker.app:create_app --factory --host 0.0.0.0 --port 8080 --no-access-log
