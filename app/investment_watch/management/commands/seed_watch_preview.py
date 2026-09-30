from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from family_core.models import Family, FamilyMember
from portfolio.models import Security
from investment_research.services import create_dossier
from investment_research.models import ResearchDossier
from investment_watch.collection import seed_sources
from investment_watch.services import save_rule
from investment_watch.models import NewsSource, WatchRule


class Command(BaseCommand):
    help = "Seed isolated local preview; refuses all other settings."

    def handle(self, *args, **options):
        if (
            not getattr(settings, "WATCH_LOCAL_PREVIEW", False)
            or settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3"
        ):
            raise CommandError("仅允许独立本机预览库。")
        family, _ = Family.objects.get_or_create(name="投资动态 · 本机预览")
        user, created = get_user_model().objects.get_or_create(username="watch-preview")
        if created:
            user.set_password("watch-local-preview")
            user.save()
        member, _ = FamilyMember.objects.get_or_create(
            user=user,
            defaults={
                "family": family,
                "display_name": "预览成员",
                "role": FamilyMember.ROLE_ADMIN,
            },
        )
        security, _ = Security.objects.get_or_create(
            symbol="MSFT", market="US", defaults={"name": "微软", "asset_type": "stock"}
        )
        dossier = ResearchDossier.objects.filter(
            owner=member, security=security
        ).first()
        if not dossier:
            dossier = create_dossier(
                actor=member,
                security=security,
                initial_thesis="【功能演示】跟踪微软云业务增长与资本开支回报。这不是用户的正式投资判断。",
                pillars=[
                    "【演示假设】云需求持续增长",
                    "【演示假设】资本投入能带来持续现金回报",
                ],
                questions=["【演示问题】电力与出口限制会影响扩张吗？"],
            )
        if not WatchRule.objects.filter(dossier=dossier).exists():
            save_rule(
                member,
                dossier.pk,
                {
                    "aliases": ["微软", "Microsoft", "MSFT", "Azure", "Copilot"],
                    "topics": ["数据中心", "资本开支", "美联储", "云计算", "出口管制"],
                    "enabled": True,
                },
                0,
            )
        seed_sources(family)
        NewsSource.objects.filter(family=family).exclude(adapter="official").update(
            enabled=True
        )
        self.stdout.write(
            f"Local family: {family.pk}. Login: watch-preview / watch-local-preview. No production data or cloud model enabled."
        )
