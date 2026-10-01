import gzip
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .permissions import get_current_member, get_accessible_dossier_or_404, is_writer
from .company_identity import search_companies, choose_company, relation, qualified
from .company_jobs import enqueue
from .company_sources import SOURCE_TASKS
from .models import CompanyIdentity, CompanyMaterialVersion
from .services import ResearchValidationError
from .material_reading import inventory, reading_sections, fact_rows
from .futu_financials import statement_tables, breakdown_tables, provider_code


@login_required
@require_http_methods(["GET", "POST"])
def start(request):
    member = get_current_member(request)
    if not is_writer(member):
        return HttpResponseForbidden("需要有效的编辑成员身份。")
    candidates, error, query = [], "", ""
    if request.method == "POST":
        try:
            if request.POST.get("choice"):
                dossier = choose_company(member, request.POST["choice"])
                return redirect("investment_research:materials", pk=dossier.pk)
            query = request.POST.get("query", "")
            candidates, error = search_companies(query)
        except ResearchValidationError as exc:
            error = str(exc)
    return render(request, "investment_research/material_start.html", {
        "candidates": candidates, "error": error, "query": query, "searched": request.method == "POST"})


@login_required
@require_http_methods(["GET", "POST"])
def library(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    if request.method == "POST":
        if not is_writer(member):
            return HttpResponseForbidden("查看者不能获取新资料。")
        try:
            enqueue(member, dossier, request.POST.getlist("source"))
            messages.success(request, "资料正在后台获取，可以留在这里查看进度，也可以稍后返回。")
        except ResearchValidationError as exc:
            messages.error(request, str(exc))
        return redirect("investment_research:materials", pk=pk)
    materials, manifest = inventory(dossier.security)
    if request.GET.get("format") == "manifest":
        return JsonResponse(manifest, json_dumps_params={"ensure_ascii": False})
    identity = CompanyIdentity.objects.filter(security=dossier.security).first()
    info = relation(qualified(dossier.security))
    job = dossier.acquisition_jobs.order_by("-pk").first()
    active = job and job.status in {"queued", "running"} and job.expires_at > timezone.now()
    from .providers.ir_registry import company_for_security
    sources = [{"key": k, "title": v, "applicable": bool(info["sec_ticker"]) if k in {"sec", "facts"}
                else bool(company_for_security(dossier.security)) if k == "ir"
                else dossier.security.market in {"US", "HK", "CN", "CN_B"}} for k, v in SOURCE_TASKS.items()]
    return render(request, "investment_research/material_library.html", {
        "dossier": dossier, "identity": identity, "identity_info": info,
        "materials": [m for m in materials if m.kind != "sec_document"],
        "sec_materials": [m for m in materials if m.kind == "sec_document"],
        "manifest": manifest, "sources": sources,
        "job": job, "active": active, "can_write": is_writer(member)})


@login_required
@require_http_methods(["GET"])
def read(request, pk, version_pk):
    dossier = get_accessible_dossier_or_404(get_current_member(request), pk)
    version = get_object_or_404(CompanyMaterialVersion.objects.select_related("material"),
        pk=version_pk, material__security=dossier.security)
    if request.GET.get("download") == "1":
        response = HttpResponse(gzip.decompress(bytes(version.raw_gzip)), content_type="application/octet-stream")
        ext = ".pdf" if version.media_type == "application/pdf" else ".html" if version.media_type == "text/html" else ".json"
        response["Content-Disposition"] = f'attachment; filename="company-material-{version.pk}{ext}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox"
        return response
    data = version.data
    return render(request, "investment_research/material_read.html", {
        "dossier": dossier, "version": version, "sections": reading_sections(version),
        "facts": fact_rows(data) if version.material.kind == "facts" else [],
        "tables": statement_tables(data.get("statements", []), provider_code(dossier.security)) if version.material.kind == "financials" else [],
        "groups": breakdown_tables(data.get("breakdown")) if version.material.kind == "financials" else [],
        "versions": version.material.versions.only("id", "number", "fetched_at", "report_date"),
    })
