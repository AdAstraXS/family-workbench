from .models import InvestmentAccount


def sync_investment_account(bank_account):
    is_broker = (
        bank_account.account_type_ref
        and bank_account.account_type_ref.name == "券商"
    )
    account = InvestmentAccount.objects.filter(
        bank_account=bank_account,
    ).first()
    if not is_broker:
        if account and account.is_active:
            account.is_active = False
            account.save(update_fields=["is_active", "updated_at"])
        return None

    if not account:
        account = InvestmentAccount.objects.filter(
            bank_account=None,
            family=bank_account.family,
            member=bank_account.member,
            account_name=bank_account.account_name,
        ).first()
    if not account:
        account = InvestmentAccount(bank_account=bank_account)

    account.bank_account = bank_account
    account.family = bank_account.family
    account.member = bank_account.member
    account.account_name = bank_account.account_name
    account.account_no_masked = bank_account.account_no_masked
    account.account_region = bank_account.account_region
    account.is_active = bank_account.is_active
    account.remark = bank_account.remark
    account.save()
    return account
