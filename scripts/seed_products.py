#!/usr/bin/env python3
"""Seed sản phẩm mẫu nếu DB trống (dùng khi deploy Render)."""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "brain.db"

SAMPLES = [
    ("Hyundai Tucson", "physical", 850_000_000.0, "Đặt cọc Hyundai Tucson", 5),
    ("Gói lái thử", "service", 2_000.0, "Dịch vụ lái thử Hyundai", None),
    ("Hyundai Santa Fe", "physical", 1_100_000_000.0, "Đặt cọc Santa Fe", 3),
    ("Hyundai Creta", "physical", 650_000_000.0, "Đặt cọc Creta", 4),
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count:
        conn.close()
        print(f"Products already seeded ({count} rows).")
        return
    for name, ptype, price, desc, stock in SAMPLES:
        conn.execute(
            """
            INSERT INTO products (name, product_type, price, description, stock_quantity)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, ptype, price, desc, stock),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(SAMPLES)} products.")


if __name__ == "__main__":
    main()
