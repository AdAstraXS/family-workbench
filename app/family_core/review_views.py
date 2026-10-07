from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.http import require_GET
from .permissions import current_member
from .content_search import GROUPS, search_content
from .review import weekly_review


def member_for(request):
    member = current_member(request)
    if member is None:
        raise PermissionDenied
    return member


@login_required
@require_GET
def review(request):
    return render(request, 'family_core/review.html', weekly_review(member_for(request)))


@login_required
@require_GET
def basis(request):
    from .financial_basis import financial_basis
    return render(request, 'family_core/financial_basis.html', financial_basis(member_for(request)))


@login_required
@require_GET
def changes(request):
    member_for(request)
    return render(request, 'family_core/changes.html')


@login_required
@require_GET
def search(request):
    member = member_for(request)
    query = request.GET.get('q', '').strip()[:120]
    group = request.GET.get('group', 'all')
    if group not in dict(GROUPS):
        group = 'all'
    groups = search_content(member, query, group, request.GET.get('page', 1))
    for item in groups:
        item['more_url'] = '?' + urlencode({'q': query, 'group': item['key']})
        item['page_query'] = urlencode({'q': query, 'group': item['key']})
        for row in item['items']:
            row['url'] += ('&' if '?' in row['url'] else '?') + urlencode({'return_to': request.get_full_path()})
    return render(request, 'family_core/search.html', {'query': query, 'selected_group': group,
                  'group_choices': GROUPS, 'groups': groups, 'total': sum(g['count'] for g in groups)})
