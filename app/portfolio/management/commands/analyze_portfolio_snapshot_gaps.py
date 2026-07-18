import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from family_core.household import get_household_family
from ledger.models import AssetBalanceEntry, AssetBalanceSnapshot
from portfolio.market_data import futu_code_for_security
from portfolio.models import (
    InvestmentAccount,
    InvestmentCashMovement,
    InvestmentTransaction,
    PortfolioAccountBalanceAnchor,
    Security,
    SecurityPriceRecord,
)
from portfolio.services import calculate_transactions
from portfolio.valuation import exchange_rate


ZERO = Decimal("0")
DEFAULT_DATES = (
    date(2024, 12, 31),
    date(2025, 12, 31),
    date(2026, 1, 31),
    date(2026, 3, 31),
    date(2026, 4, 30),
    date(2026, 5, 31),
    date(2026, 6, 30),
)


def _target_dates(include_july):
    dates = list(DEFAULT_DATES)
    if include_july:
        current = date(2026, 7, 1)
        end = date(2026, 7, 18)
        while current <= end:
            dates.append(current)
            current += timedelta(days=1)
    return dates


def _states(accounts, dates):
    max_date = max(dates)
    transactions = list(
        InvestmentTransaction.objects.filter(
            account__in=accounts,
            trade_date__lte=max_date,
        )
        .select_related(
            "account__bank_account__member",
            "security__option_contract",
            "security__bond_detail",
        )
        .order_by("trade_date", "created_at", "pk")
    )
    independent_movements = list(
        InvestmentCashMovement.objects.filter(
            account__in=accounts,
            transaction=None,
            movement_date__lte=max_date,
        ).order_by("movement_date", "created_at", "pk")
    )
    grouped = defaultdict(list)
    cash_only = defaultdict(list)
    for item in transactions:
        if item.security_id:
            grouped[(item.account_id, item.security_id)].append(item)
        else:
            cash_only[item.account_id].append(item)
    independent_by_account = defaultdict(list)
    for item in independent_movements:
        independent_by_account[item.account_id].append(item)

    states = {}
    for on_date in dates:
        by_account = {
            account.pk: {"cash": defaultdict(Decimal), "positions": [], "errors": []}
            for account in accounts
        }
        for (account_id, _security_id), items in grouped.items():
            selected = [item for item in items if item.trade_date <= on_date]
            if not selected:
                continue
            try:
                result, updates = calculate_transactions(selected)
            except Exception as exc:
                by_account[account_id]["errors"].append(str(exc))
                continue
            for item, cash_change, *_ in updates:
                by_account[account_id]["cash"][item.currency] += cash_change
            if result.quantity:
                by_account[account_id]["positions"].append(
                    {
                        "security": selected[-1].security,
                        "quantity": result.quantity,
                        "cost": result.remaining_cost,
                    }
                )
        for account_id, items in cash_only.items():
            for item in items:
                if item.trade_date > on_date:
                    continue
                try:
                    _, updates = calculate_transactions([item])
                except Exception as exc:
                    by_account[account_id]["errors"].append(str(exc))
                    continue
                by_account[account_id]["cash"][item.currency] += updates[0][1]
        for account_id, items in independent_by_account.items():
            for item in items:
                if item.movement_date <= on_date:
                    by_account[account_id]["cash"][item.currency] += item.amount
        states[on_date] = by_account
    return states


def _futu_prices(states, dates, delay):
    try:
        from django.conf import settings
        from futu import AuType, KLType, KL_FIELD, OpenQuoteContext, RET_OK
    except ImportError as exc:
        return {}, {"FUTU": str(exc)}

    security_dates = defaultdict(list)
    securities = {}
    for on_date in dates:
        for state in states[on_date].values():
            for position in state["positions"]:
                security = position["security"]
                securities[security.pk] = security
                security_dates[security.pk].append(on_date)

    prices = defaultdict(dict)
    errors = {}
    context = OpenQuoteContext(
        host=settings.FUTU_OPEND_HOST,
        port=settings.FUTU_OPEND_PORT,
    )
    try:
        for index, security_id in enumerate(sorted(security_dates)):
            security = securities[security_id]
            if security.asset_type == Security.TYPE_OPTION:
                option = getattr(security, "option_contract", None)
                if option and security.market == "US":
                    code = (
                        f"US.{option.underlying.symbol}"
                        f"{option.expiration_date:%y%m%d}"
                        f"{'C' if option.option_type == option.CALL else 'P'}"
                        f"{int(option.strike_price * 1000)}"
                    )
                else:
                    code = ""
            elif security.asset_type in {Security.TYPE_STOCK, Security.TYPE_ETF}:
                code = futu_code_for_security(security)
            else:
                errors[f"{security.market}:{security.symbol}"] = (
                    f"{security.get_asset_type_display()}需采用专用品种估值"
                )
                continue
            if not code:
                errors[f"{security.market}:{security.symbol}"] = "没有FUTU代码"
                continue
            start = min(security_dates[security_id]) - timedelta(days=10)
            end = max(security_dates[security_id])
            ret, data, _ = context.request_history_kline(
                code,
                start=start.isoformat(),
                end=end.isoformat(),
                ktype=KLType.K_DAY,
                autype=AuType.NONE,
                fields=[KL_FIELD.DATE_TIME, KL_FIELD.CLOSE],
                max_count=1000,
            )
            if ret != RET_OK:
                errors[f"{security.market}:{security.symbol}"] = str(data)
            else:
                for row in data.to_dict("records"):
                    price_date = date.fromisoformat(str(row["time_key"])[:10])
                    prices[security_id][price_date] = Decimal(str(row["close"]))
            if delay and index + 1 < len(security_dates):
                time.sleep(delay)
    finally:
        context.close()
    return prices, errors


def _latest_price(prices, security_id, on_date):
    available = [item for item in prices.get(security_id, {}) if item <= on_date]
    if not available:
        return None, None
    price_date = max(available)
    return prices[security_id][price_date], price_date


def _stored_prices(states, dates):
    security_ids = {
        position["security"].pk
        for on_date in dates
        for state in states[on_date].values()
        for position in state["positions"]
    }
    prices = defaultdict(dict)
    records = (
        SecurityPriceRecord.objects.filter(
            security_id__in=security_ids,
            price_as_of__date__lte=max(dates),
        )
        .order_by("price_as_of", "pk")
    )
    for record in records:
        prices[record.security_id][record.price_as_of.date()] = record.price
    return prices


def _transaction_prices(states, dates):
    security_ids = {
        position["security"].pk
        for on_date in dates
        for state in states[on_date].values()
        for position in state["positions"]
    }
    prices = defaultdict(dict)
    transactions = (
        InvestmentTransaction.objects.filter(
            security_id__in=security_ids,
            trade_date__in=dates,
            price__gt=0,
        )
        .order_by("trade_date", "created_at", "pk")
    )
    for item in transactions:
        prices[item.security_id][item.trade_date] = item.price
    return prices


def _snapshot_rate(currency, snapshot, on_date):
    currency = currency.upper()
    if currency == "CNY":
        return Decimal("1")
    if snapshot:
        if currency == "USD":
            return snapshot.usd_to_base or None
        if currency == "HKD":
            return snapshot.hkd_to_base or None
    return exchange_rate(currency, "CNY", on_date)


def _anchors_by_account_date(accounts, dates):
    anchors = list(
        PortfolioAccountBalanceAnchor.objects.filter(
            account__in=accounts,
            anchor_date__lte=max(dates),
            is_confirmed=True,
        ).order_by("account_id", "currency", "anchor_date")
    )
    result = {}
    for account in accounts:
        account_anchors = [item for item in anchors if item.account_id == account.pk]
        for on_date in dates:
            selected = []
            for currency in {item.currency for item in account_anchors}:
                candidates = [
                    item
                    for item in account_anchors
                    if item.currency == currency and item.anchor_date <= on_date
                ]
                if not candidates:
                    continue
                latest = candidates[-1]
                if latest.anchor_date == on_date or latest.carry_forward:
                    selected.append(latest)
            if selected:
                result[(account.pk, on_date)] = selected
    return result


class Command(BaseCommand):
    help = "只读试算历史投资账户价值，并与家庭账本资产快照逐账户核对。"

    def add_arguments(self, parser):
        parser.add_argument("--include-july", action="store_true")
        parser.add_argument("--fetch-futu", action="store_true")
        parser.add_argument(
            "--matrix",
            action="store_true",
            help="输出逐账户差额矩阵和成员汇总，而不是逐行估值明细。",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.05,
            help="FUTU逐标的请求间隔秒数，默认1.05秒。",
        )

    def handle(self, *args, **options):
        family = get_household_family()
        dates = _target_dates(options["include_july"])
        ledger_snapshots = {
            item.snapshot_date: item
            for item in AssetBalanceSnapshot.objects.filter(
                family=family,
                snapshot_date__in=dates,
                is_draft=False,
            )
        }
        ledger_account_ids = set(
            AssetBalanceEntry.objects.filter(
                snapshot__in=ledger_snapshots.values(),
                account__isnull=False,
                account__supports_investment=True,
            ).values_list("account_id", flat=True)
        )
        accounts = list(
            InvestmentAccount.objects.filter(bank_account__family=family)
            .filter(
                bank_account_id__in=ledger_account_ids
            )
            .select_related("bank_account__member")
            .order_by("bank_account__member_id", "bank_account__account_name")
        )
        active_ids = set(
            InvestmentAccount.objects.filter(
                bank_account__family=family,
                bank_account__supports_investment=True,
            )
            .filter(transactions__trade_date__lte=max(dates))
            .values_list("pk", flat=True)
        ) | set(
            InvestmentAccount.objects.filter(
                bank_account__family=family,
                bank_account__supports_investment=True,
            )
            .filter(cash_movements__movement_date__lte=max(dates))
            .values_list("pk", flat=True)
        )
        known_ids = {item.pk for item in accounts}
        accounts.extend(
            InvestmentAccount.objects.filter(pk__in=active_ids - known_ids)
            .select_related("bank_account__member")
            .order_by("bank_account__member_id", "bank_account__account_name")
        )
        states = _states(accounts, dates)
        balance_anchors = _anchors_by_account_date(accounts, dates)
        stored_prices = _stored_prices(states, dates)
        transaction_prices = _transaction_prices(states, dates)
        if options["fetch_futu"]:
            prices, price_errors = _futu_prices(states, dates, options["delay"])
            for security_id, dated_prices in stored_prices.items():
                prices[security_id].update(dated_prices)
        else:
            prices, price_errors = stored_prices, {}
        for security_id, dated_prices in transaction_prices.items():
            for price_date, price in dated_prices.items():
                prices[security_id].setdefault(price_date, price)

        ledger_values = defaultdict(Decimal)
        for row in AssetBalanceEntry.objects.filter(
            snapshot__in=ledger_snapshots.values(),
            account_id__in=[item.bank_account_id for item in accounts],
        ):
            ledger_values[(row.snapshot.snapshot_date, row.account_id)] += row.base_amount

        if not options["matrix"]:
            self.stdout.write(
                "日期\t成员\t投资账户\t账本余额(CNY)\t试算现金(CNY)\t试算持仓(CNY)\t"
                "试算总额(CNY)\t差额(试算-账本)\t缺价数\t说明"
            )
        result_rows = []
        for on_date in dates:
            snapshot = ledger_snapshots.get(on_date)
            for account in accounts:
                state = states[on_date][account.pk]
                ledger_value = ledger_values.get((on_date, account.bank_account_id))
                anchors = balance_anchors.get((account.pk, on_date), [])
                if (
                    ledger_value is None
                    and not state["positions"]
                    and not any(state["cash"].values())
                    and not anchors
                ):
                    continue
                cash_value = ZERO
                missing_rates = []
                if anchors:
                    for anchor in anchors:
                        if anchor.anchor_date == on_date:
                            cash_value += anchor.recorded_base_amount
                            continue
                        rate = _snapshot_rate(anchor.currency, snapshot, on_date)
                        if rate is None:
                            missing_rates.append(anchor.currency)
                        else:
                            cash_value += anchor.original_amount * rate
                else:
                    for currency, amount in state["cash"].items():
                        rate = _snapshot_rate(currency, snapshot, on_date)
                        if rate is None:
                            missing_rates.append(currency)
                        else:
                            cash_value += amount * rate
                market_value = ZERO
                missing_prices = []
                stale_price_dates = []
                for position in (() if anchors else state["positions"]):
                    security = position["security"]
                    price, price_date = _latest_price(prices, security.pk, on_date)
                    rate = _snapshot_rate(security.currency, snapshot, on_date)
                    if price is None:
                        missing_prices.append(f"{security.market}:{security.symbol}")
                        continue
                    if rate is None:
                        missing_rates.append(security.currency)
                        continue
                    market_value += security.market_value_for(
                        position["quantity"], price
                    ) * rate
                    if price_date != on_date:
                        stale_price_dates.append(
                            f"{security.symbol}@{price_date.isoformat()}"
                        )
                complete = not missing_prices and not missing_rates and not state["errors"]
                total = cash_value + market_value
                difference = (
                    total - ledger_value
                    if complete and ledger_value is not None
                    else None
                )
                notes = []
                if anchors:
                    notes.append("家庭账本余额锚点")
                if snapshot is None:
                    notes.append("无家庭账本快照")
                if missing_prices:
                    notes.append("缺价:" + ",".join(missing_prices))
                if missing_rates:
                    notes.append("缺汇率:" + ",".join(sorted(set(missing_rates))))
                if state["errors"]:
                    notes.append("流水错误:" + "|".join(state["errors"]))
                if stale_price_dates:
                    notes.append("沿用前收盘:" + ",".join(stale_price_dates))
                values = (
                    on_date,
                    account.member.display_name,
                    account.account_name,
                    ledger_value,
                    cash_value,
                    market_value,
                    total,
                    difference,
                    len(missing_prices),
                    ";".join(notes),
                )
                result_rows.append(
                    {
                        "date": on_date,
                        "member": account.member.display_name,
                        "account": account.account_name,
                        "ledger": ledger_value,
                        "calculated": total,
                        "difference": difference,
                        "complete": complete,
                        "missing_prices": len(missing_prices),
                    }
                )
                if not options["matrix"]:
                    self.stdout.write(
                        "\t".join("" if value is None else str(value) for value in values)
                    )

        if options["matrix"]:
            self._write_matrix(result_rows, dates)

        if not options["fetch_futu"]:
            required = {
                position["security"]
                for on_date in dates
                for state in states[on_date].values()
                for position in state["positions"]
            }
            self.stdout.write(
                f"行情未抓取；历史持仓涉及 {len(required)} 个标的。使用 --fetch-futu 试查历史收盘价。"
            )
        elif price_errors:
            self.stdout.write(f"无法自动估值的标的 {len(price_errors)} 个：")
            for code, message in sorted(price_errors.items()):
                self.stdout.write(f"{code}\t{message}")

    def _write_matrix(self, rows, dates):
        by_account = defaultdict(dict)
        for row in rows:
            by_account[(row["member"], row["account"])][row["date"]] = row
        self.stdout.write(
            "成员\t投资账户\t" + "\t".join(item.isoformat() for item in dates)
        )
        for (member, account), date_rows in sorted(by_account.items()):
            cells = []
            for on_date in dates:
                row = date_rows.get(on_date)
                if not row:
                    cells.append("")
                elif row["ledger"] is None:
                    cells.append("无账本")
                elif not row["complete"]:
                    cells.append(f"缺价{row['missing_prices']}")
                else:
                    cells.append(f"{row['difference']:.2f}")
            self.stdout.write("\t".join((member, account, *cells)))

        self.stdout.write(
            "日期\t范围\t账本总额\t可比账本\t可比试算\t可比差额\t"
            "缺价账户账本\t无账本试算"
        )
        members = sorted({row["member"] for row in rows})
        for on_date in dates:
            for scope in ("全部", *members):
                selected = [
                    row
                    for row in rows
                    if row["date"] == on_date
                    and (scope == "全部" or row["member"] == scope)
                ]
                ledger_total = sum(
                    (row["ledger"] for row in selected if row["ledger"] is not None),
                    ZERO,
                )
                comparable = [
                    row
                    for row in selected
                    if row["ledger"] is not None and row["complete"]
                ]
                comparable_ledger = sum((row["ledger"] for row in comparable), ZERO)
                comparable_calculated = sum(
                    (row["calculated"] for row in comparable), ZERO
                )
                incomplete_ledger = sum(
                    (
                        row["ledger"]
                        for row in selected
                        if row["ledger"] is not None and not row["complete"]
                    ),
                    ZERO,
                )
                no_ledger_calculated = sum(
                    (
                        row["calculated"]
                        for row in selected
                        if row["ledger"] is None and row["complete"]
                    ),
                    ZERO,
                )
                self.stdout.write(
                    "\t".join(
                        (
                            on_date.isoformat(),
                            scope,
                            f"{ledger_total:.2f}",
                            f"{comparable_ledger:.2f}",
                            f"{comparable_calculated:.2f}",
                            f"{comparable_calculated - comparable_ledger:.2f}",
                            f"{incomplete_ledger:.2f}",
                            f"{no_ledger_calculated:.2f}",
                        )
                    )
                )
