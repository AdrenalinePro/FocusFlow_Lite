#!/usr/bin/env bash
#
# Start a Unix-socket → system D-Bus forwarder that the FocusFlow
# app container can reach via the bind-mounted app directory.
#
# Why we need this
# ----------------
# Arduino App Lab containers are sealed by design: only the volumes
# explicitly declared in the auto-generated compose file are mounted
# into the container, and there is no user-facing knob to add more
# (`app-compose-overrides.yaml` / `docker-compose.override.yaml` are
# both silently ignored by the daemon).  The FocusFlow BLE stacks
# (LinuxBLEServer + HandGattServer) talk to BlueZ through the system
# D-Bus socket, which is *not* on the default mount list, so the
# container fails with `FileNotFoundError: /run/dbus/system_bus_socket`
# every time.
#
# Workaround: a socat forwarder that lives on the host filesystem but
# inside a path the container *does* see (``host/`` is a stable directory
# inside the bind-mounted app directory and is NOT cleaned by the
# daemon, unlike ``.cache/`` which is wiped on every app restart).
#
# The proxy is launched via ``systemd-run --user`` so it survives
# ``arduino-app-cli app restart`` (a plain ``nohup socat &`` was being
# killed by the daemon's process cleanup).
#
# Usage
# -----
#     bash host/start_dbus_proxy.sh start     # start in background
#     bash host/start_dbus_proxy.sh stop      # stop the running proxy
#     bash host/start_dbus_proxy.sh status    # is the proxy alive?
#     bash host/start_dbus_proxy.sh restart   # stop + start
#
# When the proxy is running, restart the FocusFlow app:
#     arduino-app-cli app restart ~/ArduinoApps/focusflow_integrated
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_DIR="${REPO_DIR}/host"
SOCK_PATH="${HOST_DIR}/host-dbus-proxy.sock"
PID_FILE="${HOST_DIR}/host-dbus-proxy.pid"
LOG_FILE="${HOST_DIR}/host-dbus-proxy.log"
REAL_SOCK="/var/run/dbus/system_bus_socket"
SYSTEMD_UNIT="focusflow-dbus-proxy.service"

mkdir -p "${HOST_DIR}"

is_running() {
    [[ -S "${SOCK_PATH}" ]] || return 1
    # Verify the systemd unit is still active.
    systemctl --user is-active --quiet "${SYSTEMD_UNIT}" 2>/dev/null || return 1
}

start() {
    if is_running; then
        echo "already running (unit=${SYSTEMD_UNIT})"
        return 0
    fi
    if [[ ! -S "${REAL_SOCK}" ]]; then
        echo "ERROR: system D-Bus socket not found at ${REAL_SOCK}" >&2
        echo "       is bluetoothd / dbus-daemon running?" >&2
        return 1
    fi

    # Start via systemd-run --user so the proxy gets a real cgroup and
    # survives the daemon's app-restart cleanup.
    : > "${LOG_FILE}"
    systemd-run --user --unit="${SYSTEMD_UNIT}" \
        --property=StandardOutput=append:"${LOG_FILE}" \
        --property=StandardError=append:"${LOG_FILE}" \
        /usr/bin/socat \
            "UNIX-LISTEN:${SOCK_PATH},fork,reuseaddr,mode=0660" \
            "UNIX-CONNECT:${REAL_SOCK}" \
        >/dev/null 2>&1

    sleep 0.3
    if is_running; then
        # Stash the main PID for diagnostics.
        systemctl --user show -p MainPID --value "${SYSTEMD_UNIT}" > "${PID_FILE}" 2>/dev/null || true
        echo "started (unit=${SYSTEMD_UNIT}, socket=${SOCK_PATH})"
    else
        echo "ERROR: socat failed to start; check ${LOG_FILE}" >&2
        return 1
    fi
}

stop() {
    if ! is_running; then
        echo "not running"
        python3 -c "
import os
for p in ('${PID_FILE}', '${SOCK_PATH}'):
    if os.path.exists(p):
        os.unlink(p)
"
        return 0
    fi
    systemctl --user stop "${SYSTEMD_UNIT}" 2>/dev/null || true
    python3 -c "
import os
for p in ('${PID_FILE}', '${SOCK_PATH}'):
    if os.path.exists(p):
        os.unlink(p)
"
    echo "stopped"
}

status() {
    if is_running; then
        local pid
        pid="$(cat "${PID_FILE}" 2>/dev/null || echo '?')"
        echo "running (unit=${SYSTEMD_UNIT}, pid=${pid})"
        echo "  socket: ${SOCK_PATH}"
        echo "  log:    ${LOG_FILE}"
        ss -lpx 2>/dev/null | grep -F "${SOCK_PATH}" | head -3 || true
    else
        echo "not running"
    fi
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac
