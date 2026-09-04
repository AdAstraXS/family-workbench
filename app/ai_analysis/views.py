from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import AiAnalysisRequest
from family_core.household import get_household_family
from family_core.models import FamilyMember


@login_required
def index(request):
    family_ids = FamilyMember.objects.filter(user=request.user, is_active=True).values("family_id")
    requests = AiAnalysisRequest.objects.filter(family_id__in=family_ids)
    if request.user.is_superuser and not family_ids.exists():
        family = get_household_family()
        requests = AiAnalysisRequest.objects.filter(family=family) if family else AiAnalysisRequest.objects.none()
    recent_requests = requests.select_related("member", "provider").order_by("-created_at")[:20]
    return render(request, "ai_analysis/index.html", {"recent_requests": recent_requests})
