"""CI-style live progress for `paperci review`: one line per pipeline step, streamed as it happens.
Silent unless enabled, so batch runs and logs are unchanged."""
from __future__ import annotations

import os
import sys
import time

_on = os.environ.get("PAPERCI_LIVE") == "1"
_t0 = time.time()
if os.name == "nt":
    os.system("")  # enables ANSI escape processing in the legacy Windows console

C = {"dim": "\033[2m", "bold": "\033[1m", "green": "\033[32m", "red": "\033[31m", "yellow": "\033[33m",
     "cyan": "\033[36m", "magenta": "\033[35m", "reset": "\033[0m"}
GLYPH = {"start": ("cyan", ">"), "ok": ("green", "+"), "fail": ("red", "x"), "warn": ("yellow", "!"),
         "heal": ("magenta", "~"), "info": ("dim", "."), "resume": ("dim", "=")}


def enable() -> None:
    global _on, _t0
    _on, _t0 = True, time.time()
    os.environ["PAPERCI_LIVE"] = "1"


def enabled() -> bool:
    return _on


def emit(kind: str, text: str, indent: int = 0) -> None:
    if not _on:
        return
    color, g = GLYPH.get(kind, ("dim", "."))
    t = time.time() - _t0
    line = f"{C['dim']}{int(t // 60):02d}:{int(t % 60):02d}{C['reset']} {'  ' * indent}{C[color]}{g}{C['reset']} {text}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)


def header(text: str) -> None:
    if _on:
        print(f"\n{C['bold']}{text}{C['reset']}", flush=True)


def short(cmd: str, n: int = 90) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= n else cmd[: n - 3] + "..."


def write(text: str = "") -> None:
    if _on:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
