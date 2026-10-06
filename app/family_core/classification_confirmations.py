"""Reviewed ledger rules anchored to immutable record identities, not display names."""
from datetime import date
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from .asset_classification import SECONDARY_CATEGORIES


def resolve_ledger_confirmations(family, entries, confirmations):
    if confirmations is None:
        return {}
    if not isinstance(confirmations, dict) or confirmations.get('version') != 1:
        raise ValidationError('确认文件格式或版本不正确。')
    rules = confirmations.get('ledger_rules')
    if not isinstance(rules, list):
        raise ValidationError('确认文件缺少账本映射规则。')
    from ledger.models import AssetBalanceEntry
    codes = {code for code, _, _ in SECONDARY_CATEGORIES}
    anchors, selectors = {}, {}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('anchor'), dict):
            raise ValidationError('账本映射缺少来源明细。')
        anchor = rule['anchor']
        required = {'id', 'date', 'member_name', 'account_name', 'currency', 'old_category_code', 'original_amount'}
        if (not required.issubset(anchor) or type(anchor['id']) is not int or anchor['id'] <= 0
                or any(not isinstance(anchor[key], str) for key in required - {'id'})):
            raise ValidationError('来源明细必须包含编号、日期、成员、账户、币种、原分类和原币金额。')
        code, scope = rule.get('target_code'), rule.get('scope')
        if not isinstance(code, str) or code not in codes or scope not in ('record', 'matching_history'):
            raise ValidationError('确认的二级类别或适用范围不正确。')
        history_code = rule.get('history_target_code')
        if history_code is not None and (not isinstance(history_code, str) or history_code not in codes or scope != 'matching_history'):
            raise ValidationError('其他日期的确认类别或适用范围不正确。')
        if anchor['id'] in anchors:
            raise ValidationError('来源明细编号重复，不能覆盖前一条规则。')
        record = AssetBalanceEntry.objects.filter(pk=anchor['id'], snapshot__family=family).select_related('snapshot', 'member', 'account', 'asset_category').first()
        if record is None:
            raise ValidationError(f"确认来源 LB-{anchor['id']} 不存在或不属于当前家庭。")
        try:
            amount = Decimal(anchor['original_amount']) if isinstance(anchor['original_amount'], str) else None
            day = date.fromisoformat(anchor['date'])
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise ValidationError('来源日期或金额格式不正确，金额必须使用十进制字符串。') from exc
        actual_name = record.account.account_name if record.account_id else record.account_name
        old = record.asset_category
        expected_category = anchor['old_category_code']
        # The same reviewed plan can be reused after it has been applied once.
        category_matches = old and ((not old.parent_id and old.code == expected_category)
                                    or (old.parent_id and old.code == code))
        if (amount is None or not amount.is_finite() or record.original_amount != amount
                or record.snapshot.snapshot_date != day or str(record.member) != anchor['member_name']
                or actual_name != anchor['account_name'] or record.currency != anchor['currency']
                or not category_matches or record.member.family_id != family.pk
                or (record.account_id and record.account.family_id != family.pk)):
            raise ValidationError(f"确认来源 LB-{record.pk} 与当前数据不一致，请重新核对；本次未写入。")
        anchors[record.pk] = (code, '按用户确认的来源明细编号映射')
        # Historical matching uses member/account IDs resolved from this anchor.
        # A missing account has no stable identity for extending a name-based rule.
        key = (record.member_id, record.account_id, record.currency, expected_category)
        info = selectors.setdefault(key, {'codes': set(), 'extend': set(), 'history_codes': set(), 'anchor_dates': set(), 'missing_account': False})
        info['codes'].add(code)
        info['anchor_dates'].add(day)
        if history_code:
            info['history_codes'].add(history_code)
        if scope == 'matching_history':
            info['extend'].add(code)
            info['missing_account'] |= record.account_id is None
    result = {}
    for entry in entries:
        if entry.pk in anchors:
            result[entry.pk] = anchors[entry.pk]
            continue
        old = entry.asset_category
        key = (entry.member_id, entry.account_id, entry.currency, old.code if old else '')
        info = selectors.get(key)
        if not info:
            continue
        if len(info['history_codes']) > 1:
            result[entry.pk] = (None, '同一历史范围被确认了不同类别，需重新核对确认文件')
        elif info['history_codes'] and entry.snapshot.snapshot_date not in info['anchor_dates'] and not info['missing_account']:
            result[entry.pk] = (next(iter(info['history_codes'])), '按用户明确确认的其他日期类别映射')
        elif len(info['codes']) > 1:
            result[entry.pk] = (None, '同成员、账户、旧类及币种对应多个新类别；缺少可区分历史产品的标识')
        elif info['missing_account']:
            result[entry.pk] = (None, '来源没有关联账户编号，不能按账户名称追溯历史')
        elif info['extend']:
            result[entry.pk] = (next(iter(info['extend'])), '按用户确认的同成员、同账户编号、同旧类及同币种历史规则')
    return result
