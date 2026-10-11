"""Failure taxonomy (always on, for measurement) and the recovery playbook
(variant v2 only). The playbook is data written by the improver agent
(paperci/improver.py) from traced failures, not hand-written code."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .config import DATA_DIR
from .schemas import Playbook

PLAYBOOK_PATH = DATA_DIR / "playbook.json"

# Ordered: first match wins for the primary category.
TAXONOMY: list[tuple[str, str]] = [
    ("proxy_unreachable", r"Could not resolve proxy|Failed to connect to paperci-egress|Cannot connect to proxy"),
    ("egress_denied", r"Tunnel connection failed: 403|403 Filtered|ProxyError|Access denied by proxy|Filtered"),
    ("no_network_at_run", r"Temporary failure in name resolution|Name or service not known|Network is unreachable|Failed to establish a new connection|nodename nor servname"),
    ("cuda_required", r"Torch not compiled with CUDA|no CUDA GPUs|CUDA driver|cuda\(\)|nvidia-smi: not found|NCCL|AssertionError: Torch not compiled"),
    ("missing_module", r"ModuleNotFoundError: No module named|ImportError: cannot import name|ImportError: No module named"),
    ("pip_resolution", r"ResolutionImpossible|No matching distribution found|Could not find a version that satisfies|conflicting dependencies"),
    ("native_build", r"Failed building wheel|error: command '.*gcc|subprocess-exited-with-error|metadata-generation-failed"),
    ("python_version", r"requires a different Python|Requires-Python|SyntaxError: invalid syntax|is not supported on this Python"),
    ("api_incompat", r"AttributeError: module '[^']+' has no attribute|TypeError: .*unexpected keyword argument|has been removed|DeprecationWarning.*removed"),
    ("data_missing", r"FileNotFoundError|No such file or directory|does not exist"),
    ("oom", r"MemoryError|Killed|out of memory|std::bad_alloc"),
    ("timeout", r"\[host timeout\]|timed out after|exit=124"),
    ("python_error", r"Traceback \(most recent call last\)"),
]
_COMPILED = [(c, re.compile(p, re.I)) for c, p in TAXONOMY]


_MASKED = re.compile(r"^(Traceback \(most recent call last\)|ERROR: (Could not|No matching)|"
                     r"(ModuleNotFound|FileNotFound|Runtime|Import|Memory)Error: )|Tunnel connection failed: 403|ProxyError", re.M)
_READ_ONLY = re.compile(r"^\s*(ls|cat|head|tail|grep|rg|find|wc|sed\s+-n|less|tree|file|du)\b")


def classify(output: str, exit_code: int, cmd: str = "") -> str | None:
    if exit_code == 0:
        # `cmd | tail` returns tail's exit code; look for unmistakable errors anyway,
        # but never in commands that merely display files (source code mentions errors).
        if _READ_ONLY.match(cmd) or not _MASKED.search(output[-6000:]):
            return None
        for cat, rx in _COMPILED:
            if rx.search(output[-6000:]):
                return cat
        return None
    if exit_code == 124:
        return "timeout"
    if exit_code == 137:
        return "oom"
    tail = output[-6000:]
    for cat, rx in _COMPILED:
        if rx.search(tail):
            return cat
    return "nonzero_exit"


def load_playbook() -> Playbook:
    if PLAYBOOK_PATH.exists():
        return Playbook.model_validate_json(PLAYBOOK_PATH.read_text(encoding="utf-8"))
    return Playbook()


def save_playbook(pb: Playbook) -> None:
    PLAYBOOK_PATH.write_text(pb.model_dump_json(indent=2), encoding="utf-8")


class Recovery:
    """Per-build recovery state: matches playbook rules and breaks stalls."""

    def __init__(self, variant: str):
        self.variant = variant
        self.playbook = load_playbook() if variant != "v1" else Playbook()
        self._rx = []
        for r in self.playbook.rules:
            try:
                self._rx.append((r, re.compile(r.pattern, re.I)))
            except re.error:
                continue
        self.counts: Counter[str] = Counter()
        self.used_rules: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.variant != "v1"

    def guidance(self, category: str | None, output: str) -> str:
        if not category or not self.enabled:
            return ""
        self.counts[category] += 1
        parts = []
        for rule, rx in self._rx:
            if (rule.category == category or rx.search(output[-6000:])) and rule.rule_id not in self.used_rules[-3:]:
                parts.append(f"[playbook {rule.rule_id}] {rule.advice}")
                self.used_rules.append(rule.rule_id)
                if len(parts) >= 2:
                    break
        if self.counts[category] >= 3:
            parts.append(f"[stall breaker] '{category}' has now happened {self.counts[category]} times. "
                         "Do not repeat the same fix. Change strategy (different entry point, smaller "
                         "config, replace the failing component) or call give_up with a precise reason.")
        return ("\n\nRECOVERY GUIDANCE (failure class: " + category + ")\n" + "\n".join(parts)) if parts else ""

    def prompt_prior(self) -> str:
        """Short list of learned lessons, shown up front in v2."""
        if not self.enabled or not self.playbook.rules:
            return ""
        lines = [f"- ({r.category}) {r.advice}" for r in self.playbook.rules[:12]]
        return ("\nLESSONS LEARNED FROM PREVIOUS PAPERS (playbook generation "
                f"{self.playbook.generation}):\n" + "\n".join(lines) + "\n")


def snippet_for(output: str, category: str) -> str:
    for cat, rx in _COMPILED:
        if cat == category:
            m = rx.search(output)
            if m:
                s = max(0, m.start() - 300)
                return output[s:m.end() + 300]
    return output[-600:]


def dump_counts(rows: list[dict]) -> str:
    return json.dumps(Counter(r["category"] for r in rows).most_common(), indent=0)
