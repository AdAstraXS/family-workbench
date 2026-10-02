from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember


class TradingJournalPlanningPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="trading-journal-planning-member",
            password="test-password",
        )
        family = Family.objects.create(name="交易复盘规划测试家庭")
        FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="复盘成员",
        )
        self.client.force_login(user)

    def test_planning_page_keeps_portfolio_as_transaction_fact_source(self):
        response = self.client.get(reverse("trading_journal:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "尚未开放业务功能")
        self.assertContains(response, "流水继续由 portfolio 负责")
        self.assertContains(response, "不得创建第二套交易流水")
        page = BeautifulSoup(response.content, "html.parser")
        self.assertIsNone(page.select_one(".knowledge-hub-sidebar"))
        self.assertIsNone(page.select_one(".knowledge-hub-shell"))
        breadcrumb = page.select_one('nav[aria-label="面包屑"]')
        self.assertIn("交易复盘", breadcrumb.get_text())
        self.assertNotIn("知识中心", breadcrumb.get_text())
        self.assertIsNotNone(page.select_one(f'#ws-sidebar a[href="{reverse("knowledge:index")}"]'))
        self.assertContains(response, "交易复盘 · 家庭工作台")
