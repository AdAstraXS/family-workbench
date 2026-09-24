"""Administrator-managed text model defaults; no automatic provider failover."""

from .models import AiModuleModel


def default_provider(module):
    setting = AiModuleModel.objects.select_related("provider").filter(module=module).first()
    return setting.provider if setting else None


def prefer_default(providers, module):
    """Keep existing manual overrides, with the configured default selected first."""
    providers = list(providers)
    preferred = default_provider(module)
    if preferred:
        providers.sort(key=lambda item: item.pk != preferred.pk)
    return providers
