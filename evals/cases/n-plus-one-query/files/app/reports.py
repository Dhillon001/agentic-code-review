def order_summaries(db, customer_ids: list[int]) -> list[dict]:
    summaries = []
    for cid in customer_ids:
        customer = db.execute("SELECT name FROM customers WHERE id = ?", (cid,)).fetchone()
        orders = db.execute("SELECT total FROM orders WHERE customer_id = ?", (cid,)).fetchall()
        summaries.append({"customer": customer["name"], "total": sum(o["total"] for o in orders)})
    return summaries
