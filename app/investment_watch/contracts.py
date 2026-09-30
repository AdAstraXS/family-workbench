"""Dependency-free contract primitives; no ORM, collection, or model calls."""

from dataclasses import dataclass
from typing import Literal

Direction = Literal["support", "weaken", "mixed", "unknown"]
Method = Literal["rule_candidate", "model", "member"]


@dataclass(frozen=True)
class Scope:
    family_id: int
    owner_id: int

    def require(self, other: "Scope") -> None:
        if self != other:
            raise PermissionError("private_scope_mismatch")


@dataclass(frozen=True)
class EvidenceReference:
    material_id: str
    material_version: int
    quote: str
    locator: str

    def __post_init__(self):
        if not self.material_id or self.material_version < 1:
            raise ValueError("invalid_material_version")
        if not self.quote.strip() or not self.locator.strip():
            raise ValueError("missing_evidence_location")


@dataclass(frozen=True)
class ThesisLink:
    scope: Scope
    thesis_revision_id: int
    assumption_key: str
    direction: Direction
    method: Method
    explanation: str
    references: tuple[EvidenceReference, ...] = ()

    def __post_init__(self):
        import re

        if self.thesis_revision_id < 1 or not re.fullmatch(
            r"(pillar|question):\d+", self.assumption_key
        ):
            raise ValueError("invalid_thesis_reference")
        if self.direction not in {"support", "weaken", "mixed", "unknown"}:
            raise ValueError("invalid_direction")
        if self.method not in {"rule_candidate", "model", "member"}:
            raise ValueError("invalid_method")
        if self.method == "rule_candidate" and self.direction != "unknown":
            raise ValueError("keyword_match_is_not_a_verdict")
        if self.direction != "unknown" and (
            not self.references or not self.explanation.strip()
        ):
            raise ValueError("verdict_requires_evidence_and_reason")

    def is_stale(self, current_revision_id: int) -> bool:
        return self.thesis_revision_id != current_revision_id


def require_version(actual: int, expected: int) -> None:
    if expected < 1 or actual != expected:
        raise ValueError("version_conflict")
