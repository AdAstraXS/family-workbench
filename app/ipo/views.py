from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def index(request):
    return render(request, "placeholder.html", {"title": "港股打新", "message": "港股打新模块已预留，后续会加入新股资料、策略和复盘。"})
