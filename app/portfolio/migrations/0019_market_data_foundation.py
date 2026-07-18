import datetime
from zoneinfo import ZoneInfo

import django.db.models.deletion
from django.db import migrations, models


def futu_symbol(security):
    explicit = (security.extra_data or {}).get("futu_code")
    if explicit:
        return str(explicit).strip().upper()
    symbol = str(security.symbol or "").strip().upper()
    for prefix in ("HK.", "US.", "SH.", "SZ."):
        if symbol.startswith(prefix):
            return symbol
    for suffix in (".HK", ".US", ".SH", ".SZ"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    if security.market == "HK" and symbol.isdigit():
        return f"HK.{symbol.zfill(5)}"
    if security.market == "US":
        return f"US.{symbol}"
    if security.market == "CN" and symbol.isdigit():
        exchange = security.exchange if security.exchange in {"SH", "SZ"} else (
            "SH" if symbol.startswith(("5", "6", "9")) else "SZ"
        )
        return f"{exchange}.{symbol}"
    return ""


def bootstrap_market_data(apps, schema_editor):
    Security = apps.get_model("portfolio", "Security")
    SecurityMarketSnapshot = apps.get_model("portfolio", "SecurityMarketSnapshot")
    SecurityQuoteConfig = apps.get_model("portfolio", "SecurityQuoteConfig")
    SecurityPriceRecord = apps.get_model("portfolio", "SecurityPriceRecord")
    InvestmentPosition = apps.get_model("portfolio", "InvestmentPosition")

    for security in Security.objects.filter(is_active=True):
        provider_symbol = futu_symbol(security)
        automatic = security.asset_type in {"stock", "etf"} and bool(provider_symbol)
        if (security.extra_data or {}).get("futu_code"):
            automatic = True
        provider = "futu" if automatic else "manual"
        SecurityQuoteConfig.objects.update_or_create(
            security=security,
            provider=provider,
            defaults={
                "provider_symbol": provider_symbol if automatic else "",
                "price_type": "last" if automatic else "manual",
                "enabled": True,
                "priority": 10,
                "max_age_hours": 96 if automatic else 720,
            },
        )

        snapshot = SecurityMarketSnapshot.objects.filter(security=security).first()
        if not snapshot or snapshot.last_price is None:
            InvestmentPosition.objects.filter(security=security).update(
                pricing_status="missing"
            )
            continue

        source = "manual" if provider == "manual" else "legacy"
        status = "manual" if source == "manual" else "legacy"
        price_as_of = snapshot.fetched_at
        if security.asset_type == "bond":
            bond = getattr(security, "bond_detail", None)
            if bond and bond.valuation_date:
                price_as_of = datetime.datetime.combine(
                    bond.valuation_date,
                    datetime.time(16, 0),
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                )
        SecurityPriceRecord.objects.get_or_create(
            security=security,
            source=source,
            price_type="manual" if source == "manual" else "last",
            price_as_of=price_as_of,
            defaults={
                "price": snapshot.last_price,
                "currency": security.currency,
                "raw_data": {"migrated_from_latest_snapshot": True},
            },
        )
        SecurityMarketSnapshot.objects.filter(pk=snapshot.pk).update(
            price_source=source,
            price_as_of=price_as_of,
            pricing_status=status,
            last_attempt_at=snapshot.fetched_at,
        )
        InvestmentPosition.objects.filter(security=security).update(
            current_price_as_of=price_as_of,
            current_price_source=source,
            pricing_status=status,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0018_correct_legacy_us_ipo_source_prices"),
    ]

    operations = [
        migrations.CreateModel(
            name="MarketDataRefreshRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(auto_now_add=True, verbose_name="开始时间")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
                ("status", models.CharField(choices=[("running", "执行中"), ("success", "成功"), ("partial", "部分成功"), ("failed", "失败")], default="running", max_length=20, verbose_name="状态")),
                ("scope", models.CharField(default="holdings", max_length=30, verbose_name="刷新范围")),
                ("target_count", models.PositiveIntegerField(default=0, verbose_name="目标数量")),
                ("success_count", models.PositiveIntegerField(default=0, verbose_name="成功数量")),
                ("stale_count", models.PositiveIntegerField(default=0, verbose_name="过期数量")),
                ("missing_count", models.PositiveIntegerField(default=0, verbose_name="缺失数量")),
                ("error_count", models.PositiveIntegerField(default=0, verbose_name="错误数量")),
                ("details", models.JSONField(blank=True, default=dict, verbose_name="执行详情")),
            ],
            options={"verbose_name": "行情刷新批次", "verbose_name_plural": "行情刷新批次", "ordering": ["-started_at", "-pk"]},
        ),
        migrations.AddField(model_name="investmentposition", name="current_price_as_of", field=models.DateTimeField(blank=True, null=True, verbose_name="当前价格时点")),
        migrations.AddField(model_name="investmentposition", name="current_price_source", field=models.CharField(choices=[("futu", "Futu"), ("manual", "手工录入"), ("legacy", "历史缓存")], default="legacy", max_length=20, verbose_name="当前价格来源")),
        migrations.AddField(model_name="investmentposition", name="pricing_status", field=models.CharField(choices=[("fresh", "最新"), ("manual", "手工价格"), ("stale", "价格过期"), ("missing", "缺少价格"), ("error", "刷新失败"), ("legacy", "历史价格"), ("expired_unresolved", "到期未处理")], default="legacy", max_length=30, verbose_name="价格状态")),
        migrations.AddField(model_name="securitymarketsnapshot", name="is_delayed", field=models.BooleanField(default=False, verbose_name="是否延迟行情")),
        migrations.AddField(model_name="securitymarketsnapshot", name="last_attempt_at", field=models.DateTimeField(blank=True, null=True, verbose_name="最近尝试时间")),
        migrations.AddField(model_name="securitymarketsnapshot", name="last_error", field=models.TextField(blank=True, verbose_name="最近错误")),
        migrations.AddField(model_name="securitymarketsnapshot", name="price_as_of", field=models.DateTimeField(blank=True, null=True, verbose_name="价格时点")),
        migrations.AddField(model_name="securitymarketsnapshot", name="price_source", field=models.CharField(choices=[("futu", "Futu"), ("manual", "手工录入"), ("legacy", "历史缓存")], default="legacy", max_length=20, verbose_name="价格来源")),
        migrations.AddField(model_name="securitymarketsnapshot", name="pricing_status", field=models.CharField(choices=[("fresh", "最新"), ("manual", "手工价格"), ("stale", "价格过期"), ("missing", "缺少价格"), ("error", "刷新失败"), ("legacy", "历史价格"), ("expired_unresolved", "到期未处理")], default="legacy", max_length=30, verbose_name="价格状态")),
        migrations.AddField(model_name="securitymarketsnapshot", name="refresh_run", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="latest_quotes", to="portfolio.marketdatarefreshrun", verbose_name="刷新批次")),
        migrations.CreateModel(
            name="SecurityPriceRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("price", models.DecimalField(decimal_places=6, max_digits=20, verbose_name="价格")),
                ("currency", models.CharField(max_length=10, verbose_name="币种")),
                ("source", models.CharField(choices=[("futu", "Futu"), ("manual", "手工录入"), ("legacy", "历史缓存")], max_length=20, verbose_name="价格来源")),
                ("price_type", models.CharField(default="last", max_length=20, verbose_name="价格类型")),
                ("price_as_of", models.DateTimeField(verbose_name="价格时点")),
                ("is_delayed", models.BooleanField(default=False, verbose_name="是否延迟行情")),
                ("raw_data", models.JSONField(blank=True, default=dict, verbose_name="原始数据")),
                ("fetched_at", models.DateTimeField(auto_now_add=True, verbose_name="获取时间")),
                ("refresh_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="price_records", to="portfolio.marketdatarefreshrun", verbose_name="刷新批次")),
                ("security", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="price_records", to="portfolio.security", verbose_name="证券标的")),
            ],
            options={
                "verbose_name": "证券历史价格",
                "verbose_name_plural": "证券历史价格",
                "ordering": ["-price_as_of", "-pk"],
                "indexes": [models.Index(fields=["security", "-price_as_of"], name="portfolio_s_securit_a433fa_idx")],
                "constraints": [models.UniqueConstraint(fields=("security", "source", "price_type", "price_as_of"), name="unique_security_price_observation")],
            },
        ),
        migrations.CreateModel(
            name="SecurityQuoteConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("provider", models.CharField(choices=[("futu", "Futu"), ("manual", "手工录入"), ("legacy", "历史缓存")], default="futu", max_length=20, verbose_name="行情来源")),
                ("provider_symbol", models.CharField(blank=True, max_length=100, verbose_name="行情源代码")),
                ("price_type", models.CharField(default="last", max_length=20, verbose_name="价格类型")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("priority", models.PositiveSmallIntegerField(default=10, verbose_name="优先级")),
                ("max_age_hours", models.PositiveIntegerField(default=96, verbose_name="最大有效小时数")),
                ("security", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="quote_configs", to="portfolio.security", verbose_name="证券标的")),
            ],
            options={
                "verbose_name": "证券行情配置",
                "verbose_name_plural": "证券行情配置",
                "ordering": ["priority", "pk"],
                "constraints": [models.UniqueConstraint(fields=("security", "provider"), name="unique_security_quote_provider")],
            },
        ),
        migrations.RunPython(bootstrap_market_data, migrations.RunPython.noop),
    ]
