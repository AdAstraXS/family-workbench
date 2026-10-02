"""Freeze selected news into a new research packet; never edit saved analyses."""

from django.db.models import F
from .models import ResearchCandidate
from .services import digest


def selected_candidates(dossier):
    return (
        ResearchCandidate.objects.filter(
            dossier=dossier,
            selected_for_research=True,
            revision_id=dossier.current_revision_id,
            material_version_id=F("material_version__material__current_version_id"),
            material_version__material__source__family_id=dossier.family_id,
        )
        .exclude(material_version__status="withdrawn")
        .select_related("material_version__material__source")
    )


def append_news(packet, dossier, excluded_versions=None):
    from .events import canonical_version

    packet = {
        **packet,
        "evidence": list(packet["evidence"]),
        "sources": list(packet["sources"]),
    }
    snapshots = []
    seen = set()
    # Do not count imported official records a second time.
    candidates = list(selected_candidates(dossier).order_by("-pk"))
    for candidate in candidates:
        version = candidate.material_version
        if version.pk in (excluded_versions or set()):
            continue
        canonical = canonical_version(version)
        if canonical.pk in (excluded_versions or set()):
            continue
        chain = canonical.original_chain
        if canonical.material.official_document_id or chain in seen:
            continue
        if len(snapshots) >= 10:
            continue
        seen.add(chain)
        version = canonical
        from .models import BodySnapshot
        body = BodySnapshot.objects.filter(material_version=version).first()
        text = version.title + "\n" + (body.text if body else version.summary)
        # The synthesis input limit rejects oversized packets explicitly; never
        # silently discard the saved body or claim to have sent a full article.
        frozen = text if body else text[:1500]
        citation = {
            "kind": "news",
            "version_id": version.pk,
            "material_id": version.material_id,
            "document_id": None,
            "start": 0,
            "end": len(frozen),
            "hash": digest(frozen),
            "body_id": body.pk if body else None,
            "body_hash": body.content_hash if body else None,
        }
        packet["evidence"].append(
            {
                "id": f"E{len(packet['evidence']) + 1}",
                "text": f"新闻来源 {version.material.source.name}；发布 {version.published_at or '未知'}；仅为来源摘录：{frozen}",
                "citations": [citation],
            }
        )
        snapshots.append(
            {
                "candidate_id": candidate.pk,
                "material_id": version.material_id,
                "version_id": version.pk,
                "title": version.title,
                "excerpt": frozen,
                "body_id": body.pk if body else None,
                "body_hash": body.content_hash if body else None,
                "content_depth": "body" if body else "summary",
                "url": version.url,
                "source": version.material.source.name,
                "original_chain": version.original_chain,
                "published_at": version.published_at.isoformat()
                if version.published_at
                else None,
            }
        )
    packet["news_snapshots"] = snapshots
    packet["news_selection_count"] = len(candidates)
    return packet
