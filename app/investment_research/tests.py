"""投研模块 M1-A 服务与权限测试。

运行：python manage.py test investment_research --settings=config.settings_research_test
全部使用合成数据；异常状态 fixture（family 不一致、跨档案版本指针）
只通过 ORM 直接改动构造，仅用于测试。
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .models import ResearchDossier, ResearchThesisRevision
from .permissions import (
    accessible_dossiers,
    get_accessible_dossier_or_404,
    is_writer,
)
from .services import (
    DossierNotFound,
    DuplicateDossier,
    ResearchValidationError,
    ThesisRevisionConflict,
    create_dossier,
    save_thesis_revision,
)

User = get_user_model()


class ResearchServiceTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Alpha 家")
        cls.other_family = Family.objects.create(name="Beta 家")
        cls.alice = FamilyMember.objects.create(
            family=cls.family, display_name="Alice", role=FamilyMember.ROLE_MEMBER
        )
        cls.bob = FamilyMember.objects.create(
            family=cls.family, display_name="Bob", role=FamilyMember.ROLE_MEMBER
        )
        cls.carol = FamilyMember.objects.create(
            family=cls.other_family, display_name="Carol", role=FamilyMember.ROLE_MEMBER
        )
        cls.viewer = FamilyMember.objects.create(
            family=cls.family, display_name="Dave", role=FamilyMember.ROLE_VIEWER
        )
        cls.security = Security.objects.create(
            symbol="ACME", name="Acme Corp", market="US", asset_type="stock"
        )

    def create(self, actor, thesis="初始判断。"):
        return create_dossier(
            actor=actor,
            security=self.security,
            initial_thesis=thesis,
            pillars=[],
            questions=[],
        )


class CreateDossierTests(ResearchServiceTestBase):
    def test_create_dossier_with_first_revision(self):
        d = self.create(self.alice)
        self.assertEqual(d.family, self.family)
        self.assertEqual(d.owner, self.alice)
        self.assertIsNotNone(d.current_revision)
        self.assertEqual(d.current_revision.revision_number, 1)
        self.assertEqual(d.current_revision.thesis, "初始判断。")
        self.assertEqual(d.current_revision.dossier_id, d.pk)
        self.assertEqual(d.current_revision.created_by, self.alice)

    def test_duplicate_create_returns_existing_no_orphan(self):
        d = self.create(self.alice)
        with self.assertRaises(DuplicateDossier) as ctx:
            self.create(self.alice, thesis="再来一次")
        self.assertEqual(ctx.exception.dossier.pk, d.pk)
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 1)

    def test_same_security_different_members_independent(self):
        d_alice = self.create(self.alice)
        d_bob = self.create(self.bob)
        d_carol = self.create(self.carol)
        self.assertEqual(len({d_alice.pk, d_bob.pk, d_carol.pk}), 3)
        self.assertEqual(set(accessible_dossiers(self.alice)), {d_alice})
        self.assertEqual(set(accessible_dossiers(self.bob)), {d_bob})
        self.assertEqual(set(accessible_dossiers(self.carol)), {d_carol})


class SaveRevisionTests(ResearchServiceTestBase):
    def setUp(self):
        super().setUp()
        self.dossier = self.create(self.alice)

    def save(self, actor=None, expected=None, thesis="判断 v2。", reason="观点更新"):
        return save_thesis_revision(
            actor=actor or self.alice,
            dossier_id=self.dossier.pk,
            expected_revision_id=expected or self.dossier.current_revision_id,
            thesis=thesis,
            pillars=[],
            questions=[],
            change_reason=reason,
        )

    def test_save_appends_version_keeps_old_unchanged(self):
        v1 = self.dossier.current_revision
        v2 = self.save()
        self.assertEqual(v2.revision_number, 2)
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.current_revision_id, v2.pk)
        v1.refresh_from_db()
        self.assertEqual(v1.thesis, "初始判断。")
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2)

    def test_stale_expected_rejected_no_new_version(self):
        v1 = self.dossier.current_revision
        self.save()  # 正常追加 v2，指针前移
        with self.assertRaises(ThesisRevisionConflict):
            self.save(expected=v1.pk)  # 旧版本 id 重试 → 冲突
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2)

    def test_stale_resubmission_rejected(self):
        v1 = self.dossier.current_revision
        self.save()  # 正常追加 v2，指针前移
        for _ in range(2):
            with self.assertRaises(ThesisRevisionConflict):
                self.save(expected=v1.pk)  # 相同提交重发仍带旧 id → 仍冲突
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2)

    def test_wrong_actor_no_escalation(self):
        # Bob 持正确 expected id 仍不能改 Alice 的档案（按 actor 限定）。
        with self.assertRaises(DossierNotFound):
            self.save(actor=self.bob)
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 1)

    def test_family_mismatch_rejected(self):
        # 异常 fixture：档案 family 被改到别家，拒写且无新版本、无指针变动。
        ResearchDossier.objects.filter(pk=self.dossier.pk).update(family=self.other_family)
        before = ResearchThesisRevision.objects.count()
        expected = self.dossier.current_revision_id
        with self.assertRaises((ResearchValidationError, DossierNotFound)):
            self.save()
        self.dossier.refresh_from_db()
        self.assertEqual(ResearchThesisRevision.objects.count(), before)
        self.assertEqual(self.dossier.current_revision_id, expected)

    def test_foreign_revision_pointer_rejected(self):
        # 异常 fixture：current_revision 指向另一档案的版本，拒写。
        other_dossier = self.create(self.carol)
        ResearchDossier.objects.filter(pk=self.dossier.pk).update(
            current_revision=other_dossier.current_revision
        )
        before = ResearchThesisRevision.objects.count()
        expected = other_dossier.current_revision_id
        with self.assertRaises((ResearchValidationError, DossierNotFound)):
            self.save(expected=expected)
        self.dossier.refresh_from_db()
        self.assertEqual(ResearchThesisRevision.objects.count(), before)
        self.assertEqual(self.dossier.current_revision_id, expected)


class PermissionTests(ResearchServiceTestBase):
    def test_none_member_no_write_no_visible(self):
        self.assertFalse(is_writer(None))
        self.assertFalse(accessible_dossiers(None).exists())

    def test_inactive_member_direct_call_restricted(self):
        d = self.create(self.alice)
        self.alice.is_active = False
        self.alice.save(update_fields=["is_active"])
        self.assertFalse(is_writer(self.alice))
        self.assertFalse(accessible_dossiers(self.alice).exists())
        with self.assertRaises(ResearchValidationError):
            self.create(self.alice, thesis="停用后创建")
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 1)

    def test_viewer_cannot_write(self):
        self.assertFalse(is_writer(self.viewer))
        with self.assertRaises(ResearchValidationError):
            self.create(self.viewer)
        self.assertFalse(ResearchDossier.objects.filter(owner=self.viewer).exists())

    def test_family_admin_cannot_see_others(self):
        admin = FamilyMember.objects.create(
            family=self.family, display_name="Admin", role=FamilyMember.ROLE_ADMIN
        )
        d_alice = self.create(self.alice)
        d_admin = self.create(admin)
        self.assertTrue(is_writer(admin))
        self.assertEqual(set(accessible_dossiers(admin)), {d_admin})
        self.assertNotIn(d_alice, accessible_dossiers(admin))

    def test_superuser_bound_cannot_see_others(self):
        u = User.objects.create_superuser("root", "root@example.com", "pw")
        member = FamilyMember.objects.create(
            user=u, family=self.family, display_name="Root", role=FamilyMember.ROLE_MEMBER
        )
        d_alice = self.create(self.alice)
        d_root = self.create(member)
        self.assertEqual(set(accessible_dossiers(member)), {d_root})
        self.assertNotIn(d_alice, accessible_dossiers(member))

    def test_non_owner_dossier_404(self):
        from django.http import Http404

        d = self.create(self.alice)
        with self.assertRaises(Http404):
            get_accessible_dossier_or_404(self.bob, d.pk)
        self.assertEqual(get_accessible_dossier_or_404(self.alice, d.pk).pk, d.pk)


class InputValidationTests(ResearchServiceTestBase):
    def test_create_invalid_inputs(self):
        cases = [
            dict(initial_thesis="   "),
            dict(initial_thesis="x" * 8001),
            dict(initial_thesis="ok", pillars=None),
            dict(initial_thesis="ok", pillars=("a",)),
            dict(initial_thesis="ok", pillars={"a": "b"}),
            dict(initial_thesis="ok", pillars="not-a-list"),
            dict(initial_thesis="ok", pillars=["a", 42]),
            dict(initial_thesis="ok", pillars=[" "]),
            dict(initial_thesis="ok", pillars=["x" * 501]),
            dict(initial_thesis="ok", pillars=["a", "b", "c", "d", "e", "f"]),
            dict(initial_thesis="ok", questions=None),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ResearchValidationError):
                    create_dossier(
                        actor=self.alice,
                        security=self.security,
                        pillars=kwargs.get("pillars", []),
                        questions=kwargs.get("questions", []),
                        initial_thesis=kwargs["initial_thesis"],
                    )
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 0)

    def test_save_invalid_inputs(self):
        d = self.create(self.alice)
        expected = d.current_revision_id
        cases = [
            dict(thesis="   "),
            dict(thesis="x" * 8001),
            dict(pillars=None),
            dict(pillars=("a",)),
            dict(pillars={"a": "b"}),
            dict(pillars="not-a-list"),
            dict(pillars=["a", 42]),
            dict(pillars=[" "]),
            dict(pillars=["x" * 501]),
            dict(pillars=["a", "b", "c", "d", "e", "f"]),
            dict(questions=None),
            dict(change_reason=""),
            dict(change_reason="x" * 501),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ResearchValidationError):
                    save_thesis_revision(
                        actor=self.alice,
                        dossier_id=d.pk,
                        expected_revision_id=expected,
                        thesis=kwargs.get("thesis", "判断 v2。"),
                        pillars=kwargs.get("pillars", []),
                        questions=kwargs.get("questions", []),
                        change_reason=kwargs.get("change_reason", "原因"),
                    )
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=d).count(), 1)

    def test_save_v2_requires_reason(self):
        # v1 的 change_reason 可为空（创建路径已覆盖）；v2 起必填。
        d = self.create(self.alice)
        with self.assertRaises(ResearchValidationError):
            save_thesis_revision(
                actor=self.alice,
                dossier_id=d.pk,
                expected_revision_id=d.current_revision_id,
                thesis="判断 v2。",
                pillars=[],
                questions=[],
                change_reason="",
            )


class RollbackTests(ResearchServiceTestBase):
    def test_create_revision_failure_rolls_back_dossier(self):
        # 首版保存阶段人为抛错：档案也应回滚（局部 mock，不改生产行为）。
        with mock.patch.object(
            ResearchThesisRevision, "save", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.create(self.alice)
        self.assertFalse(ResearchDossier.objects.filter(owner=self.alice).exists())

    def test_save_pointer_failure_rolls_back_revision(self):
        # 追加版本后、更新指针前人为抛错：新版本也应回滚。
        d = self.create(self.alice)
        v1 = d.current_revision
        before = ResearchThesisRevision.objects.filter(dossier=d).count()
        with mock.patch.object(ResearchDossier, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                save_thesis_revision(
                    actor=self.alice,
                    dossier_id=d.pk,
                    expected_revision_id=v1.pk,
                    thesis="判断 v2。",
                    pillars=[],
                    questions=[],
                    change_reason="原因",
                )
        d.refresh_from_db()
        self.assertEqual(ResearchThesisRevision.objects.filter(dossier=d).count(), before)
        self.assertEqual(d.current_revision_id, v1.pk)


# ---------------------------------------------------------------------------
# M1-C 请求测试（视图 / 表单 / 路由 / 模板）
# 运行：python manage.py test investment_research --settings=config.settings_research_test
# ---------------------------------------------------------------------------
from django.test import Client
from django.urls import get_resolver, reverse, resolve

INDEX_URL = reverse("investment_research:index")
CREATE_URL = reverse("investment_research:create")


def detail_url(dossier):
    return reverse("investment_research:detail", args=[dossier.pk])


def edit_url(dossier):
    return reverse("investment_research:edit", args=[dossier.pk])


def history_url(dossier):
    return reverse("investment_research:history", args=[dossier.pk])


class ResearchViewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Alpha 家")
        cls.other_family = Family.objects.create(name="Beta 家")
        cls.security = Security.objects.create(
            symbol="ACME", name="Acme Corp", market="US", asset_type="stock"
        )
        cls.other_security = Security.objects.create(
            symbol="GLOBEX", name="Globex Inc", market="US", asset_type="stock"
        )

    def make_member(self, family, display_name, role=FamilyMember.ROLE_MEMBER,
                    active=True, superuser=False):
        username = f"u_{family.pk}_{display_name.lower()}_{self._seq}"
        self._seq += 1
        user = User.objects.create_user(username=username, password="testpass")
        if superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save()
        return FamilyMember.objects.create(
            family=family,
            display_name=display_name,
            role=role,
            is_active=active,
            user=user,
        )

    _seq = 0

    def login(self, member):
        self.client.force_login(member.user)

    def create_dossier_for(self, actor, security=None, thesis="初始判断。"):
        return create_dossier(
            actor=actor,
            security=security or self.security,
            initial_thesis=thesis,
            pillars=[],
            questions=[],
        )


class AnonymousAndIdentityTests(ResearchViewTestBase):
    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(INDEX_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_unbound_user_403(self):
        user = User.objects.create_user(username="unbound", password="x")
        self.client.force_login(user)
        self.assertEqual(self.client.get(INDEX_URL).status_code, 403)

    def test_inactive_member_403(self):
        member = self.make_member(self.family, "Inactive", active=False)
        self.login(member)
        self.assertEqual(self.client.get(INDEX_URL).status_code, 403)

    def test_inactive_superuser_403_even_middleware_passes(self):
        # 超级用户被全局中间件放行，视图必须自行 403。
        member = self.make_member(self.family, "SuperOff", active=False, superuser=True)
        self.login(member)
        self.assertEqual(self.client.get(INDEX_URL).status_code, 403)

    def test_unbound_superuser_403(self):
        # 未绑定 superuser 被全局中间件放行，视图必须自行 403。
        user = User.objects.create_user(username="superunbound", password="x")
        user.is_superuser = True
        user.is_staff = True
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(INDEX_URL).status_code, 403)


class ViewerTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "Alice")
        self.viewer = self.make_member(
            self.family, "Dave"
        )
        self.dossier = self.create_dossier_for(self.alice)
        # 普通成员创建后降为只读，模拟其保留已有档案。
        self.viewer_dossier = self.create_dossier_for(self.viewer, security=self.other_security)
        self.viewer.role = FamilyMember.ROLE_VIEWER
        self.viewer.save(update_fields=["role"])

    def test_viewer_reads_own_pages(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(INDEX_URL).status_code, 200)
        self.assertEqual(self.client.get(detail_url(self.dossier)).status_code, 404)

    def test_viewer_reads_own_dossier_only(self):
        own = self.viewer_dossier
        self.login(self.viewer)
        self.assertEqual(self.client.get(detail_url(own)).status_code, 200)
        self.assertEqual(self.client.get(history_url(own)).status_code, 200)
        # 别人的档案：404
        self.assertEqual(self.client.get(detail_url(self.dossier)).status_code, 404)
        self.assertEqual(self.client.get(history_url(self.dossier)).status_code, 404)

    def test_viewer_create_edit_rejected_403(self):
        self.login(self.viewer)
        own = self.viewer_dossier
        self.assertEqual(self.client.get(CREATE_URL).status_code, 403)
        self.assertEqual(
            self.client.post(
                CREATE_URL,
                {
                    "security": self.security.pk,
                    "initial_thesis": "viewer 想创建",
                    "pillars": "",
                    "questions": "",
                },
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get(edit_url(own)).status_code, 403)
        self.assertEqual(
            self.client.post(
                edit_url(own),
                {
                    "thesis": "viewer 想改",
                    "pillars": "",
                    "questions": "",
                    "change_reason": "理由",
                    "expected_revision_id": own.current_revision.pk,
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=own).count(), 1
        )


class IsolationTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "Alice")
        self.bob = self.make_member(self.family, "Bob")
        self.carol = self.make_member(self.other_family, "Carol")
        self.eve = self.make_member(self.family, "Eve", role=FamilyMember.ROLE_ADMIN)
        self.frank = self.make_member(self.family, "Frank", superuser=True)
        self.bob_dossier = self.create_dossier_for(self.bob)

    def test_same_family_cannot_read_others(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(detail_url(self.bob_dossier)).status_code, 404)
        self.assertEqual(self.client.get(edit_url(self.bob_dossier)).status_code, 404)
        self.assertEqual(
            self.client.get(history_url(self.bob_dossier)).status_code, 404
        )

    def test_cross_family_cannot_read_others(self):
        self.login(self.carol)
        self.assertEqual(self.client.get(detail_url(self.bob_dossier)).status_code, 404)

    def test_family_admin_cannot_read_others(self):
        self.login(self.eve)
        self.assertEqual(self.client.get(detail_url(self.bob_dossier)).status_code, 404)
        self.assertEqual(self.client.get(edit_url(self.bob_dossier)).status_code, 404)

    def test_bound_superuser_cannot_read_others(self):
        self.login(self.frank)
        self.assertEqual(self.client.get(detail_url(self.bob_dossier)).status_code, 404)
        self.assertEqual(
            self.client.post(
                edit_url(self.bob_dossier),
                {
                    "thesis": "super 想改",
                    "pillars": "",
                    "questions": "",
                    "change_reason": "理由",
                    "expected_revision_id": self.bob_dossier.current_revision.pk,
                },
            ).status_code,
            404,
        )

    def test_owner_still_reads_own(self):
        self.login(self.bob)
        self.assertEqual(self.client.get(detail_url(self.bob_dossier)).status_code, 200)
        self.assertEqual(self.client.get(history_url(self.bob_dossier)).status_code, 200)

    def test_list_shows_only_own_dossiers(self):
        self.create_dossier_for(self.alice, security=self.other_security, thesis="Alice 的判断")
        self.login(self.alice)
        resp = self.client.get(INDEX_URL)
        self.assertContains(resp, "Globex Inc")
        # Bob 的 ACME 档案不在 Alice 列表中
        self.assertNotContains(resp, "Acme Corp")
        self.login(self.bob)
        resp = self.client.get(INDEX_URL)
        self.assertContains(resp, "Acme Corp")
        self.assertNotContains(resp, "Globex Inc")


class CreateRequestTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "Alice")
        self.bob = self.make_member(self.family, "Bob")
        self.login(self.alice)

    def valid_payload(self, security=None, thesis="我认为它会增长。"):
        return {
            "security": (security or self.security).pk,
            "initial_thesis": thesis,
            "pillars": "假设一\n假设二",
            "questions": "问题一",
        }

    def test_create_success(self):
        resp = self.client.post(CREATE_URL, self.valid_payload())
        self.assertEqual(resp.status_code, 302)
        dossier = ResearchDossier.objects.get(owner=self.alice, security=self.security)
        self.assertRedirects(resp, detail_url(dossier))
        self.assertEqual(dossier.family, self.family)
        self.assertEqual(dossier.current_revision.revision_number, 1)
        self.assertEqual(dossier.current_revision.pillars, ["假设一", "假设二"])
        self.assertEqual(dossier.current_revision.questions, ["问题一"])

    def test_empty_lists_become_empty_list(self):
        payload = self.valid_payload()
        payload["pillars"] = ""
        payload["questions"] = "\n\n  \n"
        resp = self.client.post(CREATE_URL, payload)
        self.assertEqual(resp.status_code, 302)
        dossier = ResearchDossier.objects.get(owner=self.alice)
        self.assertEqual(dossier.current_revision.pillars, [])
        self.assertEqual(dossier.current_revision.questions, [])

    def test_forged_owner_family_fields_do_not_change_attribution(self):
        payload = self.valid_payload()
        payload["owner"] = self.bob.pk
        payload["family"] = self.other_family.pk
        resp = self.client.post(CREATE_URL, payload)
        self.assertEqual(resp.status_code, 302)
        dossier = ResearchDossier.objects.get(owner=self.alice, security=self.security)
        self.assertEqual(dossier.owner, self.alice)
        self.assertEqual(dossier.family, self.family)
        self.assertEqual(ResearchDossier.objects.filter(owner=self.bob).count(), 0)

    def test_duplicate_create_redirects_to_existing(self):
        first = self.create_dossier_for(self.alice)
        resp = self.client.post(CREATE_URL, self.valid_payload(thesis="再来一次"))
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, detail_url(first))
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 1)

    def test_invalid_input_preserves_and_blocks(self):
        resp = self.client.post(CREATE_URL, self.valid_payload(thesis="   "))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].errors.as_data()["initial_thesis"][0].code, "required")
        self.assertEqual(resp.context["form"].data["pillars"], "假设一\n假设二")
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 0)

    def test_empty_post_is_bound_and_shows_errors(self):
        resp = self.client.post(CREATE_URL, {})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].is_bound)
        self.assertIn("security", resp.context["form"].errors)
        self.assertIn("initial_thesis", resp.context["form"].errors)
        self.assertFalse(ResearchDossier.objects.exists())

    def test_service_validation_error_shown_on_form(self):
        payload = self.valid_payload()
        payload["pillars"] = "\n".join(f"假设{i}" for i in range(6))
        resp = self.client.post(CREATE_URL, payload)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "最多 5 条")
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 0)

    def test_malicious_html_escaped_in_detail(self):
        payload = self.valid_payload(thesis="<script>alert('x')</script>判断")
        resp = self.client.post(CREATE_URL, payload)
        self.assertEqual(resp.status_code, 302)
        dossier = ResearchDossier.objects.get(owner=self.alice)
        resp = self.client.get(detail_url(dossier))
        self.assertContains(resp, "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;")
        self.assertNotContains(resp, "<script>alert('x')</script>")

    def test_csrf_missing_token_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice.user)
        resp = client.post(CREATE_URL, self.valid_payload())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ResearchDossier.objects.filter(owner=self.alice).count(), 0)

    def test_unsupported_write_method_405(self):
        dossier = self.create_dossier_for(self.alice)
        self.assertEqual(self.client.put(CREATE_URL).status_code, 405)
        self.assertEqual(self.client.put(detail_url(dossier)).status_code, 405)
        self.assertEqual(self.client.delete(INDEX_URL).status_code, 405)

    def test_get_requests_do_not_write(self):
        dossier = self.create_dossier_for(self.alice)
        before_count = ResearchDossier.objects.count()
        before_revision_count = ResearchThesisRevision.objects.count()
        before_updated = dossier.updated_at
        before_current = dossier.current_revision_id
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("外部请求")):
            self.assertEqual(self.client.get(INDEX_URL).status_code, 200)
            self.assertEqual(self.client.get(detail_url(dossier)).status_code, 200)
            self.assertEqual(self.client.get(history_url(dossier)).status_code, 200)
            self.assertEqual(self.client.get(CREATE_URL).status_code, 200)
            self.assertEqual(self.client.get(edit_url(dossier)).status_code, 200)
        dossier.refresh_from_db()
        self.assertEqual(ResearchDossier.objects.count(), before_count)
        self.assertEqual(ResearchThesisRevision.objects.count(), before_revision_count)
        self.assertEqual(dossier.updated_at, before_updated)
        self.assertEqual(dossier.current_revision_id, before_current)


class EditRequestTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "Alice")
        self.bob = self.make_member(self.family, "Bob")
        self.dossier = self.create_dossier_for(self.alice, thesis="第一版判断。")
        self.login(self.alice)

    def edit_payload(self, expected, thesis="第二版判断。", reason="新数据"):
        return {
            "thesis": thesis,
            "pillars": "新假设",
            "questions": "",
            "change_reason": reason,
            "expected_revision_id": expected,
        }

    def test_edit_success_appends_version(self):
        v1 = self.dossier.current_revision
        resp = self.client.post(edit_url(self.dossier), self.edit_payload(v1.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, detail_url(self.dossier))
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.current_revision.revision_number, 2)
        self.assertEqual(self.dossier.current_revision.thesis, "第二版判断。")
        # 旧版本与原始理由不变
        v1.refresh_from_db()
        self.assertEqual(v1.thesis, "第一版判断。")
        self.assertEqual(self.dossier.initial_thesis, "第一版判断。")

    def test_stale_expected_409_preserves_input_and_old_version(self):
        v1 = self.dossier.current_revision
        payload = self.edit_payload(v1.pk, thesis="旧表单 A")
        # 先让版本前进（模拟另一客户端保存成功）
        save_thesis_revision(
            actor=self.alice,
            dossier_id=self.dossier.pk,
            expected_revision_id=v1.pk,
            thesis="别的客户端的新版本",
            pillars=[],
            questions=[],
            change_reason="他人更新",
        )
        resp = self.client.post(edit_url(self.dossier), payload)
        self.assertEqual(resp.status_code, 409)
        # 输入保留、隐藏版本仍是旧值、提示查看最新详情
        self.assertContains(resp, "旧表单 A", status_code=409)
        self.assertContains(resp, f'name="expected_revision_id" value="{v1.pk}"', status_code=409)
        self.assertContains(resp, "查看最新详情", status_code=409)
        # 版本数不变，再提交仍 409
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.current_revision.revision_number, 2)
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2
        )
        resp = self.client.post(edit_url(self.dossier), payload)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2
        )

    def test_two_stale_forms_second_gets_409(self):
        v1 = self.dossier.current_revision
        form_a = self.edit_payload(v1.pk, thesis="表单 A")
        form_b = self.edit_payload(v1.pk, thesis="表单 B")
        resp_a = self.client.post(edit_url(self.dossier), form_a)
        self.assertEqual(resp_a.status_code, 302)
        resp_b = self.client.post(edit_url(self.dossier), form_b)
        self.assertEqual(resp_b.status_code, 409)
        self.assertContains(resp_b, "表单 B", status_code=409)
        self.assertContains(resp_b, f'name="expected_revision_id" value="{v1.pk}"', status_code=409)
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 2
        )

    def test_forged_expected_revision_id_no_escalation(self):
        # Bob 的档案版本 id 被 Alice 伪造提交：不能借它通过，也不能动 Bob 的档案。
        bob_dossier = self.create_dossier_for(self.bob, security=self.other_security)
        resp = self.client.post(
            edit_url(self.dossier),
            self.edit_payload(bob_dossier.current_revision.pk),
        )
        self.assertEqual(resp.status_code, 409)
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.current_revision.revision_number, 1)

    def test_missing_expected_revision_id_rejected(self):
        payload = self.edit_payload(1)
        del payload["expected_revision_id"]
        resp = self.client.post(edit_url(self.dossier), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "expected_revision_id")
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 1
        )

    def test_non_numeric_expected_revision_id_rejected(self):
        resp = self.client.post(
            edit_url(self.dossier), self.edit_payload("abc")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 1
        )

    def test_v2_requires_change_reason(self):
        v1 = self.dossier.current_revision
        resp = self.client.post(
            edit_url(self.dossier), self.edit_payload(v1.pk, reason="")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "修改原因")
        self.assertEqual(
            ResearchThesisRevision.objects.filter(dossier=self.dossier).count(), 1
        )

    def test_edit_page_does_not_expose_initial_thesis_or_security(self):
        resp = self.client.get(edit_url(self.dossier))
        self.assertContains(resp, "expected_revision_id")
        # 原始理由与证券字段不在编辑表单里
        self.assertNotContains(resp, "name=\"initial_thesis\"")
        self.assertNotContains(resp, "name=\"security\"")


    def test_forged_edit_fields_do_not_change_identity_or_initial_reason(self):
        payload = self.edit_payload(self.dossier.current_revision_id)
        payload.update(owner=self.bob.pk, family=self.other_family.pk,
                       security=self.other_security.pk, initial_thesis="forged")
        response = self.client.post(edit_url(self.dossier), payload)
        self.assertEqual(response.status_code, 302)
        self.dossier.refresh_from_db()
        self.assertEqual(self.dossier.owner_id, self.alice.pk)
        self.assertEqual(self.dossier.family_id, self.family.pk)
        self.assertEqual(self.dossier.security_id, self.security.pk)
        self.assertEqual(self.dossier.initial_thesis, "第一版判断。")

    def test_edit_service_error_preserves_input(self):
        payload = self.edit_payload(self.dossier.current_revision_id)
        payload["questions"] = "\n".join(["问题"] * 6)
        response = self.client.post(edit_url(self.dossier), payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(response.context["form"].data["questions"], payload["questions"])
        self.assertEqual(self.dossier.revisions.count(), 1)

    def test_edit_csrf_rejected_without_token_and_accepted_with_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice.user)
        url = edit_url(self.dossier)
        payload = self.edit_payload(self.dossier.current_revision_id)
        self.assertEqual(client.post(url, payload).status_code, 403)
        self.assertEqual(self.dossier.revisions.count(), 1)
        self.assertEqual(client.get(url).status_code, 200)
        payload["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertEqual(client.post(url, payload).status_code, 302)
        self.assertEqual(self.dossier.revisions.count(), 2)


class PaginationAndStateTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "Alice")
        self.login(self.alice)

    def test_index_pagination_beyond_20(self):
        for i in range(21):
            sec = Security.objects.create(
                symbol=f"SEC{i:03d}", name=f"Sec {i}", market="US", asset_type="stock"
            )
            self.create_dossier_for(self.alice, security=sec, thesis=f"判断 {i}")
        resp = self.client.get(INDEX_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "第 1 / 2 页")
        resp = self.client.get(INDEX_URL, {"page": 2})
        self.assertContains(resp, "第 2 / 2 页")

    def test_history_pagination_beyond_20(self):
        dossier = self.create_dossier_for(self.alice, thesis="判断 0")
        expected = dossier.current_revision.pk
        for i in range(1, 21):
            expected = save_thesis_revision(
                actor=self.alice,
                dossier_id=dossier.pk,
                expected_revision_id=expected,
                thesis=f"判断 {i}",
                pillars=[],
                questions=[],
                change_reason=f"原因 {i}",
            ).pk
        resp = self.client.get(history_url(dossier))
        self.assertContains(resp, "第 1 / 2 页")
        resp = self.client.get(history_url(dossier), {"page": 2})
        self.assertContains(resp, "第 2 / 2 页")

    def test_empty_state_and_success_message(self):
        resp = self.client.get(INDEX_URL)
        self.assertContains(resp, "你还没有研究档案")
        resp = self.client.post(
            CREATE_URL,
            {
                "security": self.security.pk,
                "initial_thesis": "第一份档案",
                "pillars": "",
                "questions": "",
            },
        )
        dossier = ResearchDossier.objects.get(owner=self.alice)
        self.assertRedirects(resp, detail_url(dossier), msg_prefix="成功提示跳转", fetch_redirect_response=False)
        self.assertContains(self.client.get(detail_url(dossier)), "研究档案已创建")


class WiringTests(ResearchViewTestBase):
    """M1-D 接线：原 config.urls 下投研路由可达、namespace 唯一、导航含入口。"""

    @override_settings(ROOT_URLCONF="config.urls")
    def test_research_routes_reverse_and_resolve_under_production_urlconf(self):
        from config.urls import urlpatterns

        research_entries = [p for p in urlpatterns if str(p.pattern) == "research/"]
        self.assertEqual(
            len(research_entries), 1, "生产 URLConf 中 research/ 应只注册一次"
        )

        self.assertEqual(reverse("investment_research:index"), "/research/")
        self.assertEqual(reverse("investment_research:create"), "/research/new/")
        self.assertEqual(reverse("investment_research:detail", args=[7]), "/research/7/")
        self.assertEqual(reverse("investment_research:edit", args=[7]), "/research/7/edit/")
        self.assertEqual(reverse("investment_research:history", args=[7]), "/research/7/history/")

        match = resolve("/research/")
        self.assertEqual(match.app_name, "investment_research")
        self.assertEqual(match.view_name, "investment_research:index")

    def test_navigation_contains_research_link_after_login(self):
        member = self.make_member(self.family, "NavUser")
        self.login(member)
        resp = self.client.get(INDEX_URL)
        self.assertContains(resp, '<a href="/research/">投研</a>')
