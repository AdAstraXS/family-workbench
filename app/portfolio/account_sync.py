from .models import InvestmentAccount


def sync_investment_account(bank_account):
    if not bank_account.supports_investment:
        return None
    # The profile delegates account attributes to BankAccount; unchanged profiles
    # need no write. get_or_create also handles concurrent first transactions.
    account, _ = InvestmentAccount.objects.get_or_create(bank_account=bank_account)
    return account
