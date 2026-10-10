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


class Playbook(BaseModel):
    generation: int = 0
    rules: list[PlaybookRule] = []
    notes: str = ""
    image: str = ""  # learned sandbox base image for this generation (envlearn)


class ImproverProposal(BaseModel):
    rules: list[PlaybookRule]
    rationale: str
