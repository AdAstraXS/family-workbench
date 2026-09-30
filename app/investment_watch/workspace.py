"""Member-facing company setup and read-only rule previews."""

from types import SimpleNamespace

from django import forms
from django.db import transaction
from django.db.models import Q
from portfolio.models import Security
from investment_research.models import ResearchDossier
from .models import WatchRule
from .services import writer, words, public_versions, WatchError
from .catalogue import match_rule


class CompanyForm(forms.Form):
    security = forms.ModelChoiceField(
        label="选择已有公司",
        required=False,
        queryset=Security.objects.filter(asset_type="stock", is_active=True).order_by(
            "name"
        ),
    )
    name = forms.CharField(label="公司名称", max_length=200, required=False)
    symbol = forms.RegexField(
        label="证券代码", regex=r"^[A-Za-z0-9.\-]{1,30}$", required=False
    )
    market = forms.ChoiceField(
        label="市场",
        choices=[
            ("US", "美国"),
            ("HK", "香港"),
            ("SH", "上海"),
            ("SZ", "深圳"),
            ("TW", "台湾"),
            ("KR", "韩国"),
        ],
    )

    def clean(self):
        data = super().clean()
        if not data.get("security") and not (data.get("name") and data.get("symbol")):
            raise forms.ValidationError(
                "请选择已有公司，或填写新公司的名称、代码和市场。"
            )
        return data


@transaction.atomic
def add_company(member, data):
    writer(member)
    security = data.get("security")
    if not security:
        security, _ = Security.objects.get_or_create(
            market=data["market"],
            symbol=data["symbol"].upper(),
            defaults={
                "name": data["name"],
                "asset_type": "stock",
                "currency": {
                    "US": "USD",
                    "HK": "HKD",
                    "SH": "CNY",
                    "SZ": "CNY",
                    "TW": "TWD",
                    "KR": "KRW",
                }[data["market"]],
            },
        )
    if security.asset_type != "stock":
        raise forms.ValidationError("该代码对应的品种不是公司股票，请核对市场和代码。")
    dossier, _ = ResearchDossier.objects.get_or_create(
        owner=member,
        security=security,
        defaults={"family": member.family, "initial_thesis": ""},
    )
    if dossier.family_id != member.family_id:
        raise WatchError("已有档案属于先前的家庭，请先处理成员迁移。")
    WatchRule.objects.get_or_create(
        dossier=dossier,
        defaults={
            "aliases": [security.name, security.symbol],
            "enabled": False,
        },
    )
    return dossier


def preview_rule(member, values):
    rule = SimpleNamespace(
        enabled=True,
        **{
            k: words(values.get(k, []))
            for k in ("aliases", "topics", "include", "exclude")
        },
    )
    rows = []
    total = matched = 0
    for version in (
        public_versions(member).exclude(status="withdrawn").order_by("-pk")[:500]
    ):
        hit, reason = match_rule(rule, version)
        total += 1
        matched += bool(hit)
        if len(rows) < 30 and hit:
            rows.append({"version": version, "reason": reason})
    return {"total": total, "matched": matched, "rows": rows}


def search_companies(query):
    return (
        Security.objects.filter(asset_type="stock", is_active=True)
        .filter(Q(name__icontains=query) | Q(symbol__icontains=query))
        .order_by("name")[:50]
    )


def reading_rule(dossier):
    rule = WatchRule.objects.filter(dossier=dossier).first()
    return SimpleNamespace(
        enabled=True,
        aliases=rule.aliases
        if rule
        else [dossier.security.name, dossier.security.symbol],
        topics=rule.topics if rule else [],
        include=rule.include if rule else [],
        exclude=rule.exclude if rule else [],
    )
