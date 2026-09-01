from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import Family
from portfolio.futu_option_probe import run_probe
from portfolio.models import InvestmentAccount

from option_wheel.analysis_service import WheelAnalysisError, persist_probe_symbol


class Command(BaseCommand):
    help = "只读获取 Futu 证据，并在显式确认后保存 M1 分析快照；绝不连接交易接口。"

    def add_arguments(self, parser):
        parser.add_argument("--family-id", type=int, required=True)
        parser.add_argument("--account-id", type=int, required=True)
        parser.add_argument("--symbols", nargs="+", required=True)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        try:
            family = Family.objects.get(pk=options["family_id"])
            account = InvestmentAccount.objects.select_related("bank_account").get(
                pk=options["account_id"], bank_account__family=family,
            )
        except (Family.DoesNotExist, InvestmentAccount.DoesNotExist):
            raise CommandError("家庭或投资账户映射无效。") from None

        symbols = [f"US.{value.upper().removeprefix('US.')}" for value in options["symbols"]]
        result = run_probe(
            symbols,
            profile="m1-gate",
            max_expirations=1,
            max_contracts_per_expiration=3,
        )
        self.stdout.write(
            f"Futu 探针状态={result['status']}，标的={len(result.get('symbols', []))}，"
            f"订阅清理={result.get('subscription', {}).get('cleanup_status', 'unknown')}"
        )
        if result["status"] != "success":
            raise CommandError("Futu M1 强门控未通过，拒绝保存分析证据。")
        if not options["commit"]:
            self.stdout.write(self.style.WARNING("预演完成：未写数据库；增加 --commit 才会保存。"))
            return
        decisions = []
        try:
            with transaction.atomic():
                for symbol_result in result["symbols"]:
                    decisions.append(
                        persist_probe_symbol(
                            family=family,
                            account=account,
                            symbol_result=symbol_result,
                        )
                    )
        except WheelAnalysisError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            self.style.SUCCESS(
                f"已保存 {len(decisions)} 份只读分析决策；全局下单门禁保持关闭。"
            )
        )
