def apply_discount(price: float, code: str | None) -> float:
    if code == "SAVE10":
        return round(price * 0.9, 2)
    if code == "HALFOFF" and price >= 50:
        return round(price * 0.5, 2)
    return price
