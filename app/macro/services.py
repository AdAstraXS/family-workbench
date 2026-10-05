import json
import subprocess
import sys

from django.db import transaction
from django.utils import timezone

from .adapters import SourceError, digest, parse_frame, parse_fred, parse_official
from .models import MacroImportRun, MacroIndicator, MacroObservation, MacroObservationRevision, MacroSourceMapping
from .registry import GROUPS, OFFICIAL_GROUPS


def fetch_page(url):
    from .http_worker import validate_page_url
    try:
        validate_page_url(url)
    except ValueError as exc:
        raise SourceError("官方目录或日历地址不在登记范围") from exc
    for attempt in range(2):
        try:
            result = subprocess.run([sys.executable, "-m", "macro.http_worker"], input=json.dumps({"url": url}),
                capture_output=True, encoding="utf-8", timeout=70, check=True)
            return json.loads(result.stdout)["text"]
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as exc:
            if attempt:
                raise SourceError("官方目录或日历请求失败或超时，已重试一次；保留上次数据") from exc


def fetch_source(group, url=""):
    for attempt in range(2):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "macro.fetch_worker"],
                input=json.dumps({"group": group, "url": url}), capture_output=True,
                encoding="utf-8", timeout=90, check=True,
            )
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            if attempt:
                raise SourceError("来源请求失败或超时，已重试一次；请检查网络和来源格式") from exc


def prepare(group, payload):
    specs = GROUPS[group]
    if specs[0].provider == "fred":
        points = parse_fred(payload["text"], specs[0])
    elif group in OFFICIAL_GROUPS:
        points = parse_official(payload["text"], group)
    else:
        points = parse_frame(payload["rows"], specs)
    if not points or not any(point.value is not None for point in points):
        raise SourceError("来源为空或全部缺值，未导入")
    seen = set()
    codes = {spec.code for spec in specs}
    by_code = {spec.code: spec for spec in specs}
    for point in points:
        key = (point.code, point.geography, point.period)
        if key in seen:
            raise SourceError("来源存在重复统计期/地区，未导入")
        if point.code not in codes or point.period > timezone.localdate():
            raise SourceError("来源包含未登记指标或未来统计期")
        frequency = by_code[point.code].frequency
        if ((frequency in {"月度", "季度", "年度"} and point.period.day != 1)
                or (frequency == "季度" and point.period.month not in {1, 4, 7, 10})
                or (frequency == "年度" and point.period.month != 1)):
            raise SourceError("统计期与登记频率不一致")
        if point.release_date and point.release_date < point.period:
            raise SourceError("发布日期早于统计期，请核验来源")
        if point.release_date and point.release_date > timezone.localdate():
            raise SourceError("发布日期为未来日期，不能标记为已发布")
        seen.add(key)
    return points


def mapping_for(spec):
    indicator, created = MacroIndicator.objects.get_or_create(country=spec.country, code=spec.code, defaults={
        "name": spec.name, "category": spec.category, "frequency": spec.frequency,
        "unit": spec.unit, "source": spec.provider,
    })
    if not created and (indicator.unit != spec.unit or indicator.frequency != spec.frequency):
        raise SourceError(f"已有指标口径冲突：{spec.code}；请先核验，不能覆盖")
    mapping, _ = MacroSourceMapping.objects.get_or_create(indicator=indicator, defaults={
        "provider": spec.provider, "group": spec.group, "definition": spec.definition(),
        "definition_hash": digest(spec.definition()),
    })
    # All writers acquire the same mapping locks in code order. Different groups have disjoint codes.
    mapping = MacroSourceMapping.objects.select_for_update().get(pk=mapping.pk)
    if mapping.definition_hash != digest(spec.definition()):
        raise SourceError(f"指标字典变化：{spec.code}；需人工核验或使用新代码")
    return mapping


def import_group(group, *, write=False, url="", fetcher=None):
    if group not in GROUPS:
        raise SourceError("未知采集组")
    if group in OFFICIAL_GROUPS and not url:
        raise SourceError("官方发布稿采集需要明确的 --url")
    run = MacroImportRun.objects.create(group=group) if write else None
    try:
        payload = (fetcher or fetch_source)(group, url)
        points = prepare(group, payload)
        summary = {"points": len(points), "missing": sum(p.value is None for p in points),
                   "first_period": str(min(p.period for p in points)), "latest_period": str(max(p.period for p in points)),
                   "created": 0, "revised": 0, "unchanged": 0}
        if not write:
            return summary
        source_hash = digest(payload)
        with transaction.atomic():
            mappings = {s.code: mapping_for(s) for s in sorted(GROUPS[group], key=lambda s: s.code)}
            if MacroImportRun.objects.filter(group=group, status="success", started_at__gt=run.started_at).exists():
                raise SourceError("较新的采集已完成，本次较早启动的任务不再覆盖数据")
            existing = {(p.mapping_id, p.geography, p.period_date): p for p in MacroObservation.objects.filter(mapping__in=mappings.values())}
            from .publications import verified_release_dates
            publications = {code: verified_release_dates(mapping) for code, mapping in mappings.items()}
            now = timezone.now()
            for point in points:
                mapping = mappings[point.code]
                if not mapping.indicator.is_active:
                    raise SourceError(f"指标已停用：{point.code}")
                current = existing.get((mapping.pk, point.geography, point.period))
                publication = publications[point.code].get(point.period)
                release_date = point.release_date or (publication.release_date if publication else None) or (current.release_date if current else None)
                fingerprint = digest({"value": str(point.value), "release": str(release_date),
                                      "definition": mapping.definition_hash, "notes": point.evidence.get("source_notes", "")})
                if current is None:
                    current = MacroObservation.objects.create(mapping=mapping, geography=point.geography,
                        period_date=point.period, value=point.value, release_date=release_date,
                        last_seen_at=now, fingerprint=fingerprint)
                    summary["created"] += 1
                elif current.fingerprint != fingerprint:
                    current.value = point.value
                    current.release_date = release_date
                    current.revision += 1
                    current.fingerprint = fingerprint
                    current.last_seen_at = now
                    current.save(update_fields=["value", "release_date", "revision", "fingerprint", "last_seen_at"])
                    summary["revised"] += 1
                else:
                    summary["unchanged"] += 1
                    continue
                MacroObservationRevision.objects.create(observation=current, run=run, number=current.revision,
                    value=point.value, release_date=release_date, source_url=payload["url"], source_hash=source_hash,
                    evidence={"row": point.evidence, "definition": mapping.definition,
                              "library_version": payload.get("library_version", ""),
                              **({"publication": {"url": publication.source_url, "hash": publication.source_hash,
                                   "evidence": publication.evidence}} if publication else {})})
            # Only returned observations were verified, not omitted historical rows.
            checked_ids = [existing[(mappings[p.code].pk, p.geography, p.period)].pk for p in points
                           if (mappings[p.code].pk, p.geography, p.period) in existing]
            MacroObservation.objects.filter(pk__in=checked_ids).update(last_seen_at=now)
            run.status, run.summary, run.finished_at = "success", summary, now
            run.save(update_fields=["status", "summary", "finished_at"])
        return summary
    except Exception as exc:
        if run:
            run.status, run.finished_at = "failed", timezone.now()
            run.error = str(exc)[:500] if isinstance(exc, SourceError) else "导入失败（" + type(exc).__name__ + "）；数据事务已回滚"
            run.save(update_fields=["status", "finished_at", "error"])
        if isinstance(exc, SourceError):
            raise
        raise SourceError("采集或入库异常（" + type(exc).__name__ + "），未完成导入") from exc
