from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import prefetch_related_objects

from family_core.models import FamilyMember
from .models import AssetBalanceSnapshot
from .valuation import calculate_base_amount


class SnapshotComparisonForm(forms.Form):
    snapshots = forms.ModelMultipleChoiceField(
        label="选择快照期数", queryset=AssetBalanceSnapshot.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    members = forms.ModelMultipleChoiceField(
        label="选择成员", queryset=FamilyMember.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    amount_mode = forms.ChoiceField(
        label="金额显示", choices=[("base", "本位币"), ("original", "原币")],
        initial="base",
    )

    def __init__(self, *args, family, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["snapshots"].queryset = AssetBalanceSnapshot.objects.filter(
            family=family, is_draft=False
        ).order_by("-snapshot_date", "-pk")
        self.fields["snapshots"].label_from_instance = lambda s: f"{s.snapshot_date:%Y-%m-%d}" + (f" · {s.title}" if s.title else "")
        self.fields["members"].queryset = FamilyMember.objects.filter(
            family=family
        ).order_by("display_order", "pk")

    def clean(self):
        data = super().clean()
        snapshots = data.get("snapshots")
        if snapshots is not None:
            if len(snapshots) < 2:
                self.add_error("snapshots", "请至少选择两期正式资产快照。")
            if data.get("amount_mode") == "base" and len({s.base_currency for s in snapshots}) > 1:
                self.add_error("amount_mode", "所选快照的本位币不同，请改用原币或选择相同本位币的快照。")
        return data


def comparison_cells(values):
    cells = []
    previous = None
    for value in values:
        cells.append({
            "amount": value,
            "change": value - previous if value is not None and previous is not None else None,
        })
        previous = value
    return cells


def build_snapshot_comparison(snapshots, members, amount_mode):
    """Compare saved balances; missing records are distinct from explicit zero."""
    snapshots = sorted(snapshots, key=lambda s: (s.snapshot_date, s.pk))
    prefetch_related_objects(snapshots, "entries__member", "entries__account", "entries__asset_category")
    member_order = {member.pk: index for index, member in enumerate(members)}
    rows = {}
    totals = {}
    warnings = set()
    for index, snapshot in enumerate(snapshots):
        for entry in snapshot.entries.all():
            if entry.member_id not in member_order:
                continue
            account_key = ("id", entry.account_id) if entry.account_id else ("name", entry.account_name)
            key = (entry.member_id, account_key, entry.asset_category_id, entry.currency)
            row = rows.setdefault(key, {
                "member": entry.member.display_name,
                "member_order": member_order[entry.member_id],
                "account": entry.account.account_name if entry.account else entry.account_name or "未命名账户",
            "category": str(entry.asset_category) if entry.asset_category else "未分类",
                "currency": entry.currency,
                "order": entry.display_order,
                "values": [None] * len(snapshots),
                "invalid": set(),
            })
            currency = snapshot.base_currency if amount_mode == "base" else entry.currency
            total = totals.setdefault(currency, {"values": [Decimal("0")] * len(snapshots), "invalid": set()})
            amount = entry.original_amount
            if amount_mode == "base":
                try:
                    calculate_base_amount(snapshot, entry.currency, entry.original_amount)
                except ValidationError:
                    row["invalid"].add(index)
                    total["invalid"].add(index)
                    warnings.add(f"{snapshot.snapshot_date} 的 {entry.currency} 汇率缺失，该期本位币金额无法完整对比。")
                    continue
                amount = entry.base_amount if entry.original_amount else Decimal("0")
            row["values"][index] = (row["values"][index] or Decimal("0")) + amount
            total["values"][index] += amount
    result_rows = sorted(rows.values(), key=lambda r: (r["member_order"], r["order"], r["account"], r["category"], r["currency"]))
    for row in result_rows:
        row["cells"] = comparison_cells([
            None if i in row["invalid"] else value for i, value in enumerate(row["values"])
        ])
    total_rows = []
    for currency, total in sorted(totals.items()):
        total_rows.append({"currency": currency, "cells": comparison_cells([
            None if i in total["invalid"] else value for i, value in enumerate(total["values"])
        ])})
    return {"snapshots": snapshots, "rows": result_rows, "totals": total_rows, "warnings": sorted(warnings)}
