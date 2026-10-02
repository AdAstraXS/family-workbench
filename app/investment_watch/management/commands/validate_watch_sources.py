"""Real-source acceptance in an explicitly isolated loopback database."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from family_core.models import Family
from intelligence.http_client import fetch_public_url
from investment_watch.collection import collect_source, seed_sources
from investment_watch.models import NewsSource
from investment_watch.source_templates import parse_source


class Command(BaseCommand):
    help = "Fetch selected public sources once, replay each response, and save local acceptance evidence."

    def add_arguments(self, parser):
        parser.add_argument("--family", type=int, required=True)
        parser.add_argument("--source", action="append", required=True)

    def handle(self, *args, **options):
        root = (settings.BASE_DIR.parent / ".watch-local").resolve()
        database = settings.DATABASES["default"]
        if (not getattr(settings, "WATCH_LOCAL_PREVIEW", False)
                or database["ENGINE"] != "django.db.backends.sqlite3"
                or Path(database["NAME"]).resolve().parent != root):
            raise CommandError("真实信源验收仅允许独立本机 SQLite 库。")
        if not getattr(settings, "INVESTMENT_WATCH_COLLECT_ENABLED", False):
            raise CommandError("本机采集开关尚未启用。")
        family = Family.objects.filter(pk=options["family"]).first()
        if not family:
            raise CommandError("请先初始化本机验收家庭。")
        seed_sources(family)
        keys = list(dict.fromkeys(options["source"]))
        if len(keys) > 8:
            raise CommandError("每次验收最多选择 8 个来源。")
        sources = {s.key: s for s in NewsSource.objects.filter(
            family=family, key__in=keys
        ).exclude(adapter="official")}
        if set(keys) - sources.keys():
            raise CommandError("来源 key 不存在或不是公开采集来源。")
        output = root / ("source-validation-" + timezone.now().strftime("%Y%m%d-%H%M%S-%f"))
        output.mkdir(parents=True, exist_ok=False)
        results = []
        for key in keys:
            source = sources[key]
            source.enabled = True
            source.save(update_fields=["enabled", "updated_at"])
            captured = []

            def fetch(url, **kwargs):
                # Full response checks parsing again even if the source has a cursor.
                response = fetch_public_url(url)
                captured.append(response)
                return response

            result = {"source": key, "checked_at": timezone.now().isoformat(),
                      **collect_source(source, fetcher=fetch, force=True)}
            if captured and result["status"] == "success":
                response = captured[0]
                rows = parse_source(response.body, source)
                (output / (key + ".body")).write_bytes(response.body)
                replay = collect_source(source, fetcher=lambda *a, **k: response, force=True)
                result.update({
                    "http_status": response.status, "parsed": len(rows),
                    "unknown_date": sum(r.get("published_at") is None for r in rows),
                    "replay_added": replay["added"], "replay_status": replay["status"],
                    "samples": [{**r, "version_id": source.materials.filter(
                        external_id=str(r["external_id"])[:500]
                    ).values_list("current_version_id", flat=True).first()} for r in rows],
                })
            results.append(result)
            self.stdout.write(json.dumps({k: v for k, v in result.items() if k != "samples"},
                                       ensure_ascii=False, default=str))
        report = output / "report.json"
        report.write_text(json.dumps({"scope": "isolated-local", "sources": results},
                                    ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.stdout.write(f"Local acceptance report: {report}")
        if any(r["status"] != "success" or r.get("replay_added") != 0
               or r.get("replay_status") != "success" for r in results):
            raise CommandError("信源或重放验收失败，请查看本机报告及来源状态。")
