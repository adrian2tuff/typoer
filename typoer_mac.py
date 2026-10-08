"""
Mac-native global hotkey prompt typer.

Install dependencies with: python3 -m pip install -r requirements.txt
Set OPENAI_API_KEY in your environment before running.

Hotkeys:
  Ctrl+Option+U      Start capturing a prompt
  Ctrl+Option+I      Send the prompt
  Escape             Stop generated typing
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
    model: str = "gpt-5.6"
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


def post_key(keycode: int, down: bool, shift: bool = False) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    if event is None:
        raise RuntimeError("Could not create keyboard event")
    Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskShift if shift else 0)
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

    def type(self, text: str) -> bool:
        seconds_per_character = 12 / self._settings.typing_wpm
        minimum_delay = seconds_per_character * 0.2
        maximum_delay = seconds_per_character * 1.8
        position = 0
        mistakes = 0

        while position < len(text):
            if self._stop_requested():
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

    def generate(self, prompt: str) -> str:
        if self._client is None:
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            self._client = OpenAI(timeout=self._settings.openai_timeout)

        request: dict[str, Any] = {
            "model": self._settings.model,
            "instructions": self._settings.system_prompt,
            "input": prompt,
            "service_tier": self._settings.service_tier,
        }
        if self._settings.reasoning_effort:
            request["reasoning"] = {"effort": self._settings.reasoning_effort}
        response = self._client.responses.create(**request)
        if not response.output_text:
            raise RuntimeError("The OpenAI response did not contain text")
        return response.output_text


class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    WAITING = auto()
    STOPPED = auto()


class App:
    def __init__(self, settings: Settings, service: ResponseService) -> None:
        self.settings = settings
        self.service = service
        self.typer = HumanTyper(settings)
        self.state = State.IDLE
        self.lock = threading.RLock()
        self.prompt: list[str] = []
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
        print("Escape = stop typing")
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

        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        ctrl = bool(flags & Quartz.kCGEventFlagMaskControl)
        alt = bool(flags & Quartz.kCGEventFlagMaskAlternate)

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

        if prompt.strip() == self.settings.kill_prompt:
            self.stop()
            return
        clipboard_mode = prompt == self.settings.clipboard_type_prompt
        if prompt.strip() == self.settings.clipboard_prompt:
            prompt = self._read_clipboard()
            if not prompt:
                self._set_idle()
                return
        threading.Thread(target=self._process, args=(prompt, clipboard_mode), daemon=True).start()

    def _process(self, prompt: str, clipboard_mode: bool) -> None:
        try:
            if clipboard_mode:
                text = self._read_clipboard()
            else:
                type_text(self.settings.waiting_text)
                text = " " + self.service.generate(prompt)
            completed = self.typer.type(text)
            LOGGER.info("Typing %s.", "completed" if completed else "stopped")
        except Exception:
            LOGGER.exception("Could not process command.")
            type_text(",,")
        finally:
            self._set_idle()

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

    def _set_idle(self) -> None:
        with self.lock:
            if self.state is not State.STOPPED:
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
