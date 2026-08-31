"""Django command entry point for the read-only Futu option capability probe."""

import json

from django.core.management.base import BaseCommand, CommandError

from portfolio.futu_option_probe import FAILED, PARTIAL, run_probe


def format_json(result):
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)


def format_table(result):
    subscription = result.get("subscription", {})
    lines = [
        f"Status: {result.get('status', 'unknown')}",
        f"SDK Version: {result.get('sdk_version', 'unknown')}",
        f"Profile: {result.get('profile', 'unknown')}",
        "Subscription Cleanup: "
        f"{subscription.get('cleanup_status', 'unknown')}",
        f"Cleanup Candidates: {subscription.get('owned_codes', [])}",
        "",
    ]
    for symbol_result in result.get("symbols", []):
        lines.append(
            f"  {symbol_result.get('symbol', '?')}: "
            f"status={symbol_result.get('status', '?')} "
            f"expirations={len(symbol_result.get('expirations', []))} "
            "contracts="
            f"{len(symbol_result.get('representative_contracts', []))} "
            f"errors={len(symbol_result.get('errors', []))}"
        )
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Read-only Futu option capability probe (no DB write, no trading)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--symbols", nargs="+", required=True)
        parser.add_argument(
            "--max-expirations", type=int, choices=[1, 2, 3], default=1
        )
        parser.add_argument(
            "--max-contracts-per-expiration",
            type=int,
            choices=[1, 2, 3],
            default=1,
        )
        parser.add_argument(
            "--profile", choices=["static", "m1-gate"], default="static"
        )
        parser.add_argument("--subscribe-quotes", action="store_true")
        parser.add_argument("--include-option-analytics", action="store_true")
        parser.add_argument("--include-history", action="store_true")
        parser.add_argument("--include-earnings", action="store_true")
        parser.add_argument("--allow-partial", action="store_true")
        parser.add_argument(
            "--format", choices=["table", "json"], default="table"
        )

    def handle(self, *args, **options):
        result = run_probe(
            symbols=options["symbols"],
            max_expirations=options["max_expirations"],
            max_contracts_per_expiration=options[
                "max_contracts_per_expiration"
            ],
            profile=options["profile"],
            subscribe_quotes=options["subscribe_quotes"],
            include_option_analytics=options["include_option_analytics"],
            include_history=options["include_history"],
            include_earnings=options["include_earnings"],
            allow_partial=options["allow_partial"],
        )

        if options["format"] == "json":
            self.stdout.write(format_json(result))
        else:
            self.stdout.write(format_table(result))

        status = result.get("status")
        if status == FAILED:
            raise CommandError("probe failed")
        if status == PARTIAL and not options["allow_partial"]:
            raise CommandError("probe partial")
