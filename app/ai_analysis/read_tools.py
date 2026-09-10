"""Read-only, module-scoped evidence builders for the global AI feature."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

from django.db.models import Q
from django.utils import timezone

from knowledge.models import (
    KnowledgeDocument,
    KnowledgeRevision,
    KnowledgeSearchEntry,
    KnowledgeVisibility,
)
from knowledge.permissions import accessible_documents
from ledger.models import AnnualBudget, AssetBalanceSnapshot, ExpenseRecord, IncomeRecord
from portfolio.models import InvestmentAccount, PortfolioSnapshot


SCOPE_PERSONAL = "personal"
SCOPE_FAMILY = "family"
VALID_SCOPES = {SCOPE_PERSONAL, SCOPE_FAMILY}
ZERO = Decimal("0")
AMOUNT_QUANTUM = Decimal("0.0001")
PERCENT_QUANTUM = Decimal("0.01")


class GlobalAiReadError(ValueError):
    """Raised when the trusted host context cannot produce safe evidence."""


def _validate_context(actor, scope):
    if not actor or not actor.pk or not actor.is_active:
        raise GlobalAiReadError("当前成员无效。")
    if scope not in VALID_SCOPES:
        raise GlobalAiReadError("不支持的数据范围。")


def _decimal(value):
    return str(value if value is not None else ZERO)


def _amount(value):
    return str(Decimal(value if value is not None else ZERO).quantize(AMOUNT_QUANTUM))


def _percent(numerator, denominator):
    if not denominator:
        return None
    return str(
        (Decimal(numerator) / Decimal(denominator) * Decimal("100")).quantize(
            PERCENT_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    )


def _knowledge_statuses(include_pending):
    statuses = [KnowledgeDocument.KNOWLEDGE_INCLUDED]
    if include_pending:
        statuses.append(KnowledgeDocument.KNOWLEDGE_PENDING)
    return statuses


def _knowledge_reference(document_id, revision_id):
    return f"knowledge:{document_id}:revision:{revision_id}"


def _ai_accessible_documents(actor):
    # The knowledge UI treats an owned document as sufficient. AI reads require
    # both the document and its source to remain visible to the current member.
    return accessible_documents(actor).filter(
        Q(source__owner=actor) | Q(source__visibility=KnowledgeVisibility.FAMILY)
    )


def _knowledge_document(actor, document_id, *, include_pending=False):
    document = (
        _ai_accessible_documents(actor)
        .filter(
            pk=document_id,
            knowledge_status__in=_knowledge_statuses(include_pending),
        )
        .first()
    )
    if document is None:
        raise GlobalAiReadError("知识资料不可用。")
    return document


def _matches_knowledge_query(document, revision, terms):
    text = " ".join(
        [
            document.title,
            document.author,
            document.section_name,
            document.confirmed_summary,
            document.category,
            " ".join(str(tag) for tag in (document.tags or [])),
            revision.plain_text,
        ]
    ).casefold()
    return all(term.casefold() in text for term in terms)


def _knowledge_excerpt(text, terms, *, length=240):
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    positions = [compact.casefold().find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 60)
    excerpt = compact[start : start + length]
    if start:
        excerpt = "…" + excerpt
    if start + length < len(compact):
        excerpt += "…"
    return excerpt


def knowledge_search(actor, *, query, include_pending=False, limit=10):
    """Find current document revisions while rechecking live source/document access."""

    _validate_context(actor, SCOPE_PERSONAL)
    if not isinstance(query, str):
        raise GlobalAiReadError("知识检索词必须是文本。")
    terms = [term for term in query.split() if term]
    if not terms:
        return {"module": "knowledge", "query": "", "results": []}
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise GlobalAiReadError("知识检索条数必须在 1 到 50 之间。")

    candidates = KnowledgeSearchEntry.objects.filter(
        family=actor.family,
        item_kind=KnowledgeSearchEntry.KIND_DOCUMENT,
        document__isnull=False,
    )
    for term in terms:
        candidates = candidates.filter(
            Q(searchable_text__icontains=term)
            | Q(title__icontains=term)
            | Q(body__icontains=term)
            | Q(summary__icontains=term)
        )
    candidate_ids = candidates.values_list("document_id", flat=True)
    documents = (
        _ai_accessible_documents(actor)
        .filter(
            pk__in=candidate_ids,
            current_revision__isnull=False,
            knowledge_status__in=_knowledge_statuses(include_pending),
        )
        .order_by("-content_modified_at", "-updated_at", "-pk")
    )

    results = []
    for document in documents.iterator():
        revision = document.current_revision
        # The projection can lag behind a revision or permission change. Never return
        # copied projection content unless the live document still matches.
        if not _matches_knowledge_query(document, revision, terms):
            continue
        results.append(
            {
                "reference": _knowledge_reference(document.pk, revision.pk),
                "document_id": document.pk,
                "revision_id": revision.pk,
                "revision_number": revision.revision_number,
                "title": document.title,
                "source_name": document.source.name,
                "knowledge_status": document.knowledge_status,
                "excerpt": _knowledge_excerpt(revision.plain_text, terms),
            }
        )
        if len(results) >= limit:
            break
    return {
        "module": "knowledge",
        "query": " ".join(terms),
        "include_pending": include_pending,
        "results": results,
    }


def knowledge_revision(actor, *, document_id, revision_id, include_pending=False):
    """Read an exact immutable revision after rechecking current document access."""

    _validate_context(actor, SCOPE_PERSONAL)
    document = _knowledge_document(actor, document_id, include_pending=include_pending)
    revision = KnowledgeRevision.objects.filter(
        pk=revision_id,
        document=document,
    ).first()
    if revision is None:
        raise GlobalAiReadError("知识资料不可用。")
    return {
        "module": "knowledge",
        "reference": _knowledge_reference(document.pk, revision.pk),
        "document_id": document.pk,
        "revision_id": revision.pk,
        "revision_number": revision.revision_number,
        "is_current_revision": document.current_revision_id == revision.pk,
        "title": document.title,
        "source_name": document.source.name,
        "source_url": document.source_url,
        "knowledge_status": document.knowledge_status,
        "content_hash": revision.content_hash,
        "plain_text": revision.plain_text,
    }


def _ledger_rate_available(snapshot, currency):
    currency = (currency or "").upper()
    base_currency = snapshot.base_currency.upper()
    if currency == base_currency:
        return True
    if currency == "USD":
        return snapshot.usd_to_base > ZERO
    if currency == "HKD":
        return snapshot.hkd_to_base > ZERO
    return False


def ledger_asset_snapshot(actor, *, scope=SCOPE_PERSONAL, snapshot_id=None):
    """Return one formal ledger snapshot without consulting portfolio data."""

    _validate_context(actor, scope)
    snapshots = AssetBalanceSnapshot.objects.filter(
        family=actor.family,
        is_draft=False,
    )
    if snapshot_id is not None:
        snapshot = snapshots.filter(pk=snapshot_id).first()
    else:
        snapshot = snapshots.order_by("-snapshot_date", "-created_at", "-pk").first()
    if snapshot is None:
        raise GlobalAiReadError("没有可读取的正式账户资产快照。")

    entries = snapshot.entries.select_related(
        "member",
        "account__account_type_ref",
        "asset_category",
    )
    if scope == SCOPE_PERSONAL:
        entries = entries.filter(member=actor)
    entries = list(entries.order_by("member__display_order", "display_order", "pk"))

    missing_rates = sorted(
        {entry.currency.upper() for entry in entries if not _ledger_rate_available(snapshot, entry.currency)}
    )
    warnings = []
    if not entries:
        warnings.append("所选正式快照没有当前范围的账户明细。")
    if missing_rates:
        warnings.append("部分外币缺少可核验汇率，不能提供完整本位币总额。")
    if snapshot_id is None:
        same_date_count = snapshots.filter(snapshot_date=snapshot.snapshot_date).count()
        if same_date_count > 1:
            warnings.append("同一日期存在多份正式快照，当前使用最新创建的一份。")

    complete = bool(entries) and not missing_rates
    total = sum((entry.base_amount for entry in entries), ZERO) if complete else None
    return {
        "module": "ledger",
        "scope": scope,
        "snapshot_id": snapshot.pk,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "base_currency": snapshot.base_currency,
        "complete": complete,
        "total_base_amount": _decimal(total) if total is not None else None,
        "missing_exchange_rates": missing_rates,
        "warnings": warnings,
        "accounts": [
            {
                "member_id": entry.member_id,
                "member_name": entry.member.display_name,
                "account_id": entry.account_id,
                "account_name": (
                    entry.account.account_name if entry.account_id else entry.account_name
                ),
                "account_type": (
                    entry.account.account_type_ref.name
                    if entry.account_id and entry.account.account_type_ref_id
                    else None
                ),
                "asset_category": entry.asset_category.name if entry.asset_category_id else None,
                "currency": entry.currency,
                "original_amount": _decimal(entry.original_amount),
                "base_amount": _decimal(entry.base_amount),
            }
            for entry in entries
        ],
    }


def _cashflow_records(actor, *, scope, year, model, date_field):
    records = model.objects.filter(family=actor.family).select_related(
        "member",
        "category",
        "category__parent",
        "category__parent__parent",
    )
    if scope == SCOPE_PERSONAL:
        records = records.filter(member=actor)
    records = records.filter(
        Q(period_start__year=year)
        | Q(period_start__isnull=True, **{f"{date_field}__year": year})
    )
    return list(records.order_by("period_start", date_field, "pk"))


def _record_date(record, date_field):
    return record.period_start or getattr(record, date_field)


def _category_labels(category):
    if category is None:
        return "未分类", "未分类"
    path = str(category)
    root = category
    visited = set()
    while root.parent and root.pk not in visited:
        visited.add(root.pk)
        root = root.parent
    return path, root.name


def _category_rows(amounts, total):
    return [
        {
            "category": category,
            "amount": _amount(amount),
            "share_percent": _percent(amount, total),
        }
        for category, amount in sorted(
            amounts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _year_progress(year, as_of_date):
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    days_in_year = (year_end - year_start).days + 1
    if as_of_date < year_start:
        query_through_date = year_start - timedelta(days=1)
        through_date = None
        days_elapsed = 0
    elif as_of_date >= year_end:
        query_through_date = year_end
        through_date = year_end
        days_elapsed = days_in_year
    else:
        query_through_date = as_of_date
        through_date = as_of_date
        days_elapsed = (as_of_date - year_start).days + 1
    return {
        "query_through_date": query_through_date,
        "through_date": through_date,
        "days_elapsed": days_elapsed,
        "days_in_year": days_in_year,
        "elapsed_percent": _percent(days_elapsed, days_in_year),
    }


def ledger_cashflow_budget(actor, *, year, scope=SCOPE_PERSONAL, as_of_date=None):
    """Return scoped ledger cashflow and family budget evidence without portfolio data."""

    _validate_context(actor, scope)
    if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2100:
        raise GlobalAiReadError("收支分析年份不可用。")
    as_of_date = as_of_date or timezone.localdate()
    if type(as_of_date) is not date:
        raise GlobalAiReadError("收支分析截至日期不可用。")

    progress = _year_progress(year, as_of_date)
    query_through_date = progress["query_through_date"]
    income_all = _cashflow_records(
        actor,
        scope=scope,
        year=year,
        model=IncomeRecord,
        date_field="income_date",
    )
    expense_all = _cashflow_records(
        actor,
        scope=scope,
        year=year,
        model=ExpenseRecord,
        date_field="expense_date",
    )
    income_records = [
        record
        for record in income_all
        if _record_date(record, "income_date") <= query_through_date
    ]
    expense_records = [
        record
        for record in expense_all
        if _record_date(record, "expense_date") <= query_through_date
    ]

    base_currency = actor.family.base_currency.upper()
    monthly = {
        month: {"income": ZERO, "expense": ZERO}
        for month in range(1, 13)
    }
    member_totals = {}
    income_categories = defaultdict(lambda: ZERO)
    expense_categories = defaultdict(lambda: ZERO)
    expense_groups = defaultdict(lambda: ZERO)
    other_currency_totals = defaultdict(lambda: {"income": ZERO, "expense": ZERO})
    included_dates = []
    months_with_records = set()
    base_record_count = 0

    for record_type, records, date_field in (
        ("income", income_records, "income_date"),
        ("expense", expense_records, "expense_date"),
    ):
        for record in records:
            effective_date = _record_date(record, date_field)
            included_dates.append(effective_date)
            months_with_records.add(effective_date.month)
            amount = record.amount or ZERO
            currency = (record.currency or "").upper()
            if currency != base_currency:
                other_currency_totals[currency or "未填写"][record_type] += amount
                continue
            base_record_count += 1
            monthly[effective_date.month][record_type] += amount
            member = member_totals.setdefault(
                record.member_id,
                {
                    "member_id": record.member_id,
                    "member_name": record.member.display_name,
                    "income": ZERO,
                    "expense": ZERO,
                },
            )
            member[record_type] += amount
            category_path, root_name = _category_labels(record.category)
            if record_type == "income":
                income_categories[category_path] += amount
            else:
                expense_categories[category_path] += amount
                expense_groups[root_name] += amount

    income_total = sum((row["income"] for row in monthly.values()), ZERO)
    expense_total = sum((row["expense"] for row in monthly.values()), ZERO)
    net_cashflow = income_total - expense_total
    expected_months = (
        set(range(1, progress["through_date"].month + 1))
        if progress["through_date"]
        else set()
    )
    future_months = sorted(set(range(1, 13)) - expected_months)
    warnings = []
    if not income_records and not expense_records:
        warnings.append("截至所选日期没有当前范围的收入或支出记录。")
    future_record_count = (
        len(income_all) + len(expense_all) - len(income_records) - len(expense_records)
    )
    if future_record_count:
        warnings.append(f"有 {future_record_count} 笔所选年度记录晚于截至日，未计入实际值。")
    if other_currency_totals:
        warnings.append("非本位币记录未与年度预算混合，已按原币单独列出。")

    budget_data = None
    if scope == SCOPE_FAMILY:
        budget = AnnualBudget.objects.filter(family=actor.family, year=year).first()
        if budget is None:
            warnings.append("所选年度没有家庭预算。")
        else:
            # Reuse the ledger page's category and legacy-alias matching rules so
            # AI figures reconcile with the budget report visible to the family.
            from ledger.views import build_budget_report

            budget_report = build_budget_report(
                budget,
                currency=base_currency,
                through_date=query_through_date,
            )
            ratio = Decimal(progress["days_elapsed"]) / Decimal(progress["days_in_year"])

            def budget_row(row):
                annual_budget = row["budget"] or ZERO
                actual = row["actual"] or ZERO
                linear_budget = (annual_budget * ratio).quantize(
                    AMOUNT_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )
                return {
                    "line_id": row["line"].pk,
                    "line_type": row["line"].line_type,
                    "category": row["category_label"] or "未分类",
                    "annual_budget": _amount(annual_budget),
                    "actual_to_date": _amount(actual),
                    "annual_variance": _amount(actual - annual_budget),
                    "annual_execution_percent": _percent(actual, annual_budget),
                    "linear_budget_to_date": _amount(linear_budget),
                    "variance_to_linear_budget": _amount(actual - linear_budget),
                    "remark": row["line"].remark,
                }

            summary = budget_report["summary"]
            income_linear = (summary["income_budget"] * ratio).quantize(
                AMOUNT_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
            expense_linear = (summary["expense_budget"] * ratio).quantize(
                AMOUNT_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
            net_linear = income_linear - expense_linear
            budget_data = {
                "budget_id": budget.pk,
                "year": budget.year,
                "currency": base_currency,
                "remark": budget.remark,
                "linear_pacing_note": "截至日预算按全年天数均匀摊分，仅用于观察进度。",
                "summary": {
                    "income_budget": _amount(summary["income_budget"]),
                    "income_actual_to_date": _amount(summary["income_actual"]),
                    "income_variance_to_annual": _amount(summary["income_variance"]),
                    "income_linear_budget_to_date": _amount(income_linear),
                    "income_variance_to_linear_budget": _amount(
                        summary["income_actual"] - income_linear
                    ),
                    "expense_budget": _amount(summary["expense_budget"]),
                    "expense_actual_to_date": _amount(summary["expense_actual"]),
                    "expense_variance_to_annual": _amount(summary["expense_variance"]),
                    "expense_linear_budget_to_date": _amount(expense_linear),
                    "expense_variance_to_linear_budget": _amount(
                        summary["expense_actual"] - expense_linear
                    ),
                    "net_budget": _amount(summary["net_budget"]),
                    "net_actual_to_date": _amount(summary["net_actual"]),
                    "net_variance_to_annual": _amount(summary["net_variance"]),
                    "net_linear_budget_to_date": _amount(net_linear),
                    "net_variance_to_linear_budget": _amount(
                        summary["net_actual"] - net_linear
                    ),
                },
                "lines": [budget_row(row) for row in budget_report["line_rows"]],
            }
    else:
        warnings.append("年度预算是家庭级口径，个人财务范围不与家庭预算比较。")

    result = {
        "module": "ledger",
        "report": "cashflow_budget",
        "scope": scope,
        "year": year,
        "base_currency": base_currency,
        "as_of_date": as_of_date.isoformat(),
        "coverage": {
            "through_date": (
                progress["through_date"].isoformat()
                if progress["through_date"]
                else None
            ),
            "days_elapsed": progress["days_elapsed"],
            "days_in_year": progress["days_in_year"],
            "year_elapsed_percent": progress["elapsed_percent"],
            "first_record_date": min(included_dates).isoformat() if included_dates else None,
            "latest_record_date": max(included_dates).isoformat() if included_dates else None,
            "months_with_records": sorted(months_with_records),
            "months_without_records_through_as_of": sorted(expected_months - months_with_records),
            "future_months": future_months,
            "year_record_count": len(income_all) + len(expense_all),
            "included_record_count": len(income_records) + len(expense_records),
            "included_base_currency_record_count": base_record_count,
            "future_record_count": future_record_count,
        },
        "totals": {
            "income": _amount(income_total),
            "expense": _amount(expense_total),
            "net_cashflow": _amount(net_cashflow),
            "savings_rate_percent": _percent(net_cashflow, income_total),
        },
        "monthly": [
            {
                "month": month,
                "income": _amount(row["income"]),
                "expense": _amount(row["expense"]),
                "net_cashflow": _amount(row["income"] - row["expense"]),
            }
            for month, row in monthly.items()
        ],
        "member_totals": [
            {
                **row,
                "income": _amount(row["income"]),
                "expense": _amount(row["expense"]),
                "net_cashflow": _amount(row["income"] - row["expense"]),
            }
            for row in sorted(
                member_totals.values(),
                key=lambda item: (item["member_name"], item["member_id"]),
            )
        ],
        "income_categories": _category_rows(income_categories, income_total),
        "expense_categories": _category_rows(expense_categories, expense_total),
        "expense_groups": _category_rows(expense_groups, expense_total),
        "other_currency_totals": [
            {
                "currency": currency,
                "income": _amount(row["income"]),
                "expense": _amount(row["expense"]),
                "net_cashflow": _amount(row["income"] - row["expense"]),
            }
            for currency, row in sorted(other_currency_totals.items())
        ],
        "budget": budget_data,
        "member_ids": (
            [actor.pk]
            if scope == SCOPE_PERSONAL
            else sorted({record.member_id for record in income_records + expense_records})
        ),
        "warnings": warnings,
    }
    result["evidence_fingerprint"] = sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return result


def portfolio_accounts(actor, *, scope=SCOPE_PERSONAL):
    """List accessible investment accounts without reading ledger balances."""

    _validate_context(actor, scope)
    accounts = InvestmentAccount.objects.select_related(
        "bank_account__member",
        "bank_account__family",
    ).filter(bank_account__family=actor.family)
    if scope == SCOPE_PERSONAL:
        accounts = accounts.filter(bank_account__member=actor)
    return {
        "module": "portfolio",
        "scope": scope,
        "accounts": [
            {
                "account_id": account.pk,
                "account_name": account.account_name,
                "member_id": account.member_id,
                "member_name": account.member.display_name,
            }
            for account in accounts.order_by(
                "bank_account__member__display_order", "bank_account__account_name", "pk"
            )
        ],
    }


def portfolio_account_snapshot(
    actor,
    *,
    account_id,
    scope=SCOPE_PERSONAL,
    snapshot_id=None,
):
    """Return one account-level portfolio snapshot without consulting ledger data."""

    _validate_context(actor, scope)
    accounts = InvestmentAccount.objects.select_related(
        "bank_account__member",
        "bank_account__family",
    ).filter(bank_account__family=actor.family)
    if scope == SCOPE_PERSONAL:
        accounts = accounts.filter(bank_account__member=actor)
    account = accounts.filter(pk=account_id).first()
    if account is None:
        raise GlobalAiReadError("投资账户不可用。")

    snapshots = PortfolioSnapshot.objects.filter(
        family=actor.family,
        member_id=account.member_id,
        account=account,
    )
    if snapshot_id is not None:
        snapshot = snapshots.filter(pk=snapshot_id).first()
    else:
        snapshot = snapshots.order_by("-snapshot_date", "-pk").first()
    if snapshot is None:
        raise GlobalAiReadError("该投资账户没有可读取的账户级快照。")

    lines = list(
        snapshot.position_lines.select_related("security", "account__bank_account__member")
        .filter(account=account)
        .order_by("asset_type", "asset_name", "pk")
    )
    amounts = defaultdict(lambda: ZERO)
    for line in lines:
        amounts[line.asset_type or "unknown"] += line.market_value

    audit = snapshot.extra_data if isinstance(snapshot.extra_data, dict) else {}
    audit_available = "complete" in audit
    complete = audit.get("complete") is True
    warnings = []
    if not audit_available:
        warnings.append("该快照缺少完整性审计标记。")
    if audit.get("missing_exchange_rates"):
        warnings.append("该快照存在缺失汇率。")
    if audit.get("missing_prices"):
        warnings.append("该快照存在缺失价格。")
    if audit.get("stale_prices"):
        warnings.append("该快照存在过期价格。")
    if audit.get("valuation_errors"):
        warnings.append("该快照存在估值错误。")

    percentages = None
    if complete and snapshot.total_asset:
        percentages = {
            key: str((value / snapshot.total_asset * Decimal("100")).quantize(Decimal("0.01")))
            for key, value in sorted(amounts.items())
        }
    return {
        "module": "portfolio",
        "scope": scope,
        "account_id": account.pk,
        "account_name": account.account_name,
        "member_id": account.member_id,
        "member_name": account.member.display_name,
        "snapshot_id": snapshot.pk,
        "valuation_date": snapshot.snapshot_date.isoformat(),
        "currency": snapshot.currency,
        "complete": complete,
        "total_cash": _decimal(snapshot.total_cash),
        "total_market_value": _decimal(snapshot.total_market_value),
        "total_asset": _decimal(snapshot.total_asset),
        "amounts_by_asset_type": {
            key: _decimal(value) for key, value in sorted(amounts.items())
        },
        "percentages_by_asset_type": percentages,
        "warnings": warnings,
        "positions": [
            {
                "asset_type": line.asset_type,
                "asset_name": line.asset_name,
                "security_id": line.security_id,
                "quantity": _decimal(line.quantity),
                "price": _decimal(line.price),
                "price_as_of": line.price_as_of.isoformat() if line.price_as_of else None,
                "price_source": line.price_source,
                "pricing_status": line.pricing_status,
                "currency": line.currency,
                "fx_rate": _decimal(line.fx_rate),
                "fx_rate_as_of": line.fx_rate_as_of.isoformat() if line.fx_rate_as_of else None,
                "market_value_original": _decimal(line.market_value_original),
                "market_value": _decimal(line.market_value),
            }
            for line in lines
        ],
    }
