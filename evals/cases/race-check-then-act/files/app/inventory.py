"""In-memory stock ledger. reserve() is called concurrently from request worker threads."""

_stock: dict[str, int] = {"widget": 5}


def reserve(item: str, qty: int) -> bool:
    if _stock.get(item, 0) < qty:
        return False
    _stock[item] -= qty
    return True
