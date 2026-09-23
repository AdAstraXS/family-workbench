"""按指标页所需财年归档并保存历史 10-K；只处理最多两份原件。"""
from django.utils import timezone

from .models import DOC_TYPE_10K, SOURCE_SEC, OfficialResearchDocument
from .sec_content import fetch_sec_document_content
from .services import ResearchValidationError, _require_writer
from .source_sync import _default_sec_client, _upsert_document
from .tenk_metrics import tenk_metric_grid, _document_cik


def fill_tenk_history(*, actor, dossier, version, client=None):
    _require_writer(actor)
    document = version.document
    if (dossier.owner_id != actor.pk or dossier.family_id != actor.family_id or
            document.security_id != dossier.security_id or document.source != SOURCE_SEC or
            document.document_type != DOC_TYPE_10K):
        raise ResearchValidationError("这份年报不属于当前档案。")
    cik = _document_cik(document)
    if cik is None:
        raise ResearchValidationError("当前年报缺少可核查的 SEC CIK。")
    periods, _, problem = tenk_metric_grid(version)
    if problem or len(periods) < 2:
        raise ResearchValidationError(problem or "当前年报没有可补齐的历史财年。")
    client = client or _default_sec_client(dossier.security)
    records = None
    saved = 0
    archived = 0
    missing = []
    for end in periods[1:]:
        candidates = list(OfficialResearchDocument.objects.filter(
            security=dossier.security, source=SOURCE_SEC, document_type=DOC_TYPE_10K,
            period_end=end,
        ))
        if not candidates:
            if records is None:
                records = client.get_filings(str(cik).zfill(10))
            matches = [record for record in records
                       if record["form"] == "10-K" and record["report_date"] == end]
            if len(matches) != 1:
                missing.append(end.year)
                continue
            record = matches[0]
            _upsert_document(security=dossier.security, cik=str(cik).zfill(10),
                             record=record, now=timezone.now())
            candidates = list(OfficialResearchDocument.objects.filter(
                security=dossier.security, source=SOURCE_SEC,
                external_id=record["accession"], document_type=DOC_TYPE_10K,
                period_end=end,
            ))
            archived += 1
        if len(candidates) != 1 or _document_cik(candidates[0]) != cik:
            missing.append(end.year)
            continue
        if candidates[0].content_versions.exists():
            continue
        _, created = fetch_sec_document_content(
            actor=actor, dossier_id=dossier.pk, document_id=candidates[0].pk,
            client=client,
        )
        saved += int(created)
    return {"archived": archived, "saved": saved, "missing": missing}
