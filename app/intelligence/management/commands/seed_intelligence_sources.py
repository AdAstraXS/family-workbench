from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from intelligence.models import IntelligenceSource, IntelligenceSubject


SOURCE_DEFINITIONS = (
    {
        "name": "OpenAI 官方新闻 RSS",
        "url": "https://openai.com/news/rss.xml",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_OFFICIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "OpenAI",
        "topics": (
            "OpenAI", "Sam Altman", "Artificial Intelligence Infrastructure", "AI Agents",
        ),
        "poll_interval_minutes": 120,
        "enabled_by_default": True,
    },
    {
        "name": "NVIDIA 官方新闻稿 RSS",
        "url": "https://nvidianews.nvidia.com/releases.xml",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_OFFICIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "NVIDIA Corporation",
        "topics": ("NVIDIA Corporation", "Jensen Huang", "Artificial Intelligence Infrastructure"),
        "poll_interval_minutes": 120,
        "enabled_by_default": True,
    },
    {
        "name": "NVIDIA 官方博客 RSS",
        "url": "https://feeds.feedburner.com/nvidiablog",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_OFFICIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "NVIDIA Corporation",
        "topics": ("NVIDIA Corporation", "Jensen Huang", "Artificial Intelligence Infrastructure"),
        "poll_interval_minutes": 120,
        "enabled_by_default": True,
    },
    {
        "name": "OpenAI 官方 YouTube",
        "url": "https://www.youtube.com/@OpenAI",
        "external_id": "UCXZCJLdBC09xxGZ6gcdrc6A",
        "adapter_key": IntelligenceSource.ADAPTER_YOUTUBE,
        "source_type": IntelligenceSource.TYPE_YOUTUBE,
        "source_group": IntelligenceSource.GROUP_SOCIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "OpenAI",
        "topics": (
            "OpenAI", "Sam Altman", "Artificial Intelligence Infrastructure", "AI Agents",
        ),
        "poll_interval_minutes": 180,
        "enabled_by_default": False,
    },
    {
        "name": "NVIDIA 官方 YouTube",
        "url": "https://www.youtube.com/@NVIDIA",
        "external_id": "UCL-g3eGJi1omSDSz48AML-g",
        "adapter_key": IntelligenceSource.ADAPTER_YOUTUBE,
        "source_type": IntelligenceSource.TYPE_YOUTUBE,
        "source_group": IntelligenceSource.GROUP_SOCIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "NVIDIA Corporation",
        "topics": ("NVIDIA Corporation", "Jensen Huang", "Artificial Intelligence Infrastructure"),
        "poll_interval_minutes": 180,
        "enabled_by_default": False,
    },
    {
        "name": "ARK Invest 官方 YouTube",
        "url": "https://www.youtube.com/@ARKInvest2015",
        "external_id": "UCK-zlnUfoDHzUwXcbddtnkg",
        "adapter_key": IntelligenceSource.ADAPTER_YOUTUBE,
        "source_type": IntelligenceSource.TYPE_YOUTUBE,
        "source_group": IntelligenceSource.GROUP_SOCIAL,
        "source_tier": IntelligenceSource.TIER_A,
        "primary": "ARK Investment Management LLC",
        "topics": ("ARK Investment Management LLC", "Cathie Wood"),
        "poll_interval_minutes": 180,
        "enabled_by_default": False,
    },
    {
        "name": "Ray Dalio 官方 YouTube",
        "url": "https://www.youtube.com/@principlesbyraydalio",
        "external_id": "UCqvaXJ1K3HheTPNjH-KpwXQ",
        "adapter_key": IntelligenceSource.ADAPTER_YOUTUBE,
        "source_type": IntelligenceSource.TYPE_YOUTUBE,
        "source_group": IntelligenceSource.GROUP_EXPERT,
        "source_tier": IntelligenceSource.TIER_B,
        "primary": "Ray Dalio",
        "topics": ("Ray Dalio",),
        "poll_interval_minutes": 240,
        "enabled_by_default": False,
    },
)


class Command(BaseCommand):
    help = "幂等登记 M2 首批官方 RSS 与 YouTube 信源；不立即联网采集。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="展示变化并回滚。")

    @transaction.atomic
    def handle(self, *args, **options):
        needed_names = {
            name
            for definition in SOURCE_DEFINITIONS
            for name in (definition["primary"], *definition["topics"])
        }
        subjects = {
            subject.canonical_name: subject
            for subject in IntelligenceSubject.objects.filter(canonical_name__in=needed_names)
        }
        missing = sorted(needed_names - subjects.keys())
        if missing:
            raise CommandError(
                "缺少关注主题，请先运行 seed_key_people：" + "、".join(missing)
            )

        created_count = 0
        updated_count = 0
        for definition in SOURCE_DEFINITIONS:
            defaults = {
                "subject": subjects[definition["primary"]],
                "name": definition["name"],
                "external_id": definition["external_id"],
                "source_type": definition["source_type"],
                "source_group": definition["source_group"],
                "source_tier": definition["source_tier"],
                "poll_interval_minutes": definition["poll_interval_minutes"],
                "transport_weight": 100,
                "is_active": definition["enabled_by_default"],
            }
            source, created = IntelligenceSource.objects.update_or_create(
                adapter_key=definition["adapter_key"],
                url=definition["url"],
                defaults=defaults,
            )
            source.topics.add(*(subjects[name] for name in definition["topics"]))
            created_count += int(created)
            updated_count += int(not created)

        enabled_count = sum(definition["enabled_by_default"] for definition in SOURCE_DEFINITIONS)
        summary = (
            f"M2 官方信源：新增 {created_count}，更新 {updated_count}；"
            f"默认启用 {enabled_count} 个 RSS，YouTube 登记但停用；未执行联网采集。"
        )
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("试运行（未保存）— " + summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
