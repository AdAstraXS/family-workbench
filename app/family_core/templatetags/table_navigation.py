"""Paginate already permission-filtered records without changing report totals."""
import re

from django import template
from django.core.paginator import Paginator

register = template.Library()


@register.simple_tag(takes_context=True)
def table_page(context, records, key, label="记录"):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
        raise ValueError("Table keys must be stable HTML identifiers")
    request = context["request"]
    parameter = "table_page_" + key
    # Stable fallback for simple administrative lists; ordered financial lists keep
    # their business ordering. The caller applies permissions and filters first.
    if hasattr(records, "ordered") and not records.ordered:
        records = records.order_by("pk")
    page = Paginator(records if records is not None else [], 30).get_page(request.GET.get(parameter))
    anchor = "table-" + key

    def url(number):
        query = request.GET.copy()
        query[parameter] = number
        return "?" + query.urlencode() + "#" + anchor

    return {
        "page": page, "anchor": anchor, "label": label,
        "first_url": url(1), "last_url": url(page.paginator.num_pages),
        "previous_url": url(page.previous_page_number()) if page.has_previous() else "",
        "next_url": url(page.next_page_number()) if page.has_next() else "",
    }
