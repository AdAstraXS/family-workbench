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
        result = run_probe(
            sys.argv[1:], profile="m1-gate", max_expirations=1,
            max_contracts_per_expiration=3,
        )
        # run_probe closes the SDK context and verifies subscriptions before returning.
        print("\nWHEEL_LIVE:" + json.dumps(result, ensure_ascii=True), flush=True)
    except Exception:
        sys.exit(1)
