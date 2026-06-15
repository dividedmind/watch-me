#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check input group membership
if ! groups | grep -qw input; then
    echo "WARNING: You are not in the 'input' group."
    echo "  Run: sudo usermod -aG input $USER"
    echo "  Then log out and back in."
    echo "  Without this, gamepad/keyboard monitoring may fail."
    echo ""
fi

echo "Checking Python dependencies..."
if ! python3 -c "import evdev" 2>/dev/null; then
    echo "WARNING: Python package 'evdev' is not available."
    echo "  Install it via your package manager, e.g.:"
    echo "    sudo apt install python3-evdev"
    echo "    sudo dnf install python3-evdev"
    echo "  Or in a virtualenv: pip install evdev"
    echo ""
fi

echo "Installing systemd user service..."
mkdir -p ~/.config/systemd/user
sed "s|%h/watch-me/watch_me.py|$SCRIPT_DIR/watch_me.py|g" \
    "$SCRIPT_DIR/watch-me.service" > ~/.config/systemd/user/watch-me.service

systemctl --user daemon-reload
systemctl --user enable --now watch-me

echo ""
echo "watch-me installed and started."
echo "  Status:  systemctl --user status watch-me"
echo "  Logs:    journalctl --user -u watch-me -f"
echo "  Stop:    systemctl --user stop watch-me"
