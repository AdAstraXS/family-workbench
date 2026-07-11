from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger.models import BankAccount

from .models import Family, FamilyMember, SiteSetting


class HouseholdAccessTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name="共享家庭")
        self.user = get_user_model().objects.create_user(
            username="household-member",
            password="password",
        )
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="成员 A",
        )
        self.other_member = FamilyMember.objects.create(
            family=self.family,
            display_name="成员 B",
        )
        self.other_account = BankAccount.objects.create(
            family=self.family,
            member=self.other_member,
            account_name="B 的共享账户",
        )

    def test_active_member_can_view_other_members_account(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("ledger:bank_account_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.other_account.account_name)

    def test_unlinked_or_inactive_user_is_rejected(self):
        unlinked = get_user_model().objects.create_user(
            username="unlinked",
            password="password",
        )
        self.client.force_login(unlinked)
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 403)

        self.member.is_active = False
        self.member.save(update_fields=["is_active", "updated_at"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 403)

    def test_viewer_cannot_post_but_member_can(self):
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save(update_fields=["role", "updated_at"])
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(reverse("ledger:income_create"), {}).status_code,
            403,
        )

        self.member.role = FamilyMember.ROLE_MEMBER
        self.member.save(update_fields=["role", "updated_at"])
        self.assertEqual(
            self.client.post(reverse("ledger:income_create"), {}).status_code,
            200,
        )

    def test_actor_is_distinct_from_account_owner(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("ledger:bank_account_create"),
            {
                "family": self.family.pk,
                "member": self.other_member.pk,
                "account_name": "B second account",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        account = BankAccount.objects.get(account_name="B second account")
        self.assertEqual(account.member, self.other_member)
        self.assertEqual(account.created_by, self.user)
        self.assertEqual(account.updated_by, self.user)


class SiteSettingTests(TestCase):
    def test_singleton_keeps_primary_key_one(self):
        first = SiteSetting.load()
        first.household_name = "新名称"
        first.save(update_fields=["household_name", "updated_at"])
        second = SiteSetting.load()

        self.assertEqual(first.pk, 1)
        self.assertEqual(second.pk, 1)
        self.assertEqual(SiteSetting.objects.count(), 1)
        self.assertEqual(SiteSetting.objects.get().household_name, "新名称")
