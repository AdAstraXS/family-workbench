"""Read-only view data for Put positions recorded in the portfolio."""

from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from portfolio.models import InvestmentPosition, OptionContract, Security

from .position_evidence import participating_accounts
from .models import WheelAnalysisJob, WheelCycle, WheelWatchItem

NY = ZoneInfo("America/New_York")


def open_put_rows(family):
    accounts = participating_accounts(family)
    account_ids = [account.pk for account in accounts.values() if account is not None]
    today = timezone.now().astimezone(NY).date()
    watch = {row.symbol: row for row in WheelWatchItem.objects.filter(family=family)}
    jobs = list(WheelAnalysisJob.objects.filter(family=family, status="saved").order_by("-created_at")[:30])
    positions = InvestmentPosition.objects.filter(
        account_id__in=account_ids, security__asset_type=Security.TYPE_OPTION,
        security__option_contract__option_type=OptionContract.PUT, quantity__lt=0,
    ).select_related("account__bank_account", "security__option_contract__underlying")
    rows = []
    for position in positions:
        contract = position.security.option_contract
        symbol = contract.underlying.symbol.upper()
        stock = watch.get(symbol)
        spot = stock.price if stock and stock.price and stock.price > 0 else None
        mark = position.current_price if position.current_price_as_of and position.current_price >= 0 else None
        intrinsic = max(contract.strike_price - spot, Decimal(0)) if spot is not None else None
        time_value = max(mark - intrinsic, Decimal(0)) if mark is not None and intrinsic is not None else None
        cycle = WheelCycle.objects.filter(family=family, account=position.account,
                                          underlying=contract.underlying).order_by("-opened_on", "-pk").first()
        cycle_net = sum((leg.premium_total for leg in cycle.legs.all()), Decimal(0)) if cycle else None
        roll_options = []
        for job in jobs:
            target = job.selection.get("target_expiration")
            if not target or target <= contract.expiration_date.isoformat():
                continue
            for candidate in job.screening_results:
                if candidate.get("symbol") != symbol or candidate.get("strategy") != "PUT":
                    continue
                try:
                    new_strike = Decimal(candidate["strike"])
                    new_premium = Decimal(candidate["premium"])
                except (TypeError, ValueError, ArithmeticError):
                    continue
                if new_strike > contract.strike_price or new_premium <= 0:
                    continue
                roll_options.append({
                    "code": candidate.get("code"), "expiration": target,
                    "strike": new_strike, "premium": new_premium,
                    "extra_days": (date.fromisoformat(target) - contract.expiration_date).days,
                    "break_even": candidate.get("break_even"),
                    "probability": candidate.get("probability"),
                    "source_time": job.created_at,
                    "basis": "收盘参考" if job.selection.get("mode") == "screening_close_v2" else "历史 Bid 观察",
                })
            if roll_options:
                break
        roll_options.sort(key=lambda row: (row["strike"] != contract.strike_price,
                                           Decimal(row["probability"]) if row["probability"] is not None else Decimal(101)))
        rows.append({
            "position": position, "account": position.account.account_name,
            "symbol": symbol, "contract": contract, "contracts": abs(position.quantity),
            "dte": (contract.expiration_date - today).days,
            "spot": spot, "spot_as_of": stock.price_as_of if stock else None,
            "moneyness": spot - contract.strike_price if spot is not None else None,
            "moneyness_abs": abs(spot - contract.strike_price) if spot is not None else None,
            "mark": mark, "mark_as_of": position.current_price_as_of,
            "unrealized_pnl": position.unrealized_pnl if mark is not None else None,
            "open_net_credit": position.avg_cost * abs(position.quantity) * contract.multiplier,
            "intrinsic": intrinsic, "time_value": time_value,
            "event": stock, "cycle_net_premium": cycle_net,
            "roll_options": roll_options[:3],
        })
    return sorted(rows, key=lambda row: (row["dte"], row["symbol"], row["account"]))
