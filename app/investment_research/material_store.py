"""Append-only, compressed source archive in the NAS PostgreSQL volume."""
import gzip
import hashlib
import json
from django.db import transaction
from django.utils import timezone
from .models import CompanyMaterial, CompanyMaterialVersion


def save_material(security, key, kind, title, *, source_url="", raw=None,
                  data=None, text="", report_date="", media_type="application/json"):
    data = data if data is not None else {}
    if raw is None:
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    with transaction.atomic():
        material, _ = CompanyMaterial.objects.get_or_create(security=security, key=key,
            defaults={"kind": kind, "title": title, "source_url": source_url})
        material = CompanyMaterial.objects.select_for_update().get(pk=material.pk)
        latest = material.versions.first()
        changed = not latest or latest.sha256 != digest or latest.data != data or latest.text != text
        if changed:
            latest = CompanyMaterialVersion.objects.create(material=material,
                number=latest.number + 1 if latest else 1, raw_gzip=gzip.compress(raw),
                sha256=digest, source_url=source_url, media_type=media_type,
                data=data, text=text, report_date=str(report_date or "")[:80])
        material.checked_at = timezone.now()
        material.last_error = ""
        material.title, material.source_url = title, source_url
        material.save(update_fields=["checked_at", "last_error", "title", "source_url", "updated_at"])
    return latest, changed


def record_failure(security, key, kind, title, error):
    CompanyMaterial.objects.update_or_create(security=security, key=key,
        defaults={"kind": kind, "title": title, "checked_at": timezone.now(), "last_error": str(error)[:500]})
