from django.apps import AppConfig


class PortfolioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portfolio"
    verbose_name = "投资组合"

    def ready(self):
        from . import signals  # noqa: F401
