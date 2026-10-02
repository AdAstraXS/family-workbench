"""Confirmed candidate questions can be researched without creating a formal judgment."""
from types import SimpleNamespace


def research_basis(dossier):
    if dossier.current_revision_id:
        return dossier.current_revision
    preparation = dossier.preparations.last()
    if not preparation or not (preparation.questions or preparation.hypotheses):
        return None
    return SimpleNamespace(pk=None, revision_number=0, thesis='尚未形成个人判断；以下是用户确认的候选假设与研究问题。',
        pillars=[h['claim'] for h in preparation.hypotheses], questions=preparation.questions,
        hypothesis_context=preparation.hypotheses,
        preparation_id=preparation.pk, preparation_revision=preparation.revision)


def basis_matches(dossier, scope):
    basis = research_basis(dossier)
    if not basis or scope.get('thesis_revision_id') != basis.pk:
        return False
    return bool(basis.pk or (scope.get('preparation_id') == basis.preparation_id and
                            scope.get('preparation_revision') == basis.preparation_revision))
