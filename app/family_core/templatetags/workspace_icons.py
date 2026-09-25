from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()
ICONS = frozenset("home-2 briefcase-2 wallet chart-line chart-donut building-bank notes book-2 report-money sparkles palette search arrow-right arrow-left plus plant-2 settings x layout-grid clock check sun lock".split())


@register.simple_tag
def workspace_icon(name):
    name = name if name in ICONS else "layout-grid"
    return format_html('<svg class="ws-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="{}#{}"></use></svg>', static("songting/icons.svg"), name)
