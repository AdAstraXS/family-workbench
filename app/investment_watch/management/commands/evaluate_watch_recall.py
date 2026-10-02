import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from investment_watch.evaluation import evaluate_recall
from investment_watch.models import MaterialVersion, WatchRule
from investment_watch.services import WatchError


class Command(BaseCommand):
    help = "Read-only recall comparison against an explicitly labelled local sample set; no model calls."

    def add_arguments(self, parser):
        parser.add_argument("--dossier", type=int, required=True)
        parser.add_argument("--labels", required=True)

    def handle(self, *args, **options):
        if not getattr(settings, "WATCH_LOCAL_PREVIEW", False):
            raise CommandError("样本验收仅允许隔离本机设置。")
        rule = WatchRule.objects.select_related("dossier").filter(
            dossier_id=options["dossier"]
        ).first()
        if not rule:
            raise CommandError("关注规则不存在。")
        try:
            labels = json.loads(Path(options["labels"]).read_text(encoding="utf-8"))
            cases = labels["cases"]
            versions = {v.pk: v for v in MaterialVersion.objects.filter(
                material__source__family_id=rule.dossier.family_id
            ).select_related("material__source")}
            result = evaluate_recall(rule, cases, versions)
        except (OSError, ValueError, KeyError, TypeError, WatchError) as exc:
            raise CommandError("无法读取或核对标注集：" + str(exc)) from exc
        result["label_origin"] = labels.get("label_origin", "未注明")
        self.stdout.write(json.dumps(result, ensure_ascii=False))
