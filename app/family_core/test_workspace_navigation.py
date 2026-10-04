from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, RequestFactory
from django.urls import resolve, reverse

from .context_processors import page_navigation
from .models import Family, FamilyMember
from .workspace import workspace_navigation


class WorkspaceRoutingTests(SimpleTestCase):
    def request_for(self, route, **kwargs):
        request = RequestFactory().get(reverse(route, kwargs=kwargs or None))
        request.resolver_match = resolve(request.path)
        return request

    def test_observation_groups_and_legacy_last(self):
        modules = workspace_navigation(self.request_for("investment_watch:news"))["workspace_modules"]
        observation = [m["label"] for m in modules if m["group"] == "观察与分析"]
        self.assertEqual(observation, ["新闻跟踪", "精选订阅", "宏观数据", "AI 检索与问答"])
        self.assertEqual(modules[-1]["label"], "AI 情报")
        self.assertEqual(modules[-1]["badge"], "废案待处理")

    def test_program_routes_have_one_active_entry_and_correct_parent(self):
        routes = [("program_list", {}, "dashboard:home"), ("program_detail", {"pk": 1}, "intelligence:program_list"), ("program_settings", {}, "intelligence:program_list"), ("program_source_edit", {"pk": 1}, "intelligence:program_settings"), ("program_upload", {}, "intelligence:program_list"), ("program_action", {"pk": 1}, "intelligence:program_detail")]
        for route, kwargs, parent in routes:
            with self.subTest(route=route):
                request = self.request_for(f"intelligence:{route}", **kwargs)
                context = workspace_navigation(request)
                self.assertEqual([m["label"] for m in context["workspace_modules"] if m["active"]], ["精选订阅"])
                self.assertFalse(context["workspace_is_legacy_intelligence"])
                self.assertEqual(page_navigation(request)["page_parent_url"], reverse(parent, kwargs={"pk": 1} if parent.endswith(":program_detail") else None))

    def test_legacy_routes_keep_legacy_navigation_and_bookmarks(self):
        for route in ["index", "subject_list", "source_list", "operations"]:
            with self.subTest(route=route):
                context = workspace_navigation(self.request_for(f"intelligence:{route}"))
                self.assertEqual([m["label"] for m in context["workspace_modules"] if m["active"]], ["AI 情报"])
                self.assertTrue(context["workspace_is_legacy_intelligence"])


class WorkspacePageNavigationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="navigation-admin")
        FamilyMember.objects.create(family=Family.objects.create(name="导航家庭"), user=user, display_name="管理员", role="admin")
        self.client.force_login(user)

    def test_program_list_has_independent_navigation(self):
        response = self.client.get(reverse("intelligence:program_list"))
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertIsNone(soup.select_one('nav[aria-label="情报功能导航"]'))
        self.assertIsNone(soup.select_one('aside[aria-label="旧方案状态"]'))
        active = soup.select('nav[aria-label="主导航"] a[aria-current="page"]')
        self.assertEqual([a.get("href") for a in active], [reverse("intelligence:program_list")])
        self.assertNotIn("AI 情报", soup.select_one("main").get_text())

    def test_legacy_page_displays_status_and_preserves_history_entry(self):
        response = self.client.get(reverse("intelligence:index"))
        self.assertContains(response, "AI 情报 · 废案待处理")
        soup = BeautifulSoup(response.content, "html.parser")
        self.assertIsNotNone(soup.select_one('nav[aria-label="情报功能导航"]'))
        self.assertEqual(soup.select('nav[aria-label="主导航"] a')[-1].get("href"), reverse("intelligence:index"))
