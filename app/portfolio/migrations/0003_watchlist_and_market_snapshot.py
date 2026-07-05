import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portfolio", "0002_transaction_accounting"),
    ]

    operations = [
        migrations.AddField(
            model_name="security",
            name="data_source",
            field=models.CharField(default="manual", max_length=30, verbose_name="数据来源"),
        ),
        migrations.AddField(
            model_name="security",
            name="exchange",
            field=models.CharField(blank=True, max_length=30, verbose_name="交易所"),
        ),
        migrations.AddField(
            model_name="security",
            name="is_delisted",
            field=models.BooleanField(default=False, verbose_name="是否退市"),
        ),
        migrations.AddField(
            model_name="security",
            name="listing_date",
            field=models.DateField(blank=True, null=True, verbose_name="上市日期"),
        ),
        migrations.AddField(
            model_name="security",
            name="lot_size",
            field=models.PositiveIntegerField(default=0, verbose_name="每手股数"),
        ),
        migrations.AddField(
            model_name="security",
            name="source_updated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="来源更新时间"),
        ),
        migrations.CreateModel(
            name="SecurityMarketSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quote_time", models.CharField(blank=True, max_length=50, verbose_name="行情时间")),
                ("last_price", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="最新价")),
                ("total_market_value", models.DecimalField(blank=True, decimal_places=4, max_digits=24, null=True, verbose_name="总市值")),
                ("pe_ratio", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="市盈率")),
                ("pe_ttm_ratio", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="市盈率 TTM")),
                ("pb_ratio", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="市净率")),
                ("dividend_yield_ttm", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="股息率 TTM")),
                ("turnover_rate", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="换手率")),
                ("high_52_week", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="52 周最高")),
                ("low_52_week", models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True, verbose_name="52 周最低")),
                ("issued_shares", models.BigIntegerField(blank=True, null=True, verbose_name="总股本")),
                ("outstanding_shares", models.BigIntegerField(blank=True, null=True, verbose_name="流通股本")),
                ("raw_data", models.JSONField(blank=True, default=dict, verbose_name="原始数据")),
                ("fetched_at", models.DateTimeField(auto_now=True, verbose_name="获取时间")),
                ("security", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="market_snapshot", to="portfolio.security", verbose_name="证券标的")),
            ],
            options={
                "verbose_name": "证券行情快照",
                "verbose_name_plural": "证券行情快照",
            },
        ),
        migrations.CreateModel(
            name="WatchlistItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否关注")),
                ("remark", models.TextField(blank=True, verbose_name="关注备注")),
                ("family", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="watchlist_items", to="family_core.family", verbose_name="所属家庭")),
                ("member", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="watchlist_items", to="family_core.familymember", verbose_name="添加成员")),
                ("security", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="watchlist_items", to="portfolio.security", verbose_name="证券标的")),
            ],
            options={
                "verbose_name": "自选股",
                "verbose_name_plural": "自选股",
                "ordering": ["security__market", "security__symbol"],
            },
        ),
        migrations.AddIndex(
            model_name="watchlistitem",
            index=models.Index(fields=["family", "is_active"], name="portfolio_w_family__0de032_idx"),
        ),
        migrations.AddConstraint(
            model_name="watchlistitem",
            constraint=models.UniqueConstraint(fields=("family", "security"), name="unique_family_watchlist_security"),
        ),
    ]
