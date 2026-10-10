"""Typed stage outputs."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["verified", "falsified-candidate", "toy-scale", "inconclusive", "blocked-infra"]


class TriageLLM(BaseModel):
    cpu_feasible: bool
    needs_gpu: bool
    needs_large_data: bool = False
    needs_api_keys: bool = False
    est_runtime_minutes: float = Field(ge=0)
    entry_points: list[str] = []
    reason: str


class Claim(BaseModel):
    claim_id: str
    metric: str
    value: float
    unit: str = ""
    higher_is_better: Optional[bool] = None
    setting: str = ""
    location: str = ""
    quote: str = ""


class ClaimSet(BaseModel):
    claims: list[Claim] = []


class ClaimTarget(BaseModel):
    """What the builder is allowed to see: never the value."""
    claim_id: str
    metric: str
    setting: str
    unit_hint: str = ""


class LLMExtraction(BaseModel):
    found: bool
    value: Optional[float] = None
    evidence_line: str = ""
    reason: str = ""


class PlaybookRule(BaseModel):
    rule_id: str
    category: str
    pattern: str
    advice: str
    source_failures: int = 0


class Controller(BaseModel):
    """Policy knobs enforced by the harness (not just advice). Bounds are checked by the gate."""
    max_turns: int = Field(18, ge=10, le=30)
    run_by_turn: int = Field(8, ge=3, le=20)      # first non-read command executing repo code
    script_by_turn: int = Field(12, ge=4, le=26)  # /work/paperci_run.sh must exist
    single_target: bool = True                    # aim for one claim first, submit, done
    rationale: str = ""


class Playbook(BaseModel):
    generation: int = 0
    rules: list[PlaybookRule] = []
    notes: str = ""
    image: str = ""  # learned sandbox base image for this generation (envlearn)
    controller: Optional[Controller] = None


class ImproverProposal(BaseModel):
    rules: list[PlaybookRule]
    rationale: str
    controller: Optional[Controller] = None
