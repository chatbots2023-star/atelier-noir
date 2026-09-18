#!/usr/bin/env python3
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    os.environ.setdefault(key.strip(), value.strip())

TOKEN = os.environ.get("PUSHINPAY_TOKEN", "")
API = os.environ.get("PUSHINPAY_API", "https://api.pushinpay.com.br").rstrip("/")
TELEGRAM_URL = os.environ.get("TELEGRAM_URL", "https://t.me/dannyyof")
PORT = int(os.environ.get("PORT", "8000"))


def detect_ip():
    try:
        req = urllib.request.Request("https://api.ipify.org", headers={"Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return ""


SERVER_IP = detect_ip()

PLANS = {
    "mensal": {"label": "Acesso 30 dias", "cents": 1990},
    "vitalicio": {"label": "Acesso vitalicio", "cents": 4990},
}

LOCK = threading.Lock()
ORDERS = {}


def api_request(method, path, payload=None):
    data = None
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}, resp.status
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": raw or err.reason}
        return body, err.code


def webhook_url(handler):
    proto = handler.headers.get("X-Forwarded-Proto") or "https"
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "localhost"
    return proto + "://" + host + "/api/webhook"


def create_charge(plan_id, handler):
    plan = PLANS[plan_id]
    body, status = api_request(
        "POST",
        "/api/pix/cashIn",
        {
            "value": plan["cents"],
            "webhook_url": webhook_url(handler),
            "split_rules": [],
        },
    )
    if status != 200 or not isinstance(body, dict) or not body.get("id"):
        api_error = ""
        if isinstance(body, dict):
            api_error = str(body.get("error") or body.get("message") or "")
        message = api_error or "Falha ao gerar PIX"
        if status == 401:
            ip_hint = SERVER_IP or "o IP deste servidor"
            message = (
                "PushinPay recusou o acesso (IP nao configurado). "
                "No painel PushinPay, em Seguranca / White IP List, libere o IP %s."
                % ip_hint
            )
        return {"ok": False, "error": message, "status": status, "ip": SERVER_IP}, status

    qr_raw = str(body.get("qr_code_base64") or "")
    if "," in qr_raw and qr_raw.startswith("data:"):
        qr_raw = qr_raw.split(",", 1)[1]
    qr_bytes = b""
    if qr_raw:
        try:
            qr_bytes = base64.b64decode(qr_raw)
        except Exception:
            qr_bytes = b""

    order = {
        "id": body["id"],
        "status": body.get("status") or "created",
        "plan": plan_id,
        "label": plan["label"],
        "cents": plan["cents"],
        "qr_code": body.get("qr_code") or "",
        "qr_bytes": qr_bytes,
        "checked_at": 0,
        "paid_at": None,
    }
    with LOCK:
        ORDERS[order["id"]] = order
    return {
        "ok": True,
        "id": order["id"],
        "status": order["status"],
        "label": order["label"],
        "price": "R$ " + f"{plan['cents'] / 100:.2f}".replace(".", ","),
        "qr_code": order["qr_code"],
        "qr_url": "/api/qr/" + order["id"] + ".png",
        "telegram": TELEGRAM_URL,
    }, 200


def mark_paid(order_id):
    with LOCK:
        order = ORDERS.get(order_id)
        if not order:
            ORDERS[order_id] = {
                "id": order_id,
                "status": "paid",
                "paid_at": time.time(),
                "checked_at": time.time(),
            }
            return ORDERS[order_id]
        order["status"] = "paid"
        order["paid_at"] = time.time()
        order["checked_at"] = time.time()
        return order


def public_status(order):
    return {
        "ok": True,
        "id": order.get("id"),
        "status": order.get("status", "created"),
        "telegram": TELEGRAM_URL,
        "paid": order.get("status") == "paid",
    }


def refresh_status(order_id, force=False):
    with LOCK:
        order = ORDERS.get(order_id)
        if not order:
            return {"ok": False, "error": "Pedido nao encontrado"}, 404
        if order.get("status") == "paid":
            return public_status(order), 200
        last = order.get("checked_at") or 0
        snapshot = dict(order)

    now = time.time()
    if now - last < 60:
        return public_status(snapshot), 200

    body, status = api_request("GET", "/api/transactions/" + order_id)
    with LOCK:
        order = ORDERS.get(order_id, snapshot)
        order["checked_at"] = now
        if status == 200 and isinstance(body, dict):
            remote = (body.get("status") or "").lower()
            if remote == "paid":
                order["status"] = "paid"
                order["paid_at"] = now
        ORDERS[order_id] = order
        return public_status(order), 200


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload, code=200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            order_id = (q.get("id") or [""])[0]
            if not order_id:
                return self._json({"ok": False, "error": "id obrigatorio"}, 400)
            payload, code = refresh_status(order_id, force=False)
            return self._json(payload, code)
        if path.startswith("/api/qr/") and path.endswith(".png"):
            order_id = path[len("/api/qr/"):-4]
            with LOCK:
                order = ORDERS.get(order_id)
                png = order.get("qr_bytes") if order else b""
            if not png:
                self.send_error(404, "QR nao encontrado")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
            return
        if path == "/api/config":
            return self._json({"ok": True, "telegram": TELEGRAM_URL, "brand": "Luciana"})
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        data = self._read_json()
        if path == "/api/pix":
            plan_id = data.get("plan")
            if plan_id not in PLANS:
                return self._json({"ok": False, "error": "Plano invalido"}, 400)
            payload, code = create_charge(plan_id, self)
            return self._json(payload, code)
        if path == "/api/status":
            order_id = data.get("id")
            if not order_id:
                return self._json({"ok": False, "error": "id obrigatorio"}, 400)
            payload, code = refresh_status(order_id, force=True)
            return self._json(payload, code)
        if path == "/api/webhook":
            order_id = data.get("id") or data.get("transaction_id") or ""
            status = str(data.get("status") or "").lower()
            if order_id and status in ("paid", "approved", "pago"):
                mark_paid(order_id)
            elif order_id and isinstance(data, dict):
                nested = data.get("data") or data.get("transaction") or {}
                nest_status = str(nested.get("status") or "").lower()
                nest_id = nested.get("id") or order_id
                if nest_status in ("paid", "approved", "pago"):
                    mark_paid(nest_id)
            return self._json({"ok": True})
        self._json({"ok": False, "error": "Rota nao encontrada"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("PUSHINPAY_TOKEN ausente")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Luciana store on http://0.0.0.0:%s" % PORT, flush=True)
    server.serve_forever()
