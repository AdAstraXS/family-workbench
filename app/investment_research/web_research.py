"""Explicit public-company searches, independent of the report model."""
import hashlib
import ipaddress
import json
import os
import urllib.parse
import urllib.request
from django.utils import timezone
from ai_analysis.models import AiProvider
from .research_ai import ResearchAiError, _default_transport

ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/web_search'


def search_provider():
    for provider in AiProvider.objects.filter(is_active=True).order_by('pk'):
        if urllib.parse.urlsplit(provider.base_url).hostname == 'open.bigmodel.cn':
            env = (provider.extra_data or {}).get('api_key_env_var', '')
            if env and os.getenv(env):
                return provider
    return None


def public_link(value):
    try:
        url = urllib.parse.urlsplit(value)
        host = (url.hostname or '').lower()
        if url.scheme not in {'https', 'http'} or url.username or url.password or url.port not in {None, 80, 443}:
            return False
        if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, TypeError):
        return False


def queries_for(security):
    # Deliberately exclude private thesis, holdings and custom prompt text.
    company = f'{security.name} {security.symbol}'[:120]
    return [company + ' latest earnings business competition', company + ' valuation risks recent developments']


def search(queries, provider, *, transport=None, receipt_callback=None):
    key = os.getenv((provider.extra_data or {}).get('api_key_env_var', ''), '')
    if not key or urllib.parse.urlsplit(provider.base_url).hostname != 'open.bigmodel.cn':
        raise ResearchAiError('网络搜索服务配置已变化，请重新生成。')
    rows, receipts, seen = [], [], set()
    for query in queries[:2]:
        body = json.dumps({'search_query': query, 'search_engine': 'search_std', 'count': 5,
                           'search_intent': False, 'search_recency_filter': 'oneYear'}).encode()
        request = urllib.request.Request(ENDPOINT, data=body,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}, method='POST')
        receipt = {'query': query, 'engine': 'search_std', 'status': 'requested',
                   'estimated_cost_cny': '0.01', 'pricing_checked_on': '2026-10-03',
                   'fetched_at': timezone.now().isoformat()}
        receipts.append(receipt)
        if receipt_callback:
            receipt_callback(receipts)
        response = (transport or _default_transport)(request, timeout=30)
        if len(response) > 1024 * 1024:
            raise ResearchAiError('搜索结果超过大小上限。')
        result = json.loads(response)
        if not isinstance(result.get('search_result'), list):
            raise ResearchAiError('搜索服务未返回有效结果，本次未调用报告模型。')
        fetched = timezone.now().isoformat()
        receipt.update({'request_id': str(result.get('request_id') or result.get('id') or '')[:200],
                        'status': 'completed', 'fetched_at': fetched,
                        'returned_count': len(result['search_result'])})
        if receipt_callback:
            receipt_callback(receipts)
        for item in result['search_result'][:5]:
            link = item.get('link', '')
            if not public_link(link) or link in seen or not item.get('content'):
                continue
            seen.add(link)
            text = str(item['content'])[:850]
            rows.append({'title': str(item.get('title') or link)[:250], 'kind': 'web_search',
                'date': str(item.get('publish_date') or '发布日期未标注')[:40], 'url': link,
                'publisher': str(item.get('media') or '')[:150], 'fetched_at': fetched,
                'query': query, 'text': text, 'offset': None,
                'excerpt_sha256': hashlib.sha256(text.encode()).hexdigest()})
    return rows, receipts
