from django import template
from investment_research.navigation import navigation
from investment_research.permissions import get_current_member, is_writer

register = template.Library()


@register.simple_tag(takes_context=True)
def research_navigation(context):
    return navigation(context)


@register.simple_tag(takes_context=True)
def research_writer(context):
    return is_writer(get_current_member(context['request']))
