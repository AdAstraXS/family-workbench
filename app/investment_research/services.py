"""投研模块服务层。

两个服务均以具名参数调用；actor（当前登录成员）必须来自请求身份，
不能来自 POST 数据或模型返回值。输入长度与 JSON 结构在此校验，
不依赖表单验证。
"""
from django.db import IntegrityError, transaction

from family_core.models import FamilyMember
from portfolio.models import Security

from .models import ResearchDossier, ResearchThesisRevision

THESIS_MAX_LENGTH = 8000
LIST_FIELD_MAX_ITEMS = 5
LIST_ITEM_MAX_LENGTH = 500
CHANGE_REASON_MAX_LENGTH = 500


class ResearchValidationError(ValueError):
    """输入校验失败；message 可直接显示在表单上。"""


class DuplicateDossier(ResearchValidationError):
    """同一 owner 对同一证券已有档案；携带已有档案供跳转。"""

    def __init__(self, existing):
        self.dossier = existing
        super().__init__("你已拥有该证券的研究档案。")


class DossierNotFound(LookupError):
    """档案不存在或不属于当前成员（视图统一按 404 处理）。"""


class ThesisRevisionConflict(ResearchValidationError):
    """判断版本已被更新，本次保存应拒绝并提示查看最新版本（HTTP 409）。"""

    def __init__(self, dossier):
        self.dossier = dossier
        super().__init__("判断已更新，请查看最新版本后再保存。")


def _require_writer(actor):
    if actor is None:
        raise ResearchValidationError("请先登录并绑定家庭成员后再操作。")
    if not actor.is_active:
        raise ResearchValidationError("该家庭成员已停用。")
    if actor.role == FamilyMember.ROLE_VIEWER:
        raise ResearchValidationError("查看者角色只能查看本人档案，不能创建或修改。")


def _clean_thesis(thesis):
    if thesis is None:
        raise ResearchValidationError("请填写当前判断。")
    cleaned = str(thesis).strip()
    if not cleaned:
        raise ResearchValidationError("请填写当前判断。")
    if len(cleaned) > THESIS_MAX_LENGTH:
        raise ResearchValidationError(f"当前判断不能超过 {THESIS_MAX_LENGTH} 字。")
    return cleaned


def _clean_list_field(value, label):
    # 规格要求 list：None、tuple、dict、字符串一律拒绝；空值由调用方传 []。
    if not isinstance(value, list):
        raise ResearchValidationError(f"{label}必须是列表。")
    if len(value) > LIST_FIELD_MAX_ITEMS:
        raise ResearchValidationError(f"{label}最多 {LIST_FIELD_MAX_ITEMS} 条。")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise ResearchValidationError(f"{label}的每一条都必须是文字。")
        text = item.strip()
        if not text:
            raise ResearchValidationError(f"{label}不允许空白条目。")
        if len(text) > LIST_ITEM_MAX_LENGTH:
            raise ResearchValidationError(f"{label}每条不能超过 {LIST_ITEM_MAX_LENGTH} 字。")
        cleaned.append(text)
    return cleaned


def _clean_change_reason(change_reason):
    if change_reason is None:
        return ""
    text = str(change_reason).strip()
    if len(text) > CHANGE_REASON_MAX_LENGTH:
        raise ResearchValidationError(f"修改原因不能超过 {CHANGE_REASON_MAX_LENGTH} 字。")
    return text


def create_dossier(*, actor, security, initial_thesis, pillars, questions):
    """创建研究档案及首版判断，并在同一事务内更新 current_revision。

    首版 thesis 与 initial_thesis 相同；关键假设/待验证问题可空，
    但必须显式传 list（空值传 []，不依赖默认值）。
    重复 owner/security 抛出 DuplicateDossier（携带已有档案）。
    """
    _require_writer(actor)
    thesis = _clean_thesis(initial_thesis)
    pillars = _clean_list_field(pillars, "关键假设")
    questions = _clean_list_field(questions, "待验证问题")

    with transaction.atomic():
        dossier = ResearchDossier(
            family=actor.family,
            owner=actor,
            security=security,
            initial_thesis=thesis,
        )
        try:
            # 内层 savepoint：唯一冲突只回滚到此处，不在已损坏事务里继续查询。
            with transaction.atomic():
                dossier.save()
        except IntegrityError:
            existing = ResearchDossier.objects.filter(
                owner=actor, security=security
            ).first()
            if existing is None:
                raise
            raise DuplicateDossier(existing) from None

        revision = ResearchThesisRevision(
            dossier=dossier,
            revision_number=1,
            thesis=thesis,
            pillars=pillars,
            questions=questions,
            change_reason="",
            created_by=actor,
        )
        revision.save()
        dossier.current_revision = revision
        dossier.save(update_fields=["current_revision", "updated_at"])
    return dossier


def create_exploration(*, actor, security):
    """先建立私密探索档案；还没有用户正式判断或版本。"""
    _require_writer(actor)
    from .providers.ir_registry import company_for_security
    if security.asset_type != Security.TYPE_STOCK or (security.market != "US" and not company_for_security(security)):
        raise ResearchValidationError("探索入口支持美股普通股及已配置官方 IR 的公司。")
    try:
        with transaction.atomic():
            return ResearchDossier.objects.create(
                family=actor.family, owner=actor, security=security, initial_thesis="",
            )
    except IntegrityError:
        existing = ResearchDossier.objects.filter(owner=actor, security=security).first()
        if existing is None:
            raise
        raise DuplicateDossier(existing) from None


def save_first_thesis(*, actor, dossier_id, thesis, pillars, questions):
    """探索档案首次确认判断；并发或重复提交只能产生一版。"""
    _require_writer(actor)
    clean_thesis = _clean_thesis(thesis)
    pillars = _clean_list_field(pillars, "关键假设")
    questions = _clean_list_field(questions, "待验证问题")
    with transaction.atomic():
        dossier = (
            ResearchDossier.objects.select_for_update()
            .filter(pk=dossier_id, owner=actor, family=actor.family)
            .first()
        )
        if dossier is None:
            raise DossierNotFound("档案不存在或不属于你。")
        if dossier.current_revision_id or dossier.initial_thesis:
            raise ThesisRevisionConflict(dossier)
        revision = ResearchThesisRevision.objects.create(
            dossier=dossier, revision_number=1, thesis=clean_thesis,
            pillars=pillars, questions=questions, created_by=actor,
        )
        dossier.initial_thesis = clean_thesis
        dossier.current_revision = revision
        dossier.save(update_fields=["initial_thesis", "current_revision", "updated_at"])
    return revision


def save_thesis_revision(
    *,
    actor,
    dossier_id,
    expected_revision_id,
    thesis,
    pillars,
    questions,
    change_reason="",
):
    """追加一版判断并更新 current_revision。

    锁档案后比对 expected_revision_id：旧页面提交与当前版本不符即冲突，
    禁止最后写入者静默覆盖。expected_revision_id 只是冲突校验值，
    不是授权凭证——档案同时按 actor 与其 family 限定，隐藏字段被伪造
    也不能读取或引用其他档案的版本。相同提交再次发送仍携带旧
    expected_revision_id，同样被拒绝，不追加重复版本。
    pillars/questions 必须显式传 list（空值传 []）。
    """
    _require_writer(actor)
    new_thesis = _clean_thesis(thesis)
    pillars = _clean_list_field(pillars, "关键假设")
    questions = _clean_list_field(questions, "待验证问题")
    reason = _clean_change_reason(change_reason)

    with transaction.atomic():
        dossier = (
            ResearchDossier.objects.select_for_update()
            .filter(owner=actor, family=actor.family, pk=dossier_id)
            .first()
        )
        if dossier is None:
            raise DossierNotFound(f"档案 {dossier_id} 不存在或不属于你。")
        # 先锁档案行，再在同一事务内单独读取 expected 版本；
        # 不在带 select_for_update 的查询上用 select_related——
        # current_revision 是可空 FK，外连接的可空侧不能 FOR UPDATE
        # （PostgreSQL 报 NotSupportedError）。
        current = ResearchThesisRevision.objects.filter(
            pk=expected_revision_id
        ).first()
        if (
            current is None
            or current.dossier_id != dossier.pk
            or dossier.current_revision_id != current.pk
        ):
            # 过期版本、expected 指向另一档案的版本、或当前指针异常
            # （指向别处/为空）都按冲突拒绝，不静默修复、不继续追加。
            raise ThesisRevisionConflict(dossier)
        new_number = current.revision_number + 1
        if new_number > 1 and not reason:
            raise ResearchValidationError("请填写本次修改原因。")

        revision = ResearchThesisRevision(
            dossier=dossier,
            revision_number=new_number,
            thesis=new_thesis,
            pillars=pillars,
            questions=questions,
            change_reason=reason,
            created_by=actor,
        )
        revision.save()
        dossier.current_revision = revision
        dossier.save(update_fields=["current_revision", "updated_at"])
    return revision
