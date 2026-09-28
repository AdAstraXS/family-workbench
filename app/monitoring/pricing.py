"""CNY rate snapshots. Never convert legacy USD budget settings into actual costs."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


def vendor_for(provider):
    host = urlsplit(provider.base_url or '').hostname or ''
    if host == 'api.deepseek.com': return 'deepseek'
    if host == 'open.bigmodel.cn': return 'zhipu'
    if host == 'ark.cn-beijing.volces.com': return 'volcano'
    return 'other'


def decimal_value(value):
    try:
        if isinstance(value, bool) or value is None: return None
        n = Decimal(str(value))
        return n if n.is_finite() and 0 <= n <= Decimal('1000000000000') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def rate_snapshot(provider=None, *, audio=False):
    if audio:
        return {'unit':'second','rate':'0.00022','source':'https://help.aliyun.com/zh/model-studio/fun-asr',
                'checked':'2026-09-28','note':'北京 Fun-ASR 标准价'}
    extra = provider.extra_data or {}
    custom = extra.get('monitoring_cny_rates')
    if isinstance(custom, dict):
        rates = {k:decimal_value(custom.get(k)) for k in ('input','output','cached')}
        if rates['input'] is not None and rates['output'] is not None:
            return {'unit':'million_tokens', **{k:str(v) for k,v in rates.items() if v is not None},
                    'source':'后台人民币单价','checked':str(custom.get('checked',''))[:10], 'note':'配置标准价'}
    model = provider.model_name
    vendor = vendor_for(provider)
    if vendor == 'zhipu' and model in {'glm-5.3-flashx','glm-5v-turbo'}:
        common={'unit':'million_tokens','checked':'2026-09-28',
                'source':'https://docs.bigmodel.cn/cn/guide/start/pricing'}
        if model=='glm-5.3-flashx':
            return dict(common,input='2',output='7',cached='0.57',note='官方标准价，缓存存储限时免费')
        return dict(common,tiers=[['32767','5','22','1.2'],['1000000000000','7','26','1.8']],
                    note='输入小于 32K：5 / 22 / 1.2；其余：7 / 26 / 1.8 元/百万 Token（输入/输出/缓存）')
    if vendor == 'deepseek' and model in {'deepseek-flash','deepseek-v4-flash','deepseek-v4-pro'}:
        rates = ('9','27','0.3') if model=='deepseek-v4-pro' else ('2','8','0.04')
        return dict(zip(('input','output','cached'), rates), unit='million_tokens', checked='2026-09-28',
                    source='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
                    note='按高峰标准价估算；空闲时段实际可能更低')
    if vendor == 'volcano' and model == 'doubao-seed-2-0-lite-260215':
        return {'unit':'million_tokens','tiers':[['32768','0.6','3.6','0.12'],['131072','0.9','5.4','0.18'],['262144','1.8','10.8','0.36']],
                'source':'https://docs.volcengine.com/docs/ark/model-pricing?lang=zh','checked':'2026-09-28','note':'按输入长度分档；非音频标准价'}
    return {}


def cost_for(record):
    rates = record.price_snapshot
    if record.audio_seconds is not None and rates.get('unit')=='second':
        rate = decimal_value(rates.get('rate'))
        return (record.audio_seconds*rate).quantize(Decimal('.00000001')) if rate is not None else None
    if record.input_tokens is None or record.output_tokens is None or not rates: return None
    if rates.get('tiers'):
        tier = next((t for t in rates['tiers'] if record.input_tokens <= int(t[0])), None)
        if tier is None: return None
        rates = dict(zip(('input','output','cached'),tier[1:]))
    a,b,c = (decimal_value(rates.get(k)) for k in ('input','output','cached'))
    if a is None or b is None: return None
    # Unknown cache split uses ordinary input price, still an explicitly labelled estimate.
    cached = record.cached_tokens or 0
    c = c if c is not None else a
    return ((Decimal(record.input_tokens-cached)*a+Decimal(cached)*c+Decimal(record.output_tokens)*b)/1000000).quantize(Decimal('.00000001'))
