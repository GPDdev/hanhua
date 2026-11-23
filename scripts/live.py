import ctypes
import ctypes.wintypes as wt
import json
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import psutil

from core import HOME, Ollama, db_open, glossary, store_batch, translations, unit
from sound import hear
from vision import recognize

USER = ctypes.windll.user32
USER.GetAncestor.argtypes = [wt.HWND, wt.UINT]
USER.GetAncestor.restype = wt.HWND
USER.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
USER.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]


def windows():
    rows = []
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def each(hwnd, lparam):
        if USER.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(1024)
            USER.GetWindowTextW(hwnd, title, 1024)
            rect = wt.RECT()
            USER.GetWindowRect(hwnd, ctypes.byref(rect))
            if title.value and rect.right - rect.left > 70 and rect.bottom - rect.top > 40:
                pid = wt.DWORD()
                USER.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                rows.append(dict(hwnd=int(hwnd), pid=pid.value, title=title.value))
        return True

    USER.EnumWindows(callback(each), 0)
    return rows


def target(args):
    rows = windows()
    if args.pid:
        pids = {args.pid}
        try:
            pids.update(p.pid for p in psutil.Process(args.pid).children(recursive=True))
        except psutil.Error:
            pass
        rows = [r for r in rows if r["pid"] in pids]
    if args.window:
        rows = [r for r in rows if args.window.casefold() in r["title"].casefold()]
    if args.game:
        root = Path(args.game).resolve()

        def matches(r):
            try:
                p = psutil.Process(r["pid"])
                return Path(p.exe()).resolve().is_relative_to(root) or any(
                    Path(s).is_absolute() and Path(s).resolve().is_relative_to(root)
                    for s in p.cmdline()[1:]
                )
            except (OSError, ValueError, psutil.Error):
                return False

        rows = [r for r in rows if matches(r)]
    if not (args.pid or args.game or args.window) or len(rows) != 1:
        raise ValueError(
            "Select one game window with --pid or --window: " + json.dumps(rows, ensure_ascii=False)
        )
    return rows[0]


def latest(q, value):
    try:
        q.put_nowait(value)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(value)
        except queue.Full:
            pass


def start(args):
    import tkinter as tk
    from windows_capture import WindowsCapture

    if args.list:
        return windows()
    selected = target(args)
    hwnd, pid = selected["hwnd"], selected["pid"]
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.91)
    panel = tk.Label(
        root, font=("Microsoft YaHei", 15), fg="white", bg="#17202a", justify="left", padx=10, pady=4
    )
    panel.pack(fill="both", expand=True)
    root.update_idletasks()
    ow = USER.GetAncestor(root.winfo_id(), 2)
    USER.SetWindowDisplayAffinity(ow, 0x11)
    style = USER.GetWindowLongW(ow, -20)
    USER.SetWindowLongW(ow, -20, style | 0x20 | 0x80 | 0x08000000)
    visible = True
    last_key = False
    flags = threading.Event()
    frames = queue.Queue(1)
    clips = queue.Queue(1)
    messages = queue.Queue()
    lock = threading.Lock()
    cache = Path(args.out).resolve() if args.out else HOME / "live" / str(pid)
    cache.mkdir(parents=True, exist_ok=True)
    engine = Ollama()
    terms = glossary(cache)
    frames_seen = [0]
    started = time.monotonic()
    last_capture = [0.0]
    audio = None
    log = []

    def translate(texts, kind, stamp):
        with lock:
            db = db_open(cache)
            try:
                values = translations(db)
                us = [unit(s, "游戏实时" + kind, kind=kind) for s in texts]
                pending = [u for u in us if u["id"] not in values]
                if pending:
                    answers = engine.translate(pending, terms)
                    store_batch(db, pending, answers)
                    values.update(answers)
                result = "\n".join(values[u["id"]] for u in us)
            finally:
                db.close()
        messages.put(
            dict(
                kind=kind,
                text=result,
                source=texts,
                latency=round(time.monotonic() - stamp, 3),
                cached=not pending,
            )
        )

    def read_screen():
        previous = None
        while not flags.is_set():
            try:
                stamp, bgr = frames.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                small = cv2.resize(bgr, (120, 70))
                if previous is not None and np.abs(previous.astype("int16") - small).mean() < 0.7:
                    continue
                previous = small
                rows = recognize(bgr, args.lang)
                texts = list(
                    dict.fromkeys(r["text"] for r in rows if r["text"].strip() != selected["title"].strip())
                )
                if texts:
                    translate(texts, "screen", stamp)
                else:
                    messages.put(dict(kind="screen", text="", source=[], latency=0))
            except Exception as e:
                messages.put(dict(kind="error", text=str(e)))

    def read_sound():
        prefix = deque(maxlen=2)
        noise = deque(maxlen=50)
        phrase = []
        quiet = 0
        while not flags.is_set():
            data = audio.stdout.read(3200)
            if not data:
                break
            vals = np.frombuffer(data, "<i2").astype("float32") / 32768
            level = float(np.sqrt(np.mean(vals * vals)))
            noise.append(level)
            baseline = float(np.percentile(noise, 15))
            voiced = level > max(0.004, min(0.028, baseline * 1.8))
            if not phrase:
                if voiced:
                    phrase = list(prefix) + [vals]
                    quiet = 0
                prefix.append(vals)
                continue
            phrase.append(vals)
            quiet = 0 if voiced else quiet + 1
            if quiet >= 5 or len(phrase) >= 72:
                if len(phrase) - quiet >= 3:
                    latest(clips, (time.monotonic(), np.concatenate(phrase)))
                prefix.clear()
                if quiet:
                    prefix.extend(phrase[-2:])
                phrase = []
                quiet = 0
        if not flags.is_set():
            messages.put(dict(kind="error", text="Process audio capture ended"))

    def listen():
        while not flags.is_set():
            try:
                stamp, chunk = clips.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                cues, _ = hear(chunk, True, args.lang)
                if cues:
                    translate([" ".join(c["text"] for c in cues)], "dialogue", stamp)
            except Exception as e:
                messages.put(dict(kind="error", text=str(e)))

    cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, minimum_update_interval=160)

    @cap.event
    def on_frame_arrived(frame, capture_control):
        frames_seen[0] += 1
        now = time.monotonic()
        if now - last_capture[0] < 0.55:
            return
        last_capture[0] = now
        image = np.ascontiguousarray(frame.frame_buffer[:, :, :3])
        if image.shape[1] > 1050:
            image = cv2.resize(image, (1050, round(image.shape[0] * 1050 / image.shape[1])))
        latest(frames, (now, image))

    @cap.event
    def on_closed():
        flags.set()

    control = cap.start_free_threaded()
    threading.Thread(target=read_screen, daemon=True).start()
    if not args.no_audio:
        exe = Path(__file__).parent / "native" / "loopback.exe"
        audio = subprocess.Popen(
            [str(exe), str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=read_sound, daemon=True).start()
        threading.Thread(target=listen, daemon=True).start()
    state = {}

    def tick():
        nonlocal visible, last_key
        if (
            flags.is_set()
            or not USER.IsWindow(hwnd)
            or (args.seconds and time.monotonic() - started >= args.seconds)
        ):
            root.quit()
            return
        key = bool(USER.GetAsyncKeyState(0x79) & 0x8000)
        if key and not last_key:
            visible = not visible
            if visible:
                root.deiconify()
            else:
                root.withdraw()
        last_key = key
        if USER.GetAsyncKeyState(0x7B) & 0x8000:
            root.quit()
            return
        while not messages.empty():
            event = messages.get()
            event["elapsed"] = round(time.monotonic() - started, 3)
            log.append(event)
            kind = event["kind"]
            state[kind] = (event["text"], time.monotonic())
        rect = wt.RECT()
        USER.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(120, rect.right - rect.left - 28)
        body = "\n".join(
            v[0] for k, v in state.items() if v[0] and (k == "screen" or time.monotonic() - v[1] < 7)
        )
        panel.config(text=body or "汉化字幕   F10 显隐   F12 退出", wraplength=width - 20)
        root.update_idletasks()
        height = min(max(36, panel.winfo_reqheight()), max(80, (rect.bottom - rect.top) // 2))
        root.geometry(f"{width}x{height}{rect.left + 14:+d}{rect.bottom - height - 25:+d}")
        root.after(130, tick)

    root.after(20, tick)
    try:
        root.mainloop()
    finally:
        flags.set()
        control.stop()
        if audio:
            audio.terminate()
            try:
                audio.wait(timeout=3)
            except subprocess.TimeoutExpired:
                audio.kill()
        root.destroy()
    events = cache / "live-events.json"
    from core import put

    put(events, {"target": selected, "frames": frames_seen[0], "events": log})
    return dict(
        events=str(events),
        frames=frames_seen[0],
        translated=sum(bool(e.get("source")) for e in log),
        errors=[e["text"] for e in log if e["kind"] == "error"],
    )
