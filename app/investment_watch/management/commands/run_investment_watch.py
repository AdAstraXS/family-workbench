import json
from django.core.management.base import BaseCommand, CommandError
from family_core.models import Family
from investment_watch.worker import run_cycle
from investment_watch.collection import seed_sources


class Command(BaseCommand):
    help = "Run a bounded investment news cycle; scheduled by NAS, not web reads."

    def add_arguments(self, parser):
        parser.add_argument("--family", type=int, required=True)
        parser.add_argument("--seed", action="store_true")
        parser.add_argument("--no-collect", action="store_true")
        parser.add_argument("--no-analyze", action="store_true")

    def handle(self, *args, **options):
        family = Family.objects.filter(pk=options["family"]).first()
        if not family:
            raise CommandError("家庭不存在。")
        if options["seed"]:
            seed_sources(family)
        from django.conf import settings
        from investment_watch.models import WatchConsent
        if (not options["no_analyze"] and getattr(settings, "INVESTMENT_WATCH_BODY_ENABLED", False)
                and WatchConsent.objects.filter(dossier__family=family, active=True).exists()):
            from investment_watch.capture_account import refresh_usage
            refresh_usage()
        result = run_cycle(
            family, collect=not options["no_collect"], analyze=not options["no_analyze"]
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        if any(s["status"] in {"failed", "partial"} for s in result.get("sources", [])):
            raise CommandError("部分来源失败；旧材料仍保留，请查看来源状态。")
        if result.get("blocked"):
            raise CommandError(
                "部分分析任务受阻；请查看本人任务的授权、额度或输入状态。"
            )
