"""Django application configuration for option wheel decision support."""

from django.apps import AppConfig


class OptionWheelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "option_wheel"
    verbose_name = "期权车轮"
