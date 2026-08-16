from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from intelligence.models import IntelligenceSource, IntelligenceSubject
from intelligence.processing import MEDIA_DISCOVERY_POLICY


MEDIA_TECH_TOPICS = (
    "OpenAI",
    "Sam Altman",
    "NVIDIA Corporation",
    "Jensen Huang",
    "Elon Musk",
    "Tesla, Inc.",
    "Space Exploration Technologies Corp.",
)


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
    {
        "name": "CNBC Technology",
        "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_MEDIA,
        "source_tier": IntelligenceSource.TIER_C,
        "primary": None,
        "topics": MEDIA_TECH_TOPICS,
        "poll_interval_minutes": 30,
        "transport_weight": 90,
        "enabled_by_default": True,
        "extra_data": {
            "discovery_policy": MEDIA_DISCOVERY_POLICY,
            "publisher_key": "cnbc",
            "content_scope": "technology",
            "full_text_policy": "metadata_only",
        },
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_MEDIA,
        "source_tier": IntelligenceSource.TIER_C,
        "primary": None,
        "topics": MEDIA_TECH_TOPICS,
        "poll_interval_minutes": 30,
        "transport_weight": 85,
        "enabled_by_default": True,
        "extra_data": {
            "discovery_policy": MEDIA_DISCOVERY_POLICY,
            "publisher_key": "techcrunch",
            "content_scope": "artificial-intelligence",
            "full_text_policy": "metadata_only",
        },
    },
    {
        "name": "FT Technology",
        "url": "https://www.ft.com/technology?format=rss",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_MEDIA,
        "source_tier": IntelligenceSource.TIER_C,
        "primary": None,
        "topics": MEDIA_TECH_TOPICS,
        "poll_interval_minutes": 60,
        "transport_weight": 90,
        "enabled_by_default": False,
        "extra_data": {
            "discovery_policy": MEDIA_DISCOVERY_POLICY,
            "publisher_key": "ft",
            "content_scope": "technology",
            "full_text_policy": "metadata_only",
        },
    },
    {
        "name": "WSJ Technology",
        "url": "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_MEDIA,
        "source_tier": IntelligenceSource.TIER_C,
        "primary": None,
        "topics": MEDIA_TECH_TOPICS,
        "poll_interval_minutes": 60,
        "transport_weight": 85,
        "enabled_by_default": False,
        "extra_data": {
            "discovery_policy": MEDIA_DISCOVERY_POLICY,
            "publisher_key": "wsj",
            "content_scope": "technology",
            "full_text_policy": "metadata_only",
        },
    },
    {
        "name": "NYT Technology",
        "url": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
        "external_id": "",
        "adapter_key": IntelligenceSource.ADAPTER_RSS,
        "source_type": IntelligenceSource.TYPE_RSS,
        "source_group": IntelligenceSource.GROUP_MEDIA,
        "source_tier": IntelligenceSource.TIER_C,
        "primary": None,
        "topics": MEDIA_TECH_TOPICS,
        "poll_interval_minutes": 60,
        "transport_weight": 85,
        "enabled_by_default": False,
        "extra_data": {
            "discovery_policy": MEDIA_DISCOVERY_POLICY,
            "publisher_key": "nyt",
            "content_scope": "technology",
            "full_text_policy": "metadata_only",
        },
    },
)


class Command(BaseCommand):
    help = "幂等登记 M2 官方信源与 M2.6 媒体发现源；不立即联网采集。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="展示变化并回滚。")

    @transaction.atomic
    def handle(self, *args, **options):
        needed_names = {
            name
            for definition in SOURCE_DEFINITIONS
            for name in (
                *((definition["primary"],) if definition["primary"] else ()),
                *definition["topics"],
            )
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
                "subject": subjects[definition["primary"]] if definition["primary"] else None,
                "name": definition["name"],
                "external_id": definition["external_id"],
                "source_type": definition["source_type"],
                "source_group": definition["source_group"],
                "source_tier": definition["source_tier"],
                "poll_interval_minutes": definition["poll_interval_minutes"],
                "transport_weight": definition.get("transport_weight", 100),
                "is_active": definition["enabled_by_default"],
                "extra_data": definition.get("extra_data", {}),
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
            f"M2/M2.6 信源：新增 {created_count}，更新 {updated_count}；"
            f"默认启用 {enabled_count} 个自动信源，媒体源仅保存公开元数据；"
            "YouTube 登记但停用；未执行联网采集。"
        )
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("试运行（未保存）— " + summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
