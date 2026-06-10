"""
AZM Store – Standalone Python server
Serves all /api/shop/* endpoints.
Run: python3 bot/server.py
"""
import os
import time
import sqlite3
import json
import threading
import requests as http_requests
from contextlib import contextmanager
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename

# ── paths ────────────────────────────────────────────────────────────────────
BOT_DIR    = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BOT_DIR, "products.db")

PORT = int(os.environ.get("PORT", 8082))

app = Flask(__name__, static_folder=None)

# ── DB helpers ───────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

@contextmanager
def get_db():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def rows_to_list(rows):
    return [dict(r) for r in rows]


UPLOADS_DIR = os.path.join(BOT_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

def ensure_columns():
    """Add optional columns if they don't exist yet."""
    with get_db() as conn:
        for stmt in [
            "ALTER TABLE sections ADD COLUMN image TEXT DEFAULT ''",
            "ALTER TABLE deposit_methods ADD COLUMN image TEXT DEFAULT ''",
            "ALTER TABLE products ADD COLUMN image TEXT DEFAULT ''",
            "ALTER TABLE products ADD COLUMN description TEXT DEFAULT ''",
            "ALTER TABLE categories ADD COLUMN player_id_label TEXT DEFAULT 'معرف اللاعب'",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        for key, default in [
            ("deposit_channel_id", ""),
            ("api_orders_channel_id", ""),
            ("store_api_url", "https://mhd-game.com/api"),
            ("store_api_token", ""),
            ("profit_margin", "0"),
            ("exchange_rate", "15000"),
            ("support_username", ""),
            ("bot_enabled", "1"),
            ("welcome_message", "مرحباً بك في AZM Store 🛒"),
        ]:
            try:
                existing = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                        (key, default)
                    )
            except Exception:
                pass


def init_db():
    """ينشئ جميع الجداول — مخطط مطابق لـ bot.py تماماً."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sections (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name  TEXT UNIQUE NOT NULL,
                color TEXT DEFAULT 'success',
                emoji TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL,
                emoji       TEXT DEFAULT '',
                description TEXT DEFAULT '',
                image       TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                name       TEXT NOT NULL,
                price      REAL NOT NULL,
                type       TEXT DEFAULT 'default',
                min_qty    INTEGER DEFAULT 1,
                max_qty    INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS deposit_methods (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                code          TEXT NOT NULL DEFAULT '',
                exchange_rate REAL NOT NULL DEFAULT 15000
            );
            CREATE TABLE IF NOT EXISTS settings (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                key   TEXT UNIQUE,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                balance          REAL    DEFAULT 0,
                is_admin         INTEGER DEFAULT 0,
                blocked          INTEGER DEFAULT 0,
                discount_name    TEXT    DEFAULT '',
                discount_percent REAL    DEFAULT 0,
                first_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                username         TEXT,
                full_name        TEXT,
                method_title     TEXT,
                amount_usd       REAL,
                amount_syp       REAL,
                transaction_code TEXT,
                status           TEXT DEFAULT 'pending',
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS linked_products (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id     INTEGER NOT NULL,
                category_id    INTEGER NOT NULL,
                api_product_id INTEGER NOT NULL,
                product_name   TEXT,
                category_name  TEXT,
                api_name       TEXT,
                api_price      REAL,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS api_orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                linked_id    INTEGER NOT NULL,
                order_id     TEXT,
                product_name TEXT,
                category_name TEXT,
                price        REAL,
                player_id    TEXT,
                qty          INTEGER DEFAULT 1,
                api_response TEXT,
                status       TEXT DEFAULT 'processing',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shop_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                product_name  TEXT,
                category_name TEXT,
                price         REAL,
                player_id     TEXT,
                qty           INTEGER DEFAULT 1,
                status        TEXT DEFAULT 'pending',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT,
                amount      REAL,
                description TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS category_description (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_name TEXT UNIQUE,
                description   TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_phones (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL UNIQUE,
                phone      TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id    INTEGER PRIMARY KEY,
                processed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sms_deposit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text   TEXT,
                amount     REAL,
                phone      TEXT,
                matched_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auto_credit_pending (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                phone      TEXT,
                amount     REAL,
                raw_text   TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # القيم الافتراضية للإعدادات
        for key, default in [
            ("deposit_channel_id",    ""),
            ("api_orders_channel_id", ""),
            ("store_api_url",         "https://mhd-game.com/api"),
            ("store_api_token",       ""),
            ("profit_margin",         "0"),
            ("exchange_rate",         "15000"),
            ("support_username",      "AZM1STORE"),
            ("bot_enabled",           "1"),
            ("welcome_message",       "مرحباً بك في AZM Store 🛒"),
            ("webapp_url",            ""),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, default)
            )


init_db()
ensure_columns()

# ── CORS + JSON error handler ─────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "not found"}), 404

@app.errorhandler(405)
def handle_405(e):
    return jsonify({"error": "method not allowed"}), 405

@app.route("/api/shop/<path:p>", methods=["OPTIONS"])
@app.route("/api/healthz", methods=["OPTIONS"])
def options_handler(**kwargs):
    return "", 204


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/api/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ── Image upload ──────────────────────────────────────────────────────────────
ALLOWED = {"jpg", "jpeg", "png", "gif", "webp", "svg"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED

@app.route("/api/shop/upload", methods=["POST"])
def upload_image():
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "no file"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "file type not allowed"}), 400
    ext      = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{int(time.time() * 1000)}.{ext}"
    file.save(os.path.join(UPLOADS_DIR, filename))
    url = f"/api/shop/uploads/{filename}"
    return jsonify({"url": url, "filename": filename})

@app.route("/api/shop/uploads/<filename>")
def serve_upload(filename):
    return send_from_directory(UPLOADS_DIR, filename)


# ══════════════════════════════════════════════════════════════════════════════
# SECTIONS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/sections")
def get_sections():
    show_all = request.args.get("all") == "1"
    with get_db() as conn:
        if show_all:
            rows = conn.execute("SELECT * FROM sections ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sections WHERE is_active = 1 ORDER BY id"
            ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/sections/create", methods=["POST"])
def create_section():
    body = request.get_json(force=True) or {}
    name = body.get("name")
    if not name:
        return jsonify({"error": "name required"}), 400
    color     = body.get("color", "success")
    emoji     = body.get("emoji", "")
    image     = body.get("image", "")
    is_active = body.get("is_active", 1)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (name, color, emoji, image, is_active) VALUES (?,?,?,?,?)",
            (name, color, emoji, image, is_active)
        )
        row = conn.execute("SELECT * FROM sections WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/shop/sections/<int:sid>", methods=["PATCH"])
def update_section(sid):
    body   = request.get_json(force=True) or {}
    fields, vals = [], []
    for col in ("name", "color", "emoji", "image", "is_active"):
        if col in body:
            fields.append(f"{col} = ?")
            vals.append(body[col])
    if not fields:
        return jsonify({"error": "no fields"}), 400
    vals.append(sid)
    with get_db() as conn:
        conn.execute(f"UPDATE sections SET {', '.join(fields)} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM sections WHERE id = ?", (sid,)).fetchone()
    return jsonify(dict(row) if row else {})


@app.route("/api/shop/sections/<int:sid>", methods=["DELETE"])
def delete_section(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM sections WHERE id = ?", (sid,))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/products")
def get_products():
    section = request.args.get("section")
    with get_db() as conn:
        if section:
            rows = conn.execute(
                "SELECT * FROM products WHERE category = ? ORDER BY id", (section,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/products/create", methods=["POST"])
def create_product():
    body = request.get_json(force=True) or {}
    name     = body.get("name")
    category = body.get("category")
    if not name or not category:
        return jsonify({"error": "name and category required"}), 400
    emoji       = body.get("emoji", "")
    description = body.get("description", "")
    image       = body.get("image", "")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, category, emoji, description, image) VALUES (?,?,?,?,?)",
            (name, category, emoji, description, image)
        )
        row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/shop/products/<int:pid>")
def get_product(pid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/api/shop/products/<int:pid>", methods=["PATCH"])
def update_product(pid):
    body = request.get_json(force=True) or {}
    fields, vals = [], []
    for col in ("name", "category", "emoji", "description", "image"):
        if col in body:
            fields.append(f"{col} = ?")
            vals.append(body[col])
    if not fields:
        return jsonify({"error": "no fields"}), 400
    vals.append(pid)
    with get_db() as conn:
        conn.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    return jsonify(dict(row) if row else {})


@app.route("/api/shop/products/<int:pid>", methods=["DELETE"])
def delete_product(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM categories WHERE product_id = ?", (pid,))
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORIES (product pricing tiers)
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/products/<int:pid>/categories")
def get_categories(pid):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM categories WHERE product_id = ? ORDER BY id", (pid,)
        ).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/products/<int:pid>/categories/create", methods=["POST"])
def create_category(pid):
    body = request.get_json(force=True) or {}
    name  = body.get("name")
    price = body.get("price")
    if name is None or price is None:
        return jsonify({"error": "name and price required"}), 400
    type_   = body.get("type", "default")
    min_qty = body.get("min_qty", 1)
    max_qty = body.get("max_qty", 1)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO categories (product_id, name, price, type, min_qty, max_qty) VALUES (?,?,?,?,?,?)",
            (pid, name, float(price), type_, min_qty, max_qty)
        )
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/shop/categories/<int:cid>", methods=["PATCH"])
def update_category(cid):
    body = request.get_json(force=True) or {}
    fields, vals = [], []
    for col in ("name", "price", "type", "min_qty", "max_qty", "player_id_label"):
        if col in body:
            fields.append(f"{col} = ?")
            vals.append(body[col])
    if not fields:
        return jsonify({"error": "no fields"}), 400
    vals.append(cid)
    with get_db() as conn:
        conn.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
    return jsonify(dict(row) if row else {})


@app.route("/api/shop/categories/<int:cid>", methods=["DELETE"])
def delete_category(cid):
    with get_db() as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# USER
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/user/<int:user_id>")
def get_user(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,)
            )
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return jsonify(dict(row) if row else {"user_id": user_id, "balance": 0})


# ══════════════════════════════════════════════════════════════════════════════
# ORDERS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/my-orders")
def get_my_orders():
    user_id = request.args.get("userId")
    status  = request.args.get("status", "all")
    if not user_id:
        return jsonify({"error": "userId required"}), 400
    sql    = "SELECT * FROM shop_orders WHERE user_id = ?"
    params = [int(user_id)]
    if status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    with get_db() as conn:
        orders = rows_to_list(conn.execute(sql, params).fetchall())
    total_amount    = sum((o.get("price") or 0) for o in orders)
    pending_count   = sum(1 for o in orders if o.get("status") == "pending")
    completed_count = sum(1 for o in orders if o.get("status") == "completed")
    return jsonify({
        "orders": orders,
        "total_count": len(orders),
        "total_amount": total_amount,
        "pending_count": pending_count,
        "completed_count": completed_count,
    })


@app.route("/api/shop/orders", methods=["POST"])
def create_order():
    body = request.get_json(force=True) or {}
    user_id     = body.get("user_id")
    category_id = body.get("category_id")
    player_id   = body.get("player_id")
    qty         = int(body.get("qty", 1))
    if not user_id or not category_id or not player_id:
        return jsonify({"error": "missing fields"}), 400
    with get_db() as conn:
        cat = conn.execute(
            "SELECT c.*, p.name as product_name FROM categories c "
            "JOIN products p ON p.id = c.product_id WHERE c.id = ?",
            (category_id,)
        ).fetchone()
        if not cat:
            return jsonify({"error": "category not found"}), 404
        total_price = float(cat["price"]) * qty
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user or float(user["balance"]) < total_price:
            return jsonify({"error": "رصيد غير كافٍ"}), 400
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (total_price, user_id)
        )
        cur = conn.execute(
            "INSERT INTO shop_orders (user_id, product_name, category_name, price, player_id, qty, status) "
            "VALUES (?,?,?,?,?,?,'pending')",
            (user_id, cat["product_name"], cat["name"], total_price, player_id, qty)
        )
        order = conn.execute(
            "SELECT * FROM shop_orders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify(dict(order)), 201


# ══════════════════════════════════════════════════════════════════════════════
# DEPOSITS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/my-deposits")
def get_my_deposits():
    user_id = request.args.get("userId")
    status  = request.args.get("status", "all")
    if not user_id:
        return jsonify({"error": "userId required"}), 400
    sql    = "SELECT * FROM deposit_requests WHERE user_id = ?"
    params = [int(user_id)]
    if status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    with get_db() as conn:
        deposits = rows_to_list(conn.execute(sql, params).fetchall())
    total_usd       = sum((d.get("amount_usd") or 0) for d in deposits if d.get("status") == "approved")
    pending_count   = sum(1 for d in deposits if d.get("status") == "pending")
    completed_count = sum(1 for d in deposits if d.get("status") == "approved")
    return jsonify({
        "deposits": deposits,
        "total_count": len(deposits),
        "total_amount_usd": total_usd,
        "pending_count": pending_count,
        "completed_count": completed_count,
    })


@app.route("/api/shop/deposits", methods=["POST"])
def create_deposit():
    body = request.get_json(force=True) or {}
    user_id          = body.get("user_id")
    method_id        = body.get("method_id")
    amount_syp       = body.get("amount_syp")
    transaction_code = body.get("transaction_code")
    if not user_id or not method_id or not amount_syp or not transaction_code:
        return jsonify({"error": "missing fields"}), 400
    with get_db() as conn:
        method = conn.execute(
            "SELECT * FROM deposit_methods WHERE id = ?", (method_id,)
        ).fetchone()
        if not method:
            return jsonify({"error": "method not found"}), 404
        amount_usd = float(amount_syp) / float(method["exchange_rate"])
        cur = conn.execute(
            "INSERT INTO deposit_requests "
            "(user_id, username, full_name, method_title, amount_usd, amount_syp, transaction_code, status) "
            "VALUES (?,?,?,?,?,?,?,'pending')",
            (user_id, body.get("username", ""), body.get("full_name", ""),
             method["title"], amount_usd, amount_syp, transaction_code)
        )
        deposit = conn.execute(
            "SELECT * FROM deposit_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify(dict(deposit)), 201


# ══════════════════════════════════════════════════════════════════════════════
# DEPOSIT METHODS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/deposit-methods")
def get_deposit_methods():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM deposit_methods ORDER BY id").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/deposit-methods/create", methods=["POST"])
def create_deposit_method():
    body = request.get_json(force=True) or {}
    title         = body.get("title")
    description   = body.get("description")
    code          = body.get("code")
    exchange_rate = body.get("exchange_rate")
    if not title or not description or not code or exchange_rate is None:
        return jsonify({"error": "missing fields"}), 400
    image = body.get("image", "")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO deposit_methods (title, description, code, exchange_rate, image) VALUES (?,?,?,?,?)",
            (title, description, code, exchange_rate, image)
        )
        row = conn.execute(
            "SELECT * FROM deposit_methods WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/shop/deposit-methods/<int:mid>", methods=["PATCH"])
def update_deposit_method(mid):
    body = request.get_json(force=True) or {}
    fields, vals = [], []
    for col in ("title", "description", "code", "exchange_rate", "image"):
        if col in body:
            fields.append(f"{col} = ?")
            vals.append(body[col])
    if not fields:
        return jsonify({"error": "no fields"}), 400
    vals.append(mid)
    with get_db() as conn:
        conn.execute(f"UPDATE deposit_methods SET {', '.join(fields)} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM deposit_methods WHERE id = ?", (mid,)).fetchone()
    return jsonify(dict(row) if row else {})


@app.route("/api/shop/deposit-methods/<int:mid>", methods=["DELETE"])
def delete_deposit_method(mid):
    with get_db() as conn:
        conn.execute("DELETE FROM deposit_methods WHERE id = ?", (mid,))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/settings")
def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    m = {r["key"]: r["value"] for r in rows}
    base, token = get_active_api(m)
    return jsonify({
        "welcome_message":       m.get("welcome_message", "⚡ أهلاً بك في بوت الشحن ⚡"),
        "support_username":      m.get("support_username", "support"),
        "exchange_rate":         float(m.get("exchange_rate", "15000") or "15000"),
        "profit_margin":         float(m.get("profit_margin", "0") or "0"),
        "bot_enabled":           m.get("bot_enabled") in ("true", "1", "True"),
        "deposit_channel_id":    m.get("deposit_channel_id", ""),
        "api_orders_channel_id": m.get("api_orders_channel_id", ""),
        "store_api_url":         base or m.get("store_api_url", "https://mhd-game.com/api"),
        "store_api_token":       token or m.get("store_api_token", ""),
    })


@app.route("/api/shop/settings", methods=["PATCH"])
def update_settings():
    body = request.get_json(force=True) or {}
    allowed = (
        "welcome_message", "support_username", "exchange_rate", "profit_margin",
        "bot_enabled", "deposit_channel_id", "api_orders_channel_id",
        "store_api_url", "store_api_token",
    )
    with get_db() as conn:
        for key in allowed:
            if key in body:
                val = body[key]
                if isinstance(val, bool):
                    val = "true" if val else "false"
                else:
                    val = str(val)
                existing = conn.execute(
                    "SELECT id FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if existing:
                    conn.execute("UPDATE settings SET value = ? WHERE key = ?", (val, key))
                else:
                    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — DEPOSITS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/admin/deposits")
def admin_deposits():
    status = request.args.get("status", "all")
    sql    = "SELECT * FROM deposit_requests"
    params = []
    if status and status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/admin/deposits/<int:dep_id>/approve", methods=["POST"])
def admin_approve_deposit(dep_id):
    with get_db() as conn:
        dep = conn.execute(
            "SELECT * FROM deposit_requests WHERE id = ?", (dep_id,)
        ).fetchone()
        if not dep:
            return jsonify({"error": "not found"}), 404
        conn.execute(
            "UPDATE deposit_requests SET status = 'approved' WHERE id = ?", (dep_id,)
        )
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (dep["amount_usd"], dep["user_id"])
        )
    return jsonify({"ok": True})


@app.route("/api/shop/admin/deposits/<int:dep_id>/reject", methods=["POST"])
def admin_reject_deposit(dep_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE deposit_requests SET status = 'rejected' WHERE id = ?", (dep_id,)
        )
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — USERS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/admin/users")
def admin_users():
    search = request.args.get("q", "")
    with get_db() as conn:
        if search:
            rows = conn.execute(
                "SELECT * FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ? ORDER BY first_seen DESC",
                (f"%{search}%", f"%{search}%", f"%{search}%")
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM users ORDER BY first_seen DESC").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/admin/users/<int:user_id>/add-balance", methods=["POST"])
def admin_add_balance(user_id):
    body   = request.get_json(force=True) or {}
    amount = body.get("amount")
    if amount is None:
        return jsonify({"error": "amount required"}), 400
    amount = float(amount)
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return jsonify(dict(row) if row else {"ok": True})


@app.route("/api/shop/admin/users/<int:user_id>/deduct-balance", methods=["POST"])
def admin_deduct_balance(user_id):
    body   = request.get_json(force=True) or {}
    amount = body.get("amount")
    if amount is None:
        return jsonify({"error": "amount required"}), 400
    amount = float(amount)
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE user_id = ?", (amount, user_id))
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return jsonify(dict(row) if row else {"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — STATS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/admin/stats")
def admin_stats():
    with get_db() as conn:
        total_users        = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_orders       = conn.execute("SELECT COUNT(*) FROM shop_orders").fetchone()[0]
        total_deposits_usd = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) FROM deposit_requests WHERE status='approved'"
        ).fetchone()[0]
        pending_deposits   = conn.execute(
            "SELECT COUNT(*) FROM deposit_requests WHERE status='pending'"
        ).fetchone()[0]
        total_products     = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        total_sections     = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    return jsonify({
        "total_users":        total_users,
        "total_orders":       total_orders,
        "total_deposits_usd": total_deposits_usd,
        "pending_deposits":   pending_deposits,
        "total_products":     total_products,
        "total_sections":     total_sections,
    })


# ══════════════════════════════════════════════════════════════════════════════
# LINKED PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

def ensure_linked_products_table():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS linked_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            api_product_id TEXT NOT NULL,
            product_name TEXT,
            category_name TEXT,
            api_name TEXT,
            api_price REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

ensure_linked_products_table()


@app.route("/api/shop/linked-products")
def get_linked_products():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM linked_products ORDER BY id DESC").fetchall()
    return jsonify(rows_to_list(rows))


@app.route("/api/shop/linked-products", methods=["POST"])
def create_linked_product():
    body = request.get_json(force=True) or {}
    product_id    = body.get("product_id")
    category_id   = body.get("category_id")
    api_product_id = str(body.get("api_product_id", "")).strip()
    api_name      = body.get("api_name", "")
    api_price     = float(body.get("api_price", 0) or 0)

    if not product_id or not category_id or not api_product_id:
        return jsonify({"error": "product_id, category_id, api_product_id required"}), 400

    with get_db() as conn:
        # Remove any existing link for this category
        conn.execute("DELETE FROM linked_products WHERE category_id = ?", (category_id,))
        _prod_row = conn.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
        prod_name = dict(_prod_row).get("name", "") if _prod_row else ""
        _cat_row  = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
        cat_name  = dict(_cat_row).get("name", "") if _cat_row else ""
        conn.execute(
            "INSERT INTO linked_products (product_id, category_id, api_product_id, product_name, category_name, api_name, api_price) VALUES (?,?,?,?,?,?,?)",
            (product_id, category_id, api_product_id, prod_name, cat_name, api_name, api_price)
        )
        row = conn.execute("SELECT * FROM linked_products WHERE category_id = ?", (category_id,)).fetchone()
    return jsonify(dict(row) if row else {"ok": True})


@app.route("/api/shop/linked-products/<int:link_id>", methods=["DELETE"])
def delete_linked_product(link_id):
    with get_db() as conn:
        conn.execute("DELETE FROM linked_products WHERE id = ?", (link_id,))
    return jsonify({"ok": True})


@app.route("/api/shop/categories/<int:cat_id>/linked")
def get_linked_by_category(cat_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM linked_products WHERE category_id = ?", (cat_id,)).fetchone()
    if row:
        return jsonify(dict(row))
    return jsonify(None)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — API PRODUCTS SEARCH
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/shop/admin/api-products")
def admin_api_products():
    q = request.args.get("q", "").strip().lower()
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    api_url   = settings.get("store_api_url", "")
    api_token = settings.get("store_api_token", "")
    if not api_url:
        return jsonify([])
    try:
        headers = {"api-token": api_token} if api_token else {}
        base_url = _normalize_base(api_url)
        resp = http_requests.get(build_endpoint(base_url, "products"), headers=headers, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        # extract list
        products = []
        if isinstance(data, list):
            products = [x for x in data if isinstance(x, dict)]
        elif isinstance(data, dict):
            for key in ("data", "products", "items", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    products = [x for x in v if isinstance(x, dict)]
                    break
        if q:
            products = [p for p in products if q in str(p.get("name", "")).lower() or q in str(p.get("product_name", "")).lower() or q in str(p.get("id", "")).lower()]
        return jsonify(products[:50])
    except Exception as e:
        return jsonify([])


# (duplicate /api/shop/my-orders and /api/shop/my-deposits removed — handled above)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — FETCH PRICES FROM EXTERNAL API
# ══════════════════════════════════════════════════════════════════════════════
def get_api_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

def _normalize_base(url: str) -> str:
    """Strip /client/api or /client suffix and trailing slashes."""
    url = url.rstrip("/")
    for suf in ("/client/api", "/client"):
        if url.lower().endswith(suf):
            url = url[:-len(suf)]
            break
    return url

def get_active_api(settings):
    """Return (base_url, token) using bot.py key conventions with server.py fallback."""
    source = settings.get("current_api_source", "source1")
    url    = settings.get(f"api_url_{source}", "") or settings.get("store_api_url", "")
    token  = settings.get(f"api_token_{source}", "") or settings.get("store_api_token", "")
    return _normalize_base(url), token

def build_endpoint(base: str, path: str) -> str:
    """Build: base/client/api/path — matches bot.py store_endpoint logic."""
    p = path.strip("/")
    if p.lower().startswith("client/api/"):
        p = p[len("client/api/"):]
    return f"{base}/client/api/{p}"

def api_headers(settings):
    _, token = get_active_api(settings)
    return {"api-token": token} if token else {}

def _fetch_json(url, hdrs, timeout=12):
    r = http_requests.get(url, headers=hdrs, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _extract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "products", "services", "items", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

@app.route("/api/shop/admin/fetch-prices", methods=["POST"])
def admin_fetch_prices():
    settings       = get_api_settings()
    base, token    = get_active_api(settings)
    if not base:
        return jsonify({"error": "store_api_url غير مضبوط في الإعدادات"}), 400
    hdrs = {"api-token": token} if token else {}
    for path in ("products", "services", "products/all"):
        url = build_endpoint(base, path)
        try:
            data = _fetch_json(url, hdrs, 20)
            return jsonify({"ok": True, "data": data, "endpoint": url})
        except Exception:
            pass
    return jsonify({"error": f"تعذّر جلب المنتجات من {base}/client/api/services"}), 502


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — API ACCOUNT INFO
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/admin/api-account")
def admin_api_account():
    settings    = get_api_settings()
    base, token = get_active_api(settings)
    if not base:
        return jsonify({"error": "store_api_url غير مضبوط"}), 400
    hdrs    = {"api-token": token} if token else {}
    result  = {}
    # Try profile endpoint (same as bot.py get_profile)
    for path in ("profile", "account", "balance", "user", "me"):
        url = build_endpoint(base, path)
        try:
            data = _fetch_json(url, hdrs, 8)
            result["balance_info"] = data
            result["endpoint"]     = url
            break
        except Exception:
            pass
    # Try products to verify connectivity
    try:
        prod_data = _fetch_json(build_endpoint(base, "products"), hdrs, 8)
        items     = _extract_list(prod_data)
        result["products_count"] = len(items)
        result["connected"]      = True
    except Exception as e:
        result["products_error"] = str(e)
    if result:
        return jsonify({"ok": True, "base_url": base, **result})
    return jsonify({"error": f"تعذّر الوصول لـ {base}/client/api — تأكد من الـ URL والتوكن"}), 502


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — LINKED PRODUCTS STATUS CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/shop/admin/linked-products-status")
def admin_linked_products_status():
    settings    = get_api_settings()
    base, token = get_active_api(settings)
    with get_db() as conn:
        linked = conn.execute("SELECT * FROM linked_products ORDER BY id DESC").fetchall()
    linked = rows_to_list(linked)
    if not base or not linked:
        return jsonify({"linked": linked, "api_products": {}})
    hdrs        = {"api-token": token} if token else {}
    api_products = {}
    for path in ("products", "products/all", "services"):
        url = build_endpoint(base, path)
        try:
            data  = _fetch_json(url, hdrs, 15)
            items = _extract_list(data)
            for item in items:
                pid = str(item.get("id") or item.get("product_id") or item.get("service_id") or "")
                if pid:
                    api_products[pid] = {
                        "name":      item.get("name") or item.get("product_name") or item.get("title", ""),
                        "price":     item.get("price") or item.get("rate") or 0,
                        "available": str(item.get("status", "active")).lower() not in ("disabled", "inactive", "unavailable", "0"),
                    }
            if api_products:
                break
        except Exception:
            pass
    return jsonify({"linked": linked, "api_products": api_products})


# ── static webapp (Mini App) ──────────────────────────────────────────────────
WEBAPP_BUILD = os.path.join(BOT_DIR, "webapp", "dist", "public")

@app.route("/shop/", defaults={"path": ""})
@app.route("/shop/<path:path>")
def serve_webapp(path):
    """Serve the React Mini App from /shop/."""
    full = os.path.join(WEBAPP_BUILD, path)
    if path and os.path.isfile(full):
        return send_from_directory(WEBAPP_BUILD, path)
    # SPA fallback — serve index.html for all unknown routes
    index = os.path.join(WEBAPP_BUILD, "index.html")
    if os.path.isfile(index):
        return send_file(index)
    return "Mini App not built yet. Run: cd bot/webapp && pnpm build", 503


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🚀 AZM Store server starting on port {PORT}")
    print(f"📁 DB: {os.path.abspath(DB_PATH)}")
    if os.path.isdir(WEBAPP_BUILD):
        print(f"🌐 Mini App: /shop/")
    else:
        print(f"⚠️  Mini App build not found at {WEBAPP_BUILD}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
