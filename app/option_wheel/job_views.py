from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from .jobs import job_payload
from .models import WheelAnalysisJob
from .views import _request_family


@login_required
@require_GET
def status(request, pk):
    job = get_object_or_404(WheelAnalysisJob, pk=pk, family=_request_family(request))
    response = JsonResponse(job_payload(job))
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_GET
def detail(request, pk):
    job = get_object_or_404(WheelAnalysisJob, pk=pk, family=_request_family(request))
    from .screen_advice import context_for_job
    advice = (context_for_job(job) if job.status == "saved" and
              job.selection.get("mode") in ("screening_v2", "screening_close_v2")
              else {"rows": [], "status": "disabled", "pending": False, "error": ""})
    return render(request, "option_wheel/job_detail.html", {
        "job": job, "job_state": job_payload(job), "visible_results": advice["rows"],
        "ai_status": advice["status"], "ai_pending": advice["pending"],
        "ai_error": advice["error"],
        "ai_requested": bool(job.selection.get("ai_enabled")),
        "ai_status_url": job_payload(job)["status_url"],
    })
