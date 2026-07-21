from django.core.management.base import BaseCommand, CommandError

from family_core.household import get_household_family
from ledger.investment_goals import (
    InvestmentGoalInitializationError,
    initialize_default_investment_goal_plan,
)


class Command(BaseCommand):
    help = "以 2025-12-31 正式资产快照初始化家庭投资目标"

    def handle(self, *args, **options):
        family = get_household_family()
        if not family:
            raise CommandError("尚未建立家庭资料。")
        try:
            plan, created = initialize_default_investment_goal_plan(family)
        except InvestmentGoalInitializationError as exc:
            raise CommandError(str(exc)) from exc
        if created:
            self.stdout.write(self.style.SUCCESS(f"已创建：{plan}"))
        else:
            self.stdout.write(self.style.WARNING(f"已存在，未重复创建：{plan}"))
