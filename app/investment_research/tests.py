"""投研模块 M1-A 服务与权限测试。

运行：python manage.py test investment_research --settings=config.settings_research_test
全部使用合成数据；异常状态 fixture（family 不一致、跨档案版本指针）
只通过 ORM 直接改动构造，仅用于测试。
M2A-2 追加 SEC 客户端与同步服务离线测试（mock opener，无真实网络）。
"""
import hashlib
import json
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from family_core.models import Family, FamilyMember
from portfolio.models import Security

from .models import (
    DOCUMENT_TYPE_CHOICES,
    SOURCE_CHOICES,
    SOURCE_MICROSOFT_IR,
    OfficialResearchDocument,
    ResearchDossier,
    ResearchSourceState,
    ResearchThesisRevision,
)
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
from .providers.sec import (
    SecClient,
    SecClientError,
    SecConfigError,
    SecDocumentUrlError,
    SecFilingsParseError,
    SecHTTPError,
    SecNetworkError,
    SecResponseTooLarge,
    SecTickerNotFoundError,
    SecTimeoutError,
    filing_url,
    parse_recent_filings,
)
from .providers.microsoft_ir import (
    HOME_URL,
    MicrosoftIRClient,
    MicrosoftIRConfigError,
    MicrosoftIREmptyBodyError,
    MicrosoftIRError,
    MicrosoftIRHTTPError,
    MicrosoftIRNetworkError,
    MicrosoftIRParseError,
    MicrosoftIRResponseTooLarge,
    MicrosoftIRTimeoutError,
    MicrosoftIRUrlError,
    _OfficialRedirectHandler,
    discover_earnings_links,
    extract_page_content,
    normalize_url,
)
from .source_sync import (
    MicrosoftIRSyncError,
    SecSyncError,
    sync_microsoft_ir_documents,
    sync_sec_documents,
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


def documents_url(dossier):
    return reverse("investment_research:documents", args=[dossier.pk])


def sync_documents_url(dossier):
    return reverse("investment_research:sync_documents", args=[dossier.pk])


def document_detail_url(dossier, document):
    return reverse(
        "investment_research:document_detail", args=[dossier.pk, document.pk]
    )


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
        self.assertEqual(
            reverse("investment_research:documents", args=[7]),
            "/research/7/documents/",
        )
        self.assertEqual(
            reverse("investment_research:document_detail", args=[7, 11]),
            "/research/7/documents/11/",
        )

        match = resolve("/research/")
        self.assertEqual(match.app_name, "investment_research")
        self.assertEqual(match.view_name, "investment_research:index")

    def test_navigation_contains_research_link_after_login(self):
        member = self.make_member(self.family, "NavUser")
        self.login(member)
        resp = self.client.get(INDEX_URL)
        self.assertContains(resp, '<a href="/research/">投研</a>')


class OfficialResearchDocumentModelTests(TestCase):
    """M2A-1 官方资料模型：存储契约、默认值、唯一约束与 PROTECT。"""

    @classmethod
    def setUpTestData(cls):
        cls.security = Security.objects.create(
            symbol="MSFT", name="Microsoft", market="US", asset_type="stock"
        )

    def make_doc(self, security=None, source="sec", external_id="acc-001",
                 document_type="10-k", **kwargs):
        return OfficialResearchDocument.objects.create(
            security=security or self.security,
            source=source,
            external_id=external_id,
            document_type=document_type,
            title="10-K 2025",
            source_url="https://www.sec.gov/Archives/edgar/data/789019/x.htm",
            **kwargs,
        )

    def test_create_document_defaults(self):
        doc = self.make_doc()
        self.assertIsNotNone(doc.pk)
        self.assertEqual(doc.security, self.security)
        self.assertIsInstance(doc.metadata, dict)
        self.assertEqual(doc.metadata, {})
        self.assertIsNone(doc.published_at)
        self.assertIsNone(doc.period_end)
        self.assertIsNone(doc.content_text)
        self.assertIsNone(doc.content_sha256)
        self.assertIsNone(doc.fetched_at)
        # 时间戳由 TimestampedModel 自动填充
        self.assertIsNotNone(doc.created_at)
        self.assertIsNotNone(doc.updated_at)

    def test_metadata_default_is_independent_between_instances(self):
        a = self.make_doc(external_id="acc-a")
        b = self.make_doc(external_id="acc-b")
        a.metadata["note"] = "only-a"
        a.save()
        b.refresh_from_db()
        self.assertEqual(b.metadata, {})
        a.refresh_from_db()
        self.assertEqual(a.metadata, {"note": "only-a"})

    def test_duplicate_source_external_id_rejected(self):
        self.make_doc(external_id="acc-dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_doc(external_id="acc-dup")

    def test_same_external_id_different_source_allowed(self):
        self.make_doc(source="sec", external_id="shared-id")
        other = self.make_doc(source="microsoft_ir", external_id="shared-id")
        self.assertIsNotNone(other.pk)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(external_id="shared-id").count(),
            2,
        )

    def test_security_protect_blocks_delete(self):
        from django.db.models.deletion import ProtectedError

        self.make_doc()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.security.delete()

    def test_str_does_not_hit_network(self):
        doc = self.make_doc()
        # __str__ 只依赖已加载字段，不发网络请求
        self.assertIn("MSFT", str(doc))
        self.assertIn("SEC EDGAR", str(doc))
        self.assertIn("acc-001", str(doc))

    def test_source_choices_match_spec(self):
        values = [v for v, _ in SOURCE_CHOICES]
        self.assertEqual(values, ["sec", "microsoft_ir"])

    def test_document_type_choices_match_spec(self):
        values = [v for v, _ in DOCUMENT_TYPE_CHOICES]
        self.assertEqual(
            values,
            [
                "10-k",
                "10-q",
                "8-k",
                "annual_report",
                "earnings_release",
                "earnings_call",
                "investor_update",
                "other",
            ],
        )

    def test_field_lengths_and_nullability(self):
        doc = self.make_doc()
        fields = {f.name: f for f in OfficialResearchDocument._meta.get_fields()}
        self.assertEqual(fields["external_id"].max_length, 255)
        self.assertEqual(fields["title"].max_length, 500)
        self.assertEqual(fields["source_url"].max_length, 1000)
        self.assertEqual(fields["content_sha256"].max_length, 64)
        # 可空字段
        for name in ("published_at", "period_end", "content_text",
                     "content_sha256", "fetched_at"):
            self.assertTrue(fields[name].null, f"{name} 应可空")
        # 必填字段
        for name in ("source", "external_id", "document_type", "title", "source_url"):
            self.assertFalse(fields[name].null, f"{name} 应必填")

    def test_ordering_and_indexes(self):
        meta = OfficialResearchDocument._meta
        self.assertEqual(meta.ordering, ["-published_at", "-pk"])
        index_fields = {tuple(i.fields) for i in meta.indexes}
        self.assertIn(("security", "-published_at"), index_fields)
        self.assertIn(("source", "document_type"), index_fields)

    def test_unique_constraint_registered(self):
        names = {c.name for c in OfficialResearchDocument._meta.constraints}
        self.assertIn("unique_official_document_source_external_id", names)


class ResearchSourceStateModelTests(TestCase):
    """M2A-1 来源同步状态模型：默认值、唯一约束与 PROTECT。"""

    @classmethod
    def setUpTestData(cls):
        cls.security = Security.objects.create(
            symbol="MSFT", name="Microsoft", market="US", asset_type="stock"
        )

    def make_state(self, security=None, source="sec", **kwargs):
        return ResearchSourceState.objects.create(
            security=security or self.security,
            source=source,
            **kwargs,
        )

    def test_create_state_defaults(self):
        state = self.make_state()
        self.assertIsNotNone(state.pk)
        self.assertIsInstance(state.cursor, dict)
        self.assertEqual(state.cursor, {})
        self.assertIsNone(state.external_company_id)
        self.assertIsNone(state.last_checked_at)
        self.assertIsNone(state.last_success_at)
        self.assertIsNone(state.last_error)
        self.assertIsNotNone(state.created_at)
        self.assertIsNotNone(state.updated_at)

    def test_cursor_default_is_independent_between_instances(self):
        a = self.make_state(source="sec")
        b = self.make_state(source="microsoft_ir")
        a.cursor["last_cik"] = "0000789019"
        a.save()
        b.refresh_from_db()
        self.assertEqual(b.cursor, {})
        a.refresh_from_db()
        self.assertEqual(a.cursor, {"last_cik": "0000789019"})

    def test_duplicate_security_source_rejected(self):
        self.make_state(source="sec")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_state(source="sec")

    def test_same_security_different_source_allowed(self):
        self.make_state(source="sec")
        other = self.make_state(source="microsoft_ir")
        self.assertIsNotNone(other.pk)
        self.assertEqual(
            ResearchSourceState.objects.filter(security=self.security).count(),
            2,
        )

    def test_security_protect_blocks_delete(self):
        from django.db.models.deletion import ProtectedError

        self.make_state()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.security.delete()

    def test_str_does_not_hit_network(self):
        state = self.make_state()
        self.assertIn("MSFT", str(state))
        self.assertIn("SEC EDGAR", str(state))

    def test_external_company_id_max_length(self):
        fields = {f.name: f for f in ResearchSourceState._meta.get_fields()}
        self.assertEqual(fields["external_company_id"].max_length, 64)
        self.assertTrue(fields["external_company_id"].null)

    def test_unique_constraint_registered(self):
        names = {c.name for c in ResearchSourceState._meta.constraints}
        self.assertIn("unique_research_source_state_security_source", names)


# ---------------------------------------------------------------------------
# M2A-2 SEC 客户端与同步服务离线测试（全部 mock opener，无真实网络）
# ---------------------------------------------------------------------------

TICKERS_PAYLOAD = json.dumps(
    {
        "0": {"cik_str": 320193, "ticker": "AAPL", "company": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "company": "Microsoft Corp."},
    }
).encode("utf-8")


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Sleeper:
    def __init__(self, clock=None):
        self.sleeps = []
        self.clock = clock

    def __call__(self, seconds):
        self.sleeps.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _MockOpener:
    """按序返回预设响应（bytes）或异常；记录每次 (request, timeout)。"""

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if not self.results:
            raise AssertionError("mock opener 调用次数超出预期")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


def _make_client(opener, **kwargs):
    clock = _FakeClock()
    sleeper = _Sleeper(clock)
    client = SecClient(
        user_agent=kwargs.pop("user_agent", "test-agent/1.0"),
        timeout=kwargs.pop("timeout", 2.0),
        max_response_bytes=kwargs.pop("max_response_bytes", 4096),
        rate_limit_per_second=kwargs.pop("rate_limit_per_second", 5.0),
        max_retries=kwargs.pop("max_retries", 3),
        backoff_seconds=kwargs.pop("backoff_seconds", 0.01),
        opener=opener,
        clock=clock,
        sleeper=sleeper,
    )
    return client, sleeper


def _http_error(code, body=b""):
    return urllib.error.HTTPError(
        "https://data.sec.gov/x", code, "status", {}, _FakeResponse(body)
    )


def _submissions_payload(rows, *, name="MICROSOFT CORP", include_columns=True):
    payload = {
        "cik": 789019,
        "name": name,
        "filings": {
            "recent": {
                "accessionNumber": [row[0] for row in rows],
                "filingDate": [row[1] for row in rows],
                "form": [row[2] for row in rows],
                "primaryDocument": [row[3] for row in rows],
                "reportDate": [row[4] for row in rows],
            }
        },
    }
    if not include_columns:
        del payload["filings"]["recent"]["reportDate"]
    return json.dumps(payload).encode("utf-8")


MSFT_FILING_ROWS = [
    ("0000789019-25-000011", "2025-07-30", "10-K", "msft-20250630.htm", "2025-06-30"),
    ("0000789019-25-000009", "2025-04-30", "10-Q", "msft-20250331.htm", "2025-03-31"),
    ("0000789019-25-000005", "2025-01-31", "8-K", "ex991.htm", "2025-01-31"),
    ("0000789019-25-000003", "2024-12-31", "10-K/A", "msft-20241231a.htm", "2024-12-31"),
    ("0000789019-25-000001", "2024-11-01", "DEF 14A", "def14a.htm", ""),
]


class SecClientTickerTests(SimpleTestCase):
    def test_msft_resolves_to_zero_padded_cik(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener)
        self.assertEqual(client.resolve_cik("MSFT"), "0000789019")

    def test_ticker_match_is_case_insensitive(self):
        opener = _MockOpener([TICKERS_PAYLOAD, TICKERS_PAYLOAD])
        client, _ = _make_client(opener)
        self.assertEqual(client.resolve_cik("msft"), "0000789019")
        self.assertEqual(client.resolve_cik("aapl"), "0000320193")

    def test_unknown_ticker_raises_identifiable_error(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener)
        with self.assertRaises(SecTickerNotFoundError):
            client.resolve_cik("NOPE")

    def test_tickers_payload_not_dict_raises(self):
        opener = _MockOpener([b"[1, 2, 3]"])
        client, _ = _make_client(opener)
        with self.assertRaises(SecTickerNotFoundError):
            client.resolve_cik("MSFT")

    def test_user_agent_header_is_sent(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener, user_agent="my-agent/2.0")
        client.resolve_cik("MSFT")
        request, timeout = opener.requests[0]
        # urllib 不同版本对头名大小写处理不一，按不区分大小写断言
        ua = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(ua.get("user-agent"), "my-agent/2.0")
        self.assertEqual(timeout, 2.0)

    def test_empty_user_agent_raises_before_request(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        with self.assertRaises(SecConfigError):
            _make_client(opener, user_agent="   ")
        self.assertEqual(opener.requests, [])

    def test_https_and_official_host_enforced(self):
        opener = _MockOpener([TICKERS_PAYLOAD, TICKERS_PAYLOAD])
        client, _ = _make_client(opener)
        with self.assertRaisesRegex(SecClientError, "HTTPS"):
            client._get("http://www.sec.gov/files/company_tickers.json")
        with self.assertRaisesRegex(SecClientError, "官方主机"):
            client._get("https://evil.example.com/files/company_tickers.json")

    def test_timeout_raises_sec_timeout_error(self):
        opener = _MockOpener([TimeoutError()])
        client, _ = _make_client(opener)
        with self.assertRaises(SecTimeoutError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_network_error_raises_sec_network_error(self):
        opener = _MockOpener([urllib.error.URLError("conn refused")])
        client, _ = _make_client(opener)
        with self.assertRaises(SecNetworkError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_response_too_large_raises(self):
        opener = _MockOpener([b"a" * 5000])
        client, _ = _make_client(opener, max_response_bytes=4096)
        with self.assertRaises(SecResponseTooLarge):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_429_retries_then_succeeds(self):
        opener = _MockOpener([_http_error(429), TICKERS_PAYLOAD])
        client, sleeper = _make_client(opener, max_retries=3)
        self.assertEqual(client.resolve_cik("MSFT"), "0000789019")
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(sleeper.sleeps)

    def test_503_exhausts_retries_then_raises(self):
        opener = _MockOpener([_http_error(503)] * 3)
        client, sleeper = _make_client(opener, max_retries=2)
        with self.assertRaises(SecHTTPError) as ctx:
            client.resolve_cik("MSFT")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(len(opener.requests), 3)
        # 线性退避 sleep 必须出现（可能混入节流 sleep，只断言退避值存在）
        self.assertIn(0.01, sleeper.sleeps)
        self.assertIn(0.02, sleeper.sleeps)

    def test_non_retry_http_error_no_blind_retry(self):
        body = b"X" * 5000
        opener = _MockOpener([_http_error(404, body)])
        client, _ = _make_client(opener)
        with self.assertRaises(SecHTTPError) as ctx:
            client.resolve_cik("MSFT")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(body.decode("ascii", "replace"), str(ctx.exception))

    def test_rate_limit_throttles_within_process(self):
        clock = _FakeClock()
        sleeper = _Sleeper(clock)
        opener = _MockOpener([TICKERS_PAYLOAD, TICKERS_PAYLOAD])
        client = SecClient(
            user_agent="test-agent/1.0",
            timeout=2.0,
            max_response_bytes=4096,
            rate_limit_per_second=5.0,
            max_retries=3,
            backoff_seconds=0.01,
            opener=opener,
            clock=clock,
            sleeper=sleeper,
        )
        client.resolve_cik("MSFT")
        client.resolve_cik("AAPL")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(len(sleeper.sleeps), 1)
        self.assertAlmostEqual(sleeper.sleeps[0], 0.2, places=6)


class SecFilingsParseTests(SimpleTestCase):
    def test_parse_keeps_only_allowed_forms(self):
        records = parse_recent_filings(
            json.loads(_submissions_payload(MSFT_FILING_ROWS)),
            company_name="MICROSOFT CORP",
        )
        self.assertEqual(
            [r["form"] for r in records], ["10-K", "10-Q", "8-K", "10-K/A"]
        )
        self.assertEqual(
            [r["document_type"] for r in records], ["10-k", "10-q", "8-k", "10-k"]
        )

    def test_parse_fields_are_correct(self):
        records = parse_recent_filings(
            json.loads(_submissions_payload(MSFT_FILING_ROWS)),
            company_name="MICROSOFT CORP",
        )
        first = records[0]
        self.assertEqual(first["accession"], "0000789019-25-000011")
        self.assertEqual(first["filing_date"], date(2025, 7, 30))
        self.assertEqual(first["report_date"], date(2025, 6, 30))
        self.assertEqual(first["primary_document"], "msft-20250630.htm")
        self.assertIn("10-K", first["title"])
        self.assertIn("2025-07-30", first["title"])

    def test_amendment_uses_base_form_type(self):
        records = parse_recent_filings(
            json.loads(_submissions_payload(MSFT_FILING_ROWS)),
            company_name="MICROSOFT CORP",
        )
        amendment = records[3]
        self.assertEqual(amendment["form"], "10-K/A")
        self.assertEqual(amendment["document_type"], "10-k")

    def test_missing_column_raises(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(json.loads(_submissions_payload(MSFT_FILING_ROWS, include_columns=False)))

    def test_length_mismatch_raises(self):
        payload = json.loads(_submissions_payload(MSFT_FILING_ROWS))
        payload["filings"]["recent"]["form"] = payload["filings"]["recent"]["form"][:-1]
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(payload)

    def test_invalid_filing_date_raises(self):
        rows = [
            ("0000789019-25-000011", "2025-13-40", "10-K", "x.htm", "2025-06-30"),
        ]
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(json.loads(_submissions_payload(rows)))

    def test_empty_report_date_is_allowed(self):
        rows = [
            ("0000789019-25-000011", "2025-07-30", "8-K", "x.htm", ""),
        ]
        records = parse_recent_filings(json.loads(_submissions_payload(rows)))
        self.assertIsNone(records[0]["report_date"])

    def test_missing_recent_section_raises(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings({"name": "X", "filings": {}})


class SecFilingUrlTests(SimpleTestCase):
    def test_url_is_exact(self):
        url = filing_url(
            "789019", "0000789019-25-000011", "msft-20250630.htm"
        )
        self.assertEqual(
            url,
            "https://www.sec.gov/Archives/edgar/data/789019/"
            "000078901925000011/msft-20250630.htm",
        )

    def test_padded_cik_gives_same_url(self):
        url = filing_url("0000789019", "0000789019-25-000011", "msft-20250630.htm")
        self.assertIn("/data/789019/", url)

    def test_external_url_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            filing_url("789019", "000078901925000011", "https://evil.com/x.htm")

    def test_path_traversal_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            filing_url("789019", "000078901925000011", "../x.htm")

    def test_absolute_path_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            filing_url("789019", "000078901925000011", "/x.htm")

    def test_empty_document_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            filing_url("789019", "000078901925000011", "   ")

    def test_bad_accession_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            filing_url("789019", "abc", "x.htm")


class _StubSecClient:
    def __init__(self, *, cik="0000789019", filings=None, resolve_error=None, filings_error=None):
        self.cik = cik
        self.filings = filings or []
        self.resolve_error = resolve_error
        self.filings_error = filings_error
        self.resolve_calls = []
        self.filings_calls = []

    def resolve_cik(self, ticker):
        self.resolve_calls.append(ticker)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.cik

    def get_filings(self, cik):
        self.filings_calls.append(cik)
        if self.filings_error is not None:
            raise self.filings_error
        return self.filings


def _filing_record(accession, form, filing_date, report_date, primary_document="msft.htm", company="MICROSOFT CORP"):
    base = form[:-2] if form.endswith("/A") else form
    doc_type = {"10-K": "10-k", "10-Q": "10-q", "8-K": "8-k"}[base]
    return {
        "accession": accession,
        "form": form,
        "document_type": doc_type,
        "filing_date": date.fromisoformat(filing_date),
        "report_date": date.fromisoformat(report_date) if report_date else None,
        "primary_document": primary_document,
        "title": f"{company} {form} ({filing_date})",
    }


GOOD_RECORDS = [
    _filing_record("0000789019-25-000011", "10-K", "2025-07-30", "2025-06-30", "msft-20250630.htm"),
    _filing_record("0000789019-25-000009", "10-Q", "2025-04-30", "2025-03-31", "msft-20250331.htm"),
]


class SecSyncServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.security = Security.objects.create(
            symbol="MSFT", name="Microsoft", market="US", asset_type="stock"
        )

    def sync(self, client, **kwargs):
        return sync_sec_documents(security=self.security, client=client, **kwargs)

    def test_first_sync_creates_state_and_metadata_documents(self):
        client = _StubSecClient(filings=GOOD_RECORDS)
        counters = self.sync(client)
        self.assertEqual(
            counters, {"created": 2, "updated": 0, "unchanged": 0, "failed": 0}
        )

        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertEqual(state.external_company_id, "0000789019")
        self.assertIsNotNone(state.last_checked_at)
        self.assertIsNotNone(state.last_success_at)
        self.assertIsNone(state.last_error)

        self.assertEqual(OfficialResearchDocument.objects.count(), 2)
        doc = OfficialResearchDocument.objects.get(external_id="0000789019-25-000011")
        self.assertEqual(doc.security, self.security)
        self.assertEqual(doc.document_type, "10-k")
        self.assertEqual(doc.source_url, "https://www.sec.gov/Archives/edgar/data/789019/000078901925000011/msft-20250630.htm")
        self.assertEqual(doc.published_at, date(2025, 7, 30))
        self.assertEqual(doc.period_end, date(2025, 6, 30))
        self.assertIsNotNone(doc.fetched_at)
        self.assertIn("10-K", doc.title)
        self.assertEqual(doc.metadata["cik"], "0000789019")
        self.assertEqual(doc.metadata["form"], "10-K")
        self.assertFalse(doc.metadata["amendment"])

    def test_content_text_stays_empty_in_metadata_batch(self):
        self.sync(_StubSecClient(filings=GOOD_RECORDS))
        for doc in OfficialResearchDocument.objects.all():
            self.assertIsNone(doc.content_text)
            self.assertIsNone(doc.content_sha256)

    def test_repeat_sync_is_idempotent_and_updates_metadata(self):
        self.sync(_StubSecClient(filings=GOOD_RECORDS))
        changed = [
            dict(GOOD_RECORDS[0], report_date=date(2025, 7, 1)),
            GOOD_RECORDS[1],
        ]
        counters = self.sync(_StubSecClient(filings=changed))
        # 仅 report_date 变化的一条计 updated；完全相同的一条计 unchanged。
        self.assertEqual(
            counters, {"created": 0, "updated": 1, "unchanged": 1, "failed": 0}
        )
        self.assertEqual(OfficialResearchDocument.objects.count(), 2)
        doc = OfficialResearchDocument.objects.get(external_id="0000789019-25-000011")
        self.assertEqual(doc.period_end, date(2025, 7, 1))

    def test_max_documents_limits_sync(self):
        records = [
            _filing_record(f"0000789019-25-0000{i:02d}", "8-K", "2025-01-01", "2025-01-01")
            for i in range(1, 6)
        ]
        counters = self.sync(_StubSecClient(filings=records), max_documents=2)
        self.assertEqual(counters["created"], 2)
        self.assertEqual(OfficialResearchDocument.objects.count(), 2)

    def test_non_us_market_raises(self):
        hk = Security.objects.create(symbol="0700", name="Tencent", market="HK", asset_type="stock")
        with self.assertRaises(SecSyncError):
            sync_sec_documents(security=hk, client=_StubSecClient(filings=[]))
        self.assertFalse(ResearchSourceState.objects.filter(security=hk).exists())

    def test_non_stock_raises(self):
        etf = Security.objects.create(symbol="QQQ", name="Nasdaq 100", market="US", asset_type="etf")
        with self.assertRaises(SecSyncError):
            sync_sec_documents(security=etf, client=_StubSecClient(filings=[]))

    def test_cached_cik_avoids_ticker_lookup(self):
        ResearchSourceState.objects.create(
            security=self.security,
            source="sec",
            external_company_id="0000789019",
        )
        client = _StubSecClient(filings=GOOD_RECORDS, resolve_error=AssertionError("不应查 ticker"))
        counters = self.sync(client)
        self.assertEqual(counters["created"], 2)
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.filings_calls, ["0000789019"])

    def test_fatal_filings_error_records_state_and_keeps_documents(self):
        old_success = timezone.now() - timedelta(days=3)
        ResearchSourceState.objects.create(
            security=self.security,
            source="sec",
            external_company_id="0000789019",
            last_success_at=old_success,
        )
        OfficialResearchDocument.objects.create(
            security=self.security,
            source="sec",
            external_id="0000789019-24-000001",
            document_type="10-k",
            title="old 10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/789019/000078901924000001/x.htm",
            published_at=date(2024, 7, 30),
            fetched_at=old_success,
        )
        client = _StubSecClient(filings_error=SecClientError("boom" * 500))
        with self.assertRaises(SecClientError):
            self.sync(client)
        state = ResearchSourceState.objects.get(pk__gt=0)
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIsNotNone(state.last_checked_at)
        self.assertEqual(state.last_success_at, old_success)
        self.assertIn("boom", state.last_error)
        self.assertLessEqual(len(state.last_error), 2000)
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_bad_document_does_not_delete_earlier_success(self):
        bad = _filing_record(
            "0000789019-25-000009", "10-Q", "2025-04-30", "2025-03-31", primary_document="../x.htm"
        )
        counters = self.sync(_StubSecClient(filings=[GOOD_RECORDS[0], bad]))
        self.assertEqual(
            counters, {"created": 1, "updated": 0, "unchanged": 0, "failed": 1}
        )
        self.assertTrue(
            OfficialResearchDocument.objects.filter(
                external_id="0000789019-25-000011"
            ).exists()
        )
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIn("0000789019-25-000009", state.last_error)
        self.assertIsNotNone(state.last_success_at)

    def test_all_documents_failed_keeps_previous_success(self):
        old_success = timezone.now() - timedelta(days=3)
        ResearchSourceState.objects.create(
            security=self.security,
            source="sec",
            external_company_id="0000789019",
            last_success_at=old_success,
        )
        OfficialResearchDocument.objects.create(
            security=self.security,
            source="sec",
            external_id="0000789019-24-000001",
            document_type="10-k",
            title="old 10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/789019/000078901924000001/x.htm",
            published_at=date(2024, 7, 30),
            fetched_at=old_success,
        )
        bad_records = [
            _filing_record("0000789019-25-000011", "10-K", "2025-07-30", "2025-06-30", primary_document="https://evil.com/x.htm"),
            _filing_record("0000789019-25-000009", "10-Q", "2025-04-30", "2025-03-31", primary_document="../y.htm"),
        ]
        counters = self.sync(_StubSecClient(filings=bad_records))
        self.assertEqual(
            counters, {"created": 0, "updated": 0, "unchanged": 0, "failed": 2}
        )
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertEqual(state.last_success_at, old_success)
        self.assertIsNotNone(state.last_error)
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_empty_filings_marks_success_without_documents(self):
        counters = self.sync(_StubSecClient(filings=[]))
        self.assertEqual(
            counters, {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        )
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIsNotNone(state.last_success_at)
        self.assertIsNone(state.last_error)


# ---------------------------------------------------------------------------
# M2A-2 修复批：主代理复审边界问题转正式测试
# ---------------------------------------------------------------------------


class SecClientConfigValidationTests(SimpleTestCase):
    """SecClient 构造时数值配置校验：任何请求前拒绝，统一 SecConfigError。"""

    def _assert_config_error(self, **kwargs):
        opener = _MockOpener([TICKERS_PAYLOAD])
        with self.assertRaises(SecConfigError):
            _make_client(opener, **kwargs)
        self.assertEqual(opener.requests, [])

    def test_zero_timeout_rejected(self):
        self._assert_config_error(timeout=0)

    def test_negative_timeout_rejected(self):
        self._assert_config_error(timeout=-1.5)

    def test_zero_max_response_bytes_rejected(self):
        self._assert_config_error(max_response_bytes=0)

    def test_negative_max_response_bytes_rejected(self):
        self._assert_config_error(max_response_bytes=-1)

    def test_zero_rate_limit_rejected(self):
        self._assert_config_error(rate_limit_per_second=0)

    def test_negative_rate_limit_rejected(self):
        self._assert_config_error(rate_limit_per_second=-2.0)

    def test_rate_limit_above_project_cap_rejected(self):
        self._assert_config_error(rate_limit_per_second=5.1)

    def test_rate_limit_at_project_cap_accepted(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener, rate_limit_per_second=5.0)
        self.assertEqual(client.resolve_cik("MSFT"), "0000789019")

    def test_non_numeric_timeout_rejected(self):
        self._assert_config_error(timeout="10")

    def test_bool_max_retries_rejected(self):
        self._assert_config_error(max_retries=True)

    def test_float_max_retries_rejected(self):
        self._assert_config_error(max_retries=2.5)

    def test_negative_max_retries_rejected(self):
        self._assert_config_error(max_retries=-1)

    def test_zero_max_retries_accepted(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener, max_retries=0)
        self.assertEqual(client.max_retries, 0)

    def test_negative_backoff_rejected(self):
        self._assert_config_error(backoff_seconds=-0.1)

    def test_zero_backoff_accepted(self):
        opener = _MockOpener([TICKERS_PAYLOAD])
        client, _ = _make_client(opener, backoff_seconds=0)
        self.assertEqual(client.backoff_seconds, 0)


class SecClientOSErrorWrapTests(SimpleTestCase):
    """_get 把底层 OSError 统一包装为 SecNetworkError，不泄漏响应正文。"""

    def test_connection_reset_wrapped_as_network_error(self):
        opener = _MockOpener([ConnectionResetError("reset by peer")])
        client, _ = _make_client(opener)
        with self.assertRaises(SecNetworkError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_plain_oserror_wrapped_as_network_error(self):
        opener = _MockOpener([OSError("boom")])
        client, _ = _make_client(opener)
        with self.assertRaises(SecNetworkError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_http_error_stays_http_error(self):
        opener = _MockOpener([_http_error(404, b"SECRET-BODY")])
        client, _ = _make_client(opener)
        with self.assertRaises(SecHTTPError) as ctx:
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")
        self.assertEqual(ctx.exception.status, 404)
        self.assertNotIn("SECRET-BODY", str(ctx.exception))

    def test_timeout_stays_timeout_error(self):
        opener = _MockOpener([TimeoutError()])
        client, _ = _make_client(opener)
        with self.assertRaises(SecTimeoutError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")

    def test_urlerror_stays_network_error(self):
        opener = _MockOpener([urllib.error.URLError("refused")])
        client, _ = _make_client(opener)
        with self.assertRaises(SecNetworkError):
            client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")


class SecFilingsColumnTypeTests(SimpleTestCase):
    """REQUIRED_FILING_COLUMNS 全部必须是 JSON list，畸形值抛 SecFilingsParseError。"""

    def _payload_with_column(self, column, value):
        payload = json.loads(_submissions_payload(MSFT_FILING_ROWS))
        payload["filings"]["recent"][column] = value
        return payload

    def test_none_column_raises_parse_error_not_type_error(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(
                self._payload_with_column("accessionNumber", None)
            )

    def test_string_column_raises_parse_error(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(
                self._payload_with_column("filingDate", "2025-07-30")
            )

    def test_dict_column_raises_parse_error(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(self._payload_with_column("form", {"0": "10-K"}))

    def test_number_column_raises_parse_error(self):
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(
                self._payload_with_column("primaryDocument", 42)
            )

    def test_length_check_still_applies_after_type_check(self):
        payload = json.loads(_submissions_payload(MSFT_FILING_ROWS))
        payload["filings"]["recent"]["form"] = payload["filings"]["recent"]["form"][:-1]
        with self.assertRaises(SecFilingsParseError):
            parse_recent_filings(payload)


class SecFilingUrlHardeningTests(SimpleTestCase):
    """filing_url 拒绝 query/fragment/编码穿越/控制字符；正常文件名继续通过。"""

    def _url(self, document):
        return filing_url("789019", "000078901925000011", document)

    def test_query_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            self._url("msft.htm?a=1")

    def test_fragment_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            self._url("msft.htm#top")

    def test_percent_encoded_traversal_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            self._url("..%2fx.htm")
        with self.assertRaises(SecDocumentUrlError):
            self._url("%2e%2e/x.htm")

    def test_control_character_rejected(self):
        with self.assertRaises(SecDocumentUrlError):
            self._url("msft\t.htm")
        with self.assertRaises(SecDocumentUrlError):
            self._url("msft\x01.htm")

    def test_normal_primary_document_passes(self):
        self.assertEqual(
            self._url("msft-20250630.htm"),
            "https://www.sec.gov/Archives/edgar/data/789019/"
            "000078901925000011/msft-20250630.htm",
        )

    def test_percent_encoded_normal_name_passes(self):
        url = self._url("msft%20annual.htm")
        self.assertTrue(url.endswith("/msft%20annual.htm"))


class SecSyncBoundaryRepairTests(TestCase):
    """同步服务边界：CIK 首次解析失败、accession 冲突、max_documents 校验。"""

    @classmethod
    def setUpTestData(cls):
        cls.security = Security.objects.create(
            symbol="MSFT", name="Microsoft", market="US", asset_type="stock"
        )
        cls.other = Security.objects.create(
            symbol="OTHER", name="Other", market="US", asset_type="stock"
        )

    def test_cik_resolution_failure_records_state_on_new_security(self):
        client = _StubSecClient(resolve_error=SecClientError("lookup failed"))
        with self.assertRaises(SecClientError) as ctx:
            sync_sec_documents(security=self.security, client=client)
        self.assertIn("lookup failed", str(ctx.exception))
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIsNotNone(state.last_checked_at)
        self.assertIn("lookup failed", state.last_error)
        self.assertIsNone(state.last_success_at)
        self.assertFalse(OfficialResearchDocument.objects.exists())

    def test_cik_resolution_failure_preserves_existing_success_and_documents(self):
        old_success = timezone.now() - timedelta(days=3)
        ResearchSourceState.objects.create(
            security=self.security,
            source="sec",
            external_company_id=None,
            last_success_at=old_success,
        )
        OfficialResearchDocument.objects.create(
            security=self.security,
            source="sec",
            external_id="0000789019-24-000001",
            document_type="10-k",
            title="old 10-K",
            source_url="https://www.sec.gov/Archives/edgar/data/789019/000078901924000001/x.htm",
            published_at=date(2024, 7, 30),
            fetched_at=old_success,
        )
        client = _StubSecClient(resolve_error=SecClientError("ticker down"))
        with self.assertRaises(SecClientError):
            sync_sec_documents(security=self.security, client=client)
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIsNotNone(state.last_checked_at)
        self.assertIn("ticker down", state.last_error)
        self.assertEqual(state.last_success_at, old_success)
        self.assertEqual(OfficialResearchDocument.objects.count(), 1)

    def test_max_documents_invalid_rejected_before_state_and_client(self):
        client = _StubSecClient(filings=GOOD_RECORDS)
        for bad in (0, 101, -3, True, False, 2.5, "10"):
            with self.assertRaises(SecSyncError):
                sync_sec_documents(
                    security=self.security, client=client, max_documents=bad
                )
        self.assertFalse(
            ResearchSourceState.objects.filter(security=self.security).exists()
        )
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.filings_calls, [])

    def test_max_documents_boundaries_accepted(self):
        counters = sync_sec_documents(
            security=self.security,
            client=_StubSecClient(filings=GOOD_RECORDS),
            max_documents=1,
        )
        self.assertEqual(counters["created"], 1)
        counters = sync_sec_documents(
            security=self.security,
            client=_StubSecClient(filings=GOOD_RECORDS),
            max_documents=100,
        )
        self.assertEqual(counters["updated"], 0)
        self.assertEqual(counters["unchanged"], 1)

    def test_accession_conflict_keeps_original_security_and_counts_failed(self):
        accession = "0000789019-25-000011"
        document = OfficialResearchDocument.objects.create(
            security=self.other,
            source="sec",
            external_id=accession,
            document_type="10-k",
            title="Existing",
            source_url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
        )
        counters = sync_sec_documents(
            security=self.security, client=_StubSecClient(filings=[GOOD_RECORDS[0]])
        )
        self.assertEqual(counters["failed"], 1)
        document.refresh_from_db()
        self.assertEqual(document.security_id, self.other.pk)
        self.assertEqual(document.title, "Existing")
        state = ResearchSourceState.objects.get(security=self.security, source="sec")
        self.assertIn(accession, state.last_error)
        self.assertLessEqual(len(state.last_error), 2000)

    def test_same_security_update_excludes_security_from_update_fields(self):
        sync_sec_documents(
            security=self.security, client=_StubSecClient(filings=GOOD_RECORDS)
        )
        changed = [
            dict(GOOD_RECORDS[0], report_date=date(2025, 7, 1)),
            GOOD_RECORDS[1],
        ]
        with mock.patch.object(
            OfficialResearchDocument, "save", autospec=True
        ) as save_mock:
            counters = sync_sec_documents(
                security=self.security, client=_StubSecClient(filings=changed)
            )
        self.assertEqual(counters["updated"], 1)
        self.assertEqual(counters["unchanged"], 1)
        update_calls = [
            call.kwargs
            for call in save_mock.call_args_list
            if "update_fields" in call.kwargs
        ]
        self.assertTrue(update_calls)
        for kwargs in update_calls:
            self.assertNotIn("security", kwargs["update_fields"])


# ---------------------------------------------------------------------------
# M2A-3 Microsoft IR connector tests（全 mock / fixture，离线）
# ---------------------------------------------------------------------------

MSFT_IR_TESTDATA = Path(__file__).resolve().parent / "testdata"
MSFT_IR_HOME_FIXTURE = (MSFT_IR_TESTDATA / "microsoft_ir_home.html").read_text(
    encoding="utf-8"
)
MSFT_IR_EARNINGS_FIXTURE = (
    MSFT_IR_TESTDATA / "microsoft_ir_earnings.html"
).read_text(encoding="utf-8")

FY26Q3_URL = (
    "https://www.microsoft.com/en-us/investor/earnings/fy-2026-q3/"
    "press-release-webcast"
)
FY26Q2_URL = (
    "https://www.microsoft.com/en-us/investor/earnings/fy-2026-q2/"
    "press-release-webcast"
)
FY25Q4_URL = (
    "https://www.microsoft.com/en-us/investor/earnings/fy-2025-q4/"
    "press-release-webcast"
)
class MicrosoftIRUrlNormalizationTests(SimpleTestCase):
    def test_relative_and_absolute_resolve_to_same_normalized_url(self):
        self.assertEqual(
            normalize_url(
                "/en-us/investor/earnings/fy-2026-q3/press-release-webcast"
            ),
            FY26Q3_URL,
        )
        self.assertEqual(
            normalize_url(
                "https://www.microsoft.com/en-us/investor/earnings/fy-2026-q3/"
                "press-release-webcast#top"
            ),
            FY26Q3_URL,
        )
        self.assertEqual(
            normalize_url(
                "https://microsoft.com/en-us/investor/earnings/fy-2026-q3/"
                "press-release-webcast"
            ),
            FY26Q3_URL,
        )

    def test_query_and_fragment_stripped(self):
        # 追踪 query 与 fragment 都不参与同一财报的 external_id。
        url = normalize_url(
            "/en-us/investor/earnings/fy-2026-q2/press-release-webcast"
            "?utm_source=nav#webcast"
        )
        self.assertEqual(url, FY26Q2_URL)

    def test_default_https_443_port_normalized_away(self):
        self.assertEqual(
            normalize_url(
                "https://www.microsoft.com:443/en-us/investor/earnings/"
                "fy-2026-q3/press-release-webcast"
            ),
            FY26Q3_URL,
        )

    def test_http_scheme_rejected(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(
                "http://www.microsoft.com/en-us/investor/earnings/fy-2026-q3/"
                "press-release-webcast"
            )

    def test_non_microsoft_host_rejected(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(
                "https://investors.microsoftcorp.example.com/en-us/investor/"
                "earnings/fy-2026-q3/press-release-webcast"
            )
        # 协议相对外站链接经 base 解析后仍是外站 host。
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url("//evil.example.com/earnings")

    def test_userinfo_rejected(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(
                "https://user:pass@www.microsoft.com/en-us/investor/earnings/"
                "fy-2026-q3/press-release-webcast"
            )

    def test_non_default_port_rejected(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(
                "https://www.microsoft.com:8443/en-us/investor/earnings/"
                "fy-2026-q3/press-release-webcast"
            )

    def test_malformed_port_is_wrapped_as_url_error(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(
                "https://www.microsoft.com:bad/en-us/investor/earnings/"
                "fy-2026-q3/press-release-webcast"
            )

    def test_javascript_and_empty_rejected(self):
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url("javascript:void(0)")
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url("   ")

    def test_over_255_chars_rejected(self):
        long_path = (
            "/en-us/investor/earnings/fy-2026-q3/press-release-webcast"
            + "/x" * 240
        )
        with self.assertRaises(MicrosoftIRUrlError):
            normalize_url(long_path)


class MicrosoftIRDiscoveryTests(SimpleTestCase):
    def test_discovers_only_earnings_pattern_and_dedupes(self):
        links = discover_earnings_links(MSFT_IR_HOME_FIXTURE)
        urls = [link["url"] for link in links]
        self.assertEqual(
            urls,
            [FY26Q3_URL, FY26Q2_URL, FY25Q4_URL],
        )
        by_url = {link["url"]: link for link in links}
        self.assertEqual(by_url[FY26Q3_URL]["fy"], 2026)
        self.assertEqual(by_url[FY26Q3_URL]["quarter"], 3)
        self.assertEqual(by_url[FY25Q4_URL]["fy"], 2025)
        self.assertEqual(by_url[FY25Q4_URL]["quarter"], 4)

    def test_fragment_and_query_variants_are_deduped(self):
        links = discover_earnings_links(MSFT_IR_HOME_FIXTURE)
        urls = [link["url"] for link in links]
        # fragment、query 变体与裸 URL 都归一化为同一条。
        self.assertEqual(urls.count(FY26Q2_URL), 1)

    def test_path_case_is_canonicalized(self):
        html = (
            '<a href="/en-us/Investor/Earnings/FY-2026-Q3/'
            'Press-Release-Webcast">Q3</a>'
        )
        self.assertEqual(discover_earnings_links(html)[0]["url"], FY26Q3_URL)

    def test_non_earnings_and_external_links_ignored(self):
        html = (
            "<html><body>"
            '<a href="/en-us/investor/news/2026/07-30-microsoft-reports">News</a>'
            '<a href="https://www.microsoft.com/en-us/investor/default.aspx">Home</a>'
            '<a href="javascript:void(0)">JS</a>'
            '<a href="https://blogs.microsoft.com/blog/2026/01/01/x/">Blog</a>'
            '<a href="/en-us/investor/earnings/fy-2026-q9/press-release-webcast">'
            "Bad quarter</a>"
            "</body></html>"
        )
        self.assertEqual(discover_earnings_links(html), [])

    def test_discovery_does_not_raise_on_messy_html(self):
        html = (
            "<div><a href='/en-us/investor/earnings/fy-2026-q1/"
            "press-release-webcast'>Q1</a><p>unclosed"
        )
        links = discover_earnings_links(html)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["quarter"], 1)


class MicrosoftIRExtractionTests(SimpleTestCase):
    def test_title_date_and_body_extracted(self):
        page = extract_page_content(MSFT_IR_EARNINGS_FIXTURE)
        self.assertEqual(
            page["title"],
            "Microsoft Reports Fiscal Third Quarter of 2026 Results | Microsoft",
        )
        self.assertEqual(page["published_at"], date(2026, 7, 30))
        body = page["content_text"]
        self.assertIn("Microsoft Reports Fiscal Third Quarter of 2026 Results", body)
        self.assertIn("REDMOND, Wash.", body)
        self.assertIn("Revenue increased 14 percent to $70.1 billion.", body)

    def test_only_first_head_title_is_used(self):
        html = (
            "<html><head><title>Official title</title></head><body>"
            "<svg><title>Privacy icon</title></svg><p>Body</p></body></html>"
        )
        self.assertEqual(extract_page_content(html)["title"], "Official title")

    def test_release_dateline_used_without_semantic_date_markup(self):
        html = (
            "<html><head><title>T</title></head><body><p>"
            "REDMOND, Wash. — July 29, 2026 — Microsoft announced results."
            "</p></body></html>"
        )
        self.assertEqual(extract_page_content(html)["published_at"], date(2026, 7, 29))

    def test_script_style_nav_excluded_from_body(self):
        body = extract_page_content(MSFT_IR_EARNINGS_FIXTURE)["content_text"]
        self.assertNotIn("script text should not appear", body)
        self.assertNotIn("banner", body)
        self.assertNotIn("Top of page", body)

    def test_inline_whitespace_collapsed_but_blocks_preserved(self):
        body = extract_page_content(MSFT_IR_EARNINGS_FIXTURE)["content_text"]
        self.assertNotIn("  ", body)
        self.assertNotIn("\t", body)
        self.assertIn("\n\n", body)
        self.assertIn(
            "• Intelligent Cloud revenue was $34.2 billion, up 18 percent.",
            body,
        )

    def test_press_release_scope_excludes_surrounding_page_chrome(self):
        html = (
            "<html><head><title>T</title></head><body>"
            "<div>Site navigation should be excluded</div>"
            '<div id="pressreleasecontent"><h2>Results</h2>'
            "<p>Revenue grew.</p></div>"
            "<footer>Footer should be excluded</footer>"
            "</body></html>"
        )
        body = extract_page_content(html)["content_text"]
        self.assertEqual(body, "Results\n\nRevenue grew.")

    def test_meta_published_time_used_when_no_time_tag(self):
        html = (
            "<html><head><title>T</title>"
            '<meta property="article:published_time" content="2026-01-29T09:30:00Z">'
            "</head><body><p>Body</p></body></html>"
        )
        self.assertEqual(
            extract_page_content(html)["published_at"], date(2026, 1, 29)
        )

    def test_missing_title_raises_parse_error(self):
        with self.assertRaises(MicrosoftIRParseError):
            extract_page_content(
                "<html><head></head><body><p>Body</p></body></html>"
            )

    def test_empty_body_raises_empty_body_error(self):
        html = (
            "<html><head><title>T</title></head>"
            "<body><nav><a href='/x'>N</a></nav></body></html>"
        )
        with self.assertRaises(MicrosoftIREmptyBodyError):
            extract_page_content(html)

    def test_over_char_limit_raises(self):
        html = (
            "<html><head><title>T</title></head><body><p>"
            + "word " * 100
            + "</p></body></html>"
        )
        with self.assertRaises(MicrosoftIRParseError):
            extract_page_content(html, max_content_chars=200)


class _MSFTMockOpener:
    """按 URL 返回预设响应（bytes）或异常；记录每次 (request, timeout)。"""

    def __init__(self):
        self.responses = {}
        self.raise_for = {}
        self.calls = []

    def open(self, request, timeout=None):
        # 客户端按 OpenerDirector 接口调用 .open(request, timeout=...)。
        self.calls.append((request, timeout))
        url = request.full_url
        if url in self.raise_for:
            raise self.raise_for[url]
        if url not in self.responses:
            raise AssertionError(f"mock opener 收到未预设 URL：{url}")
        return _FakeResponse(self.responses[url])


class MicrosoftIRClientTests(SimpleTestCase):
    def setUp(self):
        self.opener = _MSFTMockOpener()
        self.opener.responses[HOME_URL] = MSFT_IR_HOME_FIXTURE.encode("utf-8")
        self.opener.responses[FY26Q3_URL] = MSFT_IR_EARNINGS_FIXTURE.encode("utf-8")

    def _client(self, **kwargs):
        return MicrosoftIRClient(opener=self.opener, timeout_seconds=5, **kwargs)

    def test_fetch_home_sends_user_agent_and_timeout(self):
        client = self._client(user_agent="FamilyWorkbench-Research/1.0 (MSFT IR sync)")
        html = client.fetch_home()
        self.assertIn("Investor Relations", html)
        request, timeout = self.opener.calls[0]
        self.assertEqual(request.full_url, HOME_URL)
        # urllib 把 header 名规范化为 "User-agent"，做大小写不敏感查找。
        ua = {k.lower(): v for k, v in request.headers.items()}["user-agent"]
        self.assertEqual(ua, "FamilyWorkbench-Research/1.0 (MSFT IR sync)")
        self.assertEqual(timeout, 5)

    def test_fetch_earnings_page_validates_url_and_extracts(self):
        page = self._client().fetch_earnings_page(FY26Q3_URL)
        self.assertEqual(page["published_at"], date(2026, 7, 30))
        self.assertIn("$70.1 billion", page["content_text"])

    def test_fetch_earnings_page_rejects_foreign_url(self):
        with self.assertRaises(MicrosoftIRUrlError):
            self._client().fetch_earnings_page(
                "https://example.com/en-us/investor/earnings/fy-2026-q3/"
                "press-release-webcast"
            )

    def test_http_error_mapped(self):
        self.opener.responses[FY26Q3_URL] = b"<html>busy</html>"
        self.opener.raise_for[FY26Q3_URL] = urllib.error.HTTPError(
            FY26Q3_URL, 503, "Service Unavailable", {}, _FakeResponse(b"")
        )
        with self.assertRaises(MicrosoftIRHTTPError) as ctx:
            self._client().fetch_earnings_page(FY26Q3_URL)
        self.assertEqual(ctx.exception.status, 503)

    def test_timeout_mapped(self):
        self.opener.raise_for[FY26Q3_URL] = TimeoutError("timed out")
        with self.assertRaises(MicrosoftIRTimeoutError):
            self._client().fetch_earnings_page(FY26Q3_URL)

    def test_network_error_mapped(self):
        self.opener.raise_for[FY26Q3_URL] = urllib.error.URLError("dns failure")
        with self.assertRaises(MicrosoftIRNetworkError):
            self._client().fetch_earnings_page(FY26Q3_URL)

    def test_read_timeout_and_oserror_are_mapped(self):
        class BrokenResponse:
            def __init__(self, error):
                self.error = error

            def read(self, _size):
                raise self.error

            def close(self):
                pass

        for error, expected in (
            (TimeoutError("read timed out"), MicrosoftIRTimeoutError),
            (ConnectionResetError("reset"), MicrosoftIRNetworkError),
        ):
            with self.subTest(error=type(error).__name__):
                opener = mock.Mock()
                opener.open.return_value = BrokenResponse(error)
                with self.assertRaises(expected):
                    MicrosoftIRClient(opener=opener).fetch_home()

    def test_invalid_boolean_and_non_finite_config_rejected(self):
        for kwargs in (
            {"timeout_seconds": True},
            {"timeout_seconds": float("inf")},
            {"max_response_bytes": True},
            {"max_content_chars": True},
            {"max_content_chars": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MicrosoftIRConfigError):
                    MicrosoftIRClient(**kwargs)

    def test_oversized_response_rejected(self):
        big = (
            "<html><head><title>T</title></head><body><p>"
            + "x" * 4096
            + "</p></body></html>"
        ).encode("utf-8")
        self.opener.responses[FY26Q3_URL] = big
        with self.assertRaises(MicrosoftIRResponseTooLarge):
            MicrosoftIRClient(
                opener=self.opener, max_response_bytes=1024
            ).fetch_earnings_page(FY26Q3_URL)

    def test_invalid_utf8_raises_parse_error(self):
        self.opener.responses[FY26Q3_URL] = (
            b"<html><head><title>T</title></head><body><p>\xff\xfe</p></body></html>"
        )
        with self.assertRaises(MicrosoftIRParseError):
            self._client().fetch_earnings_page(FY26Q3_URL)

    def test_redirect_handler_rejects_non_official_host(self):
        handler = _OfficialRedirectHandler()
        request = urllib.request.Request(HOME_URL)
        with self.assertRaises(MicrosoftIRUrlError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://evil.example.com/x"
            )
        # 官方 host 放行（返回新的 Request）。
        ok = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://www.microsoft.com/en-us/investor/earnings/fy-2026-q3/"
            "press-release-webcast",
        )
        self.assertIsNotNone(ok)

    def test_redirect_handler_wraps_malformed_port(self):
        handler = _OfficialRedirectHandler()
        request = urllib.request.Request(HOME_URL)
        with self.assertRaises(MicrosoftIRUrlError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://www.microsoft.com:bad/en-us/investor/",
            )

    def test_default_opener_includes_whitelist_redirect_handler(self):
        client = MicrosoftIRClient(timeout_seconds=1)
        handlers = [type(h).__name__ for h in client._opener.handlers]
        self.assertIn("_OfficialRedirectHandler", handlers)


class _StubMSFTClient:
    """sync_microsoft_ir_documents 的 duck-typed stub（离线 fixture）。"""

    def __init__(self, home_html=MSFT_IR_HOME_FIXTURE, pages=None, home_error=None):
        self.home_html = home_html
        self.pages = dict(pages or {})
        self.home_error = home_error
        self.fetched = []

    def fetch_home(self):
        if self.home_error is not None:
            raise self.home_error
        return self.home_html

    def fetch_earnings_page(self, url):
        self.fetched.append(url)
        html = self.pages.get(url, MSFT_IR_EARNINGS_FIXTURE)
        return extract_page_content(html)


def _msft_security(symbol="MSFT"):
    security = Security.objects.create(
        symbol=symbol,
        name="Microsoft" if symbol == "MSFT" else "Apple",
        market="US",
        asset_type="stock",
    )
    ResearchSourceState.objects.create(
        security=security, source=SOURCE_MICROSOFT_IR
    )
    return security


class MicrosoftIRSyncServiceTests(TestCase):
    def test_msft_only_gate(self):
        security = _msft_security("AAPL")
        with self.assertRaises(MicrosoftIRSyncError):
            sync_microsoft_ir_documents(security=security, client=_StubMSFTClient())
        state = ResearchSourceState.objects.get(security=security)
        self.assertIsNone(state.last_success_at)

    def test_invalid_max_documents(self):
        security = _msft_security()
        for bad in (0, "10", -3, True):
            with self.assertRaises(MicrosoftIRSyncError):
                sync_microsoft_ir_documents(
                    security=security,
                    client=_StubMSFTClient(),
                    max_documents=bad,
                )

    def test_first_sync_creates_documents_and_state(self):
        security = _msft_security()
        result = sync_microsoft_ir_documents(
            security=security, client=_StubMSFTClient()
        )
        self.assertEqual(result["created"], 3)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            3,
        )
        document = OfficialResearchDocument.objects.get(external_id=FY26Q3_URL)
        self.assertEqual(document.source, SOURCE_MICROSOFT_IR)
        self.assertEqual(document.document_type, "earnings_release")
        self.assertEqual(
            document.title,
            "Microsoft Reports Fiscal Third Quarter of 2026 Results | Microsoft",
        )
        self.assertEqual(document.published_at, date(2026, 7, 30))
        self.assertEqual(document.security, security)
        self.assertEqual(document.source_url, FY26Q3_URL)
        self.assertTrue(document.content_text)
        self.assertNotIn("<p>", document.content_text)
        self.assertEqual(
            document.content_sha256,
            hashlib.sha256(document.content_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(document.metadata["fy"], 2026)
        self.assertEqual(document.metadata["quarter"], 3)
        state = ResearchSourceState.objects.get(security=security)
        self.assertIsNotNone(state.last_success_at)
        self.assertIsNone(state.last_error)

    def test_repeat_sync_is_idempotent_and_updates_sha_on_body_change(self):
        security = _msft_security()
        sync_microsoft_ir_documents(security=security, client=_StubMSFTClient())
        first_sha = OfficialResearchDocument.objects.get(
            external_id=FY26Q3_URL
        ).content_sha256
        result = sync_microsoft_ir_documents(
            security=security, client=_StubMSFTClient()
        )
        self.assertEqual(result["created"], 0)
        # 业务字段完全一致：仅 fetched_at 刷新必须计 unchanged，不计 updated。
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unchanged"], 3)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            3,
        )
        # 正文变化 → sha 更新，不新增行。
        changed = MSFT_IR_EARNINGS_FIXTURE.replace(
            "$70.1 billion", "$69.8 billion"
        )
        sync_microsoft_ir_documents(
            security=security,
            client=_StubMSFTClient(pages={FY26Q3_URL: changed}),
        )
        document = OfficialResearchDocument.objects.get(external_id=FY26Q3_URL)
        self.assertNotEqual(document.content_sha256, first_sha)
        self.assertEqual(
            document.content_sha256,
            hashlib.sha256(document.content_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            3,
        )

    def test_home_failure_preserves_old_body_and_last_success_at(self):
        security = _msft_security()
        sync_microsoft_ir_documents(security=security, client=_StubMSFTClient())
        old_document = OfficialResearchDocument.objects.get(external_id=FY26Q3_URL)
        old_body = old_document.content_text
        old_sha = old_document.content_sha256
        state = ResearchSourceState.objects.get(security=security)
        first_success = state.last_success_at
        with self.assertRaises(MicrosoftIRTimeoutError):
            sync_microsoft_ir_documents(
                security=security,
                client=_StubMSFTClient(
                    home_error=MicrosoftIRTimeoutError("timed out")
                ),
            )
        refreshed = OfficialResearchDocument.objects.get(pk=old_document.pk)
        self.assertEqual(refreshed.content_text, old_body)
        self.assertEqual(refreshed.content_sha256, old_sha)
        state.refresh_from_db()
        self.assertEqual(state.last_success_at, first_success)
        self.assertIn("timed out", state.last_error)

    def test_single_document_failure_does_not_stop_others(self):
        security = _msft_security()
        client = _StubMSFTClient()
        client.pages[FY26Q2_URL] = (
            "<html><head><title>T</title></head>"
            "<body><nav>N</nav></body></html>"
        )
        sync_microsoft_ir_documents(security=security, client=client)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            2,
        )
        state = ResearchSourceState.objects.get(security=security)
        self.assertIsNotNone(state.last_success_at)
        self.assertIn("正文为空", state.last_error)

    def test_zero_links_is_fatal_and_preserves_old_data(self):
        security = _msft_security()
        sync_microsoft_ir_documents(security=security, client=_StubMSFTClient())
        state = ResearchSourceState.objects.get(security=security)
        first_success = state.last_success_at
        empty_home = (
            "<html><head><title>IR</title></head>"
            "<body><p>No links</p></body></html>"
        )
        with self.assertRaises(MicrosoftIRError):
            sync_microsoft_ir_documents(
                security=security,
                client=_StubMSFTClient(home_html=empty_home),
            )
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            3,
        )
        state.refresh_from_db()
        self.assertEqual(state.last_success_at, first_success)
        self.assertIn("页面结构", state.last_error)

    def test_max_documents_caps_discovered_links(self):
        security = _msft_security()
        client = _StubMSFTClient()
        result = sync_microsoft_ir_documents(
            security=security, client=client, max_documents=2
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(client.fetched), 2)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(security=security).count(),
            2,
        )

    def test_external_id_conflict_across_securities_counts_failed(self):
        msft = _msft_security("MSFT")
        aapl = _msft_security("AAPL")
        OfficialResearchDocument.objects.create(
            source=SOURCE_MICROSOFT_IR,
            external_id=FY26Q3_URL,
            security=aapl,
            document_type="earnings_release",
            title="Taken",
            source_url=FY26Q3_URL,
        )
        result = sync_microsoft_ir_documents(
            security=msft, client=_StubMSFTClient()
        )
        self.assertGreaterEqual(result["failed"], 1)
        # 冲突文档保留原归属。
        taken = OfficialResearchDocument.objects.get(external_id=FY26Q3_URL)
        self.assertEqual(taken.security, aapl)


# ---------------------------------------------------------------------------
# M2A-4：统一同步编排（sync_research_sources）与管理命令
# ---------------------------------------------------------------------------

import io  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402

from .source_sync import sync_research_sources  # noqa: E402


def _dossier_security(symbol, family, member, security=None):
    """创建美股证券 + 一份研究档案（同步目标以档案为界）。

    传 security 可复用同一证券（symbol+market 唯一），用于
    “同一证券多份私人档案”场景。
    """
    if security is None:
        security = Security.objects.create(
            symbol=symbol, name=f"{symbol} Inc", market="US", asset_type="stock"
        )
    ResearchDossier.objects.create(
        family=family, owner=member, security=security, initial_thesis="初始判断。"
    )
    return security


class SyncResearchSourcesOrchestrationTests(TestCase):
    """统一编排函数：mock provider，全部离线。"""

    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Alpha 家")
        cls.alice = FamilyMember.objects.create(
            family=cls.family, display_name="Alice", role=FamilyMember.ROLE_MEMBER
        )
        cls.bob = FamilyMember.objects.create(
            family=cls.family, display_name="Bob", role=FamilyMember.ROLE_MEMBER
        )

    def run_sync(self, **kwargs):
        with mock.patch(
            "investment_research.source_sync._default_sec_client",
            side_effect=lambda security: _StubSecClient(
                filings=[] if security.symbol != "MSFT" else GOOD_RECORDS
            ),
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            return_value=_StubMSFTClient(),
        ):
            return sync_research_sources(**kwargs)

    def test_only_securities_with_dossiers_synced_once(self):
        msft = _dossier_security("MSFT", self.family, self.alice)
        # 同一证券的第二份私人档案：不增加同步次数。
        _dossier_security("MSFT", self.family, self.bob, security=msft)
        aapl = _dossier_security("AAPL", self.family, self.alice)
        # 无档案证券：不应被同步。
        Security.objects.create(
            symbol="NOFILE", name="No File", market="US", asset_type="stock"
        )

        sec_calls = []
        ir_calls = []

        def sec_factory(security):
            sec_calls.append(security.symbol)
            return _StubSecClient(
                filings=[] if security.symbol != "MSFT" else GOOD_RECORDS
            )

        def ir_factory(security):
            ir_calls.append(security.symbol)
            return _StubMSFTClient()

        results, totals = sync_research_sources(
            sec_client_factory=sec_factory, microsoft_ir_client_factory=ir_factory
        )
        # 每个（证券，来源）组合恰好一次：2 证券 x 2 来源 = 4 条。
        self.assertEqual(len(results), 4)
        self.assertEqual(sorted(sec_calls), ["AAPL", "MSFT"])
        self.assertEqual(sorted(ir_calls), ["AAPL", "MSFT"])
        self.assertNotIn("NOFILE", {r["security"] for r in results})
        # MSFT SEC 2 篇 + MSFT IR 3 篇；AAPL 两侧均 0（SEC 空、IR skipped）。
        self.assertEqual(totals["created"], 5)
        self.assertEqual(totals["skipped"], 1)
        self.assertEqual(totals["failed"], 0)
        msft_sec = next(r for r in results if r["security"] == "MSFT" and r["source"] == "sec")
        self.assertEqual(msft_sec["status"], "ok")
        self.assertEqual(
            msft_sec["counters"],
            {"created": 2, "updated": 0, "unchanged": 0, "failed": 0},
        )
        aapl_ir = next(r for r in results if r["security"] == "AAPL" and r["source"] == "microsoft_ir")
        self.assertEqual(aapl_ir["status"], "skipped")
        self.assertIn("MSFT", aapl_ir["reason"])
        # skipped 不建错误状态。
        self.assertFalse(
            ResearchSourceState.objects.filter(
                security=aapl, source=SOURCE_MICROSOFT_IR
            ).exists()
        )

    def test_symbol_filter_is_case_insensitive_and_exact(self):
        _dossier_security("MsFt", self.family, self.alice)
        _dossier_security("AAPL", self.family, self.alice)
        with mock.patch(
            "investment_research.source_sync._default_sec_client",
            return_value=_StubSecClient(filings=[]),
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            return_value=_StubMSFTClient(),
        ):
            results, totals = sync_research_sources(symbols=["msft"])
        self.assertEqual([r["security"] for r in results], ["MsFt", "MsFt"])
        self.assertEqual(totals["skipped"], 0)

    def test_unknown_symbol_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
        ir_factory = mock.Mock(return_value=_StubMSFTClient())
        with self.assertRaisesRegex(ValueError, "ZZZZ"):
            sync_research_sources(
                symbols=["ZZZZ"],
                sec_client_factory=sec_factory,
                microsoft_ir_client_factory=ir_factory,
            )
        sec_factory.assert_not_called()
        ir_factory.assert_not_called()

    def test_invalid_max_documents_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        for bad in (0, 101, "20", True):
            sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
            ir_factory = mock.Mock(return_value=_StubMSFTClient())
            with self.assertRaises(ValueError):
                sync_research_sources(
                    max_documents=bad,
                    sec_client_factory=sec_factory,
                    microsoft_ir_client_factory=ir_factory,
                )
            sec_factory.assert_not_called()
            ir_factory.assert_not_called()

    def test_invalid_source_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
        ir_factory = mock.Mock(return_value=_StubMSFTClient())
        with self.assertRaises(ValueError):
            sync_research_sources(
                sources=["sec", "bogus"],
                sec_client_factory=sec_factory,
                microsoft_ir_client_factory=ir_factory,
            )
        sec_factory.assert_not_called()
        ir_factory.assert_not_called()

    def test_source_filter_limits_synced_sources(self):
        _dossier_security("MSFT", self.family, self.alice)
        with mock.patch(
            "investment_research.source_sync._default_sec_client",
            return_value=_StubSecClient(filings=GOOD_RECORDS),
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            return_value=_StubMSFTClient(),
        ):
            results, totals = sync_research_sources(sources=["sec"])
        self.assertEqual([r["source"] for r in results], ["sec"])
        self.assertEqual(totals["created"], 2)
        self.assertEqual(
            OfficialResearchDocument.objects.filter(
                source=SOURCE_MICROSOFT_IR
            ).count(),
            0,
        )

    def test_partial_failure_counts_failed_and_ok_pairs(self):
        _dossier_security("MSFT", self.family, self.alice)
        with mock.patch(
            "investment_research.source_sync._default_sec_client",
            return_value=_StubSecClient(filings=GOOD_RECORDS),
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            return_value=_StubMSFTClient(home_error=MicrosoftIRError("首页抓取失败")),
        ):
            results, totals = sync_research_sources()
        by_source = {r["source"]: r for r in results}
        self.assertEqual(by_source["sec"]["status"], "ok")
        self.assertEqual(by_source["microsoft_ir"]["status"], "failed")
        self.assertIn("首页抓取失败", by_source["microsoft_ir"]["reason"])
        self.assertEqual(totals["created"], 2)
        self.assertEqual(totals["failed"], 1)

    def test_all_failed_pairs(self):
        _dossier_security("MSFT", self.family, self.alice)
        with mock.patch(
            "investment_research.source_sync._default_sec_client",
            side_effect=RuntimeError("UA 未配置"),
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            side_effect=RuntimeError("IR 配置缺失"),
        ):
            results, totals = sync_research_sources()
        self.assertEqual({r["status"] for r in results}, {"failed"})
        self.assertEqual(totals["failed"], 2)
        self.assertEqual(totals["created"], 0)
        self.assertEqual(OfficialResearchDocument.objects.count(), 0)

    def test_repeat_sync_no_duplicate_documents(self):
        _dossier_security("MSFT", self.family, self.alice)
        first = self.run_sync()
        self.assertEqual(first[1]["created"], 5)
        second = self.run_sync()
        self.assertEqual(second[1]["created"], 0)
        self.assertEqual(second[1]["unchanged"], 5)
        self.assertEqual(second[1]["skipped"], 0)
        self.assertEqual(OfficialResearchDocument.objects.count(), 5)


class SyncResearchSourcesCommandTests(TestCase):
    """sync_research_sources 管理命令：mock provider，全部离线。"""

    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="Alpha 家")
        cls.alice = FamilyMember.objects.create(
            family=cls.family, display_name="Alice", role=FamilyMember.ROLE_MEMBER
        )

    def _patch_factories(self, sec_filings=GOOD_RECORDS, msft_home_error=None):
        return (
            mock.patch(
                "investment_research.source_sync._default_sec_client",
                side_effect=lambda security: _StubSecClient(
                    filings=sec_filings if security.symbol == "MSFT" else []
                ),
            ),
            mock.patch(
                "investment_research.source_sync._default_microsoft_ir_client",
                return_value=_StubMSFTClient(home_error=msft_home_error),
            ),
        )

    def test_full_run_output_and_exit(self):
        _dossier_security("MSFT", self.family, self.alice)
        _dossier_security("AAPL", self.family, self.alice)
        sec_patch, ir_patch = self._patch_factories()
        with sec_patch, ir_patch:
            out = io.StringIO()
            call_command("sync_research_sources", stdout=out)
        text = out.getvalue()
        self.assertIn("MSFT sec: created=2 updated=0 unchanged=0 failed=0", text)
        self.assertIn("MSFT microsoft_ir: created=3 updated=0 unchanged=0 failed=0", text)
        self.assertIn("AAPL sec: created=0 updated=0 unchanged=0 failed=0", text)
        self.assertIn("AAPL microsoft_ir: skipped", text)
        self.assertIn(
            "汇总: created=5 updated=0 unchanged=0 skipped=1 failed=0", text
        )
        # 输出卫生：不含 fixture 正文、响应体或环境变量值。
        self.assertNotIn("$70.1 billion", text)
        self.assertNotIn("RESEARCH_SEC_USER_AGENT", text)
        self.assertEqual(OfficialResearchDocument.objects.count(), 5)

    def test_repeat_run_reports_unchanged_not_created(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_patch, ir_patch = self._patch_factories()
        with sec_patch, ir_patch:
            call_command("sync_research_sources")
            out = io.StringIO()
            call_command("sync_research_sources", stdout=out)
        text = out.getvalue()
        self.assertIn("MSFT sec: created=0 updated=0 unchanged=2 failed=0", text)
        self.assertIn("MSFT microsoft_ir: created=0 updated=0 unchanged=3 failed=0", text)
        self.assertIn("汇总: created=0 updated=0 unchanged=5 skipped=0 failed=0", text)
        self.assertEqual(OfficialResearchDocument.objects.count(), 5)

    def test_fail_on_error_exit_code(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_patch, ir_patch = self._patch_factories(
            msft_home_error=MicrosoftIRError("首页抓取失败")
        )
        with sec_patch, ir_patch:
            out = io.StringIO()
            with self.assertRaises(CommandError):
                call_command(
                    "sync_research_sources", "--fail-on-error", stdout=out
                )
        self.assertIn("failed=1", out.getvalue())

    def test_failure_reported_without_fail_on_error(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_patch, ir_patch = self._patch_factories(
            msft_home_error=MicrosoftIRError("首页抓取失败")
        )
        with sec_patch, ir_patch:
            out = io.StringIO()
            call_command("sync_research_sources", stdout=out)  # 不抛异常
        text = out.getvalue()
        self.assertIn("MSFT microsoft_ir: failed", text)
        self.assertIn("汇总: created=2 updated=0 unchanged=0 skipped=0 failed=1", text)

    def test_unknown_symbol_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
        ir_factory = mock.Mock(return_value=_StubMSFTClient())
        with mock.patch(
            "investment_research.source_sync._default_sec_client", sec_factory
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            ir_factory,
        ):
            with self.assertRaisesRegex(CommandError, "ZZZZ"):
                call_command("sync_research_sources", "--symbol", "ZZZZ")
        sec_factory.assert_not_called()
        ir_factory.assert_not_called()

    def test_invalid_max_documents_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
        ir_factory = mock.Mock(return_value=_StubMSFTClient())
        with mock.patch(
            "investment_research.source_sync._default_sec_client", sec_factory
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            ir_factory,
        ):
            for bad in ("0", "200", "abc"):
                with self.assertRaises(CommandError):
                    call_command("sync_research_sources", "--max-documents", bad)
        sec_factory.assert_not_called()
        ir_factory.assert_not_called()

    def test_invalid_source_fails_before_provider_calls(self):
        _dossier_security("MSFT", self.family, self.alice)
        sec_factory = mock.Mock(return_value=_StubSecClient(filings=[]))
        ir_factory = mock.Mock(return_value=_StubMSFTClient())
        with mock.patch(
            "investment_research.source_sync._default_sec_client", sec_factory
        ), mock.patch(
            "investment_research.source_sync._default_microsoft_ir_client",
            ir_factory,
        ):
            with self.assertRaises(CommandError):
                call_command("sync_research_sources", "--source", "bogus")
        sec_factory.assert_not_called()
        ir_factory.assert_not_called()

    def test_symbol_and_source_filters(self):
        _dossier_security("MSFT", self.family, self.alice)
        _dossier_security("AAPL", self.family, self.alice)
        sec_patch, ir_patch = self._patch_factories()
        with sec_patch, ir_patch:
            out = io.StringIO()
            call_command(
                "sync_research_sources",
                "--symbol",
                "msft",
                "--source",
                "sec",
                stdout=out,
            )
        text = out.getvalue()
        self.assertIn("MSFT sec: created=2", text)
        self.assertNotIn("AAPL", text)
        self.assertNotIn("microsoft_ir:", text)
        self.assertIn("汇总: created=2 updated=0 unchanged=0 skipped=0 failed=0", text)


# ---------------------------------------------------------------------------
# M2A-5：档案内官方资料只读页面
# ---------------------------------------------------------------------------


class OfficialDocumentsViewTests(ResearchViewTestBase):
    def setUp(self):
        self.alice = self.make_member(self.family, "AliceDocs")
        self.bob = self.make_member(self.family, "BobDocs")
        self.carol = self.make_member(self.other_family, "CarolDocs")
        self.admin = self.make_member(
            self.family, "AdminDocs", role=FamilyMember.ROLE_ADMIN
        )
        self.superuser = self.make_member(
            self.family, "RootDocs", superuser=True
        )
        self.dossier = self.create_dossier_for(self.alice)
        self.other_dossier = self.create_dossier_for(
            self.bob, security=self.other_security
        )

    def make_document(
        self,
        *,
        security=None,
        external_id=None,
        title="Microsoft quarterly results",
        source="microsoft_ir",
        document_type="earnings_release",
        published_at=date(2026, 7, 29),
        content_text="Revenue grew.\nCash flow improved.",
    ):
        external_id = external_id or f"doc-{OfficialResearchDocument.objects.count() + 1}"
        return OfficialResearchDocument.objects.create(
            security=security or self.security,
            source=source,
            external_id=external_id,
            document_type=document_type,
            title=title,
            source_url=(
                "https://www.microsoft.com/en-us/investor/earnings/"
                f"{external_id}"
            ),
            published_at=published_at,
            content_text=content_text,
        )

    def test_anonymous_redirects_to_login(self):
        document = self.make_document()
        for url in (
            documents_url(self.dossier),
            document_detail_url(self.dossier, document),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)

    def test_unbound_and_inactive_members_get_403(self):
        unbound = User.objects.create_user(username="docs_unbound", password="x")
        self.client.force_login(unbound)
        self.assertEqual(self.client.get(documents_url(self.dossier)).status_code, 403)
        inactive = self.make_member(self.family, "DocsInactive", active=False)
        self.login(inactive)
        self.assertEqual(self.client.get(documents_url(self.dossier)).status_code, 403)

    def test_owner_and_viewer_can_read_their_own_documents(self):
        document = self.make_document()
        self.login(self.alice)
        self.assertEqual(self.client.get(documents_url(self.dossier)).status_code, 200)
        self.assertEqual(
            self.client.get(document_detail_url(self.dossier, document)).status_code,
            200,
        )

        viewer = self.make_member(self.family, "DocsViewer")
        viewer_dossier = self.create_dossier_for(
            viewer, security=self.other_security
        )
        viewer.role = FamilyMember.ROLE_VIEWER
        viewer.save(update_fields=["role"])
        viewer_document = self.make_document(
            security=self.other_security, external_id="viewer-doc"
        )
        self.login(viewer)
        self.assertEqual(
            self.client.get(documents_url(viewer_dossier)).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                document_detail_url(viewer_dossier, viewer_document)
            ).status_code,
            200,
        )

    def test_other_members_admin_and_superuser_cannot_read(self):
        document = self.make_document(security=self.other_security)
        for member in (self.alice, self.carol, self.admin, self.superuser):
            with self.subTest(member=member.display_name):
                self.login(member)
                self.assertEqual(
                    self.client.get(documents_url(self.other_dossier)).status_code,
                    404,
                )
                self.assertEqual(
                    self.client.get(
                        document_detail_url(self.other_dossier, document)
                    ).status_code,
                    404,
                )

    def test_documents_are_scoped_to_dossier_security_and_query_cannot_switch(self):
        own = self.make_document(title="OWN DOCUMENT")
        other = self.make_document(
            security=self.other_security,
            external_id="other-security-document",
            title="OTHER DOCUMENT",
        )
        self.login(self.alice)
        response = self.client.get(
            documents_url(self.dossier), {"security": self.other_security.pk}
        )
        self.assertContains(response, "OWN DOCUMENT")
        self.assertNotContains(response, "OTHER DOCUMENT")
        self.assertEqual(
            self.client.get(document_detail_url(self.dossier, other)).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(document_detail_url(self.dossier, own)).status_code,
            200,
        )

    def test_title_body_and_source_error_are_escaped_and_link_is_safe(self):
        document = self.make_document(
            title='<script>alert("title")</script>',
            content_text='<img src=x onerror="alert(1)">\nSecond line',
        )
        ResearchSourceState.objects.create(
            security=self.security,
            source="microsoft_ir",
            last_error="<script>error</script>",
        )
        self.login(self.alice)
        list_response = self.client.get(documents_url(self.dossier))
        self.assertContains(list_response, "&lt;script&gt;alert")
        self.assertNotContains(list_response, '<script>alert("title")</script>')
        self.assertContains(list_response, 'target="_blank" rel="noopener noreferrer"')
        self.assertContains(list_response, "&lt;script&gt;error&lt;/script&gt;")
        detail_response = self.client.get(document_detail_url(self.dossier, document))
        self.assertContains(detail_response, "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;")
        self.assertNotContains(detail_response, '<img src=x onerror="alert(1)">')

    def test_ordering_nulls_last_and_pagination(self):
        for index in range(21):
            self.make_document(
                external_id=f"page-{index}",
                title=f"Document {index:02d}",
                published_at=date(2026, 1, 1) + timedelta(days=index),
            )
        self.make_document(
            external_id="undated", title="Undated document", published_at=None
        )
        self.login(self.alice)
        first = self.client.get(documents_url(self.dossier))
        self.assertEqual(len(first.context["page"].object_list), 20)
        self.assertEqual(first.context["page"].object_list[0].title, "Document 20")
        self.assertNotContains(first, "Undated document")
        second = self.client.get(documents_url(self.dossier), {"page": 2})
        self.assertContains(second, "Undated document")

    def test_empty_state_and_source_status_are_clear(self):
        self.login(self.alice)
        response = self.client.get(documents_url(self.dossier))
        self.assertContains(response, "还没有官方资料")
        self.assertContains(response, "不需要逐项手工填写")

        success_at = timezone.now()
        ResearchSourceState.objects.create(
            security=self.security,
            source="sec",
            last_checked_at=success_at,
            last_success_at=success_at,
        )
        self.make_document(
            source="sec",
            document_type="10-k",
            external_id="sec-doc",
            title="Annual filing",
            content_text="",
        )
        response = self.client.get(documents_url(self.dossier))
        self.assertContains(response, "SEC EDGAR")
        self.assertContains(response, "10-K 年报")
        self.assertContains(response, "仅归档元数据")
        detail = OfficialResearchDocument.objects.get(external_id="sec-doc")
        response = self.client.get(document_detail_url(self.dossier, detail))
        self.assertContains(response, "当前仅归档元数据")

    def test_dossier_detail_shows_document_count_and_latest_success(self):
        self.make_document(external_id="summary-1")
        self.make_document(external_id="summary-2")
        success_at = timezone.now()
        ResearchSourceState.objects.create(
            security=self.security,
            source="microsoft_ir",
            last_checked_at=success_at,
            last_success_at=success_at,
            last_error="temporary failure",
        )
        self.login(self.alice)
        response = self.client.get(detail_url(self.dossier))
        self.assertContains(response, "已归档 2 份")
        self.assertContains(
            response, timezone.localtime(success_at).strftime("%Y-%m-%d %H:%M")
        )
        self.assertContains(response, "1 个来源需要关注")
        self.assertContains(response, documents_url(self.dossier))

    @mock.patch("investment_research.views.sync_research_sources")
    def test_owner_can_trigger_sync_and_sees_summary(self, sync_mock):
        sync_mock.return_value = (
            [],
            {"created": 2, "updated": 1, "unchanged": 3, "skipped": 0, "failed": 0},
        )
        self.login(self.alice)
        response = self.client.post(sync_documents_url(self.dossier))
        self.assertRedirects(
            response,
            documents_url(self.dossier),
            fetch_redirect_response=False,
        )
        sync_mock.assert_called_once_with(symbols=[self.security.symbol])
        self.assertContains(
            self.client.get(documents_url(self.dossier)),
            "官方资料已更新：新增 2 份，更新 1 份，无变化 3 份。",
        )

    @mock.patch("investment_research.views.sync_research_sources")
    def test_failed_source_is_reported_without_losing_page(self, sync_mock):
        sync_mock.return_value = (
            [],
            {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 1},
        )
        self.login(self.alice)
        response = self.client.post(sync_documents_url(self.dossier))
        self.assertRedirects(
            response,
            documents_url(self.dossier),
            fetch_redirect_response=False,
        )
        self.assertContains(
            self.client.get(documents_url(self.dossier)),
            "官方资料同步完成，但有 1 项失败",
        )

    def test_viewer_cannot_trigger_sync(self):
        viewer = self.make_member(self.family, "SyncViewer")
        viewer_dossier = self.create_dossier_for(viewer)
        viewer.role = FamilyMember.ROLE_VIEWER
        viewer.save(update_fields=["role"])
        self.login(viewer)
        with mock.patch("investment_research.views.sync_research_sources") as sync_mock:
            response = self.client.post(sync_documents_url(viewer_dossier))
        self.assertEqual(response.status_code, 403)
        sync_mock.assert_not_called()
        self.assertNotContains(
            self.client.get(detail_url(viewer_dossier)),
            "立即同步",
        )

    def test_sync_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice.user)
        response = csrf_client.post(sync_documents_url(self.dossier))
        self.assertEqual(response.status_code, 403)

    def test_get_and_other_dossier_cannot_trigger_sync(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(sync_documents_url(self.dossier)).status_code, 405)
        self.assertEqual(
            self.client.post(sync_documents_url(self.other_dossier)).status_code,
            404,
        )

    def test_get_is_read_only_and_does_not_call_providers(self):
        document = self.make_document()
        state = ResearchSourceState.objects.create(
            security=self.security, source="microsoft_ir"
        )
        before = {
            "documents": OfficialResearchDocument.objects.count(),
            "states": ResearchSourceState.objects.count(),
            "dossiers": ResearchDossier.objects.count(),
            "revisions": ResearchThesisRevision.objects.count(),
            "document_updated": document.updated_at,
            "state_updated": state.updated_at,
            "dossier_updated": self.dossier.updated_at,
        }
        self.login(self.alice)
        with mock.patch(
            "investment_research.providers.sec.SecClient.get_filings",
            side_effect=AssertionError("SEC provider called"),
        ), mock.patch(
            "investment_research.providers.microsoft_ir.MicrosoftIRClient.fetch_home",
            side_effect=AssertionError("Microsoft provider called"),
        ):
            self.assertEqual(self.client.get(detail_url(self.dossier)).status_code, 200)
            self.assertEqual(
                self.client.get(documents_url(self.dossier)).status_code, 200
            )
            self.assertEqual(
                self.client.get(document_detail_url(self.dossier, document)).status_code,
                200,
            )
        document.refresh_from_db()
        state.refresh_from_db()
        self.dossier.refresh_from_db()
        self.assertEqual(OfficialResearchDocument.objects.count(), before["documents"])
        self.assertEqual(ResearchSourceState.objects.count(), before["states"])
        self.assertEqual(ResearchDossier.objects.count(), before["dossiers"])
        self.assertEqual(ResearchThesisRevision.objects.count(), before["revisions"])
        self.assertEqual(document.updated_at, before["document_updated"])
        self.assertEqual(state.updated_at, before["state_updated"])
        self.assertEqual(self.dossier.updated_at, before["dossier_updated"])

    def test_non_get_methods_are_405(self):
        document = self.make_document()
        self.login(self.alice)
        self.assertEqual(self.client.post(documents_url(self.dossier)).status_code, 405)
        self.assertEqual(
            self.client.post(document_detail_url(self.dossier, document)).status_code,
            405,
        )
