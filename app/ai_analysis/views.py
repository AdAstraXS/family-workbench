from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import AiAnalysisRequest
from family_core.household import get_household_family
from family_core.models import FamilyMember


@login_required
def index(request):
    family_ids = FamilyMember.objects.filter(user=request.user, is_active=True).values("family_id")
    # This legacy family-wide page predates private global AI conversations.
    # Global AI requests get a member-scoped page later and must not appear here.
    requests = AiAnalysisRequest.objects.filter(family_id__in=family_ids).exclude(module="global_ai")
    if request.user.is_superuser and not family_ids.exists():
        family = get_household_family()
        requests = (
            AiAnalysisRequest.objects.filter(family=family).exclude(module="global_ai")
            if family
            else AiAnalysisRequest.objects.none()
        )
    recent_requests = requests.select_related("member", "provider").order_by("-created_at")[:20]
    return render(request, "ai_analysis/index.html", {"recent_requests": recent_requests})
