from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from ipo.models import HkIpoSubscriptionTrade
from ledger.models import BankAccount

from .account_sync import sync_investment_account
from .ipo_sync import delete_synced_ipo_transactions, sync_ipo_trade
from .models import InvestmentAccount


@receiver(post_save, sender=BankAccount)
def sync_broker_investment_account(sender, instance, **kwargs):
    sync_investment_account(instance)


@receiver(pre_delete, sender=BankAccount)
def deactivate_deleted_broker_account(sender, instance, **kwargs):
    InvestmentAccount.objects.filter(bank_account=instance).update(
        is_active=False,
    )


@receiver(post_save, sender=HkIpoSubscriptionTrade)
def sync_ipo_subscription_trade(sender, instance, **kwargs):
    sync_ipo_trade(instance.pk)


@receiver(post_delete, sender=HkIpoSubscriptionTrade)
def delete_ipo_subscription_transactions(sender, instance, **kwargs):
    delete_synced_ipo_transactions(instance.pk)
