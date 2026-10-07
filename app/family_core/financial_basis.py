"""Explain saved financial results without recalculating or modifying facts."""
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone
from ledger.models import AssetBalanceSnapshot
from portfolio.account_history import family_snapshots
from portfolio.models import PortfolioSnapshot
from portfolio.views import _snapshot_audit_summary


def financial_basis(member):
    snapshot = family_snapshots(member.family).filter(snapshot_date__lte=timezone.localdate()).first()
    ledger = AssetBalanceSnapshot.objects.filter(family=member.family, is_draft=False).order_by('-snapshot_date', '-created_at').first()
    data = {'snapshot': snapshot, 'ledger_snapshot': ledger, 'accounts': [], 'issues': [], 'audit': None}
    if not snapshot:
        return data
    data['audit'] = _snapshot_audit_summary(snapshot)
    accounts = list(PortfolioSnapshot.objects.filter(family=member.family, account__isnull=False,
                    snapshot_date=snapshot.snapshot_date, currency=snapshot.currency)
                    .select_related('account__bank_account__member').order_by('account_id'))
    data['accounts'] = accounts
    data['account_total'] = sum((a.total_asset for a in accounts), Decimal('0')) if accounts else None
    data['components_total'] = snapshot.total_cash + snapshot.total_market_value
    # Stored values use four decimal places; sum-of-rounded-account discrepancies
    # are disclosed separately from mismatches exceeding that rounding envelope.
    if accounts:
        difference = snapshot.total_asset - data['account_total']
        if difference:
            tolerance = Decimal('0.0001') * len(accounts)
            data['issues'].append({'label': '家庭总额与账户合计差额', 'amount': difference,
                                   'note': '在逐账户四位小数舍入范围内。' if abs(difference) <= tolerance else '超出逐账户舍入范围，请打开快照核对范围与明细。'})
    else:
        data['issues'].append({'label': '缺少同日账户明细', 'amount': None, 'note': '无法核对家庭总额与账户合计。'})
    if data['components_total'] != snapshot.total_asset:
        data['issues'].append({'label': '现金加市值与保存总额的差额',
                               'amount': snapshot.total_asset - data['components_total'],
                               'note': '按保存值核对；本页不会改写快照。'})
    for key, label in [('missing_rates', '缺少汇率'), ('missing_prices', '缺少价格'),
                       ('stale_prices', '价格日期需核对'), ('valuation_errors', '流水或估值问题')]:
        if data['audit'][key]:
            data['issues'].append({'label': label, 'amount': None, 'note': f'保存快照记录 {len(data["audit"][key])} 项，请查看快照审计明细。'})
    data['ledger_total'] = ledger.entries.aggregate(total=Sum('base_amount'))['total'] if ledger else None
    return data
