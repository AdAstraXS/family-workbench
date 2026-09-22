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
        screening = bool(arguments and arguments[0] == "--screen")
        if screening:
            arguments.pop(0)
        calls_for = set()
        if arguments and arguments[0].startswith("--calls-for="):
            calls_for = {
                "US." + value.removeprefix("US.").upper()
                for value in arguments.pop(0).split("=", 1)[1].split(",")
                if value
            }
        target_expiration = None
        if arguments and arguments[0].startswith("--expiration="):
            target_expiration = arguments.pop(0).split("=", 1)[1]
        result = run_probe(
            arguments, profile="screen" if screening else "m1-gate", max_expirations=1,
            max_contracts_per_expiration=8 if screening else 3,
            covered_call_symbols=calls_for,
            target_expiration=target_expiration,
        )
        # run_probe closes the SDK context and verifies subscriptions before returning.
        print("\nWHEEL_LIVE:" + json.dumps(result, ensure_ascii=True), flush=True)
    except Exception:
        sys.exit(1)
