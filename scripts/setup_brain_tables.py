#!/usr/bin/env python3
"""Create products, customers, orders tables in brain.db and import waitlist.json."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "brain.db"
WAITLIST_PATH = ROOT / "waitlist.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    product_type TEXT NOT NULL CHECK (product_type IN ('physical', 'digital', 'service')),
    price REAL,
    description TEXT,
    stock_quantity INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        (product_type = 'physical' AND stock_quantity IS NOT NULL)
        OR (product_type IN ('digital', 'service') AND stock_quantity IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    email TEXT,
    zalo TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded', 'completed')),
    purchased_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def normalize_phone(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("84") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return digits or None


def pick_field(entry: dict, *keys: str):
    for key in keys:
        value = entry.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def load_waitlist() -> List[Dict]:
    if not WAITLIST_PATH.exists():
        return []
    with WAITLIST_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return data["entries"]
    raise ValueError("waitlist.json must be a JSON array or { \"entries\": [...] }")


def import_customers(conn: sqlite3.Connection, entries: List[Dict]) -> Tuple[int, int]:
    inserted = 0
    skipped = 0
    cur = conn.cursor()

    for entry in entries:
        name = pick_field(entry, "name", "fullname", "ho_ten", "ten", "customer_name")
        phone = normalize_phone(
            pick_field(entry, "phone", "sdt", "so_dien_thoai", "mobile", "tel") or ""
        )
        zalo = pick_field(entry, "zalo", "zalo_phone", "zalo_id")
        registered_at = pick_field(
            entry, "registered_at", "registration_date", "ngay_dang_ky", "created_at", "date"
        )

        if not name or not phone:
            skipped += 1
            continue

        try:
            if registered_at:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO customers (name, phone, zalo, registered_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, phone, zalo, registered_at),
                )
            else:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO customers (name, phone, zalo)
                    VALUES (?, ?, ?)
                    """,
                    (name, phone, zalo),
                )
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.Error:
            skipped += 1

    conn.commit()
    return inserted, skipped


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)

    entries = load_waitlist()
    inserted, skipped = import_customers(conn, entries)

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM products")
    product_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM customers")
    customer_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders")
    order_count = cur.fetchone()[0]

    conn.close()

    print(f"Database: {DB_PATH}")
    print(f"waitlist.json: {'found' if WAITLIST_PATH.exists() else 'not found'}")
    print(f"Customers imported: {inserted} inserted, {skipped} skipped (duplicate/invalid)")
    print(f"Table counts — products: {product_count}, customers: {customer_count}, orders: {order_count}")


if __name__ == "__main__":
    main()
