from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "knowledge"
    verbose_name = "家庭知识底座"

    def ready(self):
        from . import checks, signals  # noqa: F401
