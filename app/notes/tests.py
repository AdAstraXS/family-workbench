from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from family_core.models import Family, FamilyMember

from .models import InvestmentNote


class InvestmentNoteViewsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="note-owner", password="test-password")
        self.family = Family.objects.create(name="测试家庭")
        self.member = FamilyMember.objects.create(
            family=self.family,
            user=self.user,
            display_name="笔记作者",
            role=FamilyMember.ROLE_ADMIN,
        )
        self.other_user = User.objects.create_user(username="family-reader", password="test-password")
        self.other_member = FamilyMember.objects.create(
            family=self.family,
            user=self.other_user,
            display_name="家庭成员",
        )
        self.other_family = Family.objects.create(name="其他家庭")
        self.outsider = FamilyMember.objects.create(
            family=self.other_family,
            display_name="外部成员",
        )
        self.client.force_login(self.user)

    def make_note(self, *, member=None, visibility="private", title="投资笔记", tags=None):
        member = member or self.member
        return InvestmentNote.objects.create(
            family=member.family,
            member=member,
            title=title,
            content="记录投资判断与执行结果。",
            note_type=InvestmentNote.TYPE_RESEARCH,
            note_date=date(2026, 7, 6),
            visibility=visibility,
            tags=tags or [],
        )

    def test_index_only_lists_accessible_notes(self):
        own_note = self.make_note(title="自己的私密笔记")
        shared_note = self.make_note(
            member=self.other_member,
            visibility=InvestmentNote.VISIBILITY_FAMILY,
            title="家庭共享笔记",
        )
        hidden_note = self.make_note(member=self.other_member, title="别人的私密笔记")
        outsider_note = self.make_note(
            member=self.outsider,
            visibility=InvestmentNote.VISIBILITY_FAMILY,
            title="其他家庭的共享笔记",
        )

        response = self.client.get(reverse("notes:index"))

        self.assertContains(response, own_note.title)
        self.assertContains(response, shared_note.title)
        self.assertNotContains(response, hidden_note.title)
        self.assertNotContains(response, outsider_note.title)

    def test_create_assigns_current_family_member_and_parses_tags(self):
        response = self.client.post(
            reverse("notes:create"),
            {
                "title": "港股打新复盘",
                "note_type": InvestmentNote.TYPE_TRADE,
                "note_date": "2026-07-05",
                "visibility": InvestmentNote.VISIBILITY_PRIVATE,
                "tags_text": "港股，复盘、港股",
                "content": "申购理由与上市后的执行结果。",
            },
        )

        note = InvestmentNote.objects.get(title="港股打新复盘")
        self.assertRedirects(response, reverse("notes:detail", kwargs={"pk": note.pk}))
        self.assertEqual(note.family, self.family)
        self.assertEqual(note.member, self.member)
        self.assertEqual(note.tags, ["港股", "复盘"])

    def test_search_matches_tags_and_category_filter(self):
        matched = self.make_note(title="一篇普通标题", tags=["风险控制"])
        self.make_note(title="另一篇笔记", tags=["估值"])

        response = self.client.get(
            reverse("notes:index"),
            {"q": "风险", "category": InvestmentNote.TYPE_RESEARCH},
        )

        self.assertContains(response, matched.title)
        self.assertNotContains(response, "另一篇笔记")

    def test_cannot_edit_or_delete_another_members_shared_note(self):
        shared_note = self.make_note(
            member=self.other_member,
            visibility=InvestmentNote.VISIBILITY_FAMILY,
        )

        edit_response = self.client.get(reverse("notes:edit", kwargs={"pk": shared_note.pk}))
        delete_response = self.client.post(reverse("notes:delete", kwargs={"pk": shared_note.pk}))

        self.assertEqual(edit_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)
        self.assertTrue(InvestmentNote.objects.filter(pk=shared_note.pk).exists())

    def test_owner_can_edit_and_delete_note(self):
        note = self.make_note()
        edit_response = self.client.post(
            reverse("notes:edit", kwargs={"pk": note.pk}),
            {
                "title": "修改后的标题",
                "note_type": InvestmentNote.TYPE_STRATEGY,
                "note_date": "2026-07-06",
                "visibility": InvestmentNote.VISIBILITY_FAMILY,
                "tags_text": "长期",
                "content": "修改后的内容。",
            },
        )

        note.refresh_from_db()
        self.assertRedirects(edit_response, reverse("notes:detail", kwargs={"pk": note.pk}))
        self.assertEqual(note.title, "修改后的标题")
        self.assertEqual(note.tags, ["长期"])

        delete_response = self.client.post(reverse("notes:delete", kwargs={"pk": note.pk}))
        self.assertRedirects(delete_response, reverse("notes:index"))
        self.assertFalse(InvestmentNote.objects.filter(pk=note.pk).exists())

    def test_viewer_role_is_read_only(self):
        self.member.role = FamilyMember.ROLE_VIEWER
        self.member.save(update_fields=["role"])
        note = self.make_note()

        self.assertEqual(self.client.get(reverse("notes:create")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("notes:edit", kwargs={"pk": note.pk})).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("notes:delete", kwargs={"pk": note.pk})).status_code,
            403,
        )

    def test_account_without_family_member_gets_setup_message(self):
        unlinked_user = get_user_model().objects.create_user(
            username="unlinked",
            password="test-password",
        )
        self.client.force_login(unlinked_user)

        response = self.client.get(reverse("notes:index"))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "尚未绑定家庭成员", status_code=403)
