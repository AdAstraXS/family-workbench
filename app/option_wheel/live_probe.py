"""Isolated, read-only SDK process. No financial evidence writes."""
import json
import os
import sys


if __name__ == "__main__":
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django
        django.setup()
        from portfolio.futu_option_probe import run_probe
        arguments = sys.argv[1:]
        calls_for = set()
        if arguments and arguments[0].startswith("--calls-for="):
            calls_for = {
                "US." + value.removeprefix("US.").upper()
                for value in arguments.pop(0).split("=", 1)[1].split(",")
                if value
            }
        result = run_probe(
            arguments, profile="m1-gate", max_expirations=3,
            max_contracts_per_expiration=3,
            covered_call_symbols=calls_for,
        )
        # run_probe closes the SDK context and verifies subscriptions before returning.
        print("\nWHEEL_LIVE:" + json.dumps(result, ensure_ascii=True), flush=True)
    except Exception:
        sys.exit(1)
