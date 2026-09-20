"""投研模块权限查询入口。

身份规则与 notes/knowledge 一致：成员取自登录用户的一对一绑定
FamilyMember，未绑定或停用按无成员处理。本批全部私密：仅档案
owner 本人可查看或修改；superuser 与家庭管理员不绕过该权限。
"""
from django.shortcuts import get_object_or_404

from family_core.models import FamilyMember

from .models import ResearchDossier


def get_current_member(request):
    """返回登录用户绑定的有效家庭成员；未绑定或停用返回 None。"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    try:
        member = user.family_member
    except FamilyMember.DoesNotExist:
        return None
    return member if member.is_active else None


def is_writer(member):
    """成员能否创建/修改研究档案；停用成员与 viewer 均不可写。"""
    return (
        member is not None
        and member.is_active
        and member.role != FamilyMember.ROLE_VIEWER
    )


def accessible_dossiers(member):
    """成员可见的档案：本批全部私密，仅本人档案；停用成员无可见档案。"""
    if member is None or not member.is_active:
        return ResearchDossier.objects.none()
    return ResearchDossier.objects.filter(
        owner=member, family=member.family
    ).select_related("security", "current_revision")


def get_accessible_dossier_or_404(member, pk):
    """非本人档案的 ID 一律 404，避免泄露档案存在性。"""
    return get_object_or_404(accessible_dossiers(member), pk=pk)
