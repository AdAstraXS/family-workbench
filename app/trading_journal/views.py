from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render


@login_required
def index(request):
    if getattr(request, "family_member", None) is None:
        return HttpResponseForbidden("当前账户尚未绑定有效家庭成员。")
    return render(request, "trading_journal/index.html")
