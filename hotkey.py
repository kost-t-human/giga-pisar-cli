#!/usr/bin/env python
"""Гига Писарь для Linux — диктовка по горячей клавише. Только X11.

Держите правый Ctrl, говорите, отпустите — текст вставится в активное окно.
С Shift (правый Ctrl + Shift) после вставки ещё нажмётся Enter: наговорил
и сразу отправил в чат.

История диктовок пишется в $XDG_RUNTIME_DIR/pisar-history.log. Эта папка
живёт в памяти и пропадает при выключении. Ротация по размеру: два файла
по 256 КБ.

Клавиша меняется через PISAR_KEY (имя из pynput.keyboard.Key: ctrl_r, alt_r, ...).
Папка с моделью ищется так же, как у pisar: PISAR_MODEL_DIR, потом model рядом.
"""
import logging.handlers
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

# Под Wayland программа не видит чужих нажатий и не может вставлять текст
# в чужие окна так, как в X11, — молча висеть без толку хуже, чем сказать.
if os.environ.get("XDG_SESSION_TYPE") != "x11":
    sys.exit("hotkey.py работает только в сеансе X11, а здесь "
             f"{os.environ.get('XDG_SESSION_TYPE') or 'неизвестно что'}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import giga_core
from pynput import keyboard

KEY = getattr(keyboard.Key, os.environ.get("PISAR_KEY", "ctrl_r"))
MIN_HOLD = 0.4  # короче — случайное нажатие, не диктовка
SOUNDS = "/usr/share/sounds/freedesktop/stereo/"
SOUND_START = SOUNDS + "audio-volume-change.oga"
SOUND_DONE = SOUNDS + "message.oga"

# В памяти (tmpfs): запись и история не переживают выключения.
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())

history = logging.getLogger("history")
history.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(
    os.path.join(RUNTIME, "pisar-history.log"),
    maxBytes=256 * 1024, backupCount=1, encoding="utf-8",
)
_h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S"))
history.addHandler(_h)

engine = giga_core.Engine()
kbd = keyboard.Controller()
raw = os.path.join(RUNTIME, "pisar.raw")
wav = raw[:-3] + "wav"
rec = None
started = 0.0
other_key = False  # Ctrl нажат вместе с другой клавишей: это сочетание, а не диктовка
send = False       # во время записи был нажат Shift: после вставки жмём Enter
shift_down = False
SHIFTS = (keyboard.Key.shift, keyboard.Key.shift_r)


def beep(path: str) -> None:
    if os.path.exists(path):
        subprocess.Popen(["paplay", path], stderr=subprocess.DEVNULL)


def paste(text: str) -> None:
    # Кладём в оба буфера и жмём Shift+Insert: так вставляют и терминалы
    # (берут PRIMARY), и Qt/GTK/браузеры (берут CLIPBOARD).
    for sel in ("clipboard", "primary"):
        subprocess.run(["xclip", "-selection", sel], input=text.encode(), check=True)
    with kbd.pressed(keyboard.Key.shift):
        kbd.tap(keyboard.Key.insert)


def finish(proc, held: float, enter: bool) -> None:
    proc.terminate()
    proc.wait()
    if held < MIN_HOLD:
        return
    with open(raw, "rb") as f, wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(f.read())
    text = engine.transcribe(wav)
    print(f"{held:.1f}s, {len(text)} симв.", flush=True)
    if not text:
        return
    history.info(text)
    if enter:
        paste(text)
        time.sleep(0.05)  # дать вставке дойти до окна раньше Enter
        kbd.tap(keyboard.Key.enter)
    else:
        paste(text + " ")
    beep(SOUND_DONE)


def on_press(key):
    global rec, started, other_key, send, shift_down
    if key in SHIFTS:
        shift_down = send = True
        return
    if key != KEY:
        other_key = True
        return
    if rec is None:
        other_key = False
        send = shift_down
        beep(SOUND_START)
        started = time.monotonic()
        # Микрофон открываем только на время нажатия: между диктовками
        # он закрыт. pw-record раскачивался около секунды, parec с малой
        # задержкой — нет. Остаётся пробуждение звуковой карты, ~0,2 с;
        # его убирает install.sh --no-mic-sleep.
        with open(raw, "wb") as f:
            rec = subprocess.Popen(
                ["parec", "--format=s16le", "--rate=16000", "--channels=1", "--latency-msec=50"],
                stdout=f,
            )


def on_release(key):
    global rec, shift_down
    if key in SHIFTS:
        shift_down = False
        return
    if key != KEY or rec is None:
        return
    proc, rec = rec, None
    held = 0.0 if other_key else time.monotonic() - started
    threading.Thread(target=finish, args=(proc, held, send), daemon=True).start()


# После загрузки микрофон спит, пока его не откроют, и правило
# --no-mic-sleep начинает действовать только с этого момента. Будим сразу.
subprocess.run(["timeout", "0.3", "parec"], stdout=subprocess.DEVNULL)

print("готов, держите", KEY, flush=True)
with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
    l.join()
