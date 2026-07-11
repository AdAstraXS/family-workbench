from decimal import Decimal
import calendar

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_analysis.models import AiProvider
from family_core.models import (
    AccountRegion,
    AccountType,
    AssetCategory,
    Currency,
    Family,
    FamilyMember,
    SiteSetting,
)
from ledger.models import BankAccount, ExpenseCategory, ExpenseRecord, IncomeCategory, IncomeRecord
from portfolio.account_sync import sync_investment_account
from portfolio.models import InvestmentCashMovement, InvestmentPosition, InvestmentTransaction, Security


class Command(BaseCommand):
    help = "Create an admin user and the first family finance records for MVP testing."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", required=True)
        parser.add_argument("--email", default="admin@example.com")
        parser.add_argument("--family-name", default="我的家庭")
        parser.add_argument("--member-name", default="我")

    def handle(self, *args, **options):
        username = options["username"]
        password = options["password"]
        email = options["email"]
        family_name = options["family_name"]
        member_name = options["member_name"]

        if len(password) < 8:
            raise CommandError("Password must be at least 8 characters.")

        User = get_user_model()
        user, created_user = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        if created_user:
            user.set_password(password)
            user.save()
        else:
            changed = False
            if not user.is_staff or not user.is_superuser:
                user.is_staff = True
                user.is_superuser = True
                changed = True
            if not user.has_usable_password():
                user.set_password(password)
                changed = True
            if changed:
                user.save()

        family, _ = Family.objects.get_or_create(
            name=family_name,
            defaults={"base_currency": "CNY", "remark": "第一阶段 MVP 初始化家庭"},
        )
        SiteSetting.objects.update_or_create(
            pk=1,
            defaults={
                "household_name": family.name,
                "base_currency": family.base_currency,
                "timezone": "Asia/Shanghai",
            },
        )
        member, _ = FamilyMember.objects.get_or_create(
            family=family,
            display_name=member_name,
            defaults={"user": user, "role": FamilyMember.ROLE_ADMIN, "remark": "第一阶段 MVP 初始化成员"},
        )
        if member.user_id is None:
            member.user = user
            member.role = FamilyMember.ROLE_ADMIN
            member.save()

        for code, name, symbol in [
            ("CNY", "人民币", "¥"),
            ("HKD", "港币", "HK$"),
            ("USD", "美元", "$"),
        ]:
            Currency.objects.get_or_create(code=code, defaults={"name": name, "symbol": symbol})

        account_types = {}
        for order, name in enumerate(["银行", "券商", "支付宝", "微信", "养老金"], start=1):
            account_types[name], _ = AccountType.objects.get_or_create(
                family=family,
                name=name,
                defaults={"display_order": order},
            )

        for order, name in enumerate(["现金", "基金", "股票", "债券", "黄金", "低风险理财", "虚拟货币"], start=1):
            AssetCategory.objects.get_or_create(
                family=family,
                name=name,
                defaults={"display_order": order},
            )

        account_regions = {}
        for order, name in enumerate(["境内", "境外"], start=1):
            account_regions[name], _ = AccountRegion.objects.get_or_create(
                family=family,
                name=name,
                defaults={"display_order": order},
            )

        broker_account, _ = BankAccount.objects.get_or_create(
            family=family,
            member=member,
            account_name="示例证券账户",
            defaults={
                "account_type_ref": account_types["券商"],
                "account_region": account_regions["境外"],
                "supports_investment": True,
                "remark": "可在后台改成你的真实券商账户",
            },
        )
        if not broker_account.supports_investment:
            broker_account.supports_investment = True
            broker_account.save(update_fields=["supports_investment", "updated_at"])
        investment_account = sync_investment_account(broker_account)
        InvestmentCashMovement.objects.get_or_create(
            account=investment_account,
            movement_date=timezone.localdate(),
            movement_type="deposit",
            currency="HKD",
            defaults={"amount": Decimal("50000.00"), "remark": "示例初始资金"},
        )
        security, _ = Security.objects.get_or_create(
            symbol="00700",
            market="HK",
            defaults={
                "name": "腾讯控股",
                "asset_type": "stock",
                "currency": "HKD",
                "industry": "互联网",
            },
        )
        today = timezone.localdate()
        month_start = today.replace(day=1)
        month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        InvestmentPosition.objects.get_or_create(
            account=investment_account,
            security=security,
            position_date=today,
            defaults={
                "quantity": Decimal("100.00"),
                "avg_cost": Decimal("300.00"),
                "current_price": Decimal("350.00"),
                "market_value": Decimal("35000.00"),
                "unrealized_pnl": Decimal("5000.00"),
                "pnl_ratio": Decimal("0.166667"),
                "remark": "示例持仓，可删除或修改",
            },
        )
        InvestmentTransaction.objects.get_or_create(
            account=investment_account,
            security=security,
            trade_date=today,
            trade_type="buy",
            defaults={
                "quantity": Decimal("100.00"),
                "price": Decimal("300.00"),
                "amount": Decimal("30000.00"),
                "fee": Decimal("30.00"),
                "currency": "HKD",
                "remark": "示例交易，可删除或修改",
            },
        )

        account, _ = BankAccount.objects.get_or_create(
            family=family,
            member=member,
            account_name="工资卡",
            defaults={
                "account_no_masked": "",
                "account_type_ref": account_types["银行"],
                "account_region": account_regions["境内"],
                "remark": "这里只维护账户基础信息，余额请在资产快照中录入",
            },
        )
        account_changed = False
        if account.account_type_ref_id is None:
            account.account_type_ref = account_types["银行"]
            account_changed = True
        if account.account_region_id is None:
            account.account_region = account_regions["境内"]
            account_changed = True
        if account_changed:
            account.save()
        income_category, _ = IncomeCategory.objects.get_or_create(
            family=family,
            name="工资",
            defaults={"is_active": True},
        )
        expense_category, _ = ExpenseCategory.objects.get_or_create(
            family=family,
            name="餐饮",
            defaults={"is_active": True},
        )
        IncomeRecord.objects.get_or_create(
            family=family,
            member=member,
            bank_account=account,
            category=income_category,
            income_date=today,
            source_name="工资收入",
            defaults={
                "period_start": month_start,
                "period_end": month_end,
                "amount": Decimal("10000.00"),
                "currency": "CNY",
                "is_recurring": True,
                "visibility": "private",
                "remark": "示例收入，可删除或修改",
            },
        )
        ExpenseRecord.objects.get_or_create(
            family=family,
            member=member,
            bank_account=account,
            category=expense_category,
            expense_date=today,
            merchant="示例餐厅",
            defaults={
                "period_start": month_start,
                "period_end": month_end,
                "amount": Decimal("128.00"),
                "currency": "CNY",
                "payment_method": "银行卡",
                "visibility": "private",
                "remark": "示例支出，可删除或修改",
            },
        )

        AiProvider.objects.get_or_create(
            name="OpenAI 示例配置",
            provider_type="openai",
            defaults={
                "model_name": "待配置",
                "is_active": False,
                "extra_data": {"note": "后续填入真实 API 配置，不要把 API Key 写到前端"},
            },
        )

        self.stdout.write(self.style.SUCCESS("MVP admin user and first sample data are ready."))
        self.stdout.write(f"Admin username: {username}")
        if created_user:
            self.stdout.write(f"Admin password: {password}")
        else:
            self.stdout.write("Admin user already existed; password was not changed unless it was unusable.")
