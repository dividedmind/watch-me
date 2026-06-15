#!/usr/bin/env python3
"""watch-me — gamepad-aware break reminder (25 min work / 5 min break)"""

import asyncio
import glob
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.exit("evdev not found — install with: pip install evdev")

WORK_SECONDS = 25 * 60
BREAK_SECONDS = 5 * 60
POSTPONE_SECONDS = 5 * 60
IDLE_RESET_SECONDS = 120
WARN_BEFORE_SECONDS = 2 * 60  # notify-send warning this many seconds before break

BG_COLOR = "#0d1117"
FG_COLOR = "#e6edf3"
ACCENT_COLOR = "#238636"
BTN_BG = "#21262d"
BTN_FG = "#e6edf3"
MUTED_COLOR = "#8b949e"


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.work_start = time.monotonic()
        self.on_break = False
        self.postponed_until = 0.0
        self.warned = False  # True once the pre-break warning has fired this session

    def record_activity(self):
        now = time.monotonic()
        with self._lock:
            idle = now - self.last_activity
            self.last_activity = now
            if not self.on_break and idle > IDLE_RESET_SECONDS:
                self.work_start = now
                self.warned = False

    def work_elapsed(self):
        with self._lock:
            if self.on_break:
                return 0
            return time.monotonic() - self.work_start

    def should_break(self):
        now = time.monotonic()
        with self._lock:
            if self.on_break:
                return False
            if now < self.postponed_until:
                return False
            return (now - self.work_start) >= WORK_SECONDS

    def begin_break(self):
        with self._lock:
            self.on_break = True

    def end_break_skip(self):
        with self._lock:
            self.on_break = False
            self.work_start = time.monotonic()
            self.postponed_until = 0.0
            self.warned = False

    def end_break_postpone(self):
        with self._lock:
            self.on_break = False
            # Reset work timer so it triggers again after POSTPONE_SECONDS
            self.work_start = time.monotonic() - (WORK_SECONDS - POSTPONE_SECONDS)
            self.postponed_until = time.monotonic() + POSTPONE_SECONDS
            self.warned = False

    def should_warn(self):
        """Return True once when WARN_BEFORE_SECONDS remain before break."""
        now = time.monotonic()
        with self._lock:
            if self.on_break or self.warned:
                return False
            if now < self.postponed_until:
                return False
            elapsed = now - self.work_start
            if elapsed >= (WORK_SECONDS - WARN_BEFORE_SECONDS):
                self.warned = True
                return True
            return False


# ── evdev monitoring ──────────────────────────────────────────────────────────

ACTIVITY_TYPES = {ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS}


async def monitor_device(path: str, state: State, active: set):
    try:
        dev = evdev.InputDevice(path)
    except (PermissionError, OSError):
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
        cmd = ["notify-send", "-a", "watch-me", f"-u{urgency}", summary]
        if body:
            cmd.append(body)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass  # notify-send not installed


async def scheduler(state: State, ui_queue: queue.Queue):
    """Check work timer every second; put 'break' message in queue when due."""
    while True:
        await asyncio.sleep(1)
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


async def async_main(state: State, ui_queue: queue.Queue):
    loop = asyncio.get_running_loop()
    active: set = set()

    # Open all existing devices
    tasks = []
    for path in glob.glob("/dev/input/event*"):
        tasks.append(loop.create_task(monitor_device(path, state, active)))

    tasks.append(loop.create_task(hotplug_watcher(state, active, loop)))
    tasks.append(loop.create_task(scheduler(state, ui_queue)))

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
    state = State()
    ui_queue: queue.Queue = queue.Queue()

    # Start evdev + scheduler in background thread
    bg = threading.Thread(target=run_async, args=(state, ui_queue), daemon=True)
    bg.start()

    # Tkinter lives on the main thread
    root = tk.Tk()
    root.withdraw()
    root.title("watch-me")

    print("watch-me running — work timer started", flush=True)

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
