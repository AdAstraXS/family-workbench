from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("family_core", "0008_assetcategory_code_constraint"),
        ("portfolio", "0024_portfolio_reconciliation"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyPortfolioValuationRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("valuation_date", models.DateField(verbose_name="估值日期")),
                (
                    "started_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="开始时间"),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="完成时间",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "执行中"),
                            ("success", "成功"),
                            ("partial", "部分成功"),
                            ("failed", "失败"),
                        ],
                        default="running",
                        max_length=20,
                        verbose_name="状态",
                    ),
                ),
                (
                    "exchange_rate_status",
                    models.CharField(
                        blank=True,
                        max_length=20,
                        verbose_name="汇率刷新状态",
                    ),
                ),
                (
                    "exchange_rate_source_date",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="汇率来源日期",
                    ),
                ),
                (
                    "snapshot_count",
                    models.PositiveIntegerField(default=0, verbose_name="快照数量"),
                ),
                (
                    "quote_success_count",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="行情成功数量",
                    ),
                ),
                (
                    "stale_price_count",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="过期价格数量",
                    ),
                ),
                (
                    "missing_price_count",
                    models.PositiveIntegerField(default=0, verbose_name="缺价数量"),
                ),
                (
                    "missing_exchange_rate_count",
                    models.PositiveIntegerField(
                        default=0,
                        verbose_name="缺汇率数量",
                    ),
                ),
                (
                    "error_count",
                    models.PositiveIntegerField(default=0, verbose_name="错误数量"),
                ),
                (
                    "details",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        verbose_name="执行详情",
                    ),
                ),
                (
                    "family",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="daily_portfolio_valuation_runs",
                        to="family_core.family",
                        verbose_name="所属家庭",
                    ),
                ),
                (
                    "market_refresh",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="daily_valuation_runs",
                        to="portfolio.marketdatarefreshrun",
                        verbose_name="行情刷新批次",
                    ),
                ),
            ],
            options={
                "verbose_name": "每日投资组合估值运行",
                "verbose_name_plural": "每日投资组合估值运行",
                "ordering": ["-valuation_date", "-started_at", "-pk"],
                "indexes": [
                    models.Index(
                        fields=["family", "valuation_date"],
                        name="portfolio_d_family__047f5f_idx",
                    ),
                    models.Index(
                        fields=["status", "valuation_date"],
                        name="portfolio_d_status_dbed5b_idx",
                    ),
                ],
            },
        ),
    ]
