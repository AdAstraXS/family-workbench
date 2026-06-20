from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import AiAnalysisRequest


@login_required
def index(request):
    recent_requests = AiAnalysisRequest.objects.select_related("member", "provider").order_by("-created_at")[:20]
    return render(request, "ai_analysis/index.html", {"recent_requests": recent_requests})
