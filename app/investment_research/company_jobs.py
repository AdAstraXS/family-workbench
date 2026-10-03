"""Durable, bounded on-demand acquisition with isolated per-item retries."""
import os
import subprocess
import sys
from pathlib import Path
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import CompanyAcquisitionJob, CompanyMaterial, ResearchDossier
from .services import _require_writer, ResearchValidationError
from .company_sources import SOURCE_TASKS

ROOT = Path(__file__).resolve().parent.parent


def launch(job_id):
    kwargs = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
    try:
        subprocess.Popen([sys.executable, "manage.py", "run_company_acquisition", str(job_id)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
    except OSError:
        CompanyAcquisitionJob.objects.filter(pk=job_id, status="queued").update(
            status="failed", items=[{"title": "启动资料获取", "status": "failed", "message": "进程未启动，请重试。"}],
            finished_at=timezone.now())


def enqueue(actor, dossier, selection):
    _require_writer(actor)
    if dossier.owner_id != actor.pk or dossier.family_id != actor.family_id:
        raise ResearchValidationError("无权更新这份研究资料。")
    allowed = set(SOURCE_TASKS)
    allowed.update(CompanyMaterial.objects.filter(security=dossier.security,
                   kind="sec_document").values_list("key", flat=True))
    if not selection or len(selection) > 8 or any(s not in allowed for s in selection):
        raise ResearchValidationError("请选择有效的资料项目。")
    with transaction.atomic():
        from family_core.models import Family
        Family.objects.select_for_update().get(pk=actor.family_id)
        # Same security across members also serializes public-source acquisition.
        from portfolio.models import Security
        Security.objects.select_for_update().get(pk=dossier.security_id)
        active = CompanyAcquisitionJob.objects.filter(dossier__security=dossier.security,
            status__in=["queued", "running"], expires_at__gt=timezone.now()).first()
        if active:
            # Do not leak another member's dossier or job.
            if active.dossier_id == dossier.pk:
                return active
            raise ResearchValidationError("这家公司的公共资料正在更新，请稍后查看。")
        if CompanyAcquisitionJob.objects.filter(dossier__family_id=actor.family_id,
                status__in=["queued", "running"], expires_at__gt=timezone.now()).count() >= 2:
            raise ResearchValidationError("已有两家公司正在获取资料，请等其中一项完成后再提交。")
        CompanyAcquisitionJob.objects.filter(dossier__security=dossier.security,
            status__in=["queued", "running"]).update(status="interrupted", finished_at=timezone.now())
        job = CompanyAcquisitionJob.objects.create(dossier=dossier, selection=selection,
            expires_at=timezone.now() + timedelta(hours=1))
        transaction.on_commit(lambda: launch(job.pk))
    return job


def execute_item(job, key):
    from . import sec_library
    from .company_sources import collect_futu
    from .company_identity import identity_for
    security = job.dossier.security
    if key == "sec":
        cik, records = sec_library.discover(security)
        # Register planned items before downloading so each can be retried.
        from .providers.sec import filing_url
        for record in records:
            CompanyMaterial.objects.get_or_create(security=security, key="sec:" + record["accession"],
                defaults={"kind": "sec_document", "title": record["title"],
                          "source_url": filing_url(cik, record["accession"], record["primary_document"]),
                          "metadata": {**record, "cik": cik}})
        return f"已识别 {len(records)} 份常用报告，正文逐项获取"
    if key == "facts":
        return sec_library.company_facts(security)
    if key.startswith("sec:"):
        material = CompanyMaterial.objects.get(security=security, key=key, kind="sec_document")
        identity = identity_for(security)
        latest = material.versions.first()
        if latest and latest.text and not material.last_error and key not in job.selection:
            return "已有该 SEC 文件原件与正文，沿用本地版本"
        index = CompanyMaterial.objects.get(security=security, key="sec").versions.first()
        accession = key.split(":")[1]
        record = next((r for r in index.data["filings"] if r["accession"] == accession), None)
        if not record:
            record = latest.data if latest else material.metadata
        if not record:
            raise ValueError("文件目录中没有对应身份，请先更新 SEC 文件目录。")
        if len(key.split(":")) > 2:
            sec_library._document(security, key, material.source_url, material.title, record,
                                  sec_library._default_sec_client(security))
            return "附件已更新"
        return sec_library.download(security, identity.cik, record)
    if key == "ir":
        from .official_ir import sync_official_ir, documents_for_security, fetch_ir_content
        state, created = sync_official_ir(security)
        from .material_store import save_material
        documents = documents_for_security(security).filter(source__in=["official_ir", "microsoft_ir"]).order_by("-published_at")[:8]
        saved, errors = [], []
        for document in documents:
            try:
                version = document.content_versions.first()
                if not version:
                    version, _ = fetch_ir_content(document)
                saved.append(document.pk)
            except Exception:
                errors.append(document.pk)
        save_material(security, "ir", "ir", SOURCE_TASKS[key], data={"created": created,
            "document_ids": saved, "failed_document_ids": errors, "last_error": state.last_error})
        if state.last_error:
            raise ValueError("部分官方 IR 资料未完成，请在官方资料页核对。")
        if errors:
            raise ValueError(f"已保存 {len(saved)} 份 IR 资料，另有 {len(errors)} 份待在官方资料页重试。")
        return f"已保存 {len(saved)} 份官方 IR 资料，可在官方资料中读取和下载"
    return collect_futu(security, key)


def run(job_id):
    if not CompanyAcquisitionJob.objects.filter(pk=job_id, status="queued", expires_at__gt=timezone.now()).update(status="running"):
        return
    job = CompanyAcquisitionJob.objects.select_related("dossier__security", "dossier__owner").get(pk=job_id)
    if not job.dossier.owner.is_active:
        job.status, job.finished_at = "failed", timezone.now()
        job.save(update_fields=["status", "finished_at"])
        return
    selection, results = list(job.selection), []
    index = 0
    while index < len(selection) and timezone.now() < job.expires_at:
        key = selection[index]
        title = SOURCE_TASKS.get(key) or CompanyMaterial.objects.filter(security=job.dossier.security, key=key).values_list('title', flat=True).first() or "SEC 报告及附件"
        results.append({"key": key, "title": title, "status": "running", "message": "正在获取"})
        job.items = results
        job.save(update_fields=["items", "updated_at"])
        try:
            process = subprocess.run([sys.executable, "manage.py", "collect_company_item", str(job_id), key],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=360, check=False,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            job.refresh_from_db(fields=["items"])
            results = job.items
            if process.returncode or results[-1]["status"] == "running":
                results[-1].update(status="failed", message="该项获取中断，已保存的资料仍可阅读。")
        except (subprocess.TimeoutExpired, OSError):
            results[-1].update(status="failed", message="该项超时或未能启动，可单独重试。")
        if key == "sec" and results[-1]["status"] == "success":
            sec_index = CompanyMaterial.objects.get(security=job.dossier.security, key="sec").versions.first()
            selection.extend("sec:" + r["accession"] for r in sec_index.data["filings"])
        index += 1
    job.items = results
    job.status = "partial" if any(r["status"] == "failed" for r in results) or index < len(selection) else "success"
    job.finished_at = timezone.now()
    job.save(update_fields=["items", "status", "finished_at", "updated_at"])
