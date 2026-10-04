from django.db import migrations


def configure(apps, schema_editor):
    Rule = apps.get_model("investment_watch", "WatchRule")
    Consent = apps.get_model("investment_watch", "WatchConsent")
    Source = apps.get_model("investment_watch", "NewsSource")
    # Apply the approved Microsoft rollout only to its already-authorized owners.
    ids = list(Consent.objects.filter(active=True, dossier__security__symbol="MSFT").values_list("dossier_id", flat=True))
    families = set()
    for rule in Rule.objects.filter(dossier_id__in=ids).select_related("dossier"):
        families.add(rule.dossier.family_id)
        changed = False
        for field, value in {
            "products": ["Copilot", "Azure", "Microsoft 365", "GitHub Copilot"],
            "official_domains": ["microsoft.com"],
            "business_context": "关注重大产品升级、云与AI商业化、客户订单、数据中心供应和电力约束、资本开支、财报指引、合作与监管；行业事件须说明与微软的具体联系。",
        }.items():
            if not getattr(rule, field):
                setattr(rule, field, value)
                changed = True
        if changed:
            rule.version += 1
            rule.save()
    for family_id in families:
        for key, name, url in [
            ("microsoft-365", "微软 · Microsoft 365 产品博客", "https://www.microsoft.com/en-us/microsoft-365/blog/feed/"),
            ("azure-blog", "微软 · Azure 官方博客", "https://azure.microsoft.com/en-us/blog/feed/"),
        ]:
            Source.objects.get_or_create(family_id=family_id, key=key, defaults={
                "name": name, "url": url, "adapter": "rss", "market": "美国",
                "enabled": True, "interval_minutes": 120, "max_items": 40,
            })
        # Preserve the original feed and its history, replacing its low-relevance
        # company coverage with the two official product feeds: five active feeds.
        Source.objects.filter(family_id=family_id, key="caixin-finance").update(enabled=False)


class Migration(migrations.Migration):
    dependencies = [("investment_watch", "0009_company_reading_pipeline")]
    operations = [migrations.RunPython(configure, migrations.RunPython.noop)]
