#!/usr/bin/env bash
#
# Install script for the FocusFlow integration.
#
# Arduino App Lab looks for apps under ~/ArduinoApps/.  This repo keeps
# the source under /home/arduino/Focusflow/focusflow_integrated/, so
# we install a symlink so edits in the repo are picked up live without
# copying.  Re-run this script after cloning on a fresh UNO Q.
set -euo pipefail

REPO_DIR="${FOCUSFLOW_REPO_DIR:-/home/arduino/Focusflow}"
APP_SRC="${REPO_DIR}/focusflow_integrated"
APP_LINK="${HOME}/ArduinoApps/focusflow_integrated"

if [[ ! -d "${APP_SRC}" ]]; then
    echo "ERROR: source app not found at ${APP_SRC}" >&2
    exit 1
fi

mkdir -p "${HOME}/ArduinoApps"

# Use a symlink so live edits in the repo are visible to App Lab.
if [[ -L "${APP_LINK}" ]] || [[ -d "${APP_LINK}" ]]; then
    if [[ -L "${APP_LINK}" && "$(readlink "${APP_LINK}")" == "${APP_SRC}" ]]; then
        echo "OK: ${APP_LINK} already points at ${APP_SRC}"
        exit 0
    fi
    echo "WARNING: ${APP_LINK} already exists and is not our symlink." >&2
    echo "         Remove it manually if you want to re-install." >&2
    exit 2
fi

ln -s "${APP_SRC}" "${APP_LINK}"
echo "Linked ${APP_LINK} -> ${APP_SRC}"
echo
echo "Next steps:"
echo "  1. Install Python dependencies (already documented in source_code/CLAUDE.md):"
echo "       sudo apt-get install -y bluez libdbus-1-3"
echo "       pip3 install --user --break-system-packages -r ${APP_SRC}/python/requirements.txt"
echo "  2. Apply the dbus-fast patch:"
echo "       bash ${REPO_DIR}/source_code/linux/setup_dbus_fast.sh --apply"
echo "  3. Configure D-Bus (one-time):"
echo "       sudo cp ${REPO_DIR}/source_code/linux/../README_Linux.md -  # see \"一次性安装步骤\""
echo "  4. Build & run from App Lab:"
echo "       arduino-app-cli app build ${APP_LINK}"
echo "       arduino-app-cli app start ${APP_LINK}"
echo "       arduino-app-cli app logs ${APP_LINK} --follow"
