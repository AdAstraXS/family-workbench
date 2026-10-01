from datetime import timedelta
import uuid
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from .permissions import get_current_member, get_accessible_dossier_or_404, is_writer
from .research_ai import available_research_providers, ResearchAiError
from .preparation import history, enqueue, confirm


@login_required
@require_http_methods(["GET", "POST"])
def prepare(request, pk):
    member = get_current_member(request)
    dossier = get_accessible_dossier_or_404(member, pk)
    reports = history(dossier).select_related("provider", "result")
    selected = request.GET.get("report") or request.POST.get("report")
    job = get_object_or_404(reports, pk=selected) if selected and str(selected).isdigit() else reports.first()
    providers = available_research_providers()
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "generate":
                provider = next((p for p in providers if str(p.pk) == request.POST.get("provider")), None)
                if not provider:
                    raise ResearchAiError("请先配置并选择可用的投研 AI 服务。")
                job = enqueue(member, dossier, provider, request.POST.get("consent") == "yes", request.POST.get("nonce"))
                messages.success(request, "初识报告正在后台生成，完成后显示在这里。")
                return redirect(f"{request.path}?report={job.pk}")
            if not job or request.POST.get("action") != "confirm":
                raise ResearchAiError("请先生成报告。")
            saved = confirm(member, dossier, job, request.POST)
            messages.success(request, f"已保存：{saved.get_decision_display()}。研究问题与候选假设已保留。")
            if saved.decision == "research":
                return redirect("investment_research:company_research", pk=pk)
            return redirect(f"{request.path}?report={job.pk}&tab=questions")
        except ResearchAiError as exc:
            error = str(exc)
    result = job.result.result_json if job and job.status == "success" else None
    saved = dossier.preparations.filter(analysis=job).first() if job else None
    hypotheses = list(saved.hypotheses if saved else result.get("hypotheses", []) if result else [])
    count = len(hypotheses)
    hypotheses += [{} for _ in range(5 - count)]
    rows = []
    for i, h in enumerate(hypotheses):
        row = {**h, "index": i, "selected": i < count, "refs_text": ", ".join(h.get("refs", []))}
        if error and request.POST.get("action") == "confirm":
            row.update({k: request.POST.get(f"{k}_{i}", "") for k in ["claim", "falsifier", "tracking", "missing"]})
            row.update(selected=request.POST.get(f"use_{i}") == "on", refs_text=request.POST.get(f"refs_{i}", ""))
        rows.append(row)
    questions = "\n".join(saved.questions if saved else result.get("questions", []) if result else [])
    if error and request.POST.get("action") == "confirm":
        questions = request.POST.get("questions", "")
    expired = job and job.created_at < timezone.now() - timedelta(minutes=10)
    return render(request, "investment_research/preparation.html", {"dossier": dossier,
        "job": job, "reports": reports[:20], "providers": providers, "result": result,
        "active": job and job.status in {"pending", "running"} and not expired,
        "interrupted": job and job.status in {"pending", "running"} and expired,
        "stale": job and job.scope.get("thesis_revision_id") != dossier.current_revision_id,
        "evidence": job.sanitized_input.get("evidence", []) if job else [],
        "saved": saved, "hypothesis_rows": rows, "questions": questions, "error": error,
        "questions_tab": request.GET.get('tab') == 'questions' or request.POST.get('action') == 'confirm',
        "can_write": is_writer(member), "nonce": str(uuid.uuid4()),
        "reason": request.POST.get("reason", saved.reason if saved else "")})
