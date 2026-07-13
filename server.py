#!/usr/bin/env python3
"""Local server: static site + /admin panel + brain.db REST API."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_cors import CORS

import resend_mail

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "brain.db"

load_dotenv(ROOT / ".env")

app = Flask(__name__, static_folder=str(ROOT), static_url_path="")
CORS(app, resources={r"/api/*": {"origins": "*"}})


def sepay_config() -> tuple[str, str, str, int]:
    merchant_id = os.getenv("SEPAY_MERCHANT_ID", "").strip()
    secret_key = os.getenv("SEPAY_SECRET_KEY", "").strip()
    env = os.getenv("SEPAY_ENV", "sandbox").strip().lower()
    try:
        test_amount = int(os.getenv("SEPAY_TEST_AMOUNT", "2000"))
    except ValueError:
        test_amount = 2000
    return merchant_id, secret_key, env, test_amount


def bank_config() -> dict | None:
    bin_id = os.getenv("SEPAY_BANK_BIN", "970432").strip()
    account = os.getenv("SEPAY_BANK_ACCOUNT", "0921451991").strip()
    account_name = remove_accents(os.getenv("SEPAY_BANK_ACCOUNT_NAME", "LE NGOC TU").strip())
    bank_name = os.getenv("SEPAY_BANK_NAME", "VPBank").strip()
    prefix = os.getenv("SEPAY_PAYMENT_PREFIX", "HYU").strip()
    if not bin_id or not account or not account_name:
        return None
    return {
        "bin_id": bin_id,
        "account_number": normalize_account_number(account),
        "account_name": account_name,
        "bank_name": bank_name or "VPBank",
        "prefix": prefix,
    }


def remove_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_account_number(account: str) -> str:
    """Chuẩn hóa STK: bỏ khoảng trắng; giữ số 0 đầu nếu có."""
    return re.sub(r"\s+", "", account.strip())


def vietqr_url(bank_name: str, account: str, amount: int, content: str) -> str:
    """SePay khuyến nghị vietqr.app — app ngân hàng nhận diện STK chính xác hơn img.vietqr.io."""
    params = {
        "acc": normalize_account_number(account),
        "bank": bank_name.strip(),
        "amount": str(amount),
        "des": content,
        "template": "compact2",
    }
    return f"https://vietqr.app/img?{urlencode(params)}"


def base_url() -> str:
    return request.host_url.rstrip("/")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_customers_schema(conn)
    return conn


def ensure_customers_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()}
    if not cols:
        return
    if "email" not in cols:
        conn.execute("ALTER TABLE customers ADD COLUMN email TEXT")
        conn.execute(
            """
            UPDATE customers
            SET email = zalo
            WHERE zalo LIKE '%@%'
              AND (email IS NULL OR email = '')
            """
        )
        conn.commit()


def ensure_sepay_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sepay_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT NOT NULL UNIQUE,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            car TEXT,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'cancelled', 'error')),
            sepay_order_id TEXT,
            sepay_tx_id INTEGER UNIQUE,
            paid_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sepay_payments)").fetchall()}
        if "sepay_tx_id" not in cols:
            conn.execute("ALTER TABLE sepay_payments ADD COLUMN sepay_tx_id INTEGER")
        if "order_id" not in cols:
            conn.execute("ALTER TABLE sepay_payments ADD COLUMN order_id INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def mark_payment_paid(
    conn: sqlite3.Connection,
    invoice: str,
    *,
    sepay_ref: str | None = None,
    sepay_tx_id: int | None = None,
) -> bool:
    payment = conn.execute(
        "SELECT * FROM sepay_payments WHERE invoice_number = ?", (invoice,)
    ).fetchone()
    if not payment or payment["status"] == "paid":
        return False

    if sepay_tx_id is not None:
        dup = conn.execute(
            "SELECT id FROM sepay_payments WHERE sepay_tx_id = ?", (sepay_tx_id,)
        ).fetchone()
        if dup and dup["id"] != payment["id"]:
            return False

    conn.execute(
        """
        UPDATE sepay_payments
        SET status = 'paid',
            sepay_order_id = COALESCE(?, sepay_order_id),
            sepay_tx_id = COALESCE(?, sepay_tx_id),
            paid_at = datetime('now')
        WHERE invoice_number = ?
        """,
        (sepay_ref, sepay_tx_id, invoice),
    )

    order_id = payment["order_id"] if "order_id" in payment.keys() else None
    if order_id:
        conn.execute(
            "UPDATE orders SET status = 'paid' WHERE id = ?",
            (order_id,),
        )
    else:
        customer_id = upsert_customer(conn, payment["customer_name"], payment["phone"])
        product_id = resolve_product_id(conn, payment["car"] or "")
        if product_id:
            conn.execute(
                """
                INSERT INTO orders (customer_id, product_id, amount, status)
                VALUES (?, ?, ?, 'paid')
                """,
                (customer_id, product_id, payment["amount"]),
            )
    return True


def extract_invoice_from_webhook(data: dict, prefix: str) -> str | None:
    prefix_u = prefix.upper()
    pattern = re.compile(rf"{re.escape(prefix_u)}-?\d+")
    for raw in (data.get("code"), data.get("content"), data.get("description")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        match = pattern.search(raw.upper())
        if not match:
            continue
        token = match.group(0)
        if "-" in token:
            return token
        digits = token[len(prefix_u):]
        if digits.isdigit():
            return f"{prefix_u}-{digits}"
    return None


def upsert_customer(conn: sqlite3.Connection, name: str, phone: str, email: str | None = None) -> int:
    row = conn.execute("SELECT id FROM customers WHERE phone = ?", (phone,)).fetchone()
    if row:
        conn.execute(
            "UPDATE customers SET name = ?, email = COALESCE(?, email) WHERE id = ?",
            (name, email, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO customers (name, phone, email) VALUES (?, ?, ?)",
        (name, phone, email),
    )
    return cur.lastrowid


def resolve_product_id(conn: sqlite3.Connection, service_name: str) -> int | None:
    name = (service_name or "").strip()
    if name:
        row = conn.execute(
            "SELECT id FROM products WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM products WHERE name LIKE ?",
            (f"%{name}%",),
        ).fetchone()
        if row:
            return row["id"]
    return get_deposit_product_id(conn)


def get_deposit_product_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM products
        WHERE product_type = 'service'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    return row["id"] if row else None


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def rows_to_list(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def validate_product_payload(data: dict, is_update: bool = False) -> tuple[dict | None, str | None]:
    name = (data.get("name") or "").strip()
    product_type = (data.get("product_type") or "").strip()
    price = data.get("price")
    description = (data.get("description") or "").strip() or None
    stock_raw = data.get("stock_quantity")

    if not name:
        return None, "Tên sản phẩm không được để trống."
    if product_type not in ("physical", "digital", "service"):
        return None, "Loại sản phẩm phải là physical, digital hoặc service."

    try:
        price_val = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        return None, "Giá không hợp lệ."

    stock_quantity = None
    if product_type == "physical":
        if stock_raw in (None, ""):
            return None, "Sản phẩm vật lý phải có số lượng tồn kho."
        try:
            stock_quantity = int(stock_raw)
        except (TypeError, ValueError):
            return None, "Số lượng tồn kho phải là số nguyên."
        if stock_quantity < 0:
            return None, "Số lượng tồn kho không được âm."
    elif stock_raw not in (None, ""):
        return None, "Sản phẩm digital/service không được có tồn kho."

    payload = {
        "name": name,
        "product_type": product_type,
        "price": price_val,
        "description": description,
        "stock_quantity": stock_quantity,
    }
    return payload, None


@app.route("/admin")
@app.route("/admin/")
def admin_page():
    return send_from_directory(ROOT / "admin", "index.html")


@app.route("/admin/<path:filename>")
def admin_assets(filename: str):
    return send_from_directory(ROOT / "admin", filename)


@app.route("/thanh-toan")
@app.route("/thanh-toan/")
def thanh_toan_page():
    return send_from_directory(ROOT, "thanh-toan.html")


@app.route("/")
def index_page():
    return send_from_directory(ROOT, "index.html")


# --- SePay Payment ---


@app.post("/api/payment/checkout")
def payment_checkout():
    """Tạo đơn đặt cọc + trả thông tin QR chuyển khoản (không qua cổng checkout SePay)."""
    bank = bank_config()
    if not bank:
        return jsonify({
            "error": (
                "Chưa cấu hình tài khoản ngân hàng trong .env "
                "(SEPAY_BANK_BIN, SEPAY_BANK_ACCOUNT, SEPAY_BANK_ACCOUNT_NAME)."
            ),
        }), 503

    _, _, _, default_amount = sepay_config()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip() or None
    car = (data.get("car") or data.get("service") or "").strip()

    if not name or not phone:
        return jsonify({"error": "Vui lòng nhập họ tên và số điện thoại trước khi thanh toán."}), 400

    try:
        amount = int(data.get("amount") or default_amount)
    except (TypeError, ValueError):
        return jsonify({"error": "Số tiền không hợp lệ."}), 400

    if amount < 1000:
        return jsonify({"error": "Số tiền tối thiểu là 1.000 VND."}), 400

    invoice = f"{bank['prefix']}-{int(time.time())}"

    conn = get_db()
    ensure_sepay_tables(conn)
    customer_id = upsert_customer(conn, name, phone, email)
    product_id = resolve_product_id(conn, car)
    if not product_id:
        conn.close()
        return jsonify({"error": "Chưa có sản phẩm/dịch vụ trong hệ thống."}), 503

    order_cur = conn.execute(
        """
        INSERT INTO orders (customer_id, product_id, amount, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (customer_id, product_id, float(amount)),
    )
    order_id = order_cur.lastrowid

    conn.execute(
        """
        INSERT INTO sepay_payments (invoice_number, customer_name, phone, car, amount, status, order_id)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (invoice, name, phone, car or None, float(amount), order_id),
    )
    conn.commit()
    conn.close()

    transfer_content = invoice
    qr_url = vietqr_url(
        bank["bank_name"],
        bank["account_number"],
        amount,
        transfer_content,
    )

    return jsonify({
        "mode": "qr",
        "invoice": invoice,
        "order_id": order_id,
        "amount": amount,
        "bank_name": bank["bank_name"],
        "account_number": bank["account_number"],
        "account_name": bank["account_name"],
        "transfer_content": transfer_content,
        "qr_url": qr_url,
    })


@app.get("/api/payment/status/<invoice>")
def payment_status(invoice: str):
    conn = get_db()
    ensure_sepay_tables(conn)
    row = conn.execute(
        "SELECT invoice_number, status, amount, paid_at FROM sepay_payments WHERE invoice_number = ?",
        (invoice,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Không tìm thấy đơn."}), 404
    return jsonify(dict(row))


@app.post("/api/sepay/webhook")
def sepay_webhook():
    """SePay Webhook — nhận biến động số dư khi khách chuyển khoản."""
    data = request.get_json(silent=True) or {}
    if data.get("transferType") != "in":
        return jsonify({"success": True}), 200

    bank = bank_config()
    prefix = bank["prefix"] if bank else "HYU"
    invoice = extract_invoice_from_webhook(data, prefix)
    if not invoice:
        return jsonify({"success": True}), 200

    try:
        transfer_amount = int(data.get("transferAmount") or 0)
    except (TypeError, ValueError):
        transfer_amount = 0

    conn = get_db()
    ensure_sepay_tables(conn)
    payment = conn.execute(
        "SELECT * FROM sepay_payments WHERE invoice_number = ?", (invoice,)
    ).fetchone()

    if not payment or payment["status"] == "paid":
        conn.close()
        return jsonify({"success": True}), 200

    if transfer_amount < int(payment["amount"]):
        conn.close()
        return jsonify({"success": True}), 200

    tx_id = data.get("id")
    try:
        tx_id_int = int(tx_id) if tx_id is not None else None
    except (TypeError, ValueError):
        tx_id_int = None

    mark_payment_paid(
        conn,
        invoice,
        sepay_ref=str(data.get("referenceCode") or ""),
        sepay_tx_id=tx_id_int,
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200


@app.get("/payment/success")
def payment_success():
    return redirect("/?payment=success")


@app.get("/payment/error")
def payment_error():
    return redirect("/?payment=error")


@app.get("/payment/cancel")
def payment_cancel():
    return redirect("/?payment=cancel")


@app.post("/api/sepay/ipn")
def sepay_ipn():
    """Nhận thông báo thanh toán từ SePay (ORDER_PAID → cập nhật đơn)."""
    data = request.get_json(silent=True) or {}
    notification_type = data.get("notification_type")
    order_info = data.get("order") or {}
    invoice = (order_info.get("order_invoice_number") or "").strip()

    if notification_type != "ORDER_PAID" or not invoice:
        return jsonify({"success": True}), 200

    conn = get_db()
    ensure_sepay_tables(conn)
    payment = conn.execute(
        "SELECT * FROM sepay_payments WHERE invoice_number = ?", (invoice,)
    ).fetchone()

    if not payment:
        conn.close()
        return jsonify({"success": True}), 200

    if payment["status"] == "paid":
        conn.close()
        return jsonify({"success": True}), 200

    sepay_order_id = order_info.get("order_id") or order_info.get("id")
    mark_payment_paid(conn, invoice, sepay_ref=str(sepay_order_id or ""))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200


@app.get("/api/sepay/payments")
def list_sepay_payments():
    conn = get_db()
    ensure_sepay_tables(conn)
    rows = conn.execute(
        "SELECT * FROM sepay_payments ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# --- Products ---


@app.get("/api/products")
def list_products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/products")
def create_product():
    payload, err = validate_product_payload(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO products (name, product_type, price, description, stock_quantity)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            payload["name"],
            payload["product_type"],
            payload["price"],
            payload["description"],
            payload["stock_quantity"],
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)), 201


@app.put("/api/products/<int:product_id>")
def update_product(product_id: int):
    payload, err = validate_product_payload(request.get_json(silent=True) or {}, is_update=True)
    if err:
        return jsonify({"error": err}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Không tìm thấy sản phẩm."}), 404

    conn.execute(
        """
        UPDATE products
        SET name = ?, product_type = ?, price = ?, description = ?, stock_quantity = ?
        WHERE id = ?
        """,
        (
            payload["name"],
            payload["product_type"],
            payload["price"],
            payload["description"],
            payload["stock_quantity"],
            product_id,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row))


@app.delete("/api/products/<int:product_id>")
def delete_product(product_id: int):
    conn = get_db()
    order_count = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE product_id = ?", (product_id,)
    ).fetchone()["c"]
    if order_count:
        conn.close()
        return jsonify({"error": "Không thể xóa sản phẩm đang có đơn hàng liên quan."}), 400

    cur = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Không tìm thấy sản phẩm."}), 404
    return jsonify({"ok": True})


# --- Customers ---


@app.get("/api/customers")
def list_customers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/waitlist")
def register_waitlist():
    """Đăng ký waitlist — lưu khách hàng kèm email (dùng cho form trên website)."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()

    if not name or not phone or not email:
        return jsonify({"error": "Vui lòng nhập họ tên, số điện thoại và email."}), 400

    conn = get_db()
    customer_id = upsert_customer(conn, name, phone, email)
    conn.commit()
    try:
        resend_mail.handle_waitlist_emails(conn, customer_id, name, email)
        resend_mail.process_email_queue(conn)
    except Exception as exc:
        app.logger.warning("Waitlist email failed: %s", exc)
    conn.close()
    return jsonify({"ok": True, "customer_id": customer_id})


@app.post("/api/customers")
def create_customer():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip() or None
    zalo = (data.get("zalo") or "").strip() or None
    registered_at = (data.get("registered_at") or "").strip() or None

    if not name or not phone:
        return jsonify({"error": "Tên và số điện thoại là bắt buộc."}), 400

    conn = get_db()
    try:
        if registered_at:
            cur = conn.execute(
                "INSERT INTO customers (name, phone, email, zalo, registered_at) VALUES (?, ?, ?, ?, ?)",
                (name, phone, email, zalo, registered_at),
            )
        else:
            cur = conn.execute(
                "INSERT INTO customers (name, phone, email, zalo) VALUES (?, ?, ?, ?)",
                (name, phone, email, zalo),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Số điện thoại đã tồn tại."}), 400
    conn.close()
    return jsonify(row_to_dict(row)), 201


@app.put("/api/customers/<int:customer_id>")
def update_customer(customer_id: int):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip() or None
    zalo = (data.get("zalo") or "").strip() or None
    registered_at = (data.get("registered_at") or "").strip()

    if not name or not phone:
        return jsonify({"error": "Tên và số điện thoại là bắt buộc."}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT id, registered_at FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Không tìm thấy khách hàng."}), 404

    try:
        conn.execute(
            """
            UPDATE customers SET name = ?, phone = ?, email = ?, zalo = ?, registered_at = ?
            WHERE id = ?
            """,
            (name, phone, email, zalo, registered_at or existing["registered_at"], customer_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Số điện thoại đã tồn tại."}), 400
    conn.close()
    return jsonify(row_to_dict(row))


@app.delete("/api/customers/<int:customer_id>")
def delete_customer(customer_id: int):
    conn = get_db()
    order_count = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE customer_id = ?", (customer_id,)
    ).fetchone()["c"]
    if order_count:
        conn.close()
        return jsonify({"error": "Không thể xóa khách hàng đang có đơn hàng."}), 400

    cur = conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Không tìm thấy khách hàng."}), 404
    return jsonify({"ok": True})


# --- Orders ---


def fetch_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT o.*,
               c.name AS customer_name,
               c.phone AS customer_phone,
               p.name AS product_name,
               p.product_type AS product_type
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = o.product_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()


@app.get("/api/orders")
def list_orders():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT o.*,
               c.name AS customer_name,
               c.phone AS customer_phone,
               p.name AS product_name,
               p.product_type AS product_type
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = o.product_id
        ORDER BY o.id DESC
        """
    ).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.post("/api/orders")
def create_order():
    data = request.get_json(silent=True) or {}
    try:
        customer_id = int(data.get("customer_id"))
        product_id = int(data.get("product_id"))
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Dữ liệu đơn hàng không hợp lệ."}), 400

    status = (data.get("status") or "pending").strip()
    if status not in ("pending", "paid", "cancelled", "refunded", "completed"):
        return jsonify({"error": "Trạng thái đơn hàng không hợp lệ."}), 400

    purchased_at = (data.get("purchased_at") or "").strip() or None

    conn = get_db()
    customer = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    product = conn.execute(
        "SELECT id, product_type, stock_quantity FROM products WHERE id = ?", (product_id,)
    ).fetchone()

    if not customer:
        conn.close()
        return jsonify({"error": "Khách hàng không tồn tại."}), 400
    if not product:
        conn.close()
        return jsonify({"error": "Sản phẩm không tồn tại."}), 400

    if product["product_type"] == "physical":
        stock = product["stock_quantity"]
        if stock is None or stock < 1:
            conn.close()
            return jsonify({"error": "Sản phẩm vật lý đã hết hàng."}), 400
        conn.execute(
            "UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?",
            (product_id,),
        )

    try:
        if purchased_at:
            cur = conn.execute(
                """
                INSERT INTO orders (customer_id, product_id, amount, status, purchased_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (customer_id, product_id, amount, status, purchased_at),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO orders (customer_id, product_id, amount, status)
                VALUES (?, ?, ?, ?)
                """,
                (customer_id, product_id, amount, status),
            )
        conn.commit()
        row = fetch_order(conn, cur.lastrowid)
    except sqlite3.Error as exc:
        conn.rollback()
        conn.close()
        return jsonify({"error": str(exc)}), 400

    try:
        customer_row = conn.execute(
            "SELECT name, email FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if customer_row and customer_row["email"]:
            resend_mail.send_order_confirmation(
                to=customer_row["email"],
                name=customer_row["name"],
                order_id=cur.lastrowid,
                product_name=row["product_name"],
                amount=amount,
                status=status,
            )
        resend_mail.process_email_queue(conn)
    except Exception as exc:
        app.logger.warning("Order confirmation email failed: %s", exc)

    conn.close()
    return jsonify(row_to_dict(row)), 201


@app.put("/api/orders/<int:order_id>")
def update_order(order_id: int):
    data = request.get_json(silent=True) or {}
    try:
        customer_id = int(data.get("customer_id"))
        product_id = int(data.get("product_id"))
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Dữ liệu đơn hàng không hợp lệ."}), 400

    status = (data.get("status") or "pending").strip()
    purchased_at = (data.get("purchased_at") or "").strip() or None

    conn = get_db()
    existing = conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Không tìm thấy đơn hàng."}), 404

    conn.execute(
        """
        UPDATE orders
        SET customer_id = ?, product_id = ?, amount = ?, status = ?,
            purchased_at = COALESCE(?, purchased_at)
        WHERE id = ?
        """,
        (customer_id, product_id, amount, status, purchased_at, order_id),
    )
    conn.commit()
    row = fetch_order(conn, order_id)
    conn.close()
    return jsonify(row_to_dict(row))


@app.delete("/api/orders/<int:order_id>")
def delete_order(order_id: int):
    conn = get_db()
    order = conn.execute(
        """
        SELECT o.id, o.product_id, p.product_type
        FROM orders o
        JOIN products p ON p.id = o.product_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()

    if not order:
        conn.close()
        return jsonify({"error": "Không tìm thấy đơn hàng."}), 404

    if order["product_type"] == "physical":
        conn.execute(
            "UPDATE products SET stock_quantity = stock_quantity + 1 WHERE id = ?",
            (order["product_id"],),
        )

    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os as _os

    if not DB_PATH.exists():
        raise SystemExit(f"Không tìm thấy brain.db tại {DB_PATH}")
    port = int(_os.environ.get("PORT", "8080"))
    debug = _os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
