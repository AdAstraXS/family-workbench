from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import FamilyMember


@login_required
def member_list(request):
    members = FamilyMember.objects.select_related("family", "user").order_by("family__name", "display_name")
    return render(request, "family_core/member_list.html", {"members": members})
