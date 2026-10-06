"""Read-only proposals; applying one changes category FKs only, with an audit trail."""
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from .asset_classification import categories_for_family, government_term_category, validate_assignment
from .classification_confirmations import resolve_ledger_confirmations

SYMBOL_RULES = {
    **dict.fromkeys(('VOO', 'SPY', 'IVV', 'QQQ', 'QQQM', 'TQQQ', 'TNA', 'IWM', '03086', '03195', '510300', '159919'), 'equity_index'),
    **dict.fromkeys(('GGLL', 'DRAM', 'QTUM', 'XLU', 'XLV', 'NVDY', 'ALLW', 'SMH'), 'equity_fund'),
    **dict.fromkeys(('IBIT', 'ETHA'), 'crypto'),
    # 03433 holds 20+ year US Treasuries. This is a product-specific rule,
    # not a default for all government bond ETFs.
    '03433': 'government_long', '912810TV0': 'government_long',
    # Exact legacy symbol for the same reviewed Treasury, not a suffix guess.
    'GOVT 4.75 NOV15’53 912810TV0': 'government_long',
}

def propose_security(security):
    if security.asset_category_id and security.asset_category.parent_id:
        return security.asset_category.code, '保留已确认二级类别'
    if security.asset_type == 'stock':
        return 'equity_stock', '直接持有股票'
    if security.asset_type == 'option':
        return 'option', '直接持有期权合约'
    symbol = security.symbol.upper()
    if symbol in SYMBOL_RULES:
        return SYMBOL_RULES[symbol], '本次确认的标的映射规则'
    if security.asset_type == 'bond':
        detail = getattr(security, 'bond_detail', None)
        if detail and detail.bond_type == 'government':
            code = government_term_category(detail.original_issue_date, detail.maturity_date)
            return code, '按原始发行期限划分' if code else '缺少国债原始发行日或到期日'
        return None, '新字典没有直接公司债类别，需人工核对'
    return None, '仅凭旧一级类别或金融品种无法确定二级类别'

def propose_entry(entry):
    category = entry.asset_category
    if category and category.parent_id:
        return category.code, '保留已确认二级类别'
    name = entry.account.account_name if entry.account else entry.account_name
    account_type = getattr(entry.account, 'account_type_ref', None) if entry.account else None
    old = category.code if category else ''
    if ((category and category.name in ('信用卡', '信用卡（旧分类）'))
            or '信用卡' in name or (account_type and account_type.code == 'credit_card')):
        if entry.original_amount > 0:
            return None, '信用卡余额为正，需核对欠款与溢缴款，不能改动金额符号'
        return 'credit_card', '信用卡欠款'
    if '养老金' in name or (account_type and account_type.code == 'pension'):
        return 'savings_insurance', '用户确认养老金归储蓄型保险'
    if old == 'fund' and ('支付宝' in name or (account_type and account_type.code == 'alipay')):
        return 'equity_fund', '用户确认支付宝旧基金余额全部为股票基金'
    if old == 'cash' or name == '现金':
        return 'cash_balance', '原现金余额'
    if old == 'fixed_income' and ('医保' in name or '银行' in name or (account_type and account_type.code == 'bank')):
        return 'cash_balance', '用户确认银行及医保旧固定收益余额归现金'
    if old == 'commodities':
        return 'gold', '旧黄金类别'
    source = entry.extra_data if isinstance(entry.extra_data, dict) else {}
    source_label = source.get('source_asset_category') or source.get('asset_category_label') or ''
    if old == 'alternatives' and (category.name == '套利' or source_label == '套利' or '套利' in entry.remark or '套利' in name):
        return 'cash_balance', '用户确认套利归现金'
    # A whole-account historical balance can contain several different products.
    return None, '旧类别已合并，缺少具体产品或原始细分类别；需人工确认'

def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))

def _hash(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()

def _scoped_records(family, start, end):
    from portfolio.models import Security, InvestmentTransaction, PortfolioSnapshotPositionLine
    from ledger.models import AssetBalanceEntry
    securities = Security.objects.filter(
        Q(positions__account__bank_account__family=family)
        | Q(transactions__account__bank_account__family=family)
        | Q(watchlist_items__family=family)
        | Q(snapshot_position_lines__snapshot__family=family)
    ).distinct().select_related('asset_category__parent', 'bond_detail').order_by('pk')
    trades = InvestmentTransaction.objects.filter(account__bank_account__family=family, trade_date__range=(start, end)).select_related('asset_category__parent', 'security__asset_category__parent', 'security__bond_detail', 'account').order_by('pk')
    entries = AssetBalanceEntry.objects.filter(snapshot__family=family, snapshot__snapshot_date__range=(start, end)).select_related('asset_category__parent', 'account__account_type_ref', 'snapshot', 'member').order_by('pk')
    lines = PortfolioSnapshotPositionLine.objects.filter(snapshot__family=family, snapshot__snapshot_date__range=(start, end)).select_related('asset_category__parent', 'security__asset_category__parent', 'security__bond_detail', 'snapshot', 'account').order_by('pk')
    return [list(securities), list(trades), list(entries), list(lines)]

def _record_facts(record):
    # All stored fields except classification and timestamps must remain identical.
    return {
        field.attname: getattr(record, field.attname)
        for field in record._meta.concrete_fields
        if field.name not in ('asset_category', 'created_at', 'updated_at')
    }

def build_classification_preview(family, start, end, *, confirmations=None):
    if start > end:
        raise ValidationError('开始日期不能晚于结束日期。')
    categories = {item.code: item for item in categories_for_family(family).filter(parent__isnull=False, is_active=True).order_by(F('family_id').asc(nulls_first=True))}
    groups = _scoped_records(family, start, end)
    confirmed_entries = resolve_ledger_confirmations(family, groups[2], confirmations)
    rows, facts = [], []
    for records in groups:
        for record in records:
            label = record._meta.label_lower
            old = record.asset_category
            if label == 'portfolio.security':
                code, reason = propose_security(record)
                display, day = str(record), None
            elif label == 'ledger.assetbalanceentry':
                if old and old.parent_id:
                    code, reason = propose_entry(record)
                else:
                    code, reason = confirmed_entries.get(record.pk, propose_entry(record))
                display, day = f'{record.member} · {record.account or record.account_name}', record.snapshot.snapshot_date
            elif label == 'portfolio.investmenttransaction':
                if old and old.parent_id:
                    code, reason = old.code, '保留已确认的流水分类'
                elif record.security_id:
                    code, reason = propose_security(record.security)
                else:
                    code, reason = ('cash_balance', '无证券的现金类流水') if record.trade_type in ('deposit', 'withdraw', 'transfer_in', 'transfer_out', 'fee', 'interest') else (None, '无标的流水需核对')
                display, day = f'{record.account} · {record.security or record.trade_type}', record.trade_date
            else:
                if old and old.parent_id:
                    code, reason = old.code, '保留已保存的快照分类'
                elif record.asset_type == 'cash':
                    code, reason = 'cash_balance', '快照现金明细'
                elif record.security_id:
                    code, reason = propose_security(record.security)
                else:
                    code, reason = None, '快照缺少标的，不能推定分类'
                display, day = f'{record.account} · {record.asset_name}', record.snapshot.snapshot_date
            target = categories.get(code)
            if code and not target:
                reason = f'尚未安装二级字典：{code}；需先部署结构迁移'
            if label == 'portfolio.security' and target:
                shared = (
                    record.transactions.exclude(account__bank_account__family=family).exists()
                    or record.positions.exclude(account__bank_account__family=family).exists()
                    or record.watchlist_items.exclude(family=family).exists()
                    or record.snapshot_position_lines.exclude(snapshot__family=family).exists()
                )
                if shared and target.family_id:
                    target, reason = None, '跨家庭共享标的，不能以本家庭类别覆盖'
            if target:
                try:
                    instrument = getattr(record, 'asset_type', None)
                    if label == 'portfolio.investmenttransaction' and record.security_id:
                        instrument = record.security.asset_type
                    if instrument == 'cash':
                        instrument = None
                    validate_assignment(target, family=family, instrument=instrument)
                except ValidationError as exc:
                    target, reason = None, '; '.join(exc.messages)
            rows.append({
                'model': label, 'id': record.pk, 'label': display, 'date': str(day) if day else '',
                'source': {'portfolio.security': '证券资料', 'portfolio.investmenttransaction': '投资流水', 'ledger.assetbalanceentry': '账本余额', 'portfolio.portfoliosnapshotpositionline': '投资快照'}[label],
                'old_id': old.pk if old else None, 'old': str(old) if old else '未分类',
                'new_id': target.pk if target else None, 'new': str(target) if target else '待确认',
                'proposed_code': code, 'reason': reason,
                'status': 'unchanged' if target and target.pk == record.asset_category_id else 'ready' if target else 'unresolved',
            })
            facts.append((label, record.pk, _record_facts(record)))
    report = {
        'family_id': family.pk, 'start_date': str(start), 'end_date': str(end),
        'note': '日期范围适用于历史交易、账本和快照；当前证券主数据不分日期。只调整分类，无法确定的项目保留原值。',
        'counts': {status: sum(row['status'] == status for row in rows) for status in ('ready', 'unchanged', 'unresolved')},
        'financial_digest': _hash(facts), 'rows': rows,
    }
    if confirmations is not None:
        report['confirmation_digest'] = _hash(confirmations)
    report['digest'] = _hash(report)
    return report

@transaction.atomic
def apply_classification_preview(family, start, end, expected_digest, *, confirmations=None):
    from django.apps import apps
    from .models import AssetClassificationAudit, AssetCategory
    # Serialize with other classification updates for this family.
    type(family).objects.select_for_update().get(pk=family.pk)
    groups = _scoped_records(family, start, end)
    for records in groups:
        if records:
            list(type(records[0]).objects.select_for_update().filter(pk__in=[r.pk for r in records]).order_by('pk'))
    report = build_classification_preview(family, start, end, confirmations=confirmations)
    if report['digest'] != expected_digest:
        raise ValidationError('预览后数据或映射规则已变化，请重新预览并确认摘要。')
    batch = uuid.uuid4()
    for row in report['rows']:
        if row['status'] != 'ready':
            continue
        model = apps.get_model(row['model'])
        updated = model.objects.filter(pk=row['id'], asset_category_id=row['old_id']).update(asset_category_id=row['new_id'])
        if updated != 1:
            raise ValidationError('分类记录已变化，整批已回滚。')
        AssetClassificationAudit.objects.create(
            family=family, batch_id=batch, model_label=row['model'], object_id=row['id'],
            old_category={'id': row['old_id'], 'name': row['old']},
            new_category={'id': row['new_id'], 'name': row['new']},
            plan_digest=expected_digest, start_date=start, end_date=end,
        )
    after_facts = [(r._meta.label_lower, r.pk, _record_facts(r)) for records in _scoped_records(family, start, end) for r in records]
    if _hash(after_facts) != report['financial_digest']:
        raise ValidationError('非分类字段发生变化，整批已回滚。')
    return {'batch_id': str(batch), 'updated': report['counts']['ready'], 'unresolved': report['counts']['unresolved']}
