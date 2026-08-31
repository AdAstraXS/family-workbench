from django.core.management.base import BaseCommand, CommandError

from option_wheel.account_capacity import (
    CapacityImportError,
    build_portfolio_capacity,
    import_portfolio_capacity,
)


class Command(BaseCommand):
    help = "从家庭工作台投资组合只读生成车轮账户容量快照。"

    def add_arguments(self, parser):
        parser.add_argument("--account-id", required=True, type=int)
        parser.add_argument("--confirm-no-margin", action="store_true")
        parser.add_argument("--confirm-no-open-orders", action="store_true")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="写入不可变快照；省略时只预演。",
        )

    def handle(self, *args, **options):
        try:
            evidence = build_portfolio_capacity(
                account_id=options["account_id"],
                confirm_no_margin=options["confirm_no_margin"],
                confirm_no_open_orders=options["confirm_no_open_orders"],
            )
            result = import_portfolio_capacity(
                evidence=evidence,
                commit=options["commit"],
            )
        except CapacityImportError as exc:
            raise CommandError(str(exc)) from exc

        mode = "COMMIT" if options["commit"] else "DRY-RUN"
        available = evidence.settled_cash - evidence.reserved_cash
        self.stdout.write(
            f"mode={mode} source=portfolio account_id={evidence.account_id} "
            f"as_of={evidence.source_as_of.isoformat()}"
        )
        self.stdout.write(
            f"currency=USD settled_cash={evidence.settled_cash:.4f} "
            f"unsettled_cash={evidence.unsettled_cash:.4f} "
            f"reserved_cash={evidence.reserved_cash:.4f} "
            f"available_cash={available:.4f} nav={evidence.nav:.4f} "
            f"positions={evidence.positions_summary['count']} "
            f"obligations={evidence.open_obligations['count']}"
        )
        if result.snapshot_id and not result.snapshot_created:
            self.stdout.write(
                self.style.WARNING(
                    f"同一投资组合状态已导入，未重复创建；snapshot_id={result.snapshot_id}。"
                )
            )
        elif options["commit"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"已创建不可变账户容量快照 snapshot_id={result.snapshot_id}。"
                )
            )
        else:
            self.stdout.write(self.style.WARNING("仅预演，数据库未写入；使用 --commit 确认。"))
