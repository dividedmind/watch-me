#!/usr/bin/env python3
"""watch-me — gamepad-aware break reminder (25 min work / 5 min break)"""

import asyncio
import atexit
import glob
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

CLEANUP_PATHS = []


def cleanup_files():
    for path in CLEANUP_PATHS:
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(cleanup_files)

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.exit("evdev not found — install with: pip install evdev")

WORK_SECONDS = 25 * 60
BREAK_SECONDS = 5 * 60
POSTPONE_SECONDS = 5 * 60
# Idle shorter than this pauses the timer but doesn't reset it.
# Idle as long as BREAK_SECONDS resets the timer.
IDLE_PAUSE_SECONDS = 30
WARN_BEFORE_SECONDS = 2 * 60  # notify-send warning this many seconds before break

BG_COLOR = "#0d1117"
FG_COLOR = "#e6edf3"
ACCENT_COLOR = "#238636"
BTN_BG = "#21262d"
BTN_FG = "#e6edf3"
MUTED_COLOR = "#8b949e"


class State:
    def __init__(self, debug: bool = False):
        self._lock = threading.Lock()
        self.debug = debug
        now = time.monotonic()
        self.last_activity = now
        # Accumulated work seconds (excluding the current active streak).
        self.work_accumulated = 0.0
        # Start of the current active streak; None when idle/on break.
        self.active_since: float | None = now
        # When the user went idle (None when active or on break).
        self.idle_start: float | None = None
        self.on_break = False
        self.postponed_until = 0.0
        self.warned = False

    # ── idle transition (active → idle) ───────────────────────────────────────

    def check_idle(self):
        """Freeze timer if no input for IDLE_PAUSE_SECONDS. Call from scheduler."""
        now = time.monotonic()
        with self._lock:
            if self.active_since is not None and not self.on_break:
                if now - self.last_activity >= IDLE_PAUSE_SECONDS:
                    # Freeze: accumulate work up to last input, mark as idle
                    self.work_accumulated += self.last_activity - self.active_since
                    self.active_since = None
                    self.idle_start = self.last_activity

    # ── activity transition (idle → active) ───────────────────────────────────

    def record_activity(self):
        now = time.monotonic()
        with self._lock:
            self.last_activity = now
            if self.idle_start is not None:
                # Returning from idle
                idle_duration = now - self.idle_start
                self.idle_start = None
                if idle_duration >= BREAK_SECONDS:
                    # Long absence — reset, as if they took a full break
                    self.work_accumulated = 0.0
                    self.warned = False
                # Resume active streak
                self.active_since = now
            elif self.active_since is None and not self.on_break:
                # Shouldn't normally happen, but recover gracefully
                self.active_since = now

    # ── timer queries ─────────────────────────────────────────────────────────

    def work_elapsed(self) -> float:
        now = time.monotonic()
        with self._lock:
            if self.on_break:
                return 0.0
            extra = (now - self.active_since) if self.active_since is not None else 0.0
            return self.work_accumulated + extra

    def idle_elapsed(self) -> float:
        now = time.monotonic()
        with self._lock:
            if self.idle_start is None:
                return 0.0
            return now - self.idle_start

    def should_break(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if self.on_break:
                return False
            if now < self.postponed_until:
                return False
            if self.active_since is None:
                return False  # idle — don't trigger while away
            elapsed = self.work_accumulated + (now - self.active_since)
            return elapsed >= WORK_SECONDS

    def should_warn(self) -> bool:
        """Return True once when WARN_BEFORE_SECONDS remain before break."""
        now = time.monotonic()
        with self._lock:
            if self.on_break or self.warned:
                return False
            if now < self.postponed_until:
                return False
            if self.active_since is None:
                return False  # idle — don't warn while away
            elapsed = self.work_accumulated + (now - self.active_since)
            if elapsed >= (WORK_SECONDS - WARN_BEFORE_SECONDS):
                self.warned = True
                return True
            return False

    # ── break lifecycle ───────────────────────────────────────────────────────

    def begin_break(self):
        now = time.monotonic()
        with self._lock:
            self.on_break = True
            if self.active_since is not None:
                self.work_accumulated += now - self.active_since
                self.active_since = None
            self.idle_start = None

    def end_break_skip(self):
        with self._lock:
            self.on_break = False
            self.work_accumulated = 0.0
            self.active_since = time.monotonic()
            self.idle_start = None
            self.postponed_until = 0.0
            self.warned = False

    def end_break_postpone(self):
        now = time.monotonic()
        with self._lock:
            self.on_break = False
            self.work_accumulated = WORK_SECONDS - POSTPONE_SECONDS
            self.active_since = now
            self.idle_start = None
            self.postponed_until = now + POSTPONE_SECONDS
            self.warned = False


# ── evdev monitoring ──────────────────────────────────────────────────────────

ACTIVITY_TYPES = {ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS}


async def monitor_device(path: str, state: State, active: set):
    try:
        dev = evdev.InputDevice(path)
    except (PermissionError, OSError):
        if path not in active:
            print(f"Warning: cannot access {path}", file=sys.stderr, flush=True)
        return
    active.add(path)
    try:
        async for event in dev.async_read_loop():
            if event.type in ACTIVITY_TYPES:
                state.record_activity()
    except OSError:
        pass
    finally:
        active.discard(path)
        try:
            dev.close()
        except Exception:
            pass


async def hotplug_watcher(state: State, active: set, loop: asyncio.AbstractEventLoop):
    """Periodically discover new input devices (e.g. gamepads plugged in)."""
    while True:
        await asyncio.sleep(5)
        for path in glob.glob("/dev/input/event*"):
            if path not in active:
                loop.create_task(monitor_device(path, state, active))


def _notify(summary: str, body: str = "", urgency: str = "normal") -> None:
    """Fire a desktop notification via notify-send, silently ignoring errors."""
    try:
        cmd = ["notify-send", "-a", "watch-me", "-u", urgency, summary]
        if body:
            cmd.append(body)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass  # notify-send not installed


async def scheduler(state: State, ui_queue: queue.Queue):
    """Check work timer every second; put 'break' message in queue when due."""
    while True:
        await asyncio.sleep(1)
        state.check_idle()
        if state.debug:
            elapsed = state.work_elapsed()
            idle = state.idle_elapsed()
            status = "IDLE" if idle > 0 else "ACTIVE"
            mins, secs = divmod(int(elapsed), 60)
            print(
                f"[debug] {status:6s}  work={mins:02d}:{secs:02d}/{WORK_SECONDS // 60}:00"
                f"  idle={idle:.0f}s",
                flush=True,
            )
        if state.should_warn():
            _notify(
                f"Break in {WARN_BEFORE_SECONDS // 60} minutes",
                "Finish up what you're doing.",
                urgency="normal",
            )
        if state.should_break():
            state.begin_break()
            ui_queue.put("break")
            # Wait until break is done before checking again
            while state.on_break:
                await asyncio.sleep(0.5)


async def state_socket_server(state: State):
    """Serve real-time state as JSON over a Unix domain socket."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir or not os.path.isdir(runtime_dir):
        # Fallback to a user-writable directory (e.g., ~/.cache)
        runtime_dir = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
        try:
            os.makedirs(runtime_dir, exist_ok=True)
        except OSError:
            import tempfile
            runtime_dir = tempfile.gettempdir()

    socket_path = os.path.join(runtime_dir, "watch-me.sock")

    # Clean up any stale socket from a previous run
    try:
        os.unlink(socket_path)
    except OSError:
        pass

    # Register for atexit / SIGTERM cleanup
    CLEANUP_PATHS.append(socket_path)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            elapsed = state.work_elapsed()
            idle = state.idle_elapsed()

            if state.on_break:
                status = "BREAK"
            elif idle > 0:
                status = "IDLE"
            else:
                status = "ACTIVE"

            data = {
                "status": status,
                "work_elapsed": int(elapsed),
                "work_remaining": max(0, WORK_SECONDS - int(elapsed)),
                "idle_elapsed": int(idle),
                "on_break": state.on_break,
            }
            writer.write(json.dumps(data).encode("utf-8") + b"\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_unix_server(handle_client, path=socket_path)
    except Exception as e:
        print(f"Error starting state socket server: {e}", file=sys.stderr, flush=True)
        if socket_path in CLEANUP_PATHS:
            CLEANUP_PATHS.remove(socket_path)
        return

    try:
        async with server:
            await server.serve_forever()
    finally:
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        if socket_path in CLEANUP_PATHS:
            CLEANUP_PATHS.remove(socket_path)


async def async_main(state: State, ui_queue: queue.Queue):
    loop = asyncio.get_running_loop()
    active: set = set()

    # Open all existing devices
    tasks = []
    for path in glob.glob("/dev/input/event*"):
        tasks.append(loop.create_task(monitor_device(path, state, active)))

    tasks.append(loop.create_task(hotplug_watcher(state, active, loop)))
    tasks.append(loop.create_task(scheduler(state, ui_queue)))
    tasks.append(loop.create_task(state_socket_server(state)))

    await asyncio.gather(*tasks)


def run_async(state: State, ui_queue: queue.Queue):
    asyncio.run(async_main(state, ui_queue))


# ── break window ──────────────────────────────────────────────────────────────

class BreakWindow:
    def __init__(self, root: tk.Tk, state: State, done_event: threading.Event):
        self.root = root
        self.state = state
        self.done_event = done_event
        self.remaining = BREAK_SECONDS
        self._build()

    def _build(self):
        root = self.root
        root.configure(bg=BG_COLOR)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.deiconify()
        root.update_idletasks()

        # Some window managers ignore -fullscreen for override-redirect windows.
        # Force the geometry to screen size as a fallback.
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        root.geometry(f"{screen_w}x{screen_h}+0+0")
        try:
            root.attributes("-fullscreen", True)
        except tk.TclError:
            pass
        root.lift()
        root.focus_force()

        frame = tk.Frame(root, bg=BG_COLOR)
        frame.place(relx=0.5, rely=0.5, anchor="center")

        title_font = tkfont.Font(family="Sans", size=28, weight="bold")
        countdown_font = tkfont.Font(family="Monospace", size=96, weight="bold")
        sub_font = tkfont.Font(family="Sans", size=14)
        btn_font = tkfont.Font(family="Sans", size=13)

        tk.Label(frame, text="Time for a break", font=title_font,
                 bg=BG_COLOR, fg=FG_COLOR).pack(pady=(0, 8))

        self.countdown_var = tk.StringVar(value=self._fmt(self.remaining))
        tk.Label(frame, textvariable=self.countdown_var, font=countdown_font,
                 bg=BG_COLOR, fg=ACCENT_COLOR).pack()

        tk.Label(frame, text="Step away from the screen",
                 font=sub_font, bg=BG_COLOR, fg=MUTED_COLOR).pack(pady=(4, 32))

        btn_frame = tk.Frame(frame, bg=BG_COLOR)
        btn_frame.pack()

        skip_btn = tk.Button(btn_frame, text="Skip break", font=btn_font,
                             bg=BTN_BG, fg=BTN_FG, activebackground="#30363d",
                             activeforeground=FG_COLOR, relief="flat",
                             padx=20, pady=10, cursor="hand2",
                             command=self._skip)
        skip_btn.pack(side="left", padx=8)

        post_btn = tk.Button(btn_frame, text="Postpone 5 min", font=btn_font,
                             bg=BTN_BG, fg=BTN_FG, activebackground="#30363d",
                             activeforeground=FG_COLOR, relief="flat",
                             padx=20, pady=10, cursor="hand2",
                             command=self._postpone)
        post_btn.pack(side="left", padx=8)

        root.bind("<Escape>", lambda e: self._skip())
        self._tick()

    def _fmt(self, seconds: int) -> str:
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _tick(self):
        self.remaining -= 1
        self.countdown_var.set(self._fmt(max(0, self.remaining)))
        if self.remaining <= 0:
            self._finish()
        else:
            self.root.after(1000, self._tick)

    def _finish(self):
        self.state.end_break_skip()
        self._dismiss()

    def _skip(self):
        self.state.end_break_skip()
        self._dismiss()

    def _postpone(self):
        self.state.end_break_postpone()
        self._dismiss()

    def _dismiss(self):
        self.root.withdraw()
        self.done_event.set()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    def handle_signal(signum, frame):
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except ValueError:
        pass

    debug = "--debug" in sys.argv
    state = State(debug=debug)
    ui_queue: queue.Queue = queue.Queue()

    # Start evdev + scheduler in background thread
    bg = threading.Thread(target=run_async, args=(state, ui_queue), daemon=True)
    bg.start()

    # Tkinter lives on the main thread
    root = tk.Tk()
    root.withdraw()
    root.title("watch-me")

    print("watch-me running — work timer started", flush=True)
    if debug:
        print("[debug] mode on — printing state every second", flush=True)

    def poll():
        try:
            msg = ui_queue.get_nowait()
        except queue.Empty:
            msg = None

        if msg == "break":
            done = threading.Event()
            BreakWindow(root, state, done)
            # After the user dismisses, done is set; scheduler will see on_break=False
        root.after(200, poll)

    root.after(200, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
