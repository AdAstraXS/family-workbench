"""投研模块视图。

所有入口 login_required；每个请求自行验证有效成员（未绑定/停用 403，
即使超级用户被全局中间件放行也不能绕过）。写操作 CSRF + POST，
其余方法 405。写入只调用 services，不在视图中重复 ORM 写入。
异常顺序：ThesisRevisionConflict / DuplicateDossier 先于父类
ResearchValidationError；DossierNotFound 转 404。
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .forms import CreateDossierForm, EditThesisForm
from .models import ResearchDossier
from .permissions import (
    accessible_dossiers,
    get_accessible_dossier_or_404,
    get_current_member,
    is_writer,
)
from .services import (
    DossierNotFound,
    DuplicateDossier,
    ResearchValidationError,
    ThesisRevisionConflict,
    create_dossier,
    save_thesis_revision,
)

PAGE_SIZE = 20


def _get_member_or_403(request):
    """每个请求自行验证有效成员；未绑定/停用 403。"""
    member = get_current_member(request)
    if member is None:
        return None
    return member


def _forbidden():
    return HttpResponseForbidden("当前账户尚未绑定有效家庭成员，或成员已停用。")


def _method(methods):
    """组合装饰器：方法白名单（405）→ 登录 → CSRF。"""

    def wrap(view):
        return require_http_methods(methods)(login_required(csrf_protect(view)))

    return wrap


@_method(["GET"])
def index(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossiers = accessible_dossiers(member).order_by("-updated_at", "-pk")
    paginator = Paginator(dossiers, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "investment_research/index.html",
        {"page": page, "can_write": is_writer(member)},
    )


@_method(["GET", "POST"])
def create(request):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色只能查看本人档案，不能创建或修改。")

    form = CreateDossierForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            dossier = create_dossier(
                actor=member,
                security=form.cleaned_data["security"],
                initial_thesis=form.cleaned_data["initial_thesis"],
                pillars=form.cleaned_data["pillars"],
                questions=form.cleaned_data["questions"],
            )
        except DuplicateDossier as exc:
            messages.info(request, "你已拥有该证券的研究档案，已为你打开。")
            return redirect(
                "investment_research:detail", pk=exc.dossier.pk
            )
        except ThesisRevisionConflict:
            # 创建路径理论上不触发；按统一顺序处理为 409。
            return render(
                request,
                "investment_research/create.html",
                {"form": form, "conflict": True},
                status=409,
            )
        except ResearchValidationError as exc:
            form = CreateDossierForm(request.POST)
            form.add_error(None, str(exc))
        else:
            messages.success(request, "研究档案已创建。")
            return redirect("investment_research:detail", pk=dossier.pk)
    return render(
        request, "investment_research/create.html", {"form": form}
    )


@_method(["GET"])
def detail(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    return render(
        request,
        "investment_research/detail.html",
        {
            "dossier": dossier,
            "revision": dossier.current_revision,
            "can_write": is_writer(member),
        },
    )


@_method(["GET", "POST"])
def edit(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    if not is_writer(member):
        return HttpResponseForbidden("查看者角色只能查看本人档案，不能创建或修改。")

    current = dossier.current_revision
    if request.method == "POST":
        form = EditThesisForm(request.POST)
        if form.is_valid():
            try:
                save_thesis_revision(
                    actor=member,
                    dossier_id=dossier.pk,
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                    thesis=form.cleaned_data["thesis"],
                    pillars=form.cleaned_data["pillars"],
                    questions=form.cleaned_data["questions"],
                    change_reason=form.cleaned_data["change_reason"],
                )
            except ThesisRevisionConflict:
                # 旧版本提交：保留用户输入与旧 expected_revision_id，
                # 不自动替换为最新版本；提示查看最新详情后重新编辑。
                return render(
                    request,
                    "investment_research/edit.html",
                    {
                        "dossier": dossier,
                        "form": form,
                        "conflict": True,
                    },
                    status=409,
                )
            except DuplicateDossier:
                # 编辑路径理论上不触发；按统一顺序在父类之前处理。
                form = EditThesisForm(request.POST)
                form.add_error(None, "你已拥有该证券的研究档案。")
            except DossierNotFound:
                # 并发删除等极端情况：统一按 404 处理。
                raise Http404
            except ResearchValidationError as exc:
                form = EditThesisForm(request.POST)
                form.add_error(None, str(exc))
            else:
                messages.success(request, "判断已保存为新版本。")
                return redirect("investment_research:detail", pk=dossier.pk)
    else:
        form = EditThesisForm(
            initial={
                "thesis": current.thesis if current else "",
                "pillars": "\n".join(current.pillars) if current else "",
                "questions": "\n".join(current.questions) if current else "",
                "change_reason": "",
                "expected_revision_id": current.pk if current else None,
            }
        )
    return render(
        request,
        "investment_research/edit.html",
        {"dossier": dossier, "form": form, "conflict": False},
    )


@_method(["GET"])
def history(request, pk):
    member = _get_member_or_403(request)
    if member is None:
        return _forbidden()
    dossier = get_accessible_dossier_or_404(member, pk)
    revisions = dossier.revisions.order_by("-revision_number", "-pk")
    paginator = Paginator(revisions, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "investment_research/history.html",
        {"dossier": dossier, "page": page},
    )
