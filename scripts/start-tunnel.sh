#!/bin/bash
# Mở URL public để SePay gọi webhook về máy local (port 8080).
# Chạy: bash scripts/start-tunnel.sh
# Giữ terminal này MỞ — tắt = webhook ngừng hoạt động.

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8080}"

echo "=============================================="
echo " Kiểm tra server Flask trên port $PORT..."
echo "=============================================="
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/thanh-toan"; then
  echo "❌ Server chưa chạy. Mở terminal khác và chạy:"
  echo "   cd \"$ROOT\" && python3 server.py"
  exit 1
fi
echo "✅ Server OK"

echo ""
echo "Đang tạo tunnel public (localtunnel)..."
echo "Đợi dòng: your url is: https://...."
echo ""

npx -y localtunnel --port "$PORT" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if [[ "$line" == *"your url is:"* ]]; then
    URL=$(echo "$line" | sed 's/.*your url is: //' | tr -d '[:space:]')
    WEBHOOK="${URL}/api/sepay/webhook"
    echo ""
    echo "=============================================="
    echo " COPY DÁN VÀO FORM SEPAY (Bước 1 — Cơ bản)"
    echo "=============================================="
    echo ""
    echo "Tên webhook:"
    echo "  Hyundai QR Payment"
    echo ""
    echo "URL nhận webhook:"
    echo "  $WEBHOOK"
    echo ""
    echo "Loại giao dịch:     Tiền vào"
    echo "Định dạng dữ liệu:  JSON (application/json)"
    echo "Tự động gửi lại:    BẬT (ON)"
    echo ""
    echo "=============================================="
    echo " Bước 2 — Tài khoản: tick VPBank đã liên kết"
    echo " Bước 3 — Bảo mật:   Không xác thực"
    echo " Bước 4 — Cảnh báo:  bỏ qua → Lưu"
    echo "=============================================="
    echo ""
    echo "Lưu vào .env (tùy chọn):"
    echo "  PUBLIC_WEBHOOK_URL=$WEBHOOK"
  fi
done
