from .models import FamilyMember


def current_member(request):
    member = getattr(request, "family_member", None)
    if member is not None and member.is_active:
        return member
    try:
        member = request.user.family_member
    except FamilyMember.DoesNotExist:
        return None
    return member if member.is_active else None
