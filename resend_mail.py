"""Gửi email qua Resend API — đọc template từ email_sequence.md."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEQUENCE_PATH = ROOT / "email_sequence.md"
RESEND_CONFIG_PATH = ROOT / "resend_config.txt"

DEFAULT_FROM = "Hyundai Thành Công Quảng Bình <sales@laithuhyundai.online>"
PAYMENT_URL = "https://laithuhyundai.online/thanh-toan"


def load_api_key() -> str:
    key = os.getenv("RESEND_API_KEY", "").strip()
    if key:
        return key
    if RESEND_CONFIG_PATH.exists():
        for line in RESEND_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


def sender_address() -> str:
    addr = os.getenv("RESEND_FROM", DEFAULT_FROM).strip()
    return addr or DEFAULT_FROM


def is_test_mode(email: str) -> bool:
    local = email.split("@", 1)[0]
    return "+test" in local.lower()


def _parse_templates() -> dict[int, dict[str, str]]:
    """Parse email_sequence.md → {1: {subject, body}, ...}."""
    if not SEQUENCE_PATH.exists():
        return {}
    text = SEQUENCE_PATH.read_text(encoding="utf-8")
    blocks = re.split(r"\n## Email (\d+)", text)
    templates: dict[int, dict[str, str]] = {}
    i = 1
    while i < len(blocks):
        num = int(blocks[i])
        body_block = blocks[i + 1] if i + 1 < len(blocks) else ""
        subj_match = re.search(r"\*\*Subject:\*\*\s*(.+)", body_block)
        subject = subj_match.group(1).strip() if subj_match else f"Email {num}"
        parts = re.split(r"\n---\n", body_block, maxsplit=2)
        content = parts[-1].strip() if parts else body_block.strip()
        content = re.sub(r"^\*\*Subject:\*\*.*\n", "", content, flags=re.MULTILINE)
        content = re.sub(r"^\*\*Gửi:\*\*.*\n", "", content, flags=re.MULTILINE)
        templates[num] = {"subject": subject, "body": content.strip()}
        i += 2
    return templates


def render_template(text: str, ctx: dict[str, str]) -> str:
    out = text
    for key, val in ctx.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def body_to_html(body: str) -> str:
    lines = body.split("\n")
    parts: list[str] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            if not in_table:
                parts.append("<table style='border-collapse:collapse;margin:12px 0'>")
                in_table = True
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", " "} for c in cells):
                continue
            tag = "th" if not parts[-1].endswith("</tr>") and "table" in parts[-1] else "td"
            if tag == "th":
                parts.append("<tr>" + "".join(f"<th style='text-align:left;padding:4px 12px 4px 0'>{c}</th>" for c in cells) + "</tr>")
            else:
                parts.append("<tr>" + "".join(f"<td style='padding:4px 12px 4px 0'>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            parts.append("</table>")
            in_table = False
        if stripped.startswith("👉"):
            url_match = re.search(r"https?://\S+", stripped)
            if url_match:
                url = url_match.group(0)
                parts.append(f'<p><a href="{url}" style="color:#002c5f;font-weight:bold">{url}</a></p>')
                continue
        if stripped.startswith("http"):
            parts.append(f'<p><a href="{stripped}">{stripped}</a></p>')
        elif stripped:
            parts.append(f"<p>{stripped}</p>")
        else:
            parts.append("<br>")
    if in_table:
        parts.append("</table>")
    return (
        "<div style='font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#222;max-width:560px'>"
        + "".join(parts)
        + "</div>"
    )


def send_email(*, to: str, subject: str, html: str) -> dict:
    api_key = load_api_key()
    if not api_key:
        return {"ok": False, "error": "Chưa cấu hình RESEND_API_KEY hoặc resend_config.txt"}

    payload = {
        "from": sender_address(),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "id": data.get("id")}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": detail or str(exc)}


def ensure_email_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            recipient TEXT NOT NULL,
            email_number INTEGER NOT NULL,
            send_at TEXT NOT NULL,
            sent_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed')),
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _send_sequence_email(email_number: int, to: str, ctx: dict[str, str]) -> dict:
    templates = _parse_templates()
    tpl = templates.get(email_number)
    if not tpl:
        return {"ok": False, "error": f"Không tìm thấy template email {email_number}"}
    subject = render_template(tpl["subject"], ctx)
    body = render_template(tpl["body"], ctx)
    html = body_to_html(body)
    return send_email(to=to, subject=subject, html=html)


def send_welcome_email(to: str, name: str) -> dict:
    return _send_sequence_email(1, to, {"name": name})


def send_nurture_email(to: str, name: str) -> dict:
    return _send_sequence_email(2, to, {"name": name})


def send_close_email(to: str, name: str) -> dict:
    ctx = {"name": name}
    return _send_sequence_email(3, to, ctx)


def send_order_confirmation(
    *,
    to: str,
    name: str,
    order_id: int,
    product_name: str,
    amount: float,
    status: str,
) -> dict:
    status_labels = {
        "pending": "Chờ thanh toán",
        "paid": "Đã thanh toán",
        "cancelled": "Đã hủy",
        "refunded": "Đã hoàn tiền",
        "completed": "Hoàn tất",
    }
    amount_str = f"{int(amount):,}".replace(",", ".")
    ctx = {
        "name": name,
        "order_id": str(order_id),
        "product_name": product_name,
        "amount": amount_str,
        "status_label": status_labels.get(status, status),
    }
    return _send_sequence_email(4, to, ctx)


def queue_followup_emails(conn: sqlite3.Connection, customer_id: int, email: str) -> None:
    ensure_email_queue(conn)
    now = datetime.now(timezone.utc)
    schedule = [
        (2, now + timedelta(days=2)),
        (3, now + timedelta(days=3)),
    ]
    for email_number, send_at in schedule:
        conn.execute(
            """
            INSERT INTO email_queue (customer_id, recipient, email_number, send_at)
            VALUES (?, ?, ?, ?)
            """,
            (customer_id, email, email_number, send_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.commit()


def process_email_queue(conn: sqlite3.Connection) -> None:
    ensure_email_queue(conn)
    rows = conn.execute(
        """
        SELECT q.*, c.name AS customer_name
        FROM email_queue q
        LEFT JOIN customers c ON c.id = q.customer_id
        WHERE q.status = 'pending' AND q.send_at <= datetime('now')
        ORDER BY q.send_at ASC
        LIMIT 20
        """
    ).fetchall()
    for row in rows:
        name = (row["customer_name"] or "anh/chị").strip()
        senders = {2: send_nurture_email, 3: send_close_email}
        fn = senders.get(row["email_number"])
        if not fn:
            continue
        result = fn(row["recipient"], name)
        if result.get("ok"):
            conn.execute(
                "UPDATE email_queue SET status = 'sent', sent_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
        else:
            conn.execute(
                """
                UPDATE email_queue SET status = 'failed', error = ?, sent_at = datetime('now')
                WHERE id = ?
                """,
                (result.get("error", "unknown")[:500], row["id"]),
            )
        conn.commit()


def handle_waitlist_emails(conn: sqlite3.Connection, customer_id: int, name: str, email: str) -> None:
    """Email 1 ngay; +test → gửi cả 3; không test → hàng Email 2, 3."""
    if is_test_mode(email):
        for num, fn in [(1, send_welcome_email), (2, send_nurture_email), (3, send_close_email)]:
            fn(email, name)
        return

    send_welcome_email(email, name)
    queue_followup_emails(conn, customer_id, email)
