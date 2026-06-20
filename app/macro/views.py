from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def index(request):
    return render(request, "placeholder.html", {"title": "宏观数据", "message": "宏观数据模块已预留，后续会加入中美指标和趋势图。"})
