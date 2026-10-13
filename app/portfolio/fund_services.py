from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from family_core.models import FamilyMember
from family_core.permissions import current_member
from family_core.household import get_household_family
from .fund_models import FundDcaPlan, FundDcaChange, FundReference

PLAN_FIELDS = ["name", "member_id", "fund_id", "start_date", "end_date", "frequency",
               "amount", "fee_rate", "balance", "balance_date", "remark", "revision"]


def request_scope(request):
    member = current_member(request)
    if member:
        return member.family, member, member.role == FamilyMember.ROLE_ADMIN
    if request.user.is_superuser:
        return get_household_family(), None, True
    raise PermissionDenied("请绑定有效的家庭成员。")


def can_edit(user, member, manage_all, plan=None):
    if not user.is_active or (member and member.role == FamilyMember.ROLE_VIEWER):
        return False
    return manage_all or (member is not None and (plan is None or plan.member_id == member.pk))


def snapshot(plan):
    return {key: str(getattr(plan, key)) if getattr(plan, key) is not None else None for key in PLAN_FIELDS}


def save_plan(form, *, family, user, member, manage_all, balance_only=False):
    with transaction.atomic():
        if form.instance.pk:
            plan = FundDcaPlan.objects.select_for_update().get(pk=form.instance.pk, family=family)
            if not can_edit(user, member, manage_all, plan):
                raise PermissionDenied("只能维护自己的定投。")
            if plan.revision != form.cleaned_data["revision"]:
                raise ValidationError("这条定投已被其他页面修改，请重新打开后再编辑。")
            before = snapshot(plan)
            fields = ["balance", "balance_date"] if balance_only else [
                "name", "member", "start_date", "end_date", "frequency", "amount", "fee_rate",
                "balance", "balance_date", "remark"]
            for key in fields:
                setattr(plan, key, form.cleaned_data[key])
            plan.revision += 1
        else:
            if not can_edit(user, member, manage_all):
                raise PermissionDenied("当前账户只有查看权限。")
            plan = form.save(commit=False)
            plan.family = family
            plan.revision = 1
            plan.created_by = user
            before = {}
        if not balance_only:
            plan.fund, _ = FundReference.objects.get_or_create(code=form.cleaned_data["fund_code"])
        if not manage_all and plan.member_id != member.pk:
            raise PermissionDenied("只能维护自己的定投。")
        plan.updated_by = user
        plan.full_clean()
        plan.save()
        FundDcaChange.objects.create(plan=plan, actor=user, action="balance" if balance_only else ("edit" if before else "create"),
                                     before=before, after=snapshot(plan))
        return plan
