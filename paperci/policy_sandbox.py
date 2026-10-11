"""Loads builder control policies (paperci/stages/builder_policy.py and evolved versions) under a contract.

A policy is Python written by an LLM, so it is never imported. It is parsed, checked against a strict
allowlist, then executed in a namespace with a handful of safe builtins. This is defence in depth, not a
security boundary on its own: evolved policies also need a human approval before they run live, and every
evaluation run is checked for tampering with the referee afterwards (paperci/evolve.py)."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import time
from pathlib import Path

from .config import DATA_DIR

BASELINE_PATH = Path(__file__).resolve().parent / "stages" / "builder_policy.py"
ACTIVE_PATH = DATA_DIR / "builder_policy.py"
FUNCS = ("system_addendum", "milestone", "late_nudge")
STATE_KEYS = {"turn", "max_turns", "run_by_turn", "script_by_turn", "single_target", "controller",
              "submit_attempts", "fired", "actions", "failures"}
ACTION_KEYS = {"type", "cmd", "exit", "path"}
RESULT_KEYS = {"key", "message"}
SAFE_BUILTINS = {"len": len, "any": any, "all": all, "min": min, "max": max, "sum": sum, "sorted": sorted,
                 "str": str, "int": int, "float": float, "bool": bool, "dict": dict, "list": list, "set": set,
                 "tuple": tuple, "enumerate": enumerate, "zip": zip, "isinstance": isinstance, "reversed": reversed,
                 "abs": abs, "round": round, "True": True, "False": False, "None": None}
BANNED_NAMES = {"open", "exec", "eval", "compile", "globals", "locals", "vars", "getattr", "setattr", "delattr",
                "input", "breakpoint", "memoryview", "type", "object", "super", "range", "iter", "next",
                "os", "sys", "subprocess", "socket", "builtins", "importlib", "print", "help", "classmethod",
                "staticmethod", "property", "bytes", "bytearray"}
# Same spirit as the improver's integrity filter: a policy may steer the builder, never toward faking results.
BANNED_TEXT = re.compile(r"hard-?cod|fake|fabricat|skip (the )?verif|copy .*result|precomputed|print (the )?expected|"
                         r"sealed|guess|claimed value|tolerance", re.I)
MAX_SOURCE = 12000
MAX_MESSAGE = 1500


class PolicyError(Exception):
    pass


def policy_id(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def check(source: str) -> list[str]:
    """Static contract check. Returns a list of violations (empty means allowed)."""
    if len(source) > MAX_SOURCE:
        return [f"source too long ({len(source)} > {MAX_SOURCE} chars)"]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    bad: list[str] = []
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for f in FUNCS:
        if f not in defined:
            bad.append(f"missing function {f}(state)")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets) \
                and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # docstring
        bad.append(f"line {node.lineno}: only functions, constant assignments and docstrings allowed at top level")
    # Docstrings are never reachable at runtime (no dunder access), so they may describe the contract freely.
    docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if id(node) in docs:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bad.append(f"line {line}: imports are not allowed")
        elif isinstance(node, (ast.While, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal, ast.ClassDef,
                               ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Delete, ast.Lambda, ast.Yield,
                               ast.YieldFrom)):
            bad.append(f"line {line}: {type(node).__name__} is not allowed")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            bad.append(f"line {line}: exponentiation is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                bad.append(f"line {line}: attribute {node.attr} is not allowed")
            elif not (isinstance(node.value, ast.Name) and node.value.id == "re") and node.attr not in \
                    {"get", "lower", "upper", "strip", "startswith", "endswith", "split", "join", "count",
                     "replace", "find", "append", "items", "keys", "values", "search", "match", "fullmatch", "findall",
                     "group"}:
                bad.append(f"line {line}: method .{node.attr} is not allowed")
        elif isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id in BANNED_NAMES:
                bad.append(f"line {line}: name {node.id} is not allowed")
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and "__" in node.value:
                bad.append(f"line {line}: dunder text is not allowed")
            elif isinstance(node.value, str) and BANNED_TEXT.search(node.value):
                bad.append(f"line {line}: text violates the integrity policy ({BANNED_TEXT.search(node.value).group(0)!r})")
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool) and abs(node.value) > 10000:
                bad.append(f"line {line}: numeric constant {node.value} too large")
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            if node.slice.value not in STATE_KEYS | ACTION_KEYS | RESULT_KEYS:
                bad.append(f"line {line}: unknown state key {node.slice.value!r}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if node.args[0].value not in STATE_KEYS | ACTION_KEYS | RESULT_KEYS:
                bad.append(f"line {line}: unknown state key {node.args[0].value!r}")
    return sorted(set(bad))


class Policy:
    def __init__(self, source: str, origin: str):
        problems = check(source)
        if problems:
            raise PolicyError("; ".join(problems[:5]))
        self.source, self.origin, self.id = source, origin, policy_id(source)
        ns: dict = {"__builtins__": SAFE_BUILTINS, "re": re}
        exec(compile(source, f"<policy {self.id}>", "exec"), ns)  # noqa: S102 - checked source, restricted builtins
        self.name = str(ns.get("POLICY_NAME", self.id))[:60]
        self.fns = {f: ns[f] for f in FUNCS}

    def call(self, fn: str, state: dict):
        out = self.fns[fn](_freeze(state))
        if fn == "milestone":
            if out is None:
                return None
            if not (isinstance(out, dict) and isinstance(out.get("key"), str) and isinstance(out.get("message"), str)):
                raise PolicyError("milestone must return None or {'key': str, 'message': str}")
            return {"key": out["key"][:40], "message": out["message"][:MAX_MESSAGE]}
        if out is None or out == "":
            return None if fn != "system_addendum" else ""
        if not isinstance(out, str):
            raise PolicyError(f"{fn} must return a string or None")
        return out[:MAX_MESSAGE]


def _freeze(state: dict) -> dict:
    """A fresh copy restricted to contract keys, so a policy cannot keep or mutate harness state."""
    s = {k: state.get(k) for k in STATE_KEYS}
    s["actions"] = [{k: a.get(k) for k in ACTION_KEYS if k in a} for a in state.get("actions", [])]
    s["fired"] = list(state.get("fired", []))
    s["failures"] = list(state.get("failures", []))
    return s


def baseline() -> Policy:
    return Policy(BASELINE_PATH.read_text(encoding="utf-8"), "baseline")


def load_active() -> Policy:
    """Evaluation override (PAPERCI_POLICY_PATH), else the approved policy in data/, else the baseline.
    Anything that fails the contract falls back to the baseline instead of breaking the build."""
    for origin, p in (("candidate", os.environ.get("PAPERCI_POLICY_PATH")), ("approved", str(ACTIVE_PATH))):
        if p and Path(p).exists():
            try:
                return Policy(Path(p).read_text(encoding="utf-8"), origin)
            except Exception:
                continue
    return baseline()


def synthetic_states() -> list[dict]:
    """States a builder can be in, for the contract test: early, stuck reading, mid-run, failing, late."""
    reads = [{"type": "shell", "cmd": c, "exit": 0} for c in ("ls /work/repo", "cat README.md", "grep -r main .")]
    runs = [{"type": "shell", "cmd": "python train.py --epochs 1", "exit": 1},
            {"type": "shell", "cmd": "pip install -r requirements.txt", "exit": 0}]
    write = [{"type": "write", "path": "/work/paperci_run.sh"}]
    out = []
    for mt in (12, 18, 30):
        for t in range(1, mt + 1):
            for acts, fails in ((reads[: min(t, 3)], []), (reads + runs, ["missing_module"]),
                                (reads + runs + write, ["missing_module", "data_missing"]),
                                (reads * 4, ["timeout", "timeout", "timeout"])):
                out.append({"turn": t, "max_turns": mt, "run_by_turn": min(8, mt - 2), "script_by_turn": min(12, mt - 1),
                            "single_target": True, "controller": t % 2 == 0, "submit_attempts": int(t > mt - 3),
                            "fired": ["run"] if t > 9 else [], "actions": acts, "failures": fails})
    return out


def contract_test(source: str) -> list[str]:
    """Static check, then run every function on synthetic states: right types, bounded output, fast,
    deterministic. Returns violations."""
    bad = check(source)
    if bad:
        return bad
    try:
        a, b = Policy(source, "test"), Policy(source, "test")
    except Exception as e:
        return [f"load failed: {type(e).__name__}: {e}"]
    t0 = time.time()
    for st in synthetic_states():
        for fn in FUNCS:
            try:
                r1, r2 = a.call(fn, st), b.call(fn, st)
            except Exception as e:
                return [f"{fn} raised {type(e).__name__}: {str(e)[:120]} at turn {st['turn']}"]
            if r1 != r2:
                return [f"{fn} is not deterministic"]
            text = r1["message"] if isinstance(r1, dict) else (r1 or "")
            if BANNED_TEXT.search(text):
                return [f"{fn} produced text that violates the integrity policy"]
        if time.time() - t0 > 5:
            return ["too slow: contract test exceeded 5 s"]
    return []
