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
    return render(request, "option_wheel/job_detail.html", {"job": job, "job_state": job_payload(job)})
