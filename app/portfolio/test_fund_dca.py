from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from family_core.models import Family, FamilyMember
from .fund_models import (FundDcaPlan, FundDcaChange, FundReference, FundNavRecord,
                          FundDisclosureSnapshot, FundRefreshRun)
from .fund_data import parse_nav, parse_holdings, parse_allocations, refresh_fund, FundDataError
from .fund_jobs import enqueue, run_job

NAV = 'var fS_code="016452";var fS_name="测试基金A";var Data_netWorthTrend=[{"x":1767196800000,"y":1.2345},{"x":1767283200000,"y":1.2501}];var Data_assetAllocation={"categories":["2025-12-31"],"series":[]};'
HOLDINGS = 'var apidata={content:"<div class=\'box\'><h4>2025年4季度 截止至2025-12-31</h4><table><thead><tr><th>股票代码</th><th>股票名称</th><th>占净值 比例</th></tr></thead><tbody><tr><td>NVDA</td><td>英伟达</td><td>7.25%</td></tr></tbody></table></div>",arryear:[2025]};'


class FundDcaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.family = Family.objects.create(name="测试家庭")
        cls.other_family = Family.objects.create(name="另一家庭")
        cls.users, cls.members = {}, {}
        for role in ["admin", "member", "viewer", "peer", "foreign"]:
            user = get_user_model().objects.create_user(username="fund-"+role, password="test-only")
            member = FamilyMember.objects.create(family=cls.other_family if role=="foreign" else cls.family,
                user=user, display_name=role, role=role if role in ["admin", "member", "viewer"] else "member")
            cls.users[role], cls.members[role] = user, member
        cls.fund = FundReference.objects.create(code="016452", name="南方基金")
        cls.plan = FundDcaPlan.objects.create(family=cls.family, member=cls.members["member"], fund=cls.fund,
            name="长期计划", start_date=date(2026, 1, 1), frequency="month", amount=Decimal("500.00"),
            fee_rate=Decimal("0.15"), balance=Decimal("0"), balance_date=date(2026, 1, 1),
            created_by=cls.users["member"], updated_by=cls.users["member"])
        cls.foreign_fund = FundReference.objects.create(code="270023", name="其他家庭基金")
        cls.foreign_plan = FundDcaPlan.objects.create(family=cls.other_family, member=cls.members["foreign"],
            fund=cls.foreign_fund, name="其他家庭计划", start_date=date(2026,1,1), amount=Decimal("900"),
            fee_rate=Decimal("0"), created_by=cls.users["foreign"], updated_by=cls.users["foreign"])

    def login(self, role="member"):
        self.client.force_login(self.users[role])

    def payload(self, **overrides):
        data = {"name":"长期计划","member":self.members["member"].pk,"fund_code":"016452",
            "start_date":"2026-01-01","end_date":"","frequency":"month","amount":"500.00",
            "fee_rate":"0.15","balance":"0.00","balance_date":"2026-01-01","remark":"","revision":1}
        data.update(overrides)
        return data

    def test_all_frequency_and_fee_decimal_math(self):
        for frequency, count in [("day",252),("week",52),("month",12),("quarter",4),("year",1)]:
            self.plan.frequency = frequency
            self.plan.amount = Decimal("100.01")
            self.assertEqual(self.plan.annual_amount, Decimal("100.01")*count)
            self.assertEqual(self.plan.monthly_amount, self.plan.annual_amount/Decimal("12"))
        self.plan.amount, self.plan.fee_rate = Decimal("15.00"), Decimal("0.1")
        self.assertEqual(self.plan.estimated_fee, Decimal("0.02"))
        self.plan.fee_rate = Decimal("0")
        self.assertEqual(self.plan.estimated_fee, Decimal("0.00"))

    @patch("portfolio.fund_jobs.launch_job")
    def test_create_preserves_leading_zero_and_audit(self, launch):
        self.login()
        response = self.client.post(reverse("portfolio:fund_dca_create"), self.payload(
            name="新计划",fund_code="050025",balance="",balance_date="",revision=""))
        self.assertEqual(response.status_code,302)
        plan = FundDcaPlan.objects.get(name="新计划")
        self.assertEqual(plan.fund.code,"050025")
        self.assertIsNone(plan.balance)
        self.assertEqual(Decimal(plan.changes.get().after["fee_rate"]), Decimal("0.15"))
        self.assertEqual(FundRefreshRun.objects.count(),1)

    def test_viewer_cannot_write_and_member_cannot_edit_peer(self):
        self.login("viewer")
        self.assertEqual(self.client.post(reverse("portfolio:fund_dca_create"),self.payload()).status_code,403)
        self.assertEqual(self.client.get(reverse("portfolio:fund_dca_edit",args=[self.plan.pk])).status_code,403)
        self.login("peer")
        self.assertEqual(self.client.post(reverse("portfolio:fund_dca_edit",args=[self.plan.pk]),self.payload()).status_code,403)
        self.assertEqual(self.client.post(reverse("portfolio:fund_dca_refresh")).status_code,302)

    @patch("portfolio.fund_jobs.launch_job")
    def test_admin_can_maintain_other_member_and_member_scope_enforced(self, launch):
        self.login("admin")
        response=self.client.post(reverse("portfolio:fund_dca_edit",args=[self.plan.pk]),self.payload(amount="700"))
        self.assertEqual(response.status_code,302)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.amount,Decimal("700"))
        self.login()
        response=self.client.post(reverse("portfolio:fund_dca_create"),self.payload(member=self.members["peer"].pk))
        self.assertEqual(response.status_code,200)
        self.assertEqual(FundDcaPlan.objects.filter(family=self.family).count(),1)

    def test_cross_family_list_detail_and_forged_member_blocked(self):
        self.login()
        response=self.client.get(reverse("portfolio:fund_dca_list"))
        self.assertContains(response,"长期计划")
        self.assertNotContains(response,"其他家庭计划")
        self.assertEqual(self.client.get(reverse("portfolio:fund_dca_edit",args=[self.foreign_plan.pk])).status_code,404)
        self.assertEqual(self.client.get(reverse("portfolio:fund_detail",args=["270023"])).status_code,404)
        self.assertEqual(self.client.get(reverse("portfolio:fund_dca_list"),{"member":self.members["foreign"].pk}).status_code,404)

    def test_balance_edit_audit_and_stale_revision(self):
        self.login()
        url=reverse("portfolio:fund_dca_balance",args=[self.plan.pk])
        self.assertEqual(self.client.post(url,{"balance":"1234.56","balance_date":"2026-01-02","revision":1}).status_code,302)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.balance,Decimal("1234.56"))
        self.assertEqual(self.plan.amount,Decimal("500"))
        self.assertEqual(self.plan.fee_rate,Decimal("0.15"))
        self.assertEqual(self.plan.changes.get().action,"balance")
        response=self.client.post(url,{"balance":"999","balance_date":"2026-01-02","revision":1})
        self.assertContains(response,"这条定投已被其他页面修改")
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.balance,Decimal("1234.56"))

    def test_balance_date_pair_zero_and_future_date(self):
        self.login()
        url=reverse("portfolio:fund_dca_balance",args=[self.plan.pk])
        for data in [{"balance":"1","balance_date":"","revision":1},
                     {"balance":"0","balance_date":(timezone.localdate()+timedelta(days=1)).isoformat(),"revision":1}]:
            self.assertEqual(self.client.post(url,data).status_code,200)
        self.assertEqual(self.client.post(url,{"balance":"","balance_date":"","revision":1}).status_code,302)
        self.plan.refresh_from_db()
        self.assertIsNone(self.plan.balance)

    def test_get_pages_do_not_fetch_or_write(self):
        self.login()
        before=FundRefreshRun.objects.count()
        with patch("portfolio.fund_data.fetch_text",side_effect=AssertionError("GET fetched")):
            self.assertEqual(self.client.get(reverse("portfolio:fund_dca_list")).status_code,200)
            self.assertEqual(self.client.get(reverse("portfolio:fund_detail",args=["016452"])).status_code,200)
            self.assertEqual(self.client.get(reverse("portfolio:fund_dca_edit",args=[self.plan.pk])).status_code,200)
        self.assertEqual(FundRefreshRun.objects.count(),before)
        self.assertEqual(FundDcaChange.objects.count(),0)
        self.assertEqual(self.client.get(reverse("portfolio:fund_dca_refresh")).status_code,405)

    def test_target_etf_access_and_return_filter(self):
        target=FundReference.objects.create(code="513500",name="目标ETF")
        self.fund.target_fund=target
        self.fund.save()
        self.login("viewer")
        response=self.client.get(reverse("portfolio:fund_detail",args=["016452"]),{"member":self.members["member"].pk})
        self.assertContains(response,f'member={self.members["member"].pk}')
        self.assertContains(response,"未折算为联接基金或个人余额")
        self.assertEqual(self.client.get(reverse("portfolio:fund_detail",args=["513500"])).status_code,200)

    def test_validated_model_constraints(self):
        self.plan.member=self.members["foreign"]
        with self.assertRaises(ValidationError):self.plan.clean()
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                FundDcaPlan.objects.filter(pk=self.plan.pk).update(balance=None)

    def test_parsers_keep_disclosure_ratio_and_original_dates(self):
        name,rows=parse_nav(NAV)
        self.assertEqual(name,"测试基金A")
        self.assertEqual(rows[-1],(date(2026,1,2),Decimal("1.2501")))
        holdings=parse_holdings(HOLDINGS)
        self.assertEqual(holdings[0][0],date(2025,12,31))
        self.assertEqual(holdings[0][1][0]["code"],"NVDA")
        allocation=parse_allocations({"data":{"chart_list":[{"type_desc":"股票","percent":"101.18"}]}})
        self.assertEqual(allocation[0]["percentage"],"101.18")
        with self.assertRaises((FundDataError,ValueError)):
            parse_nav(NAV.replace('[{"x":','evil([{"x":'))

    def provider(self,url):
        import json
        if "pingzhongdata" in url:return NAV
        if "asset/percent" in url:return '{"data":{"chart_list":[{"type_desc":"股票","percent":"101.18"}]}}'
        if "FundArchivesDatas" in url:return HOLDINGS
        return json.dumps({"data":{"fd_code":"016452","fd_full_name":"测试基金全称","keeper_name":"测试公司"}})

    @patch("portfolio.fund_data.fetch_text")
    def test_refresh_idempotent_and_partial_failure_preserves_old_values(self,fetch):
        fetch.side_effect=self.provider
        for _ in range(2):
            self.assertEqual(refresh_fund(self.fund.pk)["status"],"saved")
        self.assertEqual(FundNavRecord.objects.count(),2)
        self.assertEqual(FundDisclosureSnapshot.objects.count(),1)
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.latest_nav,Decimal("1.2501"))
        old_date=self.fund.nav_date
        fetch.side_effect=OSError("provider unavailable")
        self.assertEqual(refresh_fund(self.fund.pk)["status"],"failed")
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.nav_date,old_date)
        self.assertEqual(self.fund.latest_nav,Decimal("1.2501"))
        self.assertEqual(FundNavRecord.objects.count(),2)

    @patch("portfolio.fund_jobs.launch_job")
    def test_duplicate_jobs_and_stale_recovery(self,launch):
        first=enqueue(self.family,self.users["member"],[self.fund.pk])
        self.assertEqual(enqueue(self.family,self.users["member"],[self.fund.pk]).pk,first.pk)
        FundRefreshRun.objects.filter(pk=first.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        next_job=enqueue(self.family,self.users["member"],[self.fund.pk])
        self.assertNotEqual(next_job.pk,first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status,"interrupted")

    @patch("portfolio.fund_jobs.refresh_fund",return_value={"status":"saved","code":"016452"})
    def test_job_rechecks_member_scope_and_expired_permission(self,refresh):
        job=enqueue(self.family,self.users["member"],[self.fund.pk,self.foreign_fund.pk],launch=False)
        run_job(job.pk)
        refresh.assert_called_once_with(self.fund.pk)
        run_job(job.pk)
        self.assertEqual(refresh.call_count,1)
        job2=enqueue(self.family,self.users["member"],[self.fund.pk],launch=False)
        FamilyMember.objects.filter(pk=self.members["member"].pk).update(role="viewer")
        run_job(job2.pk)
        job2.refresh_from_db()
        self.assertEqual(job2.status,"failed")
        self.assertEqual(refresh.call_count,1)

    @patch("portfolio.fund_data.fetch_text")
    def test_wrong_code_and_older_nav_do_not_replace_latest(self,fetch):
        self.fund.latest_nav=Decimal("9.8765")
        self.fund.nav_date=date(2026,10,12)
        self.fund.save()
        fetch.side_effect=self.provider
        refresh_fund(self.fund.pk)
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.latest_nav,Decimal("9.8765"))
        fetch.side_effect=lambda url: NAV.replace('fS_code="016452"', 'fS_code="270023"') if "pingzhongdata" in url else self.provider(url)
        result=refresh_fund(self.fund.pk)
        self.assertIn("净值",result["errors"])
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.nav_date,date(2026,10,12))

    def test_queued_job_merges_new_fund_without_dropping_request(self):
        first=enqueue(self.family,self.users["member"],[self.fund.pk],launch=False)
        second=enqueue(self.family,self.users["member"],[self.foreign_fund.pk],launch=False)
        self.assertEqual(first.pk,second.pk)
        second.refresh_from_db()
        self.assertEqual(second.fund_ids,sorted([self.fund.pk,self.foreign_fund.pk]))

    @patch("portfolio.fund_jobs.refresh_fund",return_value={"status":"saved","code":"016452"})
    def test_deleted_requester_is_not_treated_as_system_schedule(self,refresh):
        job=enqueue(self.family,self.users["member"],[self.fund.pk],launch=False)
        FundRefreshRun.objects.filter(pk=job.pk).update(requested_by=None)
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status,"failed")
        refresh.assert_not_called()
        scheduled=enqueue(self.family,None,[self.fund.pk],launch=False)
        run_job(scheduled.pk)
        scheduled.refresh_from_db()
        self.assertTrue(scheduled.is_scheduled)
        self.assertEqual(scheduled.status,"saved")

    def test_refresh_targets_only_own_funds_and_safe_return(self):
        self.login()
        with patch("portfolio.fund_jobs.launch_job"):
            response=self.client.post(reverse("portfolio:fund_dca_refresh"),{"return_to":"https://example.org/"})
        self.assertEqual(response.url,reverse("portfolio:fund_dca_list")+"?member=all")
        self.assertEqual(FundRefreshRun.objects.get().fund_ids,[self.fund.pk])

    def test_end_date_before_start_and_rate_as_percentage(self):
        self.login()
        response=self.client.post(reverse("portfolio:fund_dca_create"),self.payload(end_date="2025-12-31"))
        self.assertContains(response,"终止日期不能早于起始日期")
        response=self.client.post(reverse("portfolio:fund_dca_create"),self.payload(fee_rate="100.000001"))
        self.assertEqual(response.status_code,200)
        self.assertEqual(FundDcaPlan.objects.filter(family=self.family).count(),1)
