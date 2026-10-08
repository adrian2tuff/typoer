"""
Mac-native global hotkey prompt typer.

Install dependencies with: python3 -m pip install -r requirements.txt
Set OPENAI_API_KEY in your environment before running.

Hotkeys:
  Ctrl+Option+U      Start capturing a prompt
  Ctrl+Option+I      Send the prompt
  Escape             Cancel prompt/API request or stop generated typing
  Ctrl+Option+Up     Increase typing speed by 10 WPM
  Ctrl+Option+Down   Decrease typing speed by 10 WPM (minimum 10)
  Ctrl+Option+0      Log the current typing speed in Terminal

Special prompts:
  /clipboard    Type clipboard contents locally
  /clip         Use clipboard contents as the API prompt
  /kill         Stop the program
"""

from __future__ import annotations

import logging
import math
import os
import random
import string
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any

import Quartz
from openai import OpenAI

LOGGER = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "Respond in plain, straight sentences only. Do not use Markdown, bullets, "
    "numbered lists, code blocks, or line breaks. Keep the entire response on one line."
)

KEYCODES = {
    "escape": 53, "space": 49, "enter": 36, "tab": 48,
    "backspace": 51, "delete": 117, "down": 125, "up": 126,
}
SHIFTED = {
    "`": "~", "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+", "[": "{", "]": "}", "\\": "|",
    ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
}
CHAR_KEYCODES = {
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42,
    ";": 41, "'": 39, ",": 43, ".": 47, "/": 44, "`": 50,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5,
    "h": 4, "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45,
    "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32,
    "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
}
KEYCODES.update({"u": 32, "i": 34})


@dataclass(frozen=True)
class Settings:
    start_hotkey: str = "ctrl+alt+u"
    send_hotkey: str = "ctrl+alt+i"
    waiting_text: str = ">"
    prompt_marker: str = ">"
    kill_prompt: str = "/kill"
    clipboard_prompt: str = "/clip"
    clipboard_type_prompt: str = "/clipboard"
    model: str = "gpt-6.1-sol"
    reasoning_effort: str | None = "low"
    service_tier: str = "fast"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    openai_timeout: float = 300.0
    typing_wpm: float = 180.0
    typing_accuracy: float = 0.97
    backspace_duration: float = 0.1
    correction_coefficient: float = 0.4

    @classmethod
    def from_environment(cls) -> "Settings":
        defaults = cls()
        effort = os.getenv("OPENAI_REASONING_EFFORT", defaults.reasoning_effort or "").strip()
        return cls(
            start_hotkey=os.getenv("TYPOER_START_HOTKEY", defaults.start_hotkey),
            send_hotkey=os.getenv("TYPOER_SEND_HOTKEY", defaults.send_hotkey),
            waiting_text=os.getenv("TYPOER_WAITING_TEXT", defaults.waiting_text),
            prompt_marker=os.getenv("TYPOER_PROMPT_MARKER", defaults.prompt_marker),
            kill_prompt=os.getenv("TYPOER_KILL_PROMPT", defaults.kill_prompt),
            clipboard_prompt=os.getenv("TYPOER_CLIPBOARD_PROMPT", defaults.clipboard_prompt),
            clipboard_type_prompt=os.getenv("TYPOER_CLIPBOARD_TYPE_PROMPT", defaults.clipboard_type_prompt),
            model=os.getenv("OPENAI_MODEL", defaults.model),
            reasoning_effort=effort or None,
            service_tier=os.getenv("OPENAI_SERVICE_TIER", defaults.service_tier),
            system_prompt=os.getenv("OPENAI_SYSTEM_PROMPT", defaults.system_prompt),
            openai_timeout=float(os.getenv("OPENAI_TIMEOUT", str(defaults.openai_timeout))),
            typing_wpm=float(os.getenv("TYPOER_WPM", str(defaults.typing_wpm))),
        )

    def __post_init__(self) -> None:
        if self.typing_wpm <= 0:
            raise ValueError("typing_wpm must be greater than zero")
        if not 0 <= self.typing_accuracy <= 1:
            raise ValueError("typing_accuracy must be between zero and one")
        if self.backspace_duration < 0:
            raise ValueError("backspace_duration cannot be negative")
        if not 0 <= self.correction_coefficient < 1:
            raise ValueError("correction_coefficient must be less than one")
        if self.openai_timeout <= 0:
            raise ValueError("openai_timeout must be greater than zero")


def keycode_for_char(char: str) -> tuple[int, bool] | None:
    keycode = CHAR_KEYCODES.get(char.lower())
    if keycode is None:
        return None
    needs_shift = char.isupper()
    if char in SHIFTED.values():
        for base, shifted in SHIFTED.items():
            if shifted == char:
                keycode = CHAR_KEYCODES.get(base)
                needs_shift = True
                break
    return (keycode, needs_shift)


_SYNTHETIC_EVENT_TAG = 0x5459504F4552  # "TYPOER"

def post_key(keycode: int, down: bool, shift: bool = False) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    if event is None:
        raise RuntimeError("Could not create keyboard event")
    Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskShift if shift else 0)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, _SYNTHETIC_EVENT_TAG)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _clipboard_read() -> str:
    result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
    return result.stdout


def _clipboard_write(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def type_text(text: str) -> None:
    """Paste Unicode text into the currently focused macOS app."""
    old = _clipboard_read()
    try:
        _clipboard_write(text)
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "macOS could not paste text. Enable Accessibility permission "
                "for Terminal under System Settings > Privacy & Security > Accessibility."
            )
        time.sleep(0.05)
    finally:
        try:
            _clipboard_write(old)
        except Exception:
            LOGGER.warning("Could not restore the previous clipboard contents.")


class HumanTyper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def set_typing_speed(self, wpm: float) -> None:
        if not math.isfinite(wpm) or wpm <= 0:
            raise ValueError("Typing speed must be greater than zero.")
        self._settings = replace(self._settings, typing_wpm=wpm)

    def type(self, text: str, cancel_event: threading.Event | None = None) -> bool:
        seconds_per_character = 12 / self._settings.typing_wpm
        minimum_delay = seconds_per_character * 0.2
        maximum_delay = seconds_per_character * 1.8
        position = 0
        mistakes = 0

        while position < len(text):
            if (cancel_event is not None and cancel_event.is_set()) or self._stop_requested():
                return False
            if mistakes and self._should_correct(position, mistakes, len(text)):
                for _ in range(mistakes):
                    post_key(KEYCODES["backspace"], True)
                    post_key(KEYCODES["backspace"], False)
                    time.sleep(self._settings.backspace_duration)
                mistakes = 0

            if random.random() > self._settings.typing_accuracy:
                self._type_character(random.choice(string.ascii_lowercase))
                mistakes += 1
            else:
                self._type_character(text[position + mistakes])
                if mistakes:
                    mistakes += 1
                else:
                    position += 1
            time.sleep(random.uniform(minimum_delay, maximum_delay))
        return True

    def _type_character(self, char: str) -> None:
        if char == " ":
            post_key(KEYCODES["space"], True)
            post_key(KEYCODES["space"], False)
        elif char == "\n":
            post_key(KEYCODES["enter"], True)
            post_key(KEYCODES["enter"], False)
        elif char == "\t":
            post_key(KEYCODES["tab"], True)
            post_key(KEYCODES["tab"], False)
        else:
            mapped = keycode_for_char(char)
            if mapped is None:
                type_text(char)
            else:
                code, shift = mapped
                post_key(code, True, shift)
                post_key(code, False, shift)

    def increase_speed(self, amount: float = 10.0) -> float:
        self.set_typing_speed(self._settings.typing_wpm + amount)
        return self._settings.typing_wpm

    def decrease_speed(self, amount: float = 10.0) -> float:
        self.set_typing_speed(max(10.0, self._settings.typing_wpm - amount))
        return self._settings.typing_wpm

    @property
    def speed(self) -> float:
        return self._settings.typing_wpm

    @staticmethod
    def _stop_requested() -> bool:
        return bool(Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateHIDSystemState, KEYCODES["escape"]
        ))

    def _should_correct(self, position: int, mistakes: int, length: int) -> bool:
        if position + mistakes >= length:
            return True
        probability = 1 - self._settings.correction_coefficient ** mistakes
        return random.random() < probability


class ResponseService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: OpenAI | None = None
        self._stream_lock = threading.Lock()
        self._active_stream = None

    def cancel(self) -> None:
        """Close an in-flight response stream so Escape can abort the API request."""
        with self._stream_lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                LOGGER.debug("Response stream was already closed.", exc_info=True)

    def generate(self, prompt: str, cancel_event: threading.Event) -> str | None:
        if cancel_event.is_set():
            return None
        if self._client is None:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            self._client = OpenAI(timeout=self._settings.openai_timeout)

        request: dict[str, Any] = {
            "model": self._settings.model,
            "instructions": self._settings.system_prompt,
            "input": prompt,
            "service_tier": self._settings.service_tier,
            "stream": True,
        }
        if self._settings.reasoning_effort:
            request["reasoning"] = {"effort": self._settings.reasoning_effort}

        stream = self._client.responses.create(**request)
        with self._stream_lock:
            self._active_stream = stream
        if cancel_event.is_set():
            try:
                stream.close()
            finally:
                with self._stream_lock:
                    if self._active_stream is stream:
                        self._active_stream = None
            return None
        pieces: list[str] = []
        try:
            for event in stream:
                if cancel_event.is_set():
                    return None
                if event.type == "response.output_text.delta":
                    pieces.append(event.delta)
                elif event.type == "response.failed":
                    raise RuntimeError("The OpenAI response failed")
                elif event.type == "error":
                    raise RuntimeError(f"OpenAI streaming error: {event}")
            if cancel_event.is_set():
                return None
            text = "".join(pieces)
            if not text:
                raise RuntimeError("The OpenAI response did not contain text")
            return text
        finally:
            with self._stream_lock:
                if self._active_stream is stream:
                    self._active_stream = None
            try:
                stream.close()
            except Exception:
                pass


class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    WAITING = auto()
    TYPING = auto()
    STOPPED = auto()


class App:
    def __init__(self, settings: Settings, service: ResponseService) -> None:
        self.settings = settings
        self.service = service
        self.typer = HumanTyper(settings)
        self.state = State.IDLE
        self.lock = threading.RLock()
        self.prompt: list[str] = []
        self.job_id = 0
        self.cancel_event: threading.Event | None = None
        self.deferred_keys: dict[int, list[tuple[int, int]]] = {}
        self.event_tap = None
        self.run_loop = None

    def run(self) -> None:
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        )
        self.event_tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._event_callback, None,
        )
        if self.event_tap is None:
            raise RuntimeError(
                "macOS denied keyboard monitoring. Grant Accessibility/Input "
                "Monitoring permission to Terminal or the app running this script."
            )
        source = Quartz.CFMachPortCreateRunLoopSource(None, self.event_tap, 0)
        self.run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self.run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.event_tap, True)

        print("Typoer ready.")
        print("Ctrl+Option+U = start prompt")
        print("Ctrl+Option+I = send prompt")
        print("Escape = cancel prompt/request or stop generated typing")
        print(f"API model: {self.settings.model}")
        print("Ctrl+Option+Up/Down = change speed by 10 WPM")
        print("Ctrl+Option+0 = show current speed")
        print("API service tier: fast")
        print("Ctrl+C in Terminal = quit")
        try:
            Quartz.CFRunLoopRun()
        except KeyboardInterrupt:
            self.stop()

    def _event_callback(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            if self.event_tap:
                Quartz.CGEventTapEnable(self.event_tap, True)
            return event
        if event_type != Quartz.kCGEventKeyDown:
            return event

        # Let Typoer's own simulated keystrokes through without queueing them.
        try:
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == _SYNTHETIC_EVENT_TAG:
                return event
        except Exception:
            pass

        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        ctrl = bool(flags & Quartz.kCGEventFlagMaskControl)
        alt = bool(flags & Quartz.kCGEventFlagMaskAlternate)

        # Escape cancels prompt capture, an in-flight API request, or output typing.
        if keycode == KEYCODES["escape"]:
            with self.lock:
                if self.state is State.RECORDING:
                    self.prompt.clear()
                    self.state = State.IDLE
                    LOGGER.info("Prompt capture cancelled.")
                    return None
                if self.state in (State.WAITING, State.TYPING):
                    if self.cancel_event is not None:
                        self.cancel_event.set()
                    self.state = State.IDLE
                    LOGGER.info("Request/output cancelled.")
                    should_cancel_request = True
                else:
                    should_cancel_request = False
            if should_cancel_request:
                self.service.cancel()
                return None
            return event

        if ctrl and alt and keycode == KEYCODES["u"]:
            self.start()
            return None
        if ctrl and alt and keycode == KEYCODES["i"]:
            self.finish()
            return None
        if ctrl and alt and keycode == KEYCODES["up"]:
            LOGGER.info("Typing speed: %.0f WPM", self.typer.increase_speed())
            return None
        if ctrl and alt and keycode == KEYCODES["down"]:
            LOGGER.info("Typing speed: %.0f WPM", self.typer.decrease_speed())
            return None
        if ctrl and alt and keycode == 29:
            LOGGER.info("Typing speed: %.0f WPM", self.typer.speed)
            return None

        with self.lock:
            if self.state is State.RECORDING:
                self._capture_key(keycode, flags)
                return None
            if self.state is State.TYPING:
                # Keep physical typing from interleaving with generated output.
                # Replay it after this job finishes or is cancelled.
                self.deferred_keys.setdefault(self.job_id, []).append((keycode, flags))
                return None
        return event

    def start(self) -> None:
        with self.lock:
            if self.state is not State.IDLE:
                return
            self.prompt.clear()
            self.state = State.RECORDING
        if self.settings.prompt_marker:
            type_text(self.settings.prompt_marker)
        LOGGER.info("Recording prompt.")

    def finish(self) -> None:
        with self.lock:
            if self.state is not State.RECORDING:
                return
            prompt = "".join(self.prompt)
            self.state = State.WAITING
            self.job_id += 1
            job_id = self.job_id
            cancel_event = threading.Event()
            self.cancel_event = cancel_event
            self.deferred_keys[job_id] = []

        if prompt.strip() == self.settings.kill_prompt:
            self.stop()
            return
        clipboard_mode = prompt == self.settings.clipboard_type_prompt
        if prompt.strip() == self.settings.clipboard_prompt:
            prompt = self._read_clipboard()
            if not prompt:
                self._set_idle(job_id)
                return
        threading.Thread(
            target=self._process,
            args=(prompt, clipboard_mode, job_id, cancel_event),
            daemon=True,
        ).start()

    def _process(
        self, prompt: str, clipboard_mode: bool, job_id: int,
        cancel_event: threading.Event,
    ) -> None:
        try:
            if cancel_event.is_set():
                return
            if clipboard_mode:
                text = self._read_clipboard()
            else:
                type_text(self.settings.waiting_text)
                if cancel_event.is_set():
                    return
                response = self.service.generate(prompt, cancel_event)
                if response is None or cancel_event.is_set():
                    return
                text = " " + response

            if cancel_event.is_set():
                return
            with self.lock:
                if (
                    self.job_id != job_id
                    or self.state is not State.WAITING
                    or self.state is State.STOPPED
                    or cancel_event.is_set()
                ):
                    return
                self.state = State.TYPING
            completed = self.typer.type(text, cancel_event)
            LOGGER.info("Typing %s.", "completed" if completed else "stopped")
        except Exception:
            if not cancel_event.is_set():
                LOGGER.exception("Could not process command.")
                type_text(",,")
        finally:
            self._replay_deferred_keys(job_id)
            self._set_idle(job_id)

    def _replay_deferred_keys(self, job_id: int) -> None:
        with self.lock:
            queued = self.deferred_keys.pop(job_id, [])
        for keycode, flags in queued:
            down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            if down is None or up is None:
                continue
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventSetFlags(up, flags)
            Quartz.CGEventSetIntegerValueField(down, Quartz.kCGEventSourceUserData, _SYNTHETIC_EVENT_TAG)
            Quartz.CGEventSetIntegerValueField(up, Quartz.kCGEventSourceUserData, _SYNTHETIC_EVENT_TAG)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def _capture_key(self, keycode: int, flags: int) -> None:
        if keycode == KEYCODES["backspace"]:
            if self.prompt:
                self.prompt.pop()
            return
        if keycode == KEYCODES["delete"]:
            return
        if keycode == KEYCODES["space"]:
            self.prompt.append(" ")
            return
        if keycode == KEYCODES["enter"]:
            self.prompt.append("\n")
            return
        if keycode == KEYCODES["tab"]:
            self.prompt.append("\t")
            return
        char = {value: key for key, value in CHAR_KEYCODES.items()}.get(keycode)
        if char is None:
            return
        if flags & Quartz.kCGEventFlagMaskShift:
            char = SHIFTED.get(char, char.upper() if char.isalpha() else char)
        self.prompt.append(char)

    @staticmethod
    def _read_clipboard() -> str:
        return _clipboard_read()

    def _set_idle(self, job_id: int | None = None) -> None:
        with self.lock:
            if self.state is State.STOPPED:
                return
            if job_id is not None and job_id != self.job_id:
                return
            if self.state is not State.RECORDING:
                self.state = State.IDLE

    def stop(self) -> None:
        with self.lock:
            self.state = State.STOPPED
        if self.run_loop is not None:
            Quartz.CFRunLoopStop(self.run_loop)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_environment()
    App(settings, ResponseService(settings)).run()


if __name__ == "__main__":
    main()
