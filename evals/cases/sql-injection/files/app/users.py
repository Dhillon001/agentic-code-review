import sqlite3


def find_user(conn: sqlite3.Connection, username: str):
    cur = conn.cursor()
    cur.execute(f"SELECT id, email FROM users WHERE username = '{username}'")
    return cur.fetchone()
