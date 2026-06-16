# watch-me

A minimal break reminder for Arch Linux / X11 that actually notices gamepad input.

Workrave doesn't capture gamepad events, so gaming sessions slip past the break timer. watch-me reads directly from `/dev/input/event*` via **evdev**, catching keyboard, mouse, and gamepad activity with a single unified monitor.

## Behaviour

| Event | What happens |
|---|---|
| 25 min of continuous input | Fullscreen break overlay appears |
| 2 min before break | Desktop notification via `notify-send` |
| Idle for 2+ min | Work timer resets (AFK time doesn't count) |
| Gamepad plugged in mid-session | Detected automatically within 5 s |

The break overlay is a borderless fullscreen window (`overrideredirect`) — it bypasses i3 tiling and sits above everything.

## Break overlay controls

- **Skip break** — dismiss and restart the full 25-minute work timer
- **Postpone 5 min** — dismiss; break will re-trigger in 5 minutes
- **`Escape`** — same as Skip

## Requirements

- Python 3.8+
- `python-evdev` (`pip install evdev` or `pacman -S python-evdev`)
- `libnotify` / `notify-send` for pre-break warnings (optional but recommended)
- `tkinter` (usually bundled with Python; on Arch: `pacman -S tk`)
- User must be in the `input` group to read `/dev/input/*`

## Install

```bash
# Add yourself to the input group (log out/in after)
sudo usermod -aG input $USER

# Install and start as a systemd user service
bash install.sh
```

### Manual run (no systemd)

```bash
pip install --user evdev
python watch_me.py
```

## Tuning

Edit the constants at the top of `watch_me.py`:

```python
WORK_SECONDS        = 25 * 60   # work session length
BREAK_SECONDS       = 5 * 60    # break duration
POSTPONE_SECONDS    = 5 * 60    # how long a postpone delays the break
IDLE_RESET_SECONDS  = 120       # idle gap that resets the work timer
WARN_BEFORE_SECONDS = 2 * 60    # how early the pre-break notification fires
```

## Logs

```bash
journalctl --user -u watch-me -f
```

## Querying Current State

You can instantly peek at the current timer state using the Unix domain socket:

```bash
nc -U $XDG_RUNTIME_DIR/watch-me.sock
```

This returns a JSON payload containing the current status, work elapsed, work remaining, idle elapsed, and break status:

```json
{"status": "ACTIVE", "work_elapsed": 124, "work_remaining": 1376, "idle_elapsed": 0, "on_break": false}
```

This makes it extremely easy to parse with `jq` for status bar integrations (e.g., Polybar, Waybar, i3status):

```bash
nc -U $XDG_RUNTIME_DIR/watch-me.sock | jq -r '"\(.status) [\(.work_elapsed // 0)s]"'
```
