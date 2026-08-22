from django.core.management.base import BaseCommand, CommandError

from family_core.models import Family, FamilyMember
from intelligence.automation import run_intelligence_cycle
from intelligence.models import CollectionRun


class Command(BaseCommand):
    help = "运行一次 M4.1 自动情报循环：增量采集、受控 AI 整理、自动分流和简报更新。"

    def add_arguments(self, parser):
        parser.add_argument("--family-id", type=int, required=True, help="目标家庭 ID。")
        parser.add_argument("--provider-id", type=int, help="可选：指定文本 AI 服务商 ID。")
        parser.add_argument(
            "--max-items",
            type=int,
            default=20,
            help="每个到期信源最多读取条目数（1–50，默认 20）。",
        )

    def handle(self, *args, **options):
        max_items = options["max_items"]
        if not 1 <= max_items <= 50:
            raise CommandError("--max-items 必须在 1 到 50 之间。")
        family = Family.objects.filter(pk=options["family_id"]).first()
        if family is None:
            raise CommandError("找不到指定家庭。")
        member = (
            FamilyMember.objects.filter(
                family=family,
                role=FamilyMember.ROLE_ADMIN,
                user__is_active=True,
            )
            .select_related("user")
            .order_by("pk")
            .first()
        )
        if member is None:
            raise CommandError("该家庭没有可用于自动任务审计的启用管理员成员。")

        result = run_intelligence_cycle(
            family=family,
            member=member,
            user=member.user,
            provider_id=options.get("provider_id"),
            max_items=max_items,
        )
        if result.skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"已有未结束的自动情报循环 #{result.run.pk}，本次未重复启动。"
                )
            )
            return
        run = result.run
        summary = (
            f"自动循环 #{run.pk}：AI 整理 {run.classified_count}，"
            f"自动发布 {run.selected_count}，异常待复核 {run.review_count}，"
            f"噪音 {run.noise_count}，失败 {run.failed_count}"
        )
        if result.digest_id:
            summary += f"，简报 #{result.digest_id}"
        summary += "。"
        if run.status == CollectionRun.STATUS_SUCCESS:
            self.stdout.write(self.style.SUCCESS(summary))
            return
        self.stderr.write(self.style.ERROR(summary))
        if run.error_summary:
            self.stderr.write(run.error_summary)
        raise CommandError("自动情报循环未完全成功；成功完成的步骤已保留审计记录。")
