from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember


class ReadingPlanningPageTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="reading-planning-member",
            password="test-password",
        )
        family = Family.objects.create(name="阅读规划测试家庭")
        FamilyMember.objects.create(
            family=family,
            user=user,
            display_name="阅读成员",
        )
        self.client.force_login(user)

    def test_planning_page_states_boundary_without_fake_books(self):
        response = self.client.get(reverse("reading:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "尚未开放业务功能")
        self.assertContains(response, "当前不使用演示书籍伪造数据")
        self.assertContains(response, "成员主动点击后才进入归档资料")
