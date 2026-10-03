"""Member defaults and optional company overrides, frozen on every AI request."""
from django.db import transaction
from .models import ResearchPromptTemplate
from .preparation import authorize
from .research_ai import ResearchAiError


def templates(dossier):
    rows = ResearchPromptTemplate.objects.filter(owner=dossier.owner)
    return rows.filter(dossier__isnull=True).first(), rows.filter(dossier=dossier).first()


def effective_prompt(dossier, standard):
    default, company = templates(dossier)
    selected = company if company and company.instructions.strip() else default
    addition = selected.instructions.strip() if selected else ""
    return standard + ("\n用户补充研究重点（保留上述引用规则与输出结构）：\n" + addition if addition else ""), {
        "template_id": selected.pk if selected else None,
        "template_revision": selected.revision if selected else None,
        "template_scope": "company" if selected == company and selected else "default",
    }


def save_template(actor, dossier, scope, instructions, revision):
    authorize(actor, dossier)
    if scope not in {"default", "company"} or len(instructions) > 6000:
        raise ResearchAiError("请选择模板范围，补充提示词不超过 6,000 字。")
    with transaction.atomic():
        type(actor).objects.select_for_update().get(pk=actor.pk)
        row = ResearchPromptTemplate.objects.filter(owner=actor, dossier=dossier if scope == "company" else None).first()
        if str(row.revision if row else 0) != str(revision):
            raise ResearchAiError("提示词已在其他页面修改，请刷新后重试。")
        row = row or ResearchPromptTemplate(owner=actor, dossier=dossier if scope == "company" else None, revision=0)
        row.instructions = instructions.strip()
        row.revision += 1
        row.save()
        return row
