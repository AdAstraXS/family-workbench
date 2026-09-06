import re

from django.http import HttpResponseForbidden

from .models import FamilyMember


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATH_PREFIXES = ("/accounts/", "/static/", "/media/")
VIEWER_AI_WRITE_PATH = re.compile(
    r"^/ai/(?:conversations/(?:new|\d+/archive)/|memories/(?:new|\d+/(?:confirm|revise|delete))/|messages/\d+/share-preview/|shares/\d+/(?:publish|refresh|withdraw)/)$"
)


class ActiveHouseholdMemberMiddleware:
    """Require an active household identity and enforce the read-only role."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.family_member = None
        user = request.user
        if not user.is_authenticated or request.path.startswith(EXEMPT_PATH_PREFIXES):
            return self.get_response(request)

        try:
            member = user.family_member
        except FamilyMember.DoesNotExist:
            member = None

        if member is None or not member.is_active:
            if not user.is_superuser:
                return HttpResponseForbidden("当前账户尚未绑定家庭成员，或成员已停用。")
        else:
            request.family_member = member
            viewer_personal_ai_action = bool(VIEWER_AI_WRITE_PATH.fullmatch(request.path))
            if (
                member.role == FamilyMember.ROLE_VIEWER
                and request.method not in SAFE_METHODS
                and not viewer_personal_ai_action
            ):
                return HttpResponseForbidden("当前家庭成员是只读角色，不能修改数据。")

        return self.get_response(request)
