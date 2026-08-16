from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from family_core.models import Family
from intelligence.models import IntelligenceSubject, SubjectFollow, SubjectRelation


SUBJECTS = (
    {
        "canonical_name": "Elon Musk",
        "display_name": "埃隆·马斯克",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_TECH_LEADER,
        "aliases": ["Elon Musk", "马斯克"],
        "profile_summary": "Tesla 与 SpaceX 的核心领导者，关注人工智能、机器人、航天与社交平台动向。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Artificial Intelligence Infrastructure",
        "display_name": "AI 基础设施",
        "subject_type": IntelligenceSubject.TYPE_TECHNOLOGY,
        "category": IntelligenceSubject.CATEGORY_TECHNOLOGY,
        "aliases": ["AI Infrastructure", "AI 算力基础设施"],
        "profile_summary": "关注芯片、数据中心、网络、能源与 AI 资本开支。",
        "importance_level": 5,
    },
    {
        "canonical_name": "AI Agents",
        "display_name": "AI Agent",
        "subject_type": IntelligenceSubject.TYPE_TECHNOLOGY,
        "category": IntelligenceSubject.CATEGORY_TECHNOLOGY,
        "aliases": ["AI Agent", "智能体"],
        "profile_summary": "关注可执行任务的智能体技术、产品和商业化进展。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Jensen Huang",
        "display_name": "黄仁勋",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_TECH_LEADER,
        "aliases": ["Jensen Huang", "黄仁勋"],
        "profile_summary": "NVIDIA 创始人兼首席执行官，关注 AI 算力、芯片路线与资本开支周期。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Sam Altman",
        "display_name": "萨姆·奥特曼",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_TECH_LEADER,
        "aliases": ["Sam Altman", "奥特曼", "萨姆·奥特曼"],
        "profile_summary": "OpenAI 首席执行官，关注前沿模型、智能体、算力基础设施与 AI 商业化。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Warren Buffett",
        "display_name": "沃伦·巴菲特",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_INVESTOR,
        "aliases": ["Warren Buffett", "巴菲特"],
        "profile_summary": "长期价值投资者，关注伯克希尔资本配置、股东信与公开讲话。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Ray Dalio",
        "display_name": "瑞·达利欧",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_INVESTOR,
        "aliases": ["Ray Dalio", "达里欧"],
        "profile_summary": "桥水基金创始人，关注全球宏观周期、债务与地缘政治观点。",
        "importance_level": 4,
    },
    {
        "canonical_name": "Michael Burry",
        "display_name": "迈克尔·伯里",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_INVESTOR,
        "aliases": ["Michael Burry", "Burry", "伯里"],
        "profile_summary": "Scion Asset Management 投资者，关注监管持仓披露与公开观点。",
        "importance_level": 4,
    },
    {
        "canonical_name": "Cathie Wood",
        "display_name": "凯茜·伍德",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_INVESTOR,
        "aliases": ["Cathie Wood", "木头姐", "凯茜·伍德"],
        "profile_summary": "ARK Invest 创始人，关注颠覆式创新、基金交易与长期技术预测。",
        "importance_level": 4,
    },
    {
        "canonical_name": "Donald Trump",
        "display_name": "唐纳德·特朗普",
        "subject_type": IntelligenceSubject.TYPE_PERSON,
        "category": IntelligenceSubject.CATEGORY_POLICY_LEADER,
        "aliases": ["Donald Trump", "特朗普"],
        "profile_summary": "美国政治人物，关注影响市场、科技监管、贸易与产业政策的公开动向。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Tesla, Inc.",
        "display_name": "Tesla",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["Tesla", "特斯拉"],
        "profile_summary": "电动车、储能、人工智能与机器人公司。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Space Exploration Technologies Corp.",
        "display_name": "SpaceX",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["SpaceX"],
        "profile_summary": "商业航天与卫星互联网公司。",
        "importance_level": 4,
    },
    {
        "canonical_name": "NVIDIA Corporation",
        "display_name": "NVIDIA",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["NVIDIA", "英伟达"],
        "profile_summary": "AI 加速计算、芯片与软硬件平台公司。",
        "importance_level": 5,
    },
    {
        "canonical_name": "OpenAI",
        "display_name": "OpenAI",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["OpenAI"],
        "profile_summary": "人工智能研究与产品公司。",
        "importance_level": 5,
    },
    {
        "canonical_name": "Berkshire Hathaway Inc.",
        "display_name": "伯克希尔·哈撒韦",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["Berkshire Hathaway", "伯克希尔"],
        "profile_summary": "多元化控股公司，长期披露资本配置与经营情况。",
        "importance_level": 5,
    },
    {
        "canonical_name": "ARK Investment Management LLC",
        "display_name": "ARK Invest",
        "subject_type": IntelligenceSubject.TYPE_ORGANIZATION,
        "category": IntelligenceSubject.CATEGORY_ORGANIZATION,
        "aliases": ["ARK Invest", "ARK"],
        "profile_summary": "聚焦颠覆式创新主题的资产管理机构。",
        "importance_level": 4,
    },
)


RELATIONS = (
    ("Elon Musk", "Tesla, Inc.", SubjectRelation.TYPE_LEADERSHIP),
    ("Elon Musk", "Space Exploration Technologies Corp.", SubjectRelation.TYPE_FOUNDER),
    ("Jensen Huang", "NVIDIA Corporation", SubjectRelation.TYPE_FOUNDER),
    ("Sam Altman", "OpenAI", SubjectRelation.TYPE_LEADERSHIP),
    ("Warren Buffett", "Berkshire Hathaway Inc.", SubjectRelation.TYPE_LEADERSHIP),
    ("Cathie Wood", "ARK Investment Management LLC", SubjectRelation.TYPE_FOUNDER),
)


class Command(BaseCommand):
    help = "幂等创建关键人物 M1 默认关注对象，可选为指定家庭全部关注。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="展示将发生的变化并回滚。")
        parser.add_argument("--follow-all", action="store_true", help="让指定家庭关注全部默认对象。")
        parser.add_argument("--family-id", type=int, help="与 --follow-all 配合使用；未指定时仅允许数据库只有一个家庭。")

    @transaction.atomic
    def handle(self, *args, **options):
        family = self._resolve_family(options) if options["follow_all"] else None
        created_subjects = 0
        existing_subjects = 0
        followed_subjects = 0
        subject_by_name = {}

        for definition in SUBJECTS:
            canonical_name = definition["canonical_name"]
            defaults = {key: value for key, value in definition.items() if key != "canonical_name"}
            subject, created = IntelligenceSubject.objects.get_or_create(
                canonical_name=canonical_name,
                defaults=defaults,
            )
            subject_by_name[canonical_name] = subject
            created_subjects += int(created)
            existing_subjects += int(not created)
            if family:
                _follow, followed = SubjectFollow.objects.get_or_create(
                    family=family,
                    subject=subject,
                    defaults={"priority": subject.importance_level, "is_active": True},
                )
                followed_subjects += int(followed)

        created_relations = 0
        for from_name, to_name, relation_type in RELATIONS:
            _relation, created = SubjectRelation.objects.get_or_create(
                from_subject=subject_by_name[from_name],
                to_subject=subject_by_name[to_name],
                relation_type=relation_type,
            )
            created_relations += int(created)

        summary = (
            f"关注对象：新增 {created_subjects}，已有 {existing_subjects}；"
            f"关系：新增 {created_relations}；家庭关注：新增 {followed_subjects}。"
        )
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING(f"试运行（未保存）— {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    def _resolve_family(self, options):
        if options["family_id"]:
            try:
                return Family.objects.get(pk=options["family_id"])
            except Family.DoesNotExist as exc:
                raise CommandError(f"未找到家庭 ID={options['family_id']}。") from exc
        families = list(Family.objects.all()[:2])
        if len(families) != 1:
            raise CommandError("使用 --follow-all 时，请通过 --family-id 明确指定家庭。")
        return families[0]
