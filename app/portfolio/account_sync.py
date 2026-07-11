from .models import InvestmentAccount


def sync_investment_account(bank_account):
    supports_investment = bank_account.supports_investment
    account = InvestmentAccount.objects.filter(
        bank_account=bank_account,
    ).first()
    if not supports_investment:
        return None

    if not account:
        account = InvestmentAccount(bank_account=bank_account)

    account.bank_account = bank_account
    account.save()
    return account
