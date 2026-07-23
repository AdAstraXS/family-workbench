from django.utils import timezone

from family_core.household import get_household_family, get_site_setting

from .exchange_rate_service import ensure_daily_exchange_rates
from .historical_valuation import account_ids_as_of
from .market_data import refresh_market_data
from .models import (
    DailyPortfolioValuationRun,
    InvestmentAccount,
    MarketDataRunStatusChoices,
)
from .snapshot_service import (
    IncompletePortfolioSnapshotError,
    create_portfolio_snapshots_for_date,
)
from .valuation import refresh_position_valuations


class DailyPortfolioValuationError(RuntimeError):
    pass


def _json_exchange_rates(result):
    return {
        "status": result.get("status", ""),
        "source_date": (
            result["source_date"].isoformat()
            if result.get("source_date")
            else None
        ),
        "usd_cny": (
            str(result["usd_cny"]) if result.get("usd_cny") is not None else None
        ),
        "hkd_cny": (
            str(result["hkd_cny"]) if result.get("hkd_cny") is not None else None
        ),
        "error": result.get("error", ""),
        "source_url": result.get("source_url", ""),
    }


def _valuation_counts(valuation):
    return {
        "stale_price_count": len(valuation.get("stale_prices") or []),
        "missing_price_count": len(valuation.get("missing_prices") or []),
        "missing_exchange_rate_count": len(
            valuation.get("missing_exchange_rates")
            or valuation.get("missing_rates")
            or []
        ),
        "valuation_error_count": len(
            valuation.get("valuation_errors")
            or valuation.get("errors")
            or []
        ),
    }


def _valuation_details(valuation):
    return {
        "complete": bool(valuation.get("complete")),
        "missing_exchange_rates": (
            valuation.get("missing_exchange_rates")
            or valuation.get("missing_rates")
            or []
        ),
        "stale_prices": valuation.get("stale_prices") or [],
        "missing_prices": valuation.get("missing_prices") or [],
        "valuation_errors": (
            valuation.get("valuation_errors")
            or valuation.get("errors")
            or []
        ),
    }


def _save_run(run, *, status, valuation=None, error_message=""):
    valuation = valuation or {}
    counts = _valuation_counts(valuation)
    run.finished_at = timezone.now()
    run.status = status
    run.stale_price_count = counts["stale_price_count"]
    run.missing_price_count = counts["missing_price_count"]
    run.missing_exchange_rate_count = counts["missing_exchange_rate_count"]
    run.error_count = (
        counts["valuation_error_count"]
        + (run.market_refresh.error_count if run.market_refresh_id else 0)
        + (
            1
            if run.exchange_rate_status
            and run.exchange_rate_status != "success"
            else 0
        )
        + (1 if error_message else 0)
    )
    details = dict(run.details or {})
    details["valuation"] = valuation
    if error_message:
        details["exception"] = error_message
    run.details = details
    run.save()
    return run


def run_daily_portfolio_valuation(
    *,
    valuation_date=None,
    include_watchlist=False,
    require_complete=False,
    family=None,
):
    valuation_date = valuation_date or timezone.localdate()
    family = family or get_household_family()
    if not family:
        raise DailyPortfolioValuationError("尚未配置家庭，无法执行每日投资组合估值。")

    currency = get_site_setting().base_currency
    run = DailyPortfolioValuationRun.objects.create(
        family=family,
        valuation_date=valuation_date,
        details={
            "base_currency": currency,
            "include_watchlist": include_watchlist,
            "require_complete": require_complete,
        },
    )

    try:
        market_run = refresh_market_data(include_watchlist=include_watchlist)
        run.market_refresh = market_run
        run.quote_success_count = market_run.success_count
        run.details["market_refresh"] = {
            "run_id": market_run.pk,
            "status": market_run.status,
            "target_count": market_run.target_count,
            "success_count": market_run.success_count,
            "stale_count": market_run.stale_count,
            "missing_count": market_run.missing_count,
            "error_count": market_run.error_count,
            "details": market_run.details or {},
        }
        run.save(
            update_fields=[
                "market_refresh",
                "quote_success_count",
                "details",
            ]
        )

        exchange_rates = ensure_daily_exchange_rates()
        run.exchange_rate_status = exchange_rates.get("status", "")
        run.exchange_rate_source_date = exchange_rates.get("source_date")
        run.details["exchange_rates"] = _json_exchange_rates(exchange_rates)
        run.save(
            update_fields=[
                "exchange_rate_status",
                "exchange_rate_source_date",
                "details",
            ]
        )

        if valuation_date == timezone.localdate():
            refresh_position_valuations(on_date=valuation_date)

        accounts = list(
            InvestmentAccount.objects.filter(
                pk__in=account_ids_as_of(family, valuation_date)
            ).select_related("bank_account__member")
        )
        snapshots = create_portfolio_snapshots_for_date(
            family,
            accounts,
            valuation_date,
            currency,
            require_complete=require_complete,
        )
        family_snapshot = next(
            item
            for item in snapshots
            if item.member_id is None and item.account_id is None
        )
        valuation = dict(family_snapshot.extra_data or {})
        run.snapshot_count = len(snapshots)
        run.details["snapshot_ids"] = [item.pk for item in snapshots]
        run.details["family_total_asset"] = str(family_snapshot.total_asset)

        counts = _valuation_counts(valuation)
        has_warning = any(
            [
                market_run.status != MarketDataRunStatusChoices.SUCCESS,
                exchange_rates.get("status") != "success",
                counts["stale_price_count"],
                counts["missing_price_count"],
                counts["missing_exchange_rate_count"],
                counts["valuation_error_count"],
            ]
        )
        return _save_run(
            run,
            status=(
                MarketDataRunStatusChoices.PARTIAL
                if has_warning
                else MarketDataRunStatusChoices.SUCCESS
            ),
            valuation=valuation,
        )
    except IncompletePortfolioSnapshotError as exc:
        _save_run(
            run,
            status=MarketDataRunStatusChoices.FAILED,
            valuation=_valuation_details(exc.valuation),
            error_message=str(exc),
        )
        raise DailyPortfolioValuationError(str(exc)) from exc
    except Exception as exc:
        _save_run(
            run,
            status=MarketDataRunStatusChoices.FAILED,
            error_message=str(exc),
        )
        raise DailyPortfolioValuationError(str(exc)) from exc
