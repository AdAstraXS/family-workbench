"""Design-time protocol examples; runtime entry points live in services/views."""

from typing import Iterable, Protocol
from .contracts import EvidenceReference, Scope, ThesisLink


class MaterialReader(Protocol):
    def references(
        self, scope: Scope, dossier_id: int
    ) -> Iterable[EvidenceReference]: ...


class NewsPoolReader(Protocol):
    # Apply source visibility, without requiring a dossier or personal watch rule.
    def material_ids(self, scope: Scope) -> Iterable[str]: ...


class ResearchCandidateWriter(Protocol):
    # Validate ownership/version and return an idempotent pending candidate ID.
    # Association does not create a verdict or modify an analysis snapshot.
    def associate(
        self,
        scope: Scope,
        dossier_id: int,
        material_id: str,
        material_version: int,
        expected_revision: int,
        idempotency_key: str,
    ) -> str: ...


class ThesisAnalyzer(Protocol):
    def analyze(
        self, scope: Scope, revision_id: int, references: tuple[EvidenceReference, ...]
    ) -> tuple[ThesisLink, ...]: ...


class BudgetReservation(Protocol):
    # Implement with a transaction and Decimal before enabling any paid request.
    def reserve(self, scope: Scope, idempotency_key: str, maximum_cny: str) -> str: ...
