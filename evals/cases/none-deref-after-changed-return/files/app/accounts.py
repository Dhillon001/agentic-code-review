from app.db import fetch_row
from app.models import Account


def account_email(account_id: int) -> str:
    return get_account(account_id).email


def get_account(account_id: int) -> Account | None:
    row = fetch_row("accounts", account_id)
    if row is None:
        return None
    return Account(**row)
