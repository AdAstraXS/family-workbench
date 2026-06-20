from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def index(request):
    return render(request, "placeholder.html", {"title": "投资复盘", "message": "投资复盘模块已预留，后续会加入笔记、标签和 AI 总结。"})
