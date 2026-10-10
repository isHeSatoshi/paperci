"""Central configuration. Every knob can be overridden with a PAPERCI_* env var."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
REGISTRY_DIR = ROOT / "registry"
for _d in (DATA_DIR, RUNS_DIR, REGISTRY_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("PAPERCI_DB", str(DATA_DIR / "paperci.sqlite")))


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "https://litellm.vision.aivar.app/v1")

# Model routing. Builder is the only "strong" model. Verifier/claims use a
# different model family (Amazon Nova) than the builder/solver (Anthropic Claude).
MODELS = {
    "triage": os.getenv("PAPERCI_MODEL_TRIAGE", "nova-micro"),
    "claims": os.getenv("PAPERCI_MODEL_CLAIMS", "nova-pro"),
    "builder": os.getenv("PAPERCI_MODEL_BUILDER", "claude-sonnet-4.6"),
    "verifier": os.getenv("PAPERCI_MODEL_VERIFIER", "nova-pro"),
    "solver": os.getenv("PAPERCI_MODEL_SOLVER", "claude-haiku"),
}
MODEL_FAMILY = {
    "nova-micro": "amazon-nova", "nova-lite": "amazon-nova", "nova-pro": "amazon-nova",
    "nova-2-lite": "amazon-nova", "claude-haiku": "anthropic", "claude-sonnet-4.6": "anthropic",
    "claude-opus-4.6": "anthropic",
}
FORBIDDEN_MODELS = {"claude-opus-4.6"}

# USD per token, read from the LiteLLM /v1/model/info endpoint on 2026-10-11.
# cache_read / cache_write only apply to Claude models.
PRICES = {
    "nova-micro": {"in": 3.5e-08, "out": 1.4e-07},
    "nova-lite": {"in": 6e-08, "out": 2.4e-07},
    "nova-pro": {"in": 8e-07, "out": 3.2e-06},
    "nova-2-lite": {"in": 3.3e-07, "out": 2.75e-06},
    "claude-haiku": {"in": 1.1e-06, "out": 5.5e-06, "cache_read": 1.1e-07, "cache_write": 1.375e-06},
    "claude-sonnet-4.6": {"in": 3.3e-06, "out": 1.65e-05, "cache_read": 3.3e-07, "cache_write": 4.125e-06},
    "claude-opus-4.6": {"in": 5.5e-06, "out": 2.75e-05},
}

# Budget. The key has a $20 / 30 day allowance shared by the team.
GLOBAL_BUDGET_USD = _f("PAPERCI_GLOBAL_BUDGET", 20.0)
DEMO_RESERVE_USD = _f("PAPERCI_DEMO_RESERVE", 3.0)
# Our own hard stop, below the provider limit, leaves room for teammates/mistakes.
HARD_STOP_USD = _f("PAPERCI_HARD_STOP", 16.0)
PER_PAPER_CAP_USD = _f("PAPERCI_PER_PAPER_CAP", 0.60)
BUILDER_CAP_USD = _f("PAPERCI_BUILDER_CAP", 0.45)
SOLVER_ATTEMPT_CAP_USD = _f("PAPERCI_SOLVER_CAP", 0.12)

# Iteration / time caps.
BUILDER_MAX_TURNS = _i("PAPERCI_BUILDER_MAX_TURNS", 18)
SOLVER_MAX_TURNS = _i("PAPERCI_SOLVER_MAX_TURNS", 12)
CMD_TIMEOUT_SEC = _i("PAPERCI_CMD_TIMEOUT", 600)
RUN_TIMEOUT_SEC = _i("PAPERCI_RUN_TIMEOUT", 720)
PAPER_WALL_SEC = _i("PAPERCI_PAPER_WALL", 45 * 60)
LLM_MAX_RETRIES = _i("PAPERCI_LLM_RETRIES", 3)
STAGE_MAX_ATTEMPTS = _i("PAPERCI_STAGE_ATTEMPTS", 2)
ROBUST_RERUNS = _i("PAPERCI_ROBUST_RERUNS", 2)
SOLVER_ATTEMPTS = _i("PAPERCI_SOLVER_ATTEMPTS", 2)

# Sandbox resources.
SANDBOX_IMAGE = os.getenv("PAPERCI_SANDBOX_IMAGE", "paperci/sandbox:base")
PROXY_IMAGE = "paperci/proxy:latest"
SANDBOX_CPUS = os.getenv("PAPERCI_SANDBOX_CPUS", "4")
SANDBOX_MEM = os.getenv("PAPERCI_SANDBOX_MEM", "6g")
SANDBOX_PIDS = os.getenv("PAPERCI_SANDBOX_PIDS", "1024")
INTERNAL_NET = "paperci-int"
PROXY_NAME = "paperci-egress"
MAX_REPO_MB = _i("PAPERCI_MAX_REPO_MB", 150)

# Recovery strategy: "v1" = raw tool output only (baseline), "v2" = failure
# taxonomy + recovery playbook + stall breaker (mined from v1 traces).
BUILDER_VARIANT = os.getenv("PAPERCI_BUILDER_VARIANT", "v2")
