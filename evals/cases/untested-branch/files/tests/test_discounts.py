from app.discounts import apply_discount


def test_save10():
    assert apply_discount(100.0, "SAVE10") == 90.0


def test_no_code():
    assert apply_discount(100.0, None) == 100.0
