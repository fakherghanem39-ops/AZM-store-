import requests
import json
import uuid
import time
import sqlite3
import traceback
import re
import threading
import os
import sys
try:
    import fcntl as _fcntl_module
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False
from datetime import datetime
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

# مسار مجلد البوت (يعمل بشكل صحيح بغض النظر عن مجلد الـ cwd)
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_BOT_DIR, "products.db")

# ============================================================
# إعدادات البوت الثابتة (معرّفة داخل الكود مباشرة)
# ============================================================
BOT_TOKEN = "8635803760:AAH_nTdI-8NTHVNxhIcT7kOwkmVzSczE0QI"

# ══════════════════════════════════════════════════════════
#  ← اكتب هنا دومين سيرفرك (مثال: mysite.com أو 1.2.3.4:8081)
#  هذا الإعداد يجعل زر "فتح متجر AZM store" يعمل بشكل صحيح
# ══════════════════════════════════════════════════════════
HOST_DOMAIN = ""   # ← ضع دومينك هنا

MAIN_ADMIN_ID = 6200238604
ADMIN_CHAT_ID = 6200238604

# الأدمنية الفرعية الافتراضية (يمكن حذفهم من قبل المدير الرئيسي)
SECONDARY_ADMINS = [7286288857, 6200238604]

# مصادر API متعددة: source1, source2, source3 — معرّفة هنا داخل الكود
DEFAULT_STORE_API_URLS = {
    'source1': "https://mhd-game.com/api",
}
DEFAULT_STORE_API_TOKENS = {
    'source1': "V32TvnIBmqcQ5XSkbmSl5KWal9MoKoej2Jescxd8Q2mlbaohYpfpJPpmlrGQ",
}
STORE_API_URLS = dict(DEFAULT_STORE_API_URLS)
STORE_API_TOKENS = dict(DEFAULT_STORE_API_TOKENS)
current_api_source = 'source1'

# للحفاظ على التوافق مع باقي الكود
API_TOKEN = ""
API_BASE_URL = ""
user_states = {}
last_update_id = 0
pending_orders = {}
_processed_callbacks = {}
_processed_callbacks_lock = threading.Lock()
_processed_messages = {}
_processed_messages_lock = threading.Lock()
admins = set([MAIN_ADMIN_ID])
bot_enabled = True
welcome_message = "⚡ أهلاً بك في بوت الشحن ⚡"
support_username = "AZM1STORE"
DEPOSIT_CHANNEL_ID = ""
API_ORDERS_CHANNEL_ID = ""

# ============================================================
# قناة تأكيدات الإيداع التلقائي
# تستخدم نفس إعداد SMS_FORWARDER_CHAT_ID (قابل للتعديل من لوحة الأدمن)
# لتجنّب ثبات معرّف القناة في الكود
# ============================================================
AUTO_CREDIT_CHANNEL_ID = "-1003847175748"  # قيمة افتراضية فقط — تُتجاوز ديناميكياً

# ============================================================
# قاموس الإيداعات المعلّقة
# key = رقم العملية (string)، value = {amount_syp, timestamp}
# ============================================================
pending_deposits = {}
pending_deposits_lock = threading.Lock()
DEPOSIT_EXPIRY_SECONDS = 4 * 3600  # 4 ساعات

@contextmanager
def get_db():
    conn = None
    for attempt in range(5):
        try:
            conn = sqlite3.connect(_DB_PATH, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
            break
        except sqlite3.OperationalError as e:
            if conn:
                conn.rollback()
                conn.close()
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.5)
                continue
            raise e
        except Exception as e:
            if conn:
                conn.rollback()
                conn.close()
            raise e
    else:
        if conn:
            conn.close()

def normalize_api_base(url: str) -> str:
    """
    تنظيف رابط الـ API ليصبح بصيغة موحدة (بدون شرطة في النهاية وبدون /client/api).
    يقبل المستخدم أي صيغة:
      - https://api.example.com
      - https://api.example.com/
      - https://api.example.com/client/api
      - https://api.example.com/client/api/
    والنتيجة دائماً: https://api.example.com
    """
    if not url:
        return ""
    u = url.strip()
    while u.endswith("/"):
        u = u[:-1]
    for suffix in ("/client/api", "/client"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)]
            break
    while u.endswith("/"):
        u = u[:-1]
    return u


def store_endpoint(api_url: str, path: str) -> str:
    """يبني رابط نقطة النهاية بشكل صحيح: base + /client/api/<path>."""
    base = normalize_api_base(api_url)
    p = (path or "").lstrip("/")
    if p.lower().startswith("client/api/"):
        p = p[len("client/api/"):]
    return f"{base}/client/api/{p}"


def load_api_settings():
    """يحمّل إعدادات مصادر API المتعددة من قاعدة البيانات."""
    global API_TOKEN, API_BASE_URL, STORE_API_URLS, STORE_API_TOKENS, current_api_source
    with get_db() as conn:
        c = conn.cursor()
        for src in ('source1',):
            c.execute("SELECT value FROM settings WHERE key = ?", (f'api_url_{src}',))
            r = c.fetchone()
            if r:
                STORE_API_URLS[src] = normalize_api_base(r[0] or "")
            else:
                default_url = normalize_api_base(DEFAULT_STORE_API_URLS.get(src, ""))
                STORE_API_URLS[src] = default_url
                c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (f'api_url_{src}', default_url))

            c.execute("SELECT value FROM settings WHERE key = ?", (f'api_token_{src}',))
            r = c.fetchone()
            if r:
                STORE_API_TOKENS[src] = (r[0] or "").strip()
            else:
                default_tok = (DEFAULT_STORE_API_TOKENS.get(src, "") or "").strip()
                STORE_API_TOKENS[src] = default_tok
                c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (f'api_token_{src}', default_tok))

        c.execute("SELECT value FROM settings WHERE key = 'current_api_source'")
        r = c.fetchone()
        if r and r[0] in STORE_API_URLS:
            current_api_source = r[0]
        else:
            current_api_source = 'source1'
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('current_api_source', ?)", (current_api_source,))

        # هجرة من النظام القديم (api_token / api_base_url) إن وُجد
        c.execute("SELECT value FROM settings WHERE key = 'api_token'")
        old_tok = c.fetchone()
        c.execute("SELECT value FROM settings WHERE key = 'api_base_url'")
        old_url = c.fetchone()
        if old_tok and old_tok[0] and not STORE_API_TOKENS.get('source1'):
            STORE_API_TOKENS['source1'] = old_tok[0].strip()
            c.execute("UPDATE settings SET value = ? WHERE key = 'api_token_source1'", (STORE_API_TOKENS['source1'],))
        if old_url and old_url[0] and not STORE_API_URLS.get('source1'):
            STORE_API_URLS['source1'] = normalize_api_base(old_url[0])
            c.execute("UPDATE settings SET value = ? WHERE key = 'api_url_source1'", (STORE_API_URLS['source1'],))

    # توافق مع الكود القديم
    API_BASE_URL = STORE_API_URLS.get(current_api_source, "")
    API_TOKEN = STORE_API_TOKENS.get(current_api_source, "")


def get_current_api_source():
    return current_api_source


def get_api_url(source=None):
    if source is None:
        source = current_api_source
    return normalize_api_base(STORE_API_URLS.get(source, "") or STORE_API_URLS.get('source1', ""))


def get_api_token(source=None):
    if source is None:
        source = current_api_source
    return (STORE_API_TOKENS.get(source) or STORE_API_TOKENS.get('source1', "") or "").strip()


def set_api_url_for(source, url):
    global API_BASE_URL
    if source not in STORE_API_URLS:
        return
    STORE_API_URLS[source] = normalize_api_base(url)
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f'api_url_{source}', STORE_API_URLS[source]))
    if source == current_api_source:
        API_BASE_URL = STORE_API_URLS[source]


def set_api_token_for(source, token):
    global API_TOKEN
    if source not in STORE_API_TOKENS:
        return
    STORE_API_TOKENS[source] = (token or "").strip()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f'api_token_{source}', STORE_API_TOKENS[source]))
    if source == current_api_source:
        API_TOKEN = STORE_API_TOKENS[source]


def set_current_api_source(source):
    global current_api_source, API_TOKEN, API_BASE_URL
    if source not in STORE_API_URLS:
        return False
    current_api_source = source
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('current_api_source', ?)", (source,))
    API_BASE_URL = STORE_API_URLS.get(source, "")
    API_TOKEN = STORE_API_TOKENS.get(source, "")
    return True


# دوال للحفاظ على التوافق مع الاستدعاءات القديمة
def set_api_token(token):
    set_api_token_for(current_api_source, token)


def set_api_base_url(url):
    set_api_url_for(current_api_source, url)

def init_db():
    global welcome_message
    with get_db() as conn:
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            emoji TEXT DEFAULT '',
            description TEXT DEFAULT '',
            image TEXT DEFAULT ''
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            type TEXT DEFAULT 'default',
            min_qty INTEGER DEFAULT 1,
            max_qty INTEGER DEFAULT 1
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT DEFAULT 'success',
            emoji TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS deposit_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            code TEXT NOT NULL,
            exchange_rate REAL NOT NULL
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            value TEXT
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            discount_name TEXT DEFAULT '',
            discount_percent REAL DEFAULT 0,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            method_title TEXT,
            amount_usd REAL,
            amount_syp REAL,
            transaction_code TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS linked_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            api_product_id INTEGER NOT NULL,
            product_name TEXT,
            category_name TEXT,
            api_name TEXT,
            api_price REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS api_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            linked_id INTEGER NOT NULL,
            order_id TEXT,
            product_name TEXT,
            category_name TEXT,
            price REAL,
            player_id TEXT,
            qty INTEGER DEFAULT 1,
            api_response TEXT,
            status TEXT DEFAULT 'processing',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS shop_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_name TEXT,
            category_name TEXT,
            price REAL,
            player_id TEXT,
            qty INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS user_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT,
            amount REAL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS category_description (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT UNIQUE,
            description TEXT DEFAULT ''
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT UNIQUE NOT NULL
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_phones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            phone TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS processed_updates (
            update_id INTEGER PRIMARY KEY,
            processed_at REAL NOT NULL
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS sms_deposit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            amount REAL,
            user_id INTEGER,
            status TEXT DEFAULT 'auto',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # جدول الإيداعات التلقائية المعلّقة (سيريتل كاش) — يحفظ الرسائل المُلتقَطة من القناة
        # ليتمكن البوت من التحقق منها بعد إعادة التشغيل أيضاً
        c.execute('''CREATE TABLE IF NOT EXISTS auto_credit_pending (
            op_number TEXT PRIMARY KEY,
            amount_syp REAL NOT NULL,
            timestamp REAL NOT NULL,
            consumed_by_user_id INTEGER DEFAULT NULL,
            consumed_at REAL DEFAULT NULL,
            channel_id TEXT,
            raw_text TEXT
        )''')
        
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('exchange_rate', '10000')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_enabled', 'true')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('welcome_message', '⚡ أهلاً بك في بوت الشحن ⚡')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('api_token', 'a6ee3450a4b8f6a89bd8b1d2712f38b877ecd24c00608a9c')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('api_base_url', 'https://api.sahl-cash.com/')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('support_username', 'AZM1STORE')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('deposit_channel_id', '')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('api_orders_channel_id', '')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('sms_forwarder_chat_id', '-1003847175748')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_price_interval', '60')")

        # ضمان وجود طريقة الإيداع التلقائي "سيريتل كاش تلقائي"
        c.execute("SELECT id FROM deposit_methods WHERE title = ?", ('سيريتل كاش تلقائي',))
        if not c.fetchone():
            c.execute(
                "INSERT INTO deposit_methods (title, description, code, exchange_rate) VALUES (?, ?, ?, ?)",
                ('سيريتل كاش تلقائي', 'إيداع تلقائي عبر سيريتل كاش', '0', 10000)
            )

        c.execute("INSERT OR IGNORE INTO users (user_id, is_admin, balance) VALUES (?, 1, 0)", (MAIN_ADMIN_ID,))
        # ضمان أن المدير الرئيسي يبقى دائماً ادمن حتى لو تغير
        c.execute("UPDATE users SET is_admin = 1, blocked = 0 WHERE user_id = ?", (MAIN_ADMIN_ID,))
        # إضافة الأدمنية الفرعية الافتراضية مع ضمان تفعيل صلاحية الأدمن حتى لو كان المستخدم موجوداً مسبقاً
        for sec_admin_id in SECONDARY_ADMINS:
            c.execute("INSERT OR IGNORE INTO users (user_id, is_admin, balance) VALUES (?, 1, 0)", (sec_admin_id,))
            c.execute("UPDATE users SET is_admin = 1, blocked = 0 WHERE user_id = ?", (sec_admin_id,))
        
        default_sections = [
            ('العاب', 'success', ''),
            ('تطبيقات', 'success', ''),
            ('بطاقات', 'success', '')
        ]
        for name, color, emoji in default_sections:
            c.execute("INSERT OR IGNORE INTO sections (name, color, emoji) VALUES (?, ?, ?)", (name, color, emoji))
            c.execute("INSERT OR IGNORE INTO category_description (category_name, description) VALUES (?, ?)", (name, f"🔶 قائمة {name} المتاحة:"))
    
    _ensure_required_channels_columns()
    load_api_settings()
    load_admins()
    load_bot_status()
    load_welcome_message()
    load_support_username()
    load_deposit_channel()
    load_api_orders_channel()
    load_sms_forwarder()

def load_welcome_message():
    global welcome_message
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'welcome_message'")
            result = c.fetchone()
            if result:
                welcome_message = result[0]
    except:
        pass

def set_welcome_message(msg):
    global welcome_message
    welcome_message = msg
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'welcome_message'", (msg,))

def load_support_username():
    global support_username
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'support_username'")
            result = c.fetchone()
            if result and result[0]:
                support_username = result[0]
    except:
        pass

def _get_support_username_from_db():
    """يقرأ يوزر الدعم مباشرة من قاعدة البيانات (يعكس أي تغيير من لوحة الإدارة فوراً)."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'support_username'")
            result = c.fetchone()
            if result and result[0]:
                return result[0].strip()
    except:
        pass
    return support_username or "AZM1STORE"

def _get_welcome_message_from_db():
    """يقرأ رسالة الترحيب مباشرة من قاعدة البيانات."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'welcome_message'")
            result = c.fetchone()
            if result and result[0]:
                return result[0]
    except:
        pass
    return welcome_message

def _get_bot_enabled_from_db():
    """يقرأ حالة البوت مباشرة من قاعدة البيانات."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'bot_enabled'")
            result = c.fetchone()
            if result:
                return result[0] in ("true", "1", "True")
    except:
        pass
    return bot_enabled

def _get_webapp_url_from_db():
    """يقرأ رابط المتجر من قاعدة البيانات (يمكن للأدمن ضبطه مباشرة من البوت)."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'webapp_url'")
            result = c.fetchone()
            if result and result[0] and result[0].strip():
                return result[0].strip()
    except:
        pass
    return ""

def _set_webapp_url_in_db(url: str):
    """يحفظ رابط المتجر في قاعدة البيانات."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('webapp_url', ?)", (url,))
    except:
        pass

def set_support_username(username):
    global support_username
    support_username = username
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'support_username'", (username,))

def load_deposit_channel():
    global DEPOSIT_CHANNEL_ID
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'deposit_channel_id'")
            result = c.fetchone()
            if result:
                DEPOSIT_CHANNEL_ID = result[0]
    except:
        pass

def set_deposit_channel(channel_id):
    global DEPOSIT_CHANNEL_ID
    DEPOSIT_CHANNEL_ID = channel_id
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'deposit_channel_id'", (channel_id,))

def load_api_orders_channel():
    global API_ORDERS_CHANNEL_ID
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'api_orders_channel_id'")
            result = c.fetchone()
            if result:
                API_ORDERS_CHANNEL_ID = result[0]
    except:
        pass

def set_api_orders_channel(channel_id):
    global API_ORDERS_CHANNEL_ID
    API_ORDERS_CHANNEL_ID = channel_id
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'api_orders_channel_id'", (channel_id,))

SMS_FORWARDER_CHAT_ID = "-1003847175748"
SYRIATEL_SENDER_NUMBER = "84693435"

def load_sms_forwarder():
    global SMS_FORWARDER_CHAT_ID
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'sms_forwarder_chat_id'")
            r = c.fetchone()
            if r and r[0]:
                SMS_FORWARDER_CHAT_ID = r[0]
    except Exception:
        pass

def set_sms_forwarder(chat_id_str):
    global SMS_FORWARDER_CHAT_ID
    SMS_FORWARDER_CHAT_ID = (chat_id_str or "").strip()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sms_forwarder_chat_id', ?)", (SMS_FORWARDER_CHAT_ID,))

def link_phone(user_id, phone):
    phone = phone.strip().lstrip("0")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_phones (user_id, phone) VALUES (?, ?)", (user_id, phone))

def get_user_by_phone(phone):
    phone = phone.strip().lstrip("0")
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM user_phones WHERE phone = ?", (phone,))
            r = c.fetchone()
            return r[0] if r else None
    except Exception:
        return None

def log_sms_deposit(phone, amount, user_id, status="auto"):
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO sms_deposit_log (phone, amount, user_id, status) VALUES (?, ?, ?, ?)",
                      (phone, amount, user_id, status))
    except Exception:
        pass

def claim_update(update_id):
    """يحجز update_id في قاعدة البيانات. يرجع True إذا نجح (غير مُعالَج سابقاً)، False إذا كان مكرراً."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            now = time.time()
            c.execute("DELETE FROM processed_updates WHERE processed_at < ?", (now - 86400,))
            c.execute("INSERT INTO processed_updates (update_id, processed_at) VALUES (?, ?)", (update_id, now))
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception as e:
        print(f"⚠️ claim_update error: {e}")
        return True


def parse_syriatel_deposit(message_text):
    if not message_text:
        return None
    text = message_text.replace("\u200f", "").replace("\u200e", "")
    patterns = [
        r"تم\s*ايداع\s*مبلغ\s*([\d,\.]+)\s*ل\.?س?.*?من\s*(?:الرقم|الحساب)?\s*0?(\d{8,15})",
        r"استلمت\s*([\d,\.]+)\s*ل\.?س?.*?من\s*0?(\d{8,15})",
        r"تم\s*تحويل\s*مبلغ\s*([\d,\.]+)\s*ل\.?س?.*?من\s*0?(\d{8,15})",
        r"received\s*([\d,\.]+)\s*SYP.*?from\s*0?(\d{8,15})",
        r"([\d,\.]+)\s*ل\.?س?.*?من\s*0?(\d{8,15})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                amount = float(m.group(1).replace(",", "").replace(".", ""))
                if amount < 100:
                    continue
                return {"amount": amount, "sender": m.group(2)}
            except Exception:
                continue
    return None


def try_handle_sms_forward(chat_id, text):
    if not SMS_FORWARDER_CHAT_ID:
        return False
    try:
        if str(chat_id) != str(SMS_FORWARDER_CHAT_ID):
            return False
    except Exception:
        return False
    deposit = parse_syriatel_deposit(text)
    if not deposit:
        print(f"⚠️ رسالة من قناة SMS غير معروفة الصيغة: {text[:100]}")
        return True
    rate = get_exchange_rate() or 10000
    amount_syp = int(deposit["amount"])
    phone = deposit["sender"].lstrip("0")
    # ===== حفظ الإيداع في قاموس الصلاحية =====
    with pending_deposits_lock:
        pending_deposits[phone] = {"amount": amount_syp, "timestamp": time.time()}
    print(f"[pending_deposits] تم حفظ إيداع: phone={phone} amount={amount_syp}")
    matched_user = get_user_by_phone(phone)
    if matched_user:
        add_user_balance(matched_user, amount_syp)
        log_sms_deposit(phone, amount_syp, matched_user, "auto")
        try:
            send_message(matched_user,
                f"✅ <b>تم إيداع مبلغك تلقائياً</b>\n"
                f"💰 المبلغ: <b>{amount_syp:,} ل.س</b>\n"
                f"📞 من الرقم: <code>0{phone}</code>\n"
                f"📊 رصيدك الآن: <b>{get_user_balance(matched_user):,.0f} ل.س</b>")
        except Exception:
            pass
        try:
            send_message(MAIN_ADMIN_ID,
                f"✅ <b>إيداع تلقائي ناجح</b>\n"
                f"💰 المبلغ: <b>{amount_syp:,} ل.س</b>\n"
                f"📞 الرقم: <code>0{phone}</code>\n"
                f"👤 المستخدم: <code>{matched_user}</code>")
        except Exception:
            pass
        return True
    amount_usd = amount_syp / rate if rate else 0
    admin_msg = (
        f"📩 <b>إيداع جديد عبر SMS</b>\n"
        f"💰 المبلغ: <b>{amount_syp:,} ل.س</b>\n"
        f"💵 ما يعادل: <b>{amount_usd:,.2f}$</b>\n"
        f"📞 من الرقم: <code>0{phone}</code>\n"
        f"⚠️ الرقم غير مسجل لأي مستخدم\n"
        f"اضغط الزر لإسناد المبلغ يدوياً:"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ إسناد لمستخدم", "callback_data": f"sms_credit_{amount_syp}"}],
        [{"text": "🗑 تجاهل", "callback_data": "sms_ignore"}]
    ]}
    log_sms_deposit(phone, amount_syp, None, "manual")
    try:
        send_message(MAIN_ADMIN_ID, admin_msg, reply_markup=keyboard)
    except Exception as e:
        print(f"⚠️ تعذر إرسال تنبيه SMS: {e}")
    return True


def parse_deposit_channel_message(text):
    """
    تحليل رسائل تأكيد الإيداع من قناة سيريتل كاش.
    تدعم صيغ متعددة وتتعامل مع الأحرف غير المرئية والأرقام العربية.
    تعيد {"amount_syp": float, "op_number": str} أو None
    """
    if not text:
        return None

    # 1. تنظيف النص: إزالة المحارف غير المرئية (U+200E, U+200F) والمسافات الزائدة
    cleaned = text.replace("\u200e", "").replace("\u200f", "").replace("\r", " ").replace("\n", " ").strip()

    # 2. تحويل الأرقام العربية-الهندية (٠-٩) إلى أرقام ASCII
    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = cleaned.translate(arabic_digits)

    # 3. قائمة الأنماط مرتبة من الأكثر تحديداً إلى الأقل
    patterns = [
        # الصيغة الأصلية (بالنقاط)
        r'تم\s+استلام\s+مبلغ\s+([\d,\.]+)\s+ل\.?س.*?رقم\s+العملية\s+هو\s+(\d+)',
        # بدون "هو"
        r'تم\s+استلام\s+مبلغ\s+([\d,\.]+)\s+ل\.?س.*?رقم\s+العملية\s*[:\-]?\s*(\d+)',
        # "تم ايداع" (شائعة جداً)
        r'تم\s+ايداع\s+مبلغ\s+([\d,\.]+)\s+ل\.?س.*?رقم\s+العملية\s*[:\-]?\s*(\d+)',
        r'تم\s+ايداع\s+([\d,\.]+)\s+ل\.?س.*?برقم\s+مرجعي?\s*[:\-]?\s*(\d+)',
        # "استلام" بدون "تم"
        r'استلام\s+مبلغ\s+([\d,\.]+)\s+ل\.?س.*?رقم\s+العملية\s*[:\-]?\s*(\d+)',
        # "تحويل"
        r'تم\s+تحويل\s+مبلغ\s+([\d,\.]+)\s+ل\.?س.*?رقم\s+العملية\s*[:\-]?\s*(\d+)',
        # نمط عام: أي جملة تحتوي على مبلغ + ل.س + رقم عملية (بأي صيغة) مع التقاط آخر رقم كبير
        r'([\d,\.]+)\s+ل\.?س.*?رقم\s+(?:العملية|مرجعي?)\s*[:\-]?\s*(\d{8,})',
    ]

    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                # إزالة الفواصل من المبلغ والتحويل إلى float
                amount_str = m.group(1).replace(",", "").replace(".", "")
                amount = float(amount_str)
                op_number = m.group(2).strip()
                if amount > 0 and op_number and op_number.isdigit():
                    return {"amount_syp": amount, "op_number": op_number}
            except Exception:
                continue
    return None


def handle_auto_credit_channel_post(cp_chat_id, cp_text):
    """
    يستخرج رقم العملية والمبلغ من كل رسالة تصل إلى قناة الإيداع التلقائي حصراً.
    قناة الإيداع التلقائي: AUTO_CREDIT_CHANNEL_ID فقط.
    رسائل القناة اليدوية تُتجاهَل تماماً (مخصصة لإشعارات الأدمن فقط).
    """
    if str(cp_chat_id) != str(AUTO_CREDIT_CHANNEL_ID):
        print(f"[auto-credit] ⚠️ تجاهل رسالة من قناة غير الإيداع التلقائي: chat_id={cp_chat_id} (المتوقع: {AUTO_CREDIT_CHANNEL_ID})")
        return False
    parsed = parse_deposit_channel_message(cp_text)
    if not parsed:
        print(f"[auto-credit] ❌ رسالة غير مطابقة لصيغة سيريتل كاش: {cp_text[:200]}")
        return True
    print(f"[auto-credit] 📥 استخراج ناجح من القناة التلقائية: {cp_text[:120]}")
    op_number = parsed["op_number"]
    amount_syp = parsed["amount_syp"]
    now = time.time()
    with pending_deposits_lock:
        pending_deposits[op_number] = {
            "amount_syp": amount_syp,
            "timestamp": now
        }
    # حفظ في قاعدة البيانات أيضاً (يبقى بعد إعادة التشغيل)
    try:
        with get_db() as _conn:
            _c = _conn.cursor()
            _c.execute(
                "INSERT OR REPLACE INTO auto_credit_pending "
                "(op_number, amount_syp, timestamp, channel_id, raw_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (op_number, amount_syp, now, str(cp_chat_id), cp_text[:500])
            )
    except Exception as e:
        print(f"[auto-credit] ⚠️ تعذر الحفظ في القاعدة: {e}")
    print(f"[auto-credit] ✅ تم تخزين عملية: رقم={op_number} مبلغ={amount_syp} ل.س")
    return True


def get_pending_auto_deposit(op_number):
    """يبحث عن عملية إيداع تلقائي بإستخدام الذاكرة أولاً ثم قاعدة البيانات."""
    with pending_deposits_lock:
        entry = pending_deposits.get(op_number)
    if entry is not None:
        return {"amount_syp": entry["amount_syp"], "timestamp": entry["timestamp"], "source": "memory"}
    try:
        with get_db() as _conn:
            _c = _conn.cursor()
            _c.execute(
                "SELECT amount_syp, timestamp, consumed_by_user_id FROM auto_credit_pending WHERE op_number = ?",
                (op_number,)
            )
            row = _c.fetchone()
            if row:
                amount_syp, ts, consumed = row
                if consumed is not None:
                    return {"consumed": True, "consumed_by": consumed}
                return {"amount_syp": amount_syp, "timestamp": ts, "source": "db"}
    except Exception as e:
        print(f"[auto-credit] ⚠️ خطأ في قراءة القاعدة: {e}")
    return None


def consume_pending_auto_deposit(op_number, user_id):
    """يحدد العملية كمستهلكة في قاعدة البيانات وفي الذاكرة."""
    with pending_deposits_lock:
        pending_deposits.pop(op_number, None)
    try:
        with get_db() as _conn:
            _c = _conn.cursor()
            _c.execute(
                "UPDATE auto_credit_pending SET consumed_by_user_id = ?, consumed_at = ? WHERE op_number = ?",
                (user_id, time.time(), op_number)
            )
    except Exception as e:
        print(f"[auto-credit] ⚠️ تعذر تحديث استهلاك العملية: {e}")


def _cleanup_pending_deposits():
    """مهمة خلفية: تحذف كل 5 دقائق إيداعات سيريتل المنتهية الصلاحية (أقدم من 4 ساعات)."""
    while True:
        try:
            time.sleep(300)
            now = time.time()
            with pending_deposits_lock:
                expired_keys = [k for k, v in list(pending_deposits.items())
                                if now - v["timestamp"] > DEPOSIT_EXPIRY_SECONDS]
                for k in expired_keys:
                    del pending_deposits[k]
                    print(f"[cleanup] إيداع منتهي الصلاحية حُذف: phone={k}")
        except Exception as e:
            print(f"[cleanup] خطأ في التنظيف: {e}")


threading.Thread(target=_cleanup_pending_deposits, daemon=True).start()


def load_bot_status():
    global bot_enabled
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'bot_enabled'")
            result = c.fetchone()
            if result:
                bot_enabled = result[0].lower() == 'true'
    except:
        pass

def set_bot_status(enabled):
    global bot_enabled
    bot_enabled = enabled
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'bot_enabled'", ('true' if enabled else 'false',))

def load_admins():
    global admins
    admins = set([MAIN_ADMIN_ID])
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE is_admin = 1")
            results = c.fetchall()
            for row in results:
                admins.add(row[0])
    except:
        pass

def is_admin(user_id):
    return user_id in admins or user_id == MAIN_ADMIN_ID

def add_admin(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
        if c.rowcount == 0:
            c.execute("INSERT INTO users (user_id, is_admin, balance) VALUES (?, 1, 0)", (user_id,))
    load_admins()

def remove_admin(user_id):
    if user_id == MAIN_ADMIN_ID:
        return False
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
    load_admins()
    return True

def add_user_balance(user_id, amount):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        c.execute("INSERT INTO user_transactions (user_id, type, amount, description) VALUES (?, 'add', ?, 'تمت الإضافة بواسطة الأدمن')", (user_id, amount))

def deduct_user_balance(user_id, amount):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
        c.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE user_id = ?", (amount, user_id))
        c.execute("INSERT INTO user_transactions (user_id, type, amount, description) VALUES (?, 'deduct', ?, 'تم الخصم بواسطة الأدمن')", (user_id, amount))

def get_user_total_spent(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(price), 0) FROM shop_orders WHERE user_id = ? AND status = 'accepted'", (user_id,))
        total_shop = c.fetchone()[0]
        
        c.execute("SELECT COALESCE(SUM(amount_syp), 0) FROM deposit_requests WHERE user_id = ? AND status = 'accepted'", (user_id,))
        total_deposit = c.fetchone()[0]
        
        return total_shop

def get_user_discount(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT discount_name, discount_percent FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result and result[1] > 0:
            return result[0], result[1]
    return "", 0

def set_user_discount(user_id, discount_name, discount_percent):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET discount_name = ?, discount_percent = ? WHERE user_id = ?", (discount_name, discount_percent, user_id))
        if c.rowcount == 0:
            c.execute("INSERT INTO users (user_id, discount_name, discount_percent) VALUES (?, ?, ?)", (user_id, discount_name, discount_percent))

def remove_user_discount(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET discount_name = '', discount_percent = 0 WHERE user_id = ?", (user_id,))

def get_all_users_with_discount():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, discount_name, discount_percent FROM users WHERE discount_percent > 0")
        results = c.fetchall()
    return results

def get_user_stats(user_id):
    with get_db() as conn:
        c = conn.cursor()
        
        c.execute("SELECT COALESCE(SUM(price), 0) FROM shop_orders WHERE user_id = ? AND status = 'accepted'", (user_id,))
        total_shop = c.fetchone()[0]
        
        c.execute("SELECT COALESCE(SUM(amount_syp), 0) FROM deposit_requests WHERE user_id = ? AND status = 'accepted'", (user_id,))
        total_deposit = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM shop_orders WHERE user_id = ?", (user_id,))
        total_orders = c.fetchone()[0]
        
        c.execute("SELECT amount_syp, method_title, created_at FROM deposit_requests WHERE user_id = ? AND status = 'accepted' ORDER BY created_at DESC LIMIT 3", (user_id,))
        last_deposits = c.fetchall()
        
        c.execute("SELECT product_name, category_name, price, created_at FROM shop_orders WHERE user_id = ? AND status = 'accepted' ORDER BY created_at DESC LIMIT 3", (user_id,))
        last_products = c.fetchall()
        
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance_row = c.fetchone()
        balance = balance_row[0] if balance_row else 0
        
        c.execute("SELECT discount_name, discount_percent FROM users WHERE user_id = ?", (user_id,))
        discount_row = c.fetchone()
        discount_name = discount_row[0] if discount_row and discount_row[0] else ""
        discount_percent = discount_row[1] if discount_row else 0
    
    return {
        "total_shop": total_shop,
        "total_deposit": total_deposit,
        "total_orders": total_orders,
        "last_deposits": last_deposits,
        "last_products": last_products,
        "balance": balance,
        "discount_name": discount_name,
        "discount_percent": discount_percent
    }

def block_user(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, blocked) VALUES (?, 1)", (user_id,))

def unblock_user(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET blocked = 0 WHERE user_id = ?", (user_id,))

def is_blocked(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT blocked FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        return result and result[0] == 1

def get_exchange_rate():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'exchange_rate'")
        result = c.fetchone()
        if result:
            return float(result[0])
    return 10000.0

def set_exchange_rate(rate):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE settings SET value = ? WHERE key = 'exchange_rate'", (str(rate),))

def get_user_balance(user_id):
    if is_blocked(user_id):
        return 0
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result:
            return result[0]
    return 0.0

def update_user_balance(user_id, amount):
    if is_blocked(user_id):
        return get_user_balance(user_id)
    with get_db() as conn:
        c = conn.cursor()
        current = get_user_balance(user_id)
        new_balance = max(0, current + amount)
        c.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, ?)", (user_id, new_balance))
    return new_balance

def add_deposit_request(user_id, username, full_name, method_title, amount_usd, amount_syp, transaction_code):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO deposit_requests 
            (user_id, username, full_name, method_title, amount_usd, amount_syp, transaction_code, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')''',
            (user_id, username, full_name, method_title, amount_usd, amount_syp, transaction_code))
        request_id = c.lastrowid
    return request_id

def accept_deposit_request(request_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, amount_syp FROM deposit_requests WHERE id = ? AND status = 'pending'", (request_id,))
        result = c.fetchone()
        if result:
            user_id, amount_syp = result
            c.execute("UPDATE deposit_requests SET status = 'accepted' WHERE id = ?", (request_id,))
            current_balance = get_user_balance(user_id)
            new_balance = current_balance + amount_syp
            c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
            c.execute("INSERT INTO user_transactions (user_id, type, amount, description) VALUES (?, 'deposit', ?, 'تم ايداع عن طريق البوت')", (user_id, amount_syp))
            return True, user_id, amount_syp
    return False, None, None

def reject_deposit_request(request_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE deposit_requests SET status = 'rejected' WHERE id = ?", (request_id,))

def link_product(product_id, category_id, api_product_id, api_name, api_price):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO linked_products (product_id, category_id, api_product_id, product_name, category_name, api_name, api_price) 
            VALUES (?, ?, ?, (SELECT name FROM products WHERE id = ?), (SELECT name FROM categories WHERE id = ?), ?, ?)''',
            (product_id, category_id, api_product_id, product_id, category_id, api_name, api_price))
        linked_id = c.lastrowid
    return linked_id

def unlink_product(linked_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM linked_products WHERE id = ?", (linked_id,))

def get_linked_products():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM linked_products")
        results = c.fetchall()
    return results

def get_linked_by_category(category_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM linked_products WHERE category_id = ?", (category_id,))
        result = c.fetchone()
    return result

def _extract_products_list(result):
    """يستخرج قائمة المنتجات من رد المزود مهما كان شكله."""
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        # شكل {"status":"OK","data":[...]}
        data = result.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        # شكل {"status":"OK","data":{"products":[...]}}
        if isinstance(data, dict):
            for key in ("products", "items", "list", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            # قاموس مفهرس بالـ id → نأخذ القيم
            vals = [v for v in data.values() if isinstance(v, dict)]
            if vals:
                return vals
        # أحياناً يرجع products في الجذر مباشرة
        for key in ("products", "items", "list", "results"):
            v = result.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def search_api_products(search_term):
    result = api_request("client/api/products")
    products = _extract_products_list(result)
    if not products:
        print(f"[search_api_products] لم يتم استخراج منتجات. raw={result}")
        return []
    term = (search_term or "").strip().lower()
    if not term:
        return products
    matches = []
    for item in products:
        # نبحث في عدة حقول محتملة للاسم
        name_fields = [
            str(item.get("name", "")),
            str(item.get("product_name", "")),
            str(item.get("title", "")),
            str(item.get("category", "")),
            str(item.get("category_name", "")),
        ]
        haystack = " ".join(name_fields).lower()
        if term in haystack:
            matches.append(item)
    return matches


def get_api_product_by_id(api_product_id):
    result = api_request(f"client/api/products?products_id={api_product_id}")
    products = _extract_products_list(result)
    # نحاول إيجاد المطابق بالـ id إن أرجع المزود أكثر من واحد
    for p in products:
        pid = str(p.get("id") or p.get("product_id") or p.get("products_id") or "")
        if pid and pid == str(api_product_id):
            return p
    return products[0] if products else None

def get_auto_price_interval():
    """يرجع الفترة الزمنية لتحديث الأسعار تلقائياً بالدقائق (افتراضي 60)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'auto_price_interval'")
        row = c.fetchone()
    try:
        return max(1, int(float(row[0]))) if row else 60
    except Exception:
        return 60

def set_auto_price_interval(minutes):
    """يضبط الفترة الزمنية لتحديث الأسعار تلقائياً بالدقائق."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_price_interval', ?)", (str(int(minutes)),))


def refresh_linked_prices_from_api():
    """يجلب أسعار المنتجات المرتبطة فقط من API ويحدّثها في قاعدة البيانات.
    يدعم فئات العداد (counter) والفئات الثابتة (default/limited).
    - العداد: يحفظ السعر بدقة Decimal كاملة دون أي تقريب أو حذف.
    - الثابت: يحفظ السعر بدقة Decimal كاملة.
    يطبّق نسبة الربح العامة (profit_margin) من الإعدادات."""
    from decimal import Decimal as _D, InvalidOperation

    linked = get_linked_products()
    if not linked:
        return []

    # جلب نسبة الربح العامة من الإعدادات
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'profit_margin'")
        row = c.fetchone()
        margin_percent = _D(str(row[0])) if row else _D('0')

    changed = []
    for link in linked:
        # (id, product_id, category_id, api_product_id, product_name, category_name, api_name, api_price, ...)
        link_id        = link[0]
        category_id    = link[2]
        api_product_id = link[3]
        api_name       = link[6] if len(link) > 6 else str(api_product_id)

        try:
            product_data = get_api_product_by_id(api_product_id)
            if not product_data:
                changed.append(f"⚠️ {api_name}: لم يُعثر عليه في API")
                continue

            # نأخذ السعر كنص مباشرة للحفاظ على الدقة الكاملة
            raw_price = (
                product_data.get('price') or
                product_data.get('rate') or
                product_data.get('cost') or
                '0'
            )
            try:
                api_price_dec = _D(str(raw_price))
            except InvalidOperation:
                changed.append(f"⚠️ {api_name}: سعر غير صالح ({raw_price})")
                continue

            if api_price_dec <= 0:
                changed.append(f"⚠️ {api_name}: السعر = 0 أو غير متوفر")
                continue

            # تطبيق نسبة الربح بدقة Decimal كاملة
            final_dec = api_price_dec * (_D('1') + margin_percent / _D('100'))

            # جلب نوع الفئة (counter أو default/limited)
            category = get_category_by_id(category_id)
            if not category:
                changed.append(f"⚠️ {api_name}: الفئة غير موجودة في DB")
                continue
            # category = (id, product_id, name, price, type, min_qty, max_qty)
            cat_type = category[4] if len(category) > 4 else 'default'
            cat_name = category[2]

            # تحويل للتخزين — للعداد نحفظ دقة كاملة عبر float64 (≈17 خانة)
            # SQLite REAL = IEEE 754 double يحفظ 0.8989766767 بدقة كاملة
            final_float      = float(final_dec)
            api_price_float  = float(api_price_dec)

            with get_db() as conn:
                c = conn.cursor()
                c.execute("UPDATE categories SET price = ? WHERE id = ?", (final_float, category_id))
                c.execute("UPDATE linked_products SET api_price = ? WHERE id = ?", (api_price_float, link_id))

            type_label = "عداد" if cat_type == 'counter' else "ثابت"
            # نعرض السعر كما هو بدون تقريب — repr يُظهر الدقة الكاملة
            final_str    = format(final_dec, 'f').rstrip('0').rstrip('.')
            api_str      = format(api_price_dec, 'f').rstrip('0').rstrip('.')
            margin_str   = format(margin_percent, 'f').rstrip('0').rstrip('.')
            changed.append(
                f"✅ {api_name} [{cat_name}] [{type_label}]: "
                f"{api_str}$ → {final_str}$ (+{margin_str}%)"
            )

        except Exception as e:
            changed.append(f"⚠️ {api_name or api_product_id}: خطأ — {str(e)[:60]}")
            continue

    return changed


def save_api_order(user_id, linked_id, order_id, product_name, category_name, price, player_id, qty, api_response):
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO api_orders (user_id, linked_id, order_id, product_name, category_name, price, player_id, qty, api_response, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing')''',
            (user_id, linked_id, order_id, product_name, category_name, price, player_id, qty, api_response))
        order_db_id = c.lastrowid
    return order_db_id

def update_api_order_status(order_db_id, status, api_response=None):
    with get_db() as conn:
        c = conn.cursor()
        if api_response:
            c.execute("UPDATE api_orders SET status = ?, api_response = ?, completed_at = ? WHERE id = ?", 
                      (status, api_response, datetime.now(), order_db_id))
        else:
            c.execute("UPDATE api_orders SET status = ?, completed_at = ? WHERE id = ?", 
                      (status, datetime.now(), order_db_id))

def get_api_order_by_id(order_db_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM api_orders WHERE id = ?", (order_db_id,))
        result = c.fetchone()
    return result

def get_pending_api_orders():
    """يرجع كل طلبات الـ API التي لم تكتمل بعد (status='processing')."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM api_orders WHERE status = 'processing'")
        return c.fetchall()


def check_order_status(order_id):
    """
    التحقق من حالة الطلب من الـ API.
    يدعم استجابات متعددة الأشكال:
      - {"status":"OK","data":[ {...} ]}
      - {"status":"OK","data":{ "ORDER_ID": {...} }}
      - {"status":"OK","data":{...}}  (طلب واحد كقاموس مباشر)
      - status بأي صيغة (OK / success / 200 / true)
    يرجع dict يحوي بيانات الطلب، أو None لو لم يتم العثور عليه.
    """
    if not order_id:
        return None
    try:
        # المزود يتطلب الصيغة: orders=[ORDER_ID]&uuid=1 (مع الأقواس المربعة)
        from urllib.parse import quote
        encoded = quote(f"[{order_id}]", safe="")
        result = api_request(f"client/api/check?orders={encoded}&uuid=1")
    except Exception as e:
        print(f"[check_order_status] خطأ في الطلب: {e}")
        return None

    print(f"[check_order_status] order_id={order_id} raw={result}")

    if not isinstance(result, dict):
        return None

    # نقبل عدة قيم لحقل status
    status_field = result.get("status")
    is_ok = (
        status_field in ("OK", "ok", "success", "Success", True, 200, "200")
        or result.get("success") is True
    )
    # حتى لو لم يأتِ status، نحاول استخراج data
    data = result.get("data")
    if data is None:
        # بعض المزودين يضعون الطلب مباشرة في الجذر
        if any(k in result for k in ("order_id", "status", "order_status", "state")) and is_ok:
            return result
        return None

    # data كقائمة
    if isinstance(data, list):
        if not data:
            return None
        # ابحث عن المطابق إن كان order_id في عناصرها
        for item in data:
            if isinstance(item, dict):
                oid = str(item.get("order_id") or item.get("id") or "")
                if oid and oid == str(order_id):
                    return item
        # إن لم نجد مطابق، نُرجع أول عنصر
        first = data[0]
        return first if isinstance(first, dict) else None

    # data كقاموس
    if isinstance(data, dict):
        # هل القاموس مفهرس بالـ order_id؟
        if str(order_id) in data and isinstance(data[str(order_id)], dict):
            return data[str(order_id)]
        # هل هو طلب واحد مباشر؟
        if any(k in data for k in ("order_id", "status", "order_status", "state", "replay_api")):
            return data
        # خذ أول قيمة قاموس متاحة
        for v in data.values():
            if isinstance(v, dict):
                return v
        return None

    return None


# مجموعات حالات الطلب من المزود
_ACCEPT_STATUSES = {
    "accept", "accepted", "completed", "complete", "success", "successful",
    "done", "delivered", "ok", "approved", "finished",
}
_REJECT_STATUSES = {
    "reject", "rejected", "cancel", "cancelled", "canceled",
    "failed", "fail", "error", "refunded", "declined",
}


def _classify_order_status(raw_status):
    """يصنّف حالة الطلب من المزود إلى accept / reject / pending."""
    if raw_status is None:
        return "pending"
    s = str(raw_status).strip().lower()
    if s in _ACCEPT_STATUSES:
        return "accept"
    if s in _REJECT_STATUSES:
        return "reject"
    return "pending"

def buy_from_api(api_product_id, player_id, qty=1):
    order_uuid = str(uuid.uuid4())
    endpoint = f"client/api/newOrder/{api_product_id}/params?qty={qty}&playerId={player_id}&order_uuid={order_uuid}"
    result = api_request(endpoint)
    return result

def monitor_api_order(order_db_id, user_id, chat_id, start_time, price_syp):
    """مراقبة حالة الطلب - تتابع حتى تظهر accept أو reject بدون حد زمني"""
    try:
        return _monitor_api_order_impl(order_db_id, user_id, chat_id, start_time, price_syp)
    except Exception as e:
        print(f"[monitor_api_order] خطأ غير متوقع لطلب id={order_db_id}: {e}")
        traceback.print_exc()
        try:
            api_channel = API_ORDERS_CHANNEL_ID if API_ORDERS_CHANNEL_ID else ADMIN_CHAT_ID
            send_message(api_channel, f"⚠️ توقفت مراقبة الطلب الداخلي #{order_db_id} بسبب خطأ:\n<code>{e}</code>\nسيستمر استرجاع الطلبات المعلّقة في إكمال متابعته تلقائياً.")
        except Exception:
            pass


def _monitor_api_order_impl(order_db_id, user_id, chat_id, start_time, price_syp):
    order = get_api_order_by_id(order_db_id)
    if not order:
        return
    
    order_id = order[3]
    product_name = order[4]
    category_name = order[5]
    player_id = order[7]
    qty = order[9] if order[9] else 1
    
    user_info = get_telegram_user_info(user_id)
    balance = get_user_balance(user_id)
    
    # تحديد قناة مراقبة طلبات API
    api_channel = API_ORDERS_CHANNEL_ID if API_ORDERS_CHANNEL_ID else ADMIN_CHAT_ID

    # رسالة بدء المعالجة للقناة
    processing_msg = f"<b>جار معالجة طلب اوتوماتيكي عبر Api 🟣:</b>\n"
    processing_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
    processing_msg += f"<b>♕🟡 اللعبة: {product_name}</b>\n"
    processing_msg += f"<b>♕🟡 المنتج: {category_name}</b>\n"
    processing_msg += f"<b>♕🟡 السعر: {price_syp:,.2f} ليرة</b>\n"
    processing_msg += f"<b>♕🟡 ايدي الاعب: {player_id}</b>\n"
    processing_msg += f"<b>♕🟡 رد نظام: {order[8]}</b>\n"
    processing_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
    processing_msg += f"<b>◽ المستخدم: {user_info['name']}</b>\n"
    processing_msg += f"<b>◽ ايديه: {user_id}</b>\n"
    processing_msg += f"<b>◽ اليوزر: {user_info['username']}</b>\n"
    processing_msg += f"<b>◽ رصيده الان: {balance:,.2f} ليرة</b>\n"
    processing_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
    sent = send_message(api_channel, processing_msg)
    channel_msg_id = None
    try:
        if isinstance(sent, dict) and sent.get("ok"):
            channel_msg_id = sent.get("result", {}).get("message_id")
    except Exception:
        channel_msg_id = None
    # حفظ معرّف الرسالة في قاعدة البيانات حتى تُحدَّث لاحقاً حتى بعد إعادة تشغيل البوت
    if channel_msg_id:
        try:
            set_api_order_channel_msg(order_db_id, channel_msg_id)
        except Exception:
            pass
    
    # متابعة الحالة بأسرع ما يمكن — أول فحص فوري ثم كل ثانيتين
    check_interval = 2  # فحص كل ثانيتين للاستجابة الفورية
    max_checks = 1800  # كحد أقصى ساعة (1800 * 2s) لتجنب thread معلّق للأبد
    checks = 0
    first_check = True
    while checks < max_checks:
        if first_check:
            # فحص فوري بعد ثانية واحدة فقط لإعطاء المزود وقت تسجيل الطلب
            time.sleep(1)
            first_check = False
        else:
            time.sleep(check_interval)
        checks += 1

        order_data = check_order_status(order_id)

        if order_data:
            # نقرأ الحالة من عدة حقول محتملة (status / order_status / state)
            raw_status = (
                order_data.get("status")
                or order_data.get("order_status")
                or order_data.get("state")
            )
            classified = _classify_order_status(raw_status)
            replay_data = order_data.get("replay_api") or order_data.get("response") or []
            replay_text = str(replay_data) if replay_data else "لا يوجد"
            print(f"[monitor_api_order] order_id={order_id} raw_status={raw_status} → {classified}")

            if classified == "accept":
                status = "accept"
                update_api_order_status(order_db_id, "completed", json.dumps(order_data))
                elapsed_time = time.time() - start_time
                
                # رسالة نجاح للمستخدم
                success_msg = f"<b>✅ تم تنفيذ طلبك بنجاح🎉:</b>\n"
                success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                success_msg += f"<b>🟢⚡ المنتج: {product_name}</b>\n"
                success_msg += f"<b>🟢⚡ الفئة: {category_name}</b>\n"
                success_msg += f"<b>🟢⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
                success_msg += f"<b>🟢⚡ الايدي: {player_id}</b>\n"
                success_msg += f"<b>🟢⚡ الكمية: {qty}</b>\n"
                success_msg += f"<b>🟢⚡ الوقت المستغرق: {elapsed_time:.2f} ثانية</b>\n"
                success_msg += f"<b>🟢⚡ رد النظام: <code>{replay_text}</code></b>\n"
                success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
                send_message(chat_id, success_msg)
                
                # رسالة نجاح للقناة
                channel_success_msg = f"<b>🎉 طلب تم اكتماله عبر API 🎉:</b>\n"
                channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                channel_success_msg += f"<b>🟢⚡اللعبة: {product_name}</b>\n"
                channel_success_msg += f"<b>🟢⚡المنتج: {category_name}</b>\n"
                channel_success_msg += f"<b>🟢⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
                channel_success_msg += f"<b>🟢⚡ الايدي الاعب: {player_id}</b>\n"
                channel_success_msg += f"<b>🟢⚡ رد نظام: <code>{replay_text}</code></b>\n"
                channel_success_msg += f"<b>🟢⚡ الوقت المستغرق: {elapsed_time:.2f} ثانية</b>\n"
                channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━</b>\n"
                channel_success_msg += f"<b>◽ المستخدم: {user_info['name']}</b>\n"
                channel_success_msg += f"<b>◽ ايديه: {user_id}</b>\n"
                channel_success_msg += f"<b>◽ اليوزر: {user_info['username']}</b>\n"
                channel_success_msg += f"<b>◽ رصيده الان: {balance:,.2f} ليرة</b>\n"
                channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
                if channel_msg_id:
                    edit_message(api_channel, channel_msg_id, channel_success_msg)
                else:
                    send_message(api_channel, channel_success_msg)
                return
                
            elif classified == "reject":
                update_api_order_status(order_db_id, "rejected", json.dumps(order_data))
                elapsed_time = time.time() - start_time
                
                # استرداد الرصيد للمستخدم
                update_user_balance(user_id, price_syp)
                new_balance = get_user_balance(user_id)
                
                # رسالة رفض للمستخدم
                reject_msg = f"<b>❌ تم رفض طلبك من المزود، يرجى إعادة المحاولة:</b>\n"
                reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                reject_msg += f"<b>🔴⚡ المنتج: {product_name}</b>\n"
                reject_msg += f"<b>🔴⚡ الفئة: {category_name}</b>\n"
                reject_msg += f"<b>🔴⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
                reject_msg += f"<b>🔴⚡ الايدي: {player_id}</b>\n"
                reject_msg += f"<b>🔴⚡ الكمية: {qty}</b>\n"
                reject_msg += f"<b>🔴⚡ المدة قبل الرفض: {elapsed_time:.2f} ثانية</b>\n"
                reject_msg += f"<b>🔴⚡ رد النظام: <code>{replay_text}</code></b>\n"
                reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                reject_msg += f"<b>💰 تم استرداد رصيدك: {price_syp:,.2f} ليرة</b>\n"
                reject_msg += f"<b>💰 رصيدك الحالي: {new_balance:,.2f} ليرة</b>\n"
                reject_msg += f"<b>🔁 يرجى إعادة المحاولة، وإذا تكرّر الرفض تواصل مع الدعم.</b>"
                send_message(chat_id, reject_msg)
                
                # رسالة رفض للقناة
                channel_reject_msg = f"<b>تم رفض الطلب من API❌:</b>\n"
                channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                channel_reject_msg += f"<b>🔴⚡ المنتج: {product_name}</b>\n"
                channel_reject_msg += f"<b>🔴⚡ الفئة: {category_name}</b>\n"
                channel_reject_msg += f"<b>🔴⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
                channel_reject_msg += f"<b>🔴⚡ الايدي: {player_id}</b>\n"
                channel_reject_msg += f"<b>🔴⚡ الكمية: {qty}</b>\n"
                channel_reject_msg += f"<b>🔴⚡ الوقت المستغرق: {elapsed_time:.2f} ثانية</b>\n"
                channel_reject_msg += f"<b>🔴⚡ رد النظام: <code>{replay_text}</code></b>\n"
                channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                channel_reject_msg += f"<b>◽ المستخدم: {user_info['name']}</b>\n"
                channel_reject_msg += f"<b>◽ ايديه: {user_id}</b>\n"
                channel_reject_msg += f"<b>◽ اليوزر: {user_info['username']}</b>\n"
                channel_reject_msg += f"<b>◽ رصيده الان: {balance:,.2f} ليرة</b>\n"
                channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
                if channel_msg_id:
                    edit_message(api_channel, channel_msg_id, channel_reject_msg)
                else:
                    send_message(api_channel, channel_reject_msg)
                return

def _recover_single_order(order, api_channel):
    """
    يعالج طلباً معلّقاً واحداً. يُستخدم داخل ThreadPoolExecutor للتوازي.
    يُرجع dict يصف نتيجة الفحص لاستخدامه في تقرير الأدمن:
      {"db_id":..., "order_id":..., "user_id":..., "product":..., "category":...,
       "player_id":..., "qty":..., "price":..., "created_at":...,
       "outcome": "completed"|"rejected"|"still_pending"|"unknown"|"no_api_id",
       "raw_status": "..." or None}
    """
    summary = {
        "db_id": None, "order_id": None, "user_id": None,
        "product": "", "category": "", "player_id": "",
        "qty": 1, "price": 0, "created_at": "",
        "outcome": "unknown", "raw_status": None,
    }
    try:
        order_db_id = order[0]
        user_id = order[1]
        order_id = order[3]
        product_name = order[4]
        category_name = order[5]
        price_syp = order[6] or 0
        player_id = order[7]
        qty = order[9] if order[9] else 1
        created_at = order[11]

        summary.update({
            "db_id": order_db_id, "order_id": order_id, "user_id": user_id,
            "product": product_name or "", "category": category_name or "",
            "player_id": player_id or "", "qty": qty, "price": price_syp,
            "created_at": str(created_at) if created_at else "",
        })

        if not order_id:
            summary["outcome"] = "no_api_id"
            return summary

        order_data = check_order_status(order_id)
        if not order_data:
            summary["outcome"] = "still_pending"
            # نعيد تشغيل المراقبة لهذا الطلب لضمان متابعته
            threading.Thread(
                target=monitor_api_order,
                args=(order_db_id, user_id, user_id, time.time(), price_syp),
                daemon=True,
            ).start()
            return summary

        raw_status = (
            order_data.get("status")
            or order_data.get("order_status")
            or order_data.get("state")
        )
        summary["raw_status"] = raw_status
        classified = _classify_order_status(raw_status)
        replay_data = order_data.get("replay_api") or order_data.get("response") or []
        replay_text = str(replay_data) if replay_data else "لا يوجد"

        if classified == "pending":
            summary["outcome"] = "still_pending"
            threading.Thread(
                target=monitor_api_order,
                args=(order_db_id, user_id, user_id, time.time(), price_syp),
                daemon=True,
            ).start()
            return summary

        user_info = get_telegram_user_info(user_id)
        balance = get_user_balance(user_id)
        # نحاول استرجاع معرّف رسالة القناة لتحديثها بدل إرسال رسالة جديدة
        channel_msg_id = get_api_order_channel_msg(order_db_id)

        if classified == "accept":
            summary["outcome"] = "completed"
            update_api_order_status(order_db_id, "completed", json.dumps(order_data))

            success_msg = f"<b>✅ تم تنفيذ طلبك بنجاح🎉:</b>\n"
            success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            success_msg += f"<b>🟢⚡ المنتج: {product_name}</b>\n"
            success_msg += f"<b>🟢⚡ الفئة: {category_name}</b>\n"
            success_msg += f"<b>🟢⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
            success_msg += f"<b>🟢⚡ الايدي: {player_id}</b>\n"
            success_msg += f"<b>🟢⚡ الكمية: {qty}</b>\n"
            success_msg += f"<b>🟢⚡ تاريخ الطلب: {created_at}</b>\n"
            success_msg += f"<b>🟢⚡ رد النظام: <code>{replay_text}</code></b>\n"
            success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            success_msg += f"<b>ℹ️ تم اكتشاف اكتمال الطلب بعد إعادة تشغيل البوت</b>"
            send_message(user_id, success_msg)

            channel_success_msg = f"<b>🎉 طلب تم اكتماله عبر API (استرجاع) 🎉:</b>\n"
            channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            channel_success_msg += f"<b>🟢⚡اللعبة: {product_name}</b>\n"
            channel_success_msg += f"<b>🟢⚡المنتج: {category_name}</b>\n"
            channel_success_msg += f"<b>🟢⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
            channel_success_msg += f"<b>🟢⚡ الايدي الاعب: {player_id}</b>\n"
            channel_success_msg += f"<b>🟢⚡ تاريخ الطلب: {created_at}</b>\n"
            channel_success_msg += f"<b>🟢⚡ رد نظام: <code>{replay_text}</code></b>\n"
            channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━</b>\n"
            channel_success_msg += f"<b>◽ المستخدم: {user_info['name']}</b>\n"
            channel_success_msg += f"<b>◽ ايديه: {user_id}</b>\n"
            channel_success_msg += f"<b>◽ اليوزر: {user_info['username']}</b>\n"
            channel_success_msg += f"<b>◽ رصيده الان: {balance:,.2f} ليرة</b>\n"
            channel_success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
            if channel_msg_id:
                edit_message(api_channel, channel_msg_id, channel_success_msg)
            else:
                send_message(api_channel, channel_success_msg)

        elif classified == "reject":
            summary["outcome"] = "rejected"
            update_api_order_status(order_db_id, "rejected", json.dumps(order_data))
            update_user_balance(user_id, price_syp)
            new_balance = get_user_balance(user_id)

            reject_msg = f"<b>❌ تم رفض طلبك من المزود، يرجى إعادة المحاولة:</b>\n"
            reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            reject_msg += f"<b>🔴⚡ المنتج: {product_name}</b>\n"
            reject_msg += f"<b>🔴⚡ الفئة: {category_name}</b>\n"
            reject_msg += f"<b>🔴⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
            reject_msg += f"<b>🔴⚡ الايدي: {player_id}</b>\n"
            reject_msg += f"<b>🔴⚡ الكمية: {qty}</b>\n"
            reject_msg += f"<b>🔴⚡ تاريخ الطلب: {created_at}</b>\n"
            reject_msg += f"<b>🔴⚡ رد النظام: <code>{replay_text}</code></b>\n"
            reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            reject_msg += f"<b>💰 تم استرداد رصيدك: {price_syp:,.2f} ليرة</b>\n"
            reject_msg += f"<b>💰 رصيدك الحالي: {new_balance:,.2f} ليرة</b>\n"
            reject_msg += f"<b>🔁 يرجى إعادة المحاولة، وإذا تكرّر الرفض تواصل مع الدعم.</b>"
            send_message(user_id, reject_msg)

            channel_reject_msg = f"<b>تم رفض الطلب من API (استرجاع) ❌:</b>\n"
            channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            channel_reject_msg += f"<b>🔴⚡ المنتج: {product_name}</b>\n"
            channel_reject_msg += f"<b>🔴⚡ الفئة: {category_name}</b>\n"
            channel_reject_msg += f"<b>🔴⚡ السعر: {price_syp:,.2f} ليرة</b>\n"
            channel_reject_msg += f"<b>🔴⚡ الايدي: {player_id}</b>\n"
            channel_reject_msg += f"<b>🔴⚡ الكمية: {qty}</b>\n"
            channel_reject_msg += f"<b>🔴⚡ تاريخ الطلب: {created_at}</b>\n"
            channel_reject_msg += f"<b>🔴⚡ رد النظام: <code>{replay_text}</code></b>\n"
            channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
            channel_reject_msg += f"<b>◽ المستخدم: {user_info['name']}</b>\n"
            channel_reject_msg += f"<b>◽ ايديه: {user_id}</b>\n"
            channel_reject_msg += f"<b>◽ اليوزر: {user_info['username']}</b>\n"
            channel_reject_msg += f"<b>◽ رصيده الان: {new_balance:,.2f} ليرة</b>\n"
            channel_reject_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
            if channel_msg_id:
                edit_message(api_channel, channel_msg_id, channel_reject_msg)
            else:
                send_message(api_channel, channel_reject_msg)
        return summary
    except Exception as inner:
        print(f"[recover] فشل تحديث الطلب: {inner}")
        traceback.print_exc()
        summary["outcome"] = "unknown"
        return summary


_recover_lock = threading.Lock()


def _build_pending_orders_report(summaries, total):
    """يبني تقريراً نصياً مفصّلاً للأدمن عن نتائج فحص الطلبات المعلّقة."""
    completed = [s for s in summaries if s and s.get("outcome") == "completed"]
    rejected = [s for s in summaries if s and s.get("outcome") == "rejected"]
    still_pending = [s for s in summaries if s and s.get("outcome") in ("still_pending", "no_api_id", "unknown")]

    lines = []
    lines.append("<b>📊 تقرير استرجاع الطلبات المعلّقة</b>")
    lines.append("<b>━━━━━━━━━━━━━━━━━━━━</b>")
    lines.append(f"<b>🔢 إجمالي الطلبات المفحوصة:</b> {total}")
    lines.append(f"<b>✅ مكتملة الآن:</b> {len(completed)}")
    lines.append(f"<b>❌ مرفوضة (تم استرداد الرصيد):</b> {len(rejected)}")
    lines.append(f"<b>⏳ ما زالت معلّقة لدى المزود:</b> {len(still_pending)}")
    lines.append("<b>━━━━━━━━━━━━━━━━━━━━</b>")

    if not still_pending:
        lines.append("✨ <b>لا توجد طلبات معلّقة عند المزود.</b>")
        return "\n".join(lines)

    lines.append("<b>📋 تفاصيل الطلبات المعلّقة لدى المزود:</b>")
    # نُظهر أول 15 طلباً كحد أقصى لتجنب تجاوز حد رسالة تيليجرام
    for idx, s in enumerate(still_pending[:15], 1):
        raw = s.get("raw_status") or "—"
        reason = ""
        if s.get("outcome") == "no_api_id":
            reason = " (بدون order_id من API)"
        elif s.get("outcome") == "unknown":
            reason = " (تعذر قراءة الحالة)"
        lines.append(f"\n<b>#{idx}{reason}</b>")
        lines.append(f"<b>🆔 الطلب الداخلي:</b> <code>{s.get('db_id')}</code>")
        lines.append(f"<b>🆔 رقم الطلب لدى المزود:</b> <code>{s.get('order_id') or '—'}</code>")
        lines.append(f"<b>👤 المستخدم:</b> <code>{s.get('user_id')}</code>")
        lines.append(f"<b>🎮 ايدي اللاعب:</b> <code>{s.get('player_id') or '—'}</code>")
        lines.append(f"<b>📦 المنتج:</b> {s.get('product')}")
        lines.append(f"<b>🏷️ الفئة:</b> {s.get('category')}")
        lines.append(f"<b>🔢 الكمية:</b> {s.get('qty')}")
        lines.append(f"<b>💰 السعر:</b> {float(s.get('price') or 0):,.2f} ليرة")
        lines.append(f"<b>📅 تاريخ الطلب:</b> {s.get('created_at') or '—'}")
        lines.append(f"<b>📡 حالة المزود الخام:</b> <code>{raw}</code>")

    if len(still_pending) > 15:
        lines.append(f"\n... و {len(still_pending) - 15} طلب آخر معلّق.")

    return "\n".join(lines)


def recover_pending_api_orders(startup_delay=False, triggered_by=None):
    """
    يفحص كل الطلبات المعلّقة بالتوازي ويحدّث المستخدم والقناة.
    إذا تم تمرير triggered_by (chat_id للأدمن)، يُرسل له تقريراً مفصلاً بنتيجة الفحص.
    """
    if not _recover_lock.acquire(blocking=False):
        print("[recover] فحص آخر يعمل بالفعل، تم تجاهل الطلب")
        if triggered_by:
            try:
                send_message(triggered_by, "⚠️ يوجد فحص قيد التنفيذ بالفعل، الرجاء الانتظار حتى ينتهي قبل المحاولة مجدداً.")
            except Exception:
                pass
        return
    try:
        if startup_delay:
            time.sleep(3)
        pending = get_pending_api_orders()
        if not pending:
            print("[recover] لا توجد طلبات معلّقة لاسترجاعها")
            if triggered_by:
                try:
                    send_message(triggered_by, "✨ <b>لا توجد طلبات معلّقة في قاعدة البيانات.</b>")
                except Exception:
                    pass
            return
        print(f"[recover] فحص {len(pending)} طلب معلّق بالتوازي...")
        api_channel = API_ORDERS_CHANNEL_ID if API_ORDERS_CHANNEL_ID else ADMIN_CHAT_ID

        with ThreadPoolExecutor(max_workers=15) as pool:
            summaries = list(pool.map(lambda o: _recover_single_order(o, api_channel), pending))

        print("[recover] انتهى فحص الطلبات المعلّقة")

        if triggered_by:
            try:
                report = _build_pending_orders_report(summaries, len(pending))
                # تيليجرام يحدد الرسالة بـ ~4096 حرفاً، نقطّعها إن لزم
                if len(report) <= 4000:
                    send_message(triggered_by, report)
                else:
                    chunks = []
                    cur = ""
                    for line in report.split("\n"):
                        if len(cur) + len(line) + 1 > 3800:
                            chunks.append(cur)
                            cur = line
                        else:
                            cur = (cur + "\n" + line) if cur else line
                    if cur:
                        chunks.append(cur)
                    for ch in chunks:
                        send_message(triggered_by, ch)
            except Exception as e:
                print(f"[recover] فشل إرسال التقرير للأدمن: {e}")
    except Exception as e:
        print(f"[recover] خطأ عام: {e}")
        traceback.print_exc()
        if triggered_by:
            try:
                send_message(triggered_by, f"❌ خطأ أثناء فحص الطلبات: <code>{e}</code>")
            except Exception:
                pass
    finally:
        _recover_lock.release()


def add_product(name, category, emoji="", description="", image=""):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO products (name, category, emoji, description, image) VALUES (?, ?, ?, ?, ?)", 
                  (name, category, emoji, description, image))
        product_id = c.lastrowid
    return product_id

def update_product_description(product_id, description):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE products SET description = ? WHERE id = ?", (description, product_id))

def update_product_image(product_id, image):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE products SET image = ? WHERE id = ?", (image, product_id))

def get_product_image(product_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT image FROM products WHERE id = ?", (product_id,))
        result = c.fetchone()
        return result[0] if result else ""

def get_product_description(product_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT description FROM products WHERE id = ?", (product_id,))
        result = c.fetchone()
        return result[0] if result else ""

def get_products_by_category(category):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, emoji FROM products WHERE category = ?", (category,))
        products = c.fetchall()
    return products

def get_all_products():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, category, emoji, description, image FROM products")
        products = c.fetchall()
    return products

def get_product_by_id(product_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, category, emoji, description, image FROM products WHERE id = ?", (product_id,))
        product = c.fetchone()
    return product

def delete_product(product_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM categories WHERE product_id = ?", (product_id,))
        c.execute("DELETE FROM products WHERE id = ?", (product_id,))

def add_category(product_id, name, price, cat_type='default', min_qty=1, max_qty=1):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO categories (product_id, name, price, type, min_qty, max_qty) VALUES (?, ?, ?, ?, ?, ?)", 
                  (product_id, name, float(price), cat_type, min_qty, max_qty))
        category_id = c.lastrowid
    return category_id

def get_categories_by_product(product_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, price, type, min_qty, max_qty FROM categories WHERE product_id = ?", (product_id,))
        categories = c.fetchall()
    return categories

def get_category_by_id(category_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, product_id, name, price, type, min_qty, max_qty FROM categories WHERE id = ?", (category_id,))
        category = c.fetchone()
    return category

def delete_category(category_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM categories WHERE id = ?", (category_id,))

def add_deposit_method(title, description, code, exchange_rate):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO deposit_methods (title, description, code, exchange_rate) VALUES (?, ?, ?, ?)", 
                  (title, description, code, float(exchange_rate)))
        method_id = c.lastrowid
    return method_id

def get_all_deposit_methods():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, description, code, exchange_rate FROM deposit_methods")
        methods = c.fetchall()
    return methods

def get_deposit_method_by_id(method_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title, description, code, exchange_rate FROM deposit_methods WHERE id = ?", (method_id,))
        method = c.fetchone()
    return method

def update_deposit_method(method_id, title, description, code, exchange_rate):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE deposit_methods SET title = ?, description = ?, code = ?, exchange_rate = ? WHERE id = ?", 
                  (title, description, code, float(exchange_rate), method_id))

def delete_deposit_method(method_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM deposit_methods WHERE id = ?", (method_id,))

def get_category_description(category_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT description FROM category_description WHERE category_name = ?", (category_name,))
        result = c.fetchone()
        if result and result[0]:
            return result[0]
    return f"🔶 قائمة {category_name} المتاحة:"

def set_category_description(category_name, description):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO category_description (category_name, description) VALUES (?, ?)", (category_name, description))

def get_all_sections():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, color, emoji, is_active FROM sections WHERE is_active = 1")
        sections = c.fetchall()
    return sections

def add_section(name, color='success', emoji=''):
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO sections (name, color, emoji) VALUES (?, ?, ?)", (name, color, emoji))
            c.execute("INSERT INTO category_description (category_name, description) VALUES (?, ?)", (name, f"🔶 قائمة {name} المتاحة:"))
        return True
    except:
        return False

def delete_section(section_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT name FROM sections WHERE id = ?", (section_id,))
        result = c.fetchone()
        if result:
            section_name = result[0]
            c.execute("DELETE FROM products WHERE category = ?", (section_name,))
            c.execute("DELETE FROM sections WHERE id = ?", (section_id,))
            c.execute("DELETE FROM category_description WHERE category_name = ?", (section_name,))

def update_section_color(section_id, color):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE sections SET color = ? WHERE id = ?", (color, section_id))

def update_section_name(section_id, new_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT name FROM sections WHERE id = ?", (section_id,))
        result = c.fetchone()
        if result:
            old_name = result[0]
            c.execute("UPDATE products SET category = ? WHERE category = ?", (new_name, old_name))
            c.execute("UPDATE sections SET name = ? WHERE id = ?", (new_name, section_id))
            c.execute("UPDATE category_description SET category_name = ? WHERE category_name = ?", (new_name, old_name))

def get_section_by_id(section_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, color, emoji FROM sections WHERE id = ?", (section_id,))
        result = c.fetchone()
    return result

def get_section_color(section_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT color FROM sections WHERE name = ?", (section_name,))
        result = c.fetchone()
        if result:
            return result[0]
    return "success"

def _ensure_required_channels_columns():
    """يضمن وجود أعمدة title و invite_link في جدول required_channels (هجرة)."""
    with get_db() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(required_channels)")
        cols = [row[1] for row in c.fetchall()]
        if 'title' not in cols:
            c.execute("ALTER TABLE required_channels ADD COLUMN title TEXT DEFAULT ''")
        if 'invite_link' not in cols:
            c.execute("ALTER TABLE required_channels ADD COLUMN invite_link TEXT DEFAULT ''")
        # هجرة: عمود channel_msg_id في api_orders لتحديث رسالة القناة لاحقاً
        c.execute("PRAGMA table_info(api_orders)")
        ao_cols = [row[1] for row in c.fetchall()]
        if 'channel_msg_id' not in ao_cols:
            c.execute("ALTER TABLE api_orders ADD COLUMN channel_msg_id INTEGER")


def set_api_order_channel_msg(order_db_id, channel_msg_id):
    """يحفظ معرّف رسالة القناة الخاصة بالطلب لتحديثها لاحقاً."""
    if not channel_msg_id:
        return
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE api_orders SET channel_msg_id = ? WHERE id = ?",
                  (channel_msg_id, order_db_id))


def get_api_order_channel_msg(order_db_id):
    """يرجع معرّف رسالة القناة المحفوظ لطلب معين، أو None."""
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT channel_msg_id FROM api_orders WHERE id = ?", (order_db_id,))
            row = c.fetchone()
            if row:
                return row[0]
    except Exception:
        return None
    return None


def _normalize_channel_identifier(raw: str) -> str:
    """يحول المدخل إلى صيغة معرف صالحة لـ Telegram API.
    يقبل: @username, username, -1001234567890, 1234567890, https://t.me/xxx
    ويعيد: @username  أو  -100...  أو نص فارغ عند الفشل.
    """
    if not raw:
        return ""
    s = raw.strip()
    # روابط tme
    m = re.match(r"^https?://t\.me/(?:joinchat/)?([^/?\s]+)/?", s, re.IGNORECASE)
    if m:
        s = m.group(1)
        if s.startswith('+'):
            return ""
    # رقم سالب (قناة خاصة) أو موجب طويل
    if re.match(r"^-?\d+$", s):
        if s.startswith('-100') or s.startswith('-'):
            return s
        if len(s) >= 9 and not s.startswith('-'):
            return f"-100{s}"
        return s
    # يوزر
    if not s.startswith('@'):
        s = '@' + s
    return s


def _telegram_get_chat(chat_identifier):
    """يستعلم عن معلومات قناة. يعيد dict أو None."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
        r = requests.post(url, json={"chat_id": chat_identifier}, timeout=10)
        result = r.json()
        if result.get("ok"):
            return result.get("result", {})
    except Exception as e:
        print(f"[getChat] error: {e}")
    return None


def add_required_channel(raw_identifier):
    """يضيف قناة اشتراك إجباري. يقبل @username أو -100... أو رابط t.me.
    يعيد (success: bool, message: str).
    """
    _ensure_required_channels_columns()
    identifier = _normalize_channel_identifier(raw_identifier)
    if not identifier:
        return False, "❌ صيغة المعرف غير صحيحة"

    info = _telegram_get_chat(identifier)
    if not info:
        return False, "❌ لم أتمكن من الوصول للقناة. تأكد أن البوت مشرف فيها وأن المعرف صحيح"

    chat_id_num = info.get("id")
    title = info.get("title") or info.get("username") or str(chat_id_num)
    username = info.get("username")
    invite_link = info.get("invite_link") or ""

    if username:
        stored_id = f"@{username}"
        link = f"https://t.me/{username}"
    else:
        stored_id = str(chat_id_num)
        if not invite_link:
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/exportChatInviteLink"
                r = requests.post(url, json={"chat_id": chat_id_num}, timeout=10)
                rj = r.json()
                if rj.get("ok"):
                    invite_link = rj.get("result", "")
            except Exception as e:
                print(f"[exportChatInviteLink] error: {e}")
        link = invite_link or ""

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM required_channels WHERE channel_username = ?", (stored_id,))
            if c.fetchone():
                return False, "❌ هذه القناة موجودة مسبقاً"
            c.execute(
                "INSERT INTO required_channels (channel_username, title, invite_link) VALUES (?, ?, ?)",
                (stored_id, title, link),
            )
        return True, f"✅ تم حفظ قناة الاشتراك بنجاح: {title}"
    except Exception as e:
        return False, f"❌ خطأ في الحفظ: {e}"


def get_all_required_channels():
    _ensure_required_channels_columns()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, channel_username, title, invite_link FROM required_channels")
        results = c.fetchall()
    return results


def delete_required_channel(channel_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM required_channels WHERE id = ?", (channel_id,))


def check_user_subscribed(user_id):
    """يتحقق من اشتراك المستخدم في كل القنوات المطلوبة عبر getChatMember.
    الحالات المعتبرة "مشترك": creator, administrator, member, restricted.
    الحالات المعتبرة "غير مشترك": left, kicked, و user not found.
    """
    channels = get_all_required_channels()
    if not channels:
        return True

    for channel in channels:
        # channel = (id, channel_username, title, invite_link)
        chat_identifier = channel[1]
        # دعم القنوات الرقمية والقنوات بالـ username معاً
        if re.match(r"^-?\d+$", str(chat_identifier)):
            chat_arg = int(chat_identifier)
        else:
            chat_arg = chat_identifier if str(chat_identifier).startswith('@') else f"@{chat_identifier}"

        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
            data = {"chat_id": chat_arg, "user_id": user_id}
            r = requests.post(url, json=data, timeout=10)
            result = r.json()

            if result.get("ok"):
                status = (result.get("result") or {}).get("status", "")
                # صراحةً: هذه الحالات تعني عدم الاشتراك
                if status in ("left", "kicked"):
                    return False
                # أي حالة أخرى (member, administrator, creator, restricted) تعني مشترك
            else:
                desc = (result.get("description") or "").lower()
                # المستخدم لم يبدأ البوت أو غير موجود في القناة → غير مشترك
                if "user not found" in desc or "participant_id_invalid" in desc or "user_not_participant" in desc:
                    return False
                # خطأ في إعدادات البوت داخل القناة (ليس مشرفاً، القناة غير موجودة، إلخ)
                # نسجل الخطأ ونعتبره غير مشترك حتى لا يمر بدون تحقق
                print(f"[check_sub] FAILED chat={chat_arg} user={user_id}: {result.get('description')}")
                return False
        except Exception as e:
            print(f"[check_sub] EXCEPTION chat={chat_arg} user={user_id}: {e}")
            return False

    return True

def get_all_users():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        users = c.fetchall()
    return [u[0] for u in users]

def get_total_users_count():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        result = c.fetchone()
        return result[0] if result else 0

def get_total_balance():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
        total = c.fetchone()[0]
    return total

def get_total_orders():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM shop_orders WHERE status = 'accepted'")
        total = c.fetchone()[0]
    return total

def broadcast_to_all(message, admin_id):
    users = get_all_users()
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            send_message(user_id, message)
            success += 1
        except:
            failed += 1
        time.sleep(0.05)
    
    return success, failed, len(users)

def broadcast_to_user(user_id, message):
    try:
        send_message(user_id, message)
        return True
    except:
        return False

def tg_api(method, data=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if data:
            r = requests.post(url, json=data, timeout=15)
        else:
            r = requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        print(f"tg_api error: {e}")
        return {"ok": False}

def send_message(chat_id, text, reply_markup=None, photo=None):
    if photo:
        data = {"chat_id": chat_id, "photo": photo, "caption": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return tg_api("sendPhoto", data)
    else:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return tg_api("sendMessage", data)

def edit_message(chat_id, message_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return tg_api("editMessageText", data)

def answer_callback(callback_id):
    return tg_api("answerCallbackQuery", {"callback_query_id": callback_id})

def delete_message(chat_id, message_id):
    return tg_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def api_request(endpoint, params=None, source=None):
    """
    يرسل طلب GET إلى مزوّد API الخارجي.
    يدعم مصادر متعددة (source1/source2/source3) ويستعمل المصدر الحالي افتراضياً.
    يقبل المسار بأي صيغة: "profile" أو "client/api/profile".
    """
    base = get_api_url(source)
    token = get_api_token(source)
    if not base or not token:
        return {"error": True, "message": "لم يتم تكوين مصدر API الحالي"}

    url = store_endpoint(base, endpoint)
    headers = {"api-token": token}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        else:
            try:
                return r.json()
            except:
                return {"error": True, "status": r.status_code, "text": r.text}
    except Exception as e:
        return {"error": True, "message": str(e)}

def get_profile():
    result = api_request("client/api/profile")
    if not isinstance(result, dict):
        return None
    if "balance" in result:
        return result
    data = result.get("data")
    if isinstance(data, dict) and "balance" in data:
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict) and "balance" in data[0]:
        return data[0]
    for v in result.values():
        if isinstance(v, dict) and "balance" in v:
            return v
    if not result.get("error") and (result.get("status") in ("OK", "ok", "success", True, 200, "200") or result.get("success") is True):
        return result
    return None

def get_telegram_user_info(user_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
    data = {"chat_id": user_id}
    try:
        r = requests.post(url, json=data, timeout=10)
        result = r.json()
        if result.get("ok"):
            chat = result.get("result", {})
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            username = chat.get("username", "")
            full_name = f"{first_name} {last_name}".strip()
            return {
                "name": full_name if full_name else first_name,
                "user_id": user_id,
                "username": f"@{username}" if username else "لا يوجد"
            }
    except:
        pass
    return {
        "name": "مستخدم",
        "user_id": user_id,
        "username": "لا يوجد"
    }

def get_user_orders(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT product_name, category_name, price, player_id, qty, status, created_at FROM shop_orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        orders = c.fetchall()
    return orders

def get_user_deposits(user_id):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT method_title, amount_syp, transaction_code, status, created_at FROM deposit_requests WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        deposits = c.fetchall()
    return deposits

def get_webapp_url():
    """يُرجع رابط تطبيق Mini App.

    أولوية الكشف:
    1. قاعدة البيانات  ← يضبطه الأدمن من داخل البوت (الأعلى أولوية)
    2. HOST_DOMAIN في أعلى الملف
    3. متغيرات البيئة WEBAPP_URL / HOST_DOMAIN
    4. Replit domains
    """
    # 1. من قاعدة البيانات (الأدمن ضبطه عبر البوت)
    db_url = _get_webapp_url_from_db()
    if db_url:
        return db_url if db_url.endswith("/") else db_url + "/"
    # 2. الخانة المكتوبة مباشرة في أعلى bot.py
    if HOST_DOMAIN and HOST_DOMAIN.strip():
        host = HOST_DOMAIN.strip().rstrip("/")
        if not host.startswith("http"):
            host = "https://" + host
        return host + "/shop/"
    # 3. رابط كامل من متغير البيئة
    url = os.environ.get("WEBAPP_URL", "").strip()
    if url:
        return url.rstrip("/") + "/"
    # 4. دومين من متغير البيئة
    host = os.environ.get("HOST_DOMAIN", "").strip()
    if host:
        host = host.rstrip("/")
        if not host.startswith("http"):
            host = "https://" + host
        return host + "/shop/"
    # 5. Replit production domains
    replit_domains = os.environ.get("REPLIT_DOMAINS", "").strip()
    if replit_domains:
        first_domain = replit_domains.split(",")[0].strip()
        if first_domain:
            return f"https://{first_domain}/shop/"
    # 6. Replit dev domain
    domain = os.environ.get("REPLIT_DEV_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}/shop/"
    return ""

def get_main_keyboard(is_admin_user=False):
    webapp_url = get_webapp_url()
    rows = []
    # زر فتح المتجر — يظهر دائماً حتى لو الرابط غير مضبوط
    if webapp_url:
        rows.append([{"text": "🛒 فتح متجر AZM store", "web_app": {"url": webapp_url}}])
    else:
        # رابط احتياطي — يفتح كرابط عادي إذا لم يُضبط الدومين
        rows.append([{"text": "🛒 فتح متجر AZM store", "callback_data": "no_webapp_url"}])
    rows += [
        [
            {"text": "معلومات الحساب 💼", "callback_data": "main_account"},
            {"text": "تواصل معنا 🦉", "callback_data": "main_support"}
        ],
        [
            {"text": "تعليمات استخدام البوت", "callback_data": "main_help"}
        ],
    ]
    return {"inline_keyboard": rows}

def get_products_buttons(category):
    products = get_products_by_category(category)
    if not products:
        return None, f"🚫 لا توجد منتجات في {category}"
    
    section_color = get_section_color(category)
    
    keyboard = {"inline_keyboard": []}
    row = []
    for product in products:
        product_id, product_name, emoji = product
        display_name = f"{emoji} {product_name}" if emoji else product_name
        row.append({"text": display_name, "callback_data": f"show_product_{product_id}", "style": section_color})
        if len(row) == 2:
            keyboard["inline_keyboard"].append(row)
            row = []
    if row:
        keyboard["inline_keyboard"].append(row)
    
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع للقائمة", "callback_data": "back_main"}])
    description = get_category_description(category)
    return keyboard, description

def get_categories_buttons(product_id, product_name):
    categories = get_categories_by_product(product_id)
    
    if not categories:
        return None, f"🚫 لا توجد فئات لهذا المنتج: {product_name}\n\n📌 يرجى إضافة فئات من لوحة التحكم → إدارة المنتجات → إضافة فئة"
    
    keyboard = {"inline_keyboard": []}
    for cat in categories:
        cat_id, cat_name, price_usd, cat_type, min_qty, max_qty = cat
        if cat_type == 'counter':
            display_text = f"{cat_name} (عداد)"
        elif cat_type == 'limited':
            display_text = f"{cat_name} (كمية محددة)"
        else:
            display_text = cat_name
        keyboard["inline_keyboard"].append([{"text": display_text, "callback_data": f"category_{cat_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "back_to_products"}])
    
    product_description = get_product_description(product_id)
    if product_description:
        msg = product_description
    else:
        msg = f"📦 {product_name}:"
    
    return keyboard, msg

def get_deposit_methods_buttons():
    methods = get_all_deposit_methods()
    if not methods:
        return None, "🚫 لا توجد طرق إيداع حالياً"
    
    keyboard = {"inline_keyboard": []}
    for method in methods:
        method_id, title, description, code, exchange_rate = method
        keyboard["inline_keyboard"].append([{"text": title, "callback_data": f"deposit_{method_id}"}])
    
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "back_main"}])
    return keyboard, "💰 اختر طريقة الإيداع:"

def get_deposit_method_details(method_id):
    method = get_deposit_method_by_id(method_id)
    if not method:
        return None
    mid, title, description, code, exchange_rate = method
    msg = f"{description}\n"
    msg += "--------------------------------\n"
    msg += f"<code>{code}</code>\n"
    msg += "--------------------------------\n"
    msg += f"كل 1$ = {exchange_rate:,.2f} ليرة سورية\n"
    msg += "--------------------------------\n"
    msg += "ارسل قيمة المبلغ بالليرة السورية:"
    return msg

def get_admin_products_keyboard():
    return {"inline_keyboard": [
        [{"text": "➕ إضافة منتج", "callback_data": "add_product"}, {"text": "🗑 حذف منتج", "callback_data": "delete_product_menu"}],
        [{"text": "📂 إضافة فئة", "callback_data": "add_category_menu"}, {"text": "🗑 حذف فئة", "callback_data": "delete_category_menu"}],
        [{"text": "🖼️ إدارة الصور", "callback_data": "manage_images"}, {"text": "📝 وصف المنتجات", "callback_data": "product_description_menu"}],
        [{"text": "📝 وصف الأقسام", "callback_data": "manage_description"}],
        [{"text": "➕ إضافة فئة عداد", "callback_data": "add_counter_category"}, {"text": "➕ إضافة فئة (كمية محددة)", "callback_data": "add_limited_category"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_sections_management_keyboard():
    sections = get_all_sections()
    keyboard = {"inline_keyboard": []}
    keyboard["inline_keyboard"].append([{"text": "➕ إضافة قسم", "callback_data": "add_section"}])
    for section in sections:
        sec_id, sec_name, color, emoji, is_active = section
        display_name = f"{sec_name}"
        keyboard["inline_keyboard"].append([
            {"text": f"✏️ {display_name}", "callback_data": f"rename_section_{sec_id}"},
            {"text": f"🗑 {display_name}", "callback_data": f"delete_section_{sec_id}"},
            {"text": f"🎨 {display_name}", "callback_data": f"color_section_{sec_id}"}
        ])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "back_admin"}])
    return keyboard, "📂 إدارة الأقسام:"

def get_color_selection_keyboard(section_id):
    return {"inline_keyboard": [
        [{"text": "🟢 اخضر", "callback_data": f"set_color_{section_id}_success", "color": "success"}],
        [{"text": "🔴 احمر", "callback_data": f"set_color_{section_id}_danger", "color": "danger"}],
        [{"text": "🔵 ازرق", "callback_data": f"set_color_{section_id}_primary", "color": "primary"}],
        [{"text": "🔙 رجوع", "callback_data": "sections_management"}]
    ]}

def get_delete_category_menu():
    products = get_all_products()
    if not products:
        return None, "🚫 لا توجد منتجات"
    keyboard = {"inline_keyboard": []}
    for product in products:
        product_id, name, category, emoji, desc, img = product
        categories = get_categories_by_product(product_id)
        for cat in categories:
            cat_id, cat_name, price_usd, cat_type, min_qty, max_qty = cat
            keyboard["inline_keyboard"].append([{"text": f"🗑 {name} - {cat_name}", "callback_data": f"delete_category_{cat_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard, "اختر الفئة لحذفها:"

def get_products_for_category_menu():
    products = get_all_products()
    if not products:
        return None, "🚫 لا توجد منتجات"
    keyboard = {"inline_keyboard": []}
    for product in products:
        product_id, name, category, emoji, desc, img = product
        display_name = f"{emoji} {name}" if emoji else name
        keyboard["inline_keyboard"].append([{"text": f"{display_name} ({category})", "callback_data": f"select_product_{product_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard, "اختر المنتج:"

def get_delete_products_menu():
    products = get_all_products()
    if not products:
        return None, "🚫 لا توجد منتجات"
    keyboard = {"inline_keyboard": []}
    for product in products:
        product_id, name, category, emoji, desc, img = product
        display_name = f"{emoji} {name}" if emoji else name
        keyboard["inline_keyboard"].append([{"text": f"🗑 {display_name} ({category})", "callback_data": f"delete_product_{product_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard, "اختر المنتج لحذفه:"

def get_products_for_image_menu():
    products = get_all_products()
    if not products:
        return None, "🚫 لا توجد منتجات"
    keyboard = {"inline_keyboard": []}
    for product in products:
        product_id, name, category, emoji, desc, img = product
        display_name = f"{emoji} {name}" if emoji else name
        keyboard["inline_keyboard"].append([{"text": f"🖼️ {display_name} ({category})", "callback_data": f"image_product_{product_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard, "اختر المنتج لوضع صورة له:"

def get_products_for_product_desc_menu():
    products = get_all_products()
    if not products:
        return None, "🚫 لا توجد منتجات"
    keyboard = {"inline_keyboard": []}
    for product in products:
        product_id, name, category, emoji, desc, img = product
        display_name = f"{emoji} {name}" if emoji else name
        keyboard["inline_keyboard"].append([{"text": f"📝 {display_name} ({category})", "callback_data": f"desc_product_{product_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard, "اختر المنتج لإضافة وصف له:"

def get_description_management_keyboard():
    sections = get_all_sections()
    keyboard = {"inline_keyboard": []}
    for section in sections:
        sec_id, sec_name, color, emoji, is_active = section
        display_name = f"{sec_name}"
        keyboard["inline_keyboard"].append([{"text": f"📝 {display_name}", "callback_data": f"desc_section_{sec_name}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
    return keyboard

def get_link_products_menu():
    keyboard = {"inline_keyboard": []}
    sections = get_all_sections()
    for section in sections:
        sec_id, sec_name, color, emoji, is_active = section
        keyboard["inline_keyboard"].append([{"text": f"📁 {sec_name}", "callback_data": f"link_section_{sec_name}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_api"}])
    return keyboard, "اختر القسم لربط المنتجات:"

def get_unlink_products_menu():
    linked = get_linked_products()
    if not linked:
        return None, "🚫 لا توجد منتجات مرتبطة"
    
    keyboard = {"inline_keyboard": []}
    for link in linked:
        link_id, product_id, category_id, api_product_id, product_name, category_name, api_name, api_price, created_at = link
        keyboard["inline_keyboard"].append([{"text": f"🔓 {product_name} - {category_name} → {api_name}", "callback_data": f"unlink_{link_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_api"}])
    return keyboard, "اختر المنتج المراد فك ربطه:"

def get_user_management_keyboard():
    return {"inline_keyboard": [
        [{"text": "🟢 اضافة رصيد", "callback_data": "add_balance"}],
        [{"text": "🔴 خصم رصيد", "callback_data": "deduct_balance"}],
        [{"text": "👑 اضافة ادمن", "callback_data": "add_admin"}],
        [{"text": "🗑 حذف ادمن", "callback_data": "remove_admin"}],
        [{"text": "🔍 كشف مستخدم", "callback_data": "user_info"}],
        [{"text": "🚫 حظر عضو", "callback_data": "block_user"},
         {"text": "🔓 فك حظر", "callback_data": "unblock_user"}],
        [{"text": "📊 احصائية عامة", "callback_data": "general_stats"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_discount_management_keyboard():
    return {"inline_keyboard": [
        [{"text": "🎁 اضافة خصم لمستخدم", "callback_data": "add_discount"}],
        [{"text": "🗑 حذف خصم لمستخدم", "callback_data": "remove_discount"}],
        [{"text": "👥 عرض المستخدمين (المميزين)", "callback_data": "list_discount_users"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_subscription_management_keyboard():
    channels = get_all_required_channels()
    keyboard = {"inline_keyboard": []}
    keyboard["inline_keyboard"].append([{"text": "➕ اضافة قناة اشتراك", "callback_data": "add_channel"}])
    for channel in channels:
        ch_id, ch_username, ch_title, ch_link = channel
        display = ch_title if ch_title else ch_username
        keyboard["inline_keyboard"].append([{"text": f"🗑 حذف {display}", "callback_data": f"delete_channel_{ch_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "back_admin"}])
    return keyboard, "📢 إدارة الاشتراك الاجباري:\nيمكنك إضافة القناة بـ @username أو معرف رقمي مثل -1001234567890\n(يجب أن يكون البوت مشرفاً في القناة)"

def get_broadcast_keyboard():
    return {"inline_keyboard": [
        [{"text": "📢 اذاعة عامة", "callback_data": "broadcast_all"}],
        [{"text": "👤 اذاعة لمستخدم", "callback_data": "broadcast_user"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_deposit_management_keyboard():
    return {"inline_keyboard": [
        [{"text": "➕ اضافة طريقة ايداع", "callback_data": "add_deposit"}],
        [{"text": "✏️ تعديل طريقة ايداع", "callback_data": "edit_deposit"}],
        [{"text": "🗑 حذف طريقة ايداع", "callback_data": "delete_deposit"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_deposit_methods_for_edit():
    methods = get_all_deposit_methods()
    if not methods:
        return None, "🚫 لا توجد طرق إيداع"
    keyboard = {"inline_keyboard": []}
    for method in methods:
        method_id, title, description, code, exchange_rate = method
        keyboard["inline_keyboard"].append([{"text": f"✏️ {title}", "callback_data": f"edit_method_{method_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "deposit_management"}])
    return keyboard, "اختر طريقة الإيداع لتعديلها:"

def get_deposit_methods_for_delete():
    methods = get_all_deposit_methods()
    if not methods:
        return None, "🚫 لا توجد طرق إيداع"
    keyboard = {"inline_keyboard": []}
    for method in methods:
        method_id, title, description, code, exchange_rate = method
        keyboard["inline_keyboard"].append([{"text": f"🗑 {title}", "callback_data": f"delete_method_{method_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "deposit_management"}])
    return keyboard, "اختر طريقة الإيداع لحذفها:"

def get_basic_settings_keyboard():
    return {"inline_keyboard": [
        [{"text": "✏️ تعديل اسماء الاقسام", "callback_data": "rename_sections"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_general_settings_keyboard():
    status_text = "🟢 تشغيل" if _get_bot_enabled_from_db() else "🔴 ايقاف"
    deposit_ch = DEPOSIT_CHANNEL_ID if DEPOSIT_CHANNEL_ID else "غير محدد"
    api_ch = API_ORDERS_CHANNEL_ID if API_ORDERS_CHANNEL_ID else "غير محدد"
    current_url = _get_webapp_url_from_db() or HOST_DOMAIN or "غير مضبوط"
    if len(current_url) > 25:
        current_url = current_url[:25] + "…"
    return {"inline_keyboard": [
        [{"text": f"{status_text} البوت", "callback_data": "toggle_bot"}],
        [{"text": f"🌐 رابط المتجر: {current_url}", "callback_data": "set_webapp_domain"}],
        [{"text": "✏️ تغيير رسالة الترحيب", "callback_data": "change_welcome"}],
        [{"text": "👨‍💻 تغيير معرف الدعم", "callback_data": "change_support"}],
        [{"text": f"💳 قناة الإيداعات ({deposit_ch})", "callback_data": "set_deposit_channel"}],
        [{"text": f"🔗 قناة طلبات API ({api_ch})", "callback_data": "set_api_orders_channel"}],
        [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
    ]}

def get_api_settings_keyboard():
    rows = []
    url = STORE_API_URLS.get('source1', "") or "غير محدد"
    rows.append([{"text": f"✅ مصدر مهد: {url[:30]}", "callback_data": "api_src_source1"}])
    rows.append([{"text": "🔙 رجوع", "callback_data": "back_admin"}])
    return {"inline_keyboard": rows}


def get_api_source_keyboard(src):
    label = "مصدر مهد"
    url = STORE_API_URLS.get(src, "") or "غير محدد"
    tok = STORE_API_TOKENS.get(src, "")
    tok_disp = (tok[:8] + "..." + tok[-4:]) if len(tok) > 14 else (tok or "غير محدد")
    rows = [
        [{"text": f"📎 الرابط: {url[:30]}", "callback_data": f"set_api_url_{src}"}],
        [{"text": f"🔑 التوكن: {tok_disp}", "callback_data": f"set_api_token_{src}"}],
        [{"text": "🟢 المصدر الحالي (مُفعّل)", "callback_data": "noop"}],
        [{"text": "🔙 رجوع", "callback_data": "api_settings"}],
    ]
    return {"inline_keyboard": rows}, f"⚙️ إعدادات {label}:"

def get_rename_sections_menu():
    sections = get_all_sections()
    if not sections:
        return None, "🚫 لا توجد اقسام"
    keyboard = {"inline_keyboard": []}
    for section in sections:
        sec_id, sec_name, color, emoji, is_active = section
        keyboard["inline_keyboard"].append([{"text": f"✏️ {sec_name}", "callback_data": f"rename_section_{sec_id}"}])
    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "basic_settings"}])
    return keyboard, "اختر القسم لتعديل اسمه:"

def get_check_subscription_keyboard():
    channels = get_all_required_channels()
    keyboard = {"inline_keyboard": []}
    for channel in channels:
        ch_id, ch_username, ch_title, ch_link = channel
        display = ch_title if ch_title else ch_username
        if ch_link:
            url = ch_link
        elif ch_username and ch_username.startswith('@'):
            url = f"https://t.me/{ch_username.replace('@', '')}"
        else:
            # حاول جلب رابط دعوة جديد للقناة الخاصة
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/exportChatInviteLink",
                    json={"chat_id": int(ch_username) if re.match(r"^-?\d+$", str(ch_username)) else ch_username},
                    timeout=10,
                )
                rj = resp.json()
                url = rj.get("result") if rj.get("ok") else "https://t.me/"
            except:
                url = "https://t.me/"
        keyboard["inline_keyboard"].append([{"text": f"📢 {display}", "url": url}])
    keyboard["inline_keyboard"].append([{"text": "🔄 تحقق الاشتراك", "callback_data": "check_sub", "color": "success"}])
    return keyboard

def register_new_user(user_id, username, full_name):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = c.fetchone()
        if not exists:
            c.execute("INSERT INTO users (user_id, balance, is_admin, blocked) VALUES (?, 0, 0, 0)", (user_id,))
            welcome_msg = f"<b>عضو جديد انضم لبوتك الفاخر:</b>\n"
            welcome_msg += f"<b>الاسم: {full_name}</b>\n"
            welcome_msg += f"<b>الايدي: {user_id}</b>\n"
            welcome_msg += f"<b>يوزر: {username}</b>\n"
            welcome_msg += f"<b>شرفت ونورت البوت 🎉❤</b>"
            send_message(ADMIN_CHAT_ID, welcome_msg)
            return True
    return False

def handle_message(chat_id, user_id, text):
    print(f"[MSG] user_id={user_id} text={repr(text)}")
    user_info = get_telegram_user_info(user_id)
    register_new_user(user_id, user_info["username"], user_info["name"])
    
    if not _get_bot_enabled_from_db() and user_id != MAIN_ADMIN_ID and not is_admin(user_id):
        send_message(chat_id, "⚠️ الــبــوت مــتــوقــف حــالــيًــا عــن الــعــمــل .⚠️")
        return
    
    if is_blocked(user_id) and user_id != MAIN_ADMIN_ID and not is_admin(user_id):
        send_message(chat_id, "🚫 تم حظرك من استخدام البوت")
        return
    
    if not check_user_subscribed(user_id) and user_id != MAIN_ADMIN_ID and not is_admin(user_id):
        msg = "⚠️ عذرا عليك الاشتراك بالقنوات التالية:"
        send_message(chat_id, msg, reply_markup=get_check_subscription_keyboard())
        return
    
    # ===== إلغاء الحالة عند كلمات الإلغاء =====
    cancel_words = ("/cancel", "/الغاء", "إلغاء", "الغاء")
    if text in cancel_words:
        if user_id in user_states:
            del user_states[user_id]
        send_message(chat_id, "✅ تم إلغاء العملية. اختر من القائمة:", reply_markup=get_main_keyboard(is_admin(user_id)))
        return

    if user_id in user_states:
        state = user_states[user_id]
        
        # معالجة إضافة API (متعدد المصادر)
        if state.get("action") == "waiting_api_token":
            src = state.get("api_src", current_api_source)
            set_api_token_for(src, text)
            del user_states[user_id]
            send_message(chat_id, f"✅ تم حفظ التوكن للمصدر ({src}) بنجاح")
            return

        elif state.get("action") == "waiting_api_base_url":
            src = state.get("api_src", current_api_source)
            set_api_url_for(src, text)
            del user_states[user_id]
            send_message(chat_id, f"✅ تم حفظ رابط الـ API للمصدر ({src}) بنجاح")
            return
        
        elif state.get("action") == "waiting_category_name":
            user_states[user_id]["category_name"] = text
            cat_type = state.get("cat_type", "default")
            if cat_type == 'counter':
                user_states[user_id]["action"] = "waiting_counter_min_qty"
                send_message(chat_id, "📊 ارسل الحد الادنى للكمية:")
            elif cat_type == 'limited':
                user_states[user_id]["action"] = "waiting_limited_min_qty"
                send_message(chat_id, "📊 ارسل الحد الادنى للكمية:")
            else:
                user_states[user_id]["action"] = "waiting_category_price"
                send_message(chat_id, "💰 اكتب سعر الفئة بدولار:")
            return
        
        elif state.get("action") == "waiting_category_price":
            try:
                price = float(text.replace(',', '.').replace(' ', ''))
                product_id = state.get("product_id")
                product_name = state.get("product_name")
                category_name = state.get("category_name")
                cat_type = state.get("cat_type", "default")
                min_qty = state.get("min_qty", 1)
                max_qty = state.get("max_qty", 1)
                add_category(product_id, category_name, price, cat_type, min_qty, max_qty)
                send_message(chat_id, f"✅ تم اضافة {category_name} بسعر {price:.2f}$")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ السعر غير صحيح، أرسل رقم فقط مثل 5.99")
            return
        
        elif state.get("action") == "waiting_counter_min_qty":
            try:
                min_qty = int(text)
                user_states[user_id]["min_qty"] = min_qty
                user_states[user_id]["action"] = "waiting_counter_max_qty"
                send_message(chat_id, "📊 ارسل الحد الاعلى للكمية:")
            except:
                send_message(chat_id, "❌ الرقم غير صحيح، أرسل رقم صحيح")
            return
        
        elif state.get("action") == "waiting_counter_max_qty":
            try:
                max_qty = int(text)
                if max_qty < user_states[user_id]["min_qty"]:
                    send_message(chat_id, "❌ الحد الاعلى يجب ان يكون اكبر من الحد الادنى")
                    return
                user_states[user_id]["max_qty"] = max_qty
                user_states[user_id]["action"] = "waiting_counter_price"
                send_message(chat_id, "💰 قم بتعيين سعر الكمية الواحدة دولار:")
            except:
                send_message(chat_id, "❌ الرقم غير صحيح")
            return
        
        elif state.get("action") == "waiting_counter_price":
            try:
                price = float(text.replace(',', '.').replace(' ', ''))
                product_id = user_states[user_id]["product_id"]
                product_name = user_states[user_id]["product_name"]
                category_name = user_states[user_id]["category_name"]
                min_qty = user_states[user_id]["min_qty"]
                max_qty = user_states[user_id]["max_qty"]
                add_category(product_id, category_name, price, 'counter', min_qty, max_qty)
                send_message(chat_id, f"✅ تم اضافة فئة العداد بنجاح\n📊 {category_name}\n💰 سعر الواحدة: {price:.2f}$\n📈 الحد الادنى: {min_qty}\n📉 الحد الاعلى: {max_qty}")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ السعر غير صحيح، أرسل رقم فقط مثل 5.99")
            return
        
        elif state.get("action") == "waiting_limited_min_qty":
            try:
                min_qty = int(text)
                user_states[user_id]["min_qty"] = min_qty
                user_states[user_id]["action"] = "waiting_limited_max_qty"
                send_message(chat_id, "📊 ارسل الحد الاعلى للكمية:")
            except:
                send_message(chat_id, "❌ الرقم غير صحيح")
            return
        
        elif state.get("action") == "waiting_limited_max_qty":
            try:
                max_qty = int(text)
                if max_qty < user_states[user_id]["min_qty"]:
                    send_message(chat_id, "❌ الحد الاعلى يجب ان يكون اكبر من الحد الادنى")
                    return
                user_states[user_id]["max_qty"] = max_qty
                user_states[user_id]["action"] = "waiting_limited_price"
                send_message(chat_id, "💰 قم بتعيين سعر الكمية الواحدة دولار:")
            except:
                send_message(chat_id, "❌ الرقم غير صحيح")
            return
        
        elif state.get("action") == "waiting_limited_price":
            try:
                price = float(text.replace(',', '.').replace(' ', ''))
                product_id = user_states[user_id]["product_id"]
                product_name = user_states[user_id]["product_name"]
                category_name = user_states[user_id]["category_name"]
                min_qty = user_states[user_id]["min_qty"]
                max_qty = user_states[user_id]["max_qty"]
                add_category(product_id, category_name, price, 'limited', min_qty, max_qty)
                send_message(chat_id, f"✅ تم اضافة الفئة بنجاح\n📊 {category_name}\n💰 سعر الواحدة: {price:.2f}$\n📈 الحد الادنى: {min_qty}\n📉 الحد الاعلى: {max_qty}")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ السعر غير صحيح، أرسل رقم فقط مثل 5.99")
            return
        
        elif state.get("action") == "waiting_product_name":
            user_states[user_id]["product_name"] = text
            user_states[user_id]["action"] = "waiting_product_category"
            sections = get_all_sections()
            keyboard = {"inline_keyboard": []}
            for section in sections:
                sec_id, sec_name, color, emoji, is_active = section
                keyboard["inline_keyboard"].append([{"text": sec_name, "callback_data": f"save_product_{sec_name}"}])
            send_message(chat_id, f"اختر القسم للمنتج: {text}", reply_markup=keyboard)
            return
        
        elif state.get("action") == "waiting_player_id":
            player_id = text
            category_id = state.get("category_id")
            
            category = get_category_by_id(category_id)
            if not category:
                send_message(chat_id, "❌ خطأ في الفئة")
                del user_states[user_id]
                return
            
            cat_id, product_id, cat_name, price_usd, cat_type, min_qty, max_qty = category
            exchange_rate = get_exchange_rate()
            product = get_product_by_id(product_id)
            
            discount_name, discount_percent = get_user_discount(user_id)
            price_before_discount_usd = price_usd
            if discount_percent > 0:
                price_usd = price_usd * (1 - discount_percent / 100)
            
            price_before_discount_syp = price_before_discount_usd * exchange_rate
            price_after_discount_syp = price_usd * exchange_rate
            
            if cat_type == 'counter' or cat_type == 'limited':
                user_states[user_id] = {
                    "action": "waiting_qty",
                    "category_id": category_id,
                    "product_id": product_id,
                    "product_name": product[1] if product else "غير معروف",
                    "cat_name": cat_name,
                    "price_usd": price_usd,
                    "price_usd_before": price_before_discount_usd,
                    "player_id": player_id,
                    "min_qty": min_qty,
                    "max_qty": max_qty,
                    "cat_type": cat_type
                }
                
                msg = f"<b>⚡ المنتج: {product[1] if product else 'غير معروف'}</b>\n"
                msg += f"<b>⚡ الفئة: {cat_name}</b>\n"
                msg += f"<b>🔷 سعر الواحدة بدولار: {price_before_discount_usd:.2f}$</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🔷 سعر الواحدة بعد الخصم: {price_usd:.2f}$</b>\n"
                msg += f"<b>🔷 سعر الواحدة بليرة: {price_before_discount_syp:,.2f} ليرة</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🔷 سعر الواحدة بعد الخصم بليرة: {price_after_discount_syp:,.2f} ليرة</b>\n"
                msg += f"<b>⚡ الحد الادنى: {min_qty}</b>\n"
                msg += f"<b>⚡ الحد الاعلى: {max_qty}</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🎁 خصم {discount_name}: {discount_percent}%</b>\n"
                msg += f"<b>🔶ارســــــــل الكمــــية المــــراد شحـــــنها 🔶:</b>"
                send_message(chat_id, msg)
                return
            
            price_syp = price_usd * exchange_rate
            balance = get_user_balance(user_id)
            linked = get_linked_by_category(category_id)
            
            pending_orders[user_id] = {
                "category_id": category_id,
                "product_id": product_id,
                "product_name": product[1] if product else "غير معروف",
                "cat_name": cat_name,
                "price_usd": price_usd,
                "price_usd_before": price_before_discount_usd,
                "price_syp": price_syp,
                "price_syp_before": price_before_discount_syp,
                "player_id": player_id,
                "qty": 1,
                "start_time": time.time(),
                "linked": linked is not None,
                "api_product_id": linked[3] if linked else None,
                "discount_percent": discount_percent,
                "discount_name": discount_name
            }
            
            msg = f"<b>❄️ تاكيد عملية الشراء ❄️</b>\n\n"
            msg += f"<b>🔷 المنتج: {product[1] if product else 'غير معروف'}</b>\n\n"
            msg += f"<b>🔷 الفئة: {cat_name}</b>\n\n"
            msg += f"<b>🔷 السعر الاصلي بالدولار: {price_before_discount_usd:.2f}$</b>\n"
            if discount_percent > 0:
                msg += f"<b>🔷 السعر بعد الخصم بالدولار: {price_usd:.2f}$</b>\n"
            msg += f"<b>🔷 السعر الاصلي بليرة: {price_before_discount_syp:,.2f} ليرة</b>\n"
            if discount_percent > 0:
                msg += f"<b>🔷 السعر بعد الخصم بليرة: {price_syp:,.2f} ليرة</b>\n\n"
            else:
                msg += f"<b>🔷 السعر بليرة: {price_syp:,.2f} ليرة</b>\n\n"
            if discount_percent > 0:
                msg += f"<b>🎁 الخصم: {discount_name} {discount_percent}%</b>\n\n"
            msg += f"<b>🔷 رصيدك الان: {balance:,.2f} ليرة</b>\n\n"
            msg += f"<b>◽ ادخل ايدي الاعب لشحن المنتج او متطلبات الخدمة:</b>"
            
            keyboard = {"inline_keyboard": [
                [{"text": "🟢 تأكيد الشراء", "callback_data": f"confirm_buy_{user_id}", "color": "success"}],
                [{"text": "🔵 تغيير الايدي", "callback_data": "change_player_id", "color": "primary"},
                 {"text": "🔴 إلغاء الشراء", "callback_data": "cancel_buy", "color": "danger"}],
                [{"text": "🔵 عودة للفئات", "callback_data": "back_to_categories"}]
            ]}
            send_message(chat_id, msg, reply_markup=keyboard)
            
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_qty":
            try:
                qty = int(text)
                category_id = state.get("category_id")
                min_qty = state.get("min_qty")
                max_qty = state.get("max_qty")
                
                if qty < min_qty or qty > max_qty:
                    send_message(chat_id, f"❌ الكمية غير مقبولة، الرجاء ادخال كمية بين {min_qty} و {max_qty}")
                    return
                
                category = get_category_by_id(category_id)
                if not category:
                    send_message(chat_id, "❌ خطأ في الفئة")
                    del user_states[user_id]
                    return
                
                cat_id, product_id, cat_name, price_usd, cat_type, min_qty, max_qty = category
                exchange_rate = get_exchange_rate()
                
                discount_name, discount_percent = get_user_discount(user_id)
                price_usd_before = price_usd
                if discount_percent > 0:
                    price_usd = price_usd * (1 - discount_percent / 100)
                
                price_syp = price_usd * qty * exchange_rate
                price_syp_before = price_usd_before * qty * exchange_rate
                balance = get_user_balance(user_id)
                product = get_product_by_id(product_id)
                linked = get_linked_by_category(category_id)
                player_id = state.get("player_id")
                
                pending_orders[user_id] = {
                    "category_id": category_id,
                    "product_id": product_id,
                    "product_name": product[1] if product else "غير معروف",
                    "cat_name": cat_name,
                    "price_usd": price_usd,
                    "price_usd_before": price_usd_before,
                    "price_syp": price_syp,
                    "price_syp_before": price_syp_before,
                    "player_id": player_id,
                    "qty": qty,
                    "start_time": time.time(),
                    "linked": linked is not None,
                    "api_product_id": linked[3] if linked else None,
                    "min_qty": min_qty,
                    "max_qty": max_qty,
                    "discount_percent": discount_percent,
                    "discount_name": discount_name
                }
                
                msg = f"<b>❄️ تاكيد عملية الشراء ❄️</b>\n\n"
                msg += f"<b>🔷 المنتج: {product[1] if product else 'غير معروف'}</b>\n\n"
                msg += f"<b>🔷 الفئة: {cat_name}</b>\n\n"
                msg += f"<b>🔷 سعر الواحدة الاصلي بالدولار: {price_usd_before:.2f}$</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🔷 سعر الواحدة بعد الخصم بالدولار: {price_usd:.2f}$</b>\n"
                msg += f"<b>🔷 سعر الواحدة الاصلي بليرة: {price_usd_before * exchange_rate:,.2f} ليرة</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🔷 سعر الواحدة بعد الخصم بليرة: {price_usd * exchange_rate:,.2f} ليرة</b>\n"
                msg += f"<b>🔷 الكمية: {qty}</b>\n\n"
                msg += f"<b>🔷 السعر الكلي الاصلي: {price_syp_before:,.2f} ليرة</b>\n"
                if discount_percent > 0:
                    msg += f"<b>🔷 السعر الكلي بعد الخصم: {price_syp:,.2f} ليرة</b>\n\n"
                else:
                    msg += f"<b>🔷 السعر الكلي: {price_syp:,.2f} ليرة</b>\n\n"
                if discount_percent > 0:
                    msg += f"<b>🎁 الخصم: {discount_name} {discount_percent}%</b>\n\n"
                msg += f"<b>🔷 رصيدك الان: {balance:,.2f} ليرة</b>\n\n"
                msg += f"<b>🔷 رصيدك بعد الطلب: {max(0, balance - price_syp):,.2f} ليرة</b>\n\n"
                
                keyboard = {"inline_keyboard": [
                    [{"text": "🟢 تأكيد الشراء", "callback_data": f"confirm_buy_{user_id}", "color": "success"}],
                    [{"text": "🔵 تغيير الايدي", "callback_data": "change_player_id", "color": "primary"},
                     {"text": "🔴 إلغاء الشراء", "callback_data": "cancel_buy", "color": "danger"}],
                    [{"text": "🔵 عودة للفئات", "callback_data": "back_to_categories"}]
                ]}
                send_message(chat_id, msg, reply_markup=keyboard)
                
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الكمية غير صحيحة، أرسل رقماً صحيحاً")
            return
        
        elif state.get("action") == "waiting_deposit_title":
            user_states[user_id]["title"] = text
            user_states[user_id]["action"] = "waiting_deposit_description"
            send_message(chat_id, "✏️ اكتب وصف الطريقة:")
            return
        
        elif state.get("action") == "waiting_deposit_description":
            user_states[user_id]["description"] = text
            user_states[user_id]["action"] = "waiting_deposit_code"
            send_message(chat_id, "🔢 اكتب كود التحويل (رقم الحساب/المحفظة):")
            return
        
        elif state.get("action") == "waiting_deposit_code":
            user_states[user_id]["code"] = text
            user_states[user_id]["action"] = "waiting_deposit_rate"
            send_message(chat_id, "💰 اكتب سعر الصرف بليرة سورية (رقم فقط):")
            return
        
        elif state.get("action") == "waiting_deposit_rate":
            try:
                rate_text = text.strip().replace(',', '').replace(' ', '').replace('،', '')
                rate = float(rate_text)
                title = user_states[user_id]["title"]
                description = user_states[user_id]["description"]
                code = user_states[user_id]["code"]
                add_deposit_method(title, description, code, rate)
                send_message(chat_id, f"✅ تم اضافة طريقة الإيداع '{title}' بنجاح\n💰 سعر الصرف: {rate:,.2f} ليرة")
                del user_states[user_id]
            except ValueError:
                send_message(chat_id, f"❌ السعر غير صحيح: '{text}'\nأرسل رقم فقط مثل 15000")
            return
        
        elif state.get("action") == "waiting_edit_title":
            user_states[user_id]["new_title"] = text
            user_states[user_id]["action"] = "waiting_edit_description"
            send_message(chat_id, "✏️ اكتب الوصف الجديد:")
            return
        
        elif state.get("action") == "waiting_edit_description":
            user_states[user_id]["new_description"] = text
            user_states[user_id]["action"] = "waiting_edit_code"
            send_message(chat_id, "🔢 اكتب كود التحويل الجديد:")
            return
        
        elif state.get("action") == "waiting_edit_code":
            user_states[user_id]["new_code"] = text
            user_states[user_id]["action"] = "waiting_edit_rate"
            send_message(chat_id, "💰 اكتب سعر الصرف الجديد:")
            return
        
        elif state.get("action") == "waiting_edit_rate":
            try:
                rate_text = text.strip().replace(',', '').replace(' ', '')
                rate = float(rate_text)
                method_id = user_states[user_id]["method_id"]
                new_title = user_states[user_id]["new_title"]
                new_description = user_states[user_id]["new_description"]
                new_code = user_states[user_id]["new_code"]
                update_deposit_method(method_id, new_title, new_description, new_code, rate)
                send_message(chat_id, "✅ تم تعديل طريقة الإيداع بنجاح")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ السعر غير صحيح، أرسل رقم فقط")
            return
        
        elif state.get("action") == "waiting_deposit_amount":
            try:
                amount_syp = float(text.replace(',', '.').replace(' ', ''))
                method_id = state.get("method_id")
                method = get_deposit_method_by_id(method_id)
                if method:
                    exchange_rate = method[4]
                    amount_usd = amount_syp / exchange_rate
                    user_states[user_id]["amount_usd"] = amount_usd
                    user_states[user_id]["amount_syp"] = amount_syp
                    user_states[user_id]["method_title"] = method[1]
                    user_states[user_id]["action"] = "waiting_transaction_code"
                    send_message(chat_id, f"💰 المبلغ: {amount_syp:,.2f} ليرة\n🔢 ارسل رقم عملية التحويل:")
                else:
                    send_message(chat_id, "❌ خطأ في طريقة الإيداع")
            except:
                send_message(chat_id, "❌ المبلغ غير صحيح، أرسل رقم فقط مثل 50000")
            return
        
        elif state.get("action") == "waiting_transaction_code":
            transaction_code = text.strip()
            amount_usd = user_states[user_id]["amount_usd"]
            amount_syp = user_states[user_id]["amount_syp"]
            method_title = user_states[user_id]["method_title"]

            # ===== الإيداع التلقائي: فقط إذا كانت الطريقة "سيريتل كاش تلقائي" =====
            if method_title == 'سيريتل كاش تلقائي':
                _auto_entry = get_pending_auto_deposit(transaction_code)

                # حالة 1: العملية مستهلكة سابقاً من مستخدم آخر
                if _auto_entry and _auto_entry.get("consumed"):
                    del user_states[user_id]
                    send_message(chat_id,
                        "⚠️ <b>هذه العملية تم استخدامها مسبقاً!</b>\n"
                        f"🔢 رقم العملية: <code>{transaction_code}</code>\n"
                        "لا يمكن استخدام نفس رقم العملية مرتين.")
                    print(f"[auto-credit] ⛔ محاولة إعادة استخدام عملية {transaction_code} من قبل {user_id}")
                    return

                if _auto_entry is not None:
                    _age = time.time() - _auto_entry["timestamp"]
                    if _age > DEPOSIT_EXPIRY_SECONDS:
                        # العملية منتهية الصلاحية
                        consume_pending_auto_deposit(transaction_code, -1)  # علّمها كمستهلكة لتجنّب إعادة المحاولة
                        del user_states[user_id]
                        send_message(chat_id,
                            "⌛ انتهت صلاحية هذه العملية (أكثر من 4 ساعات).\n"
                            "يرجى إجراء تحويل جديد والمحاولة مجدداً.")
                        return

                    # ✅ التحقق من تطابق المبلغ المُدخل مع المبلغ الفعلي (تحقق إضافي للأمان)
                    confirmed_amount = _auto_entry["amount_syp"]
                    try:
                        _entered = float(amount_syp)
                    except Exception:
                        _entered = 0
                    if abs(confirmed_amount - _entered) > 0.5:
                        del user_states[user_id]
                        send_message(chat_id,
                            "❌ <b>المبلغ لا يطابق رقم العملية!</b>\n"
                            f"🔢 رقم العملية: <code>{transaction_code}</code>\n"
                            f"💰 المبلغ المُسجَّل في القناة: <b>{confirmed_amount:,.0f} ل.س</b>\n"
                            f"💸 المبلغ الذي أدخلته: <b>{_entered:,.0f} ل.س</b>\n"
                            "أعد المحاولة بإدخال نفس المبلغ المحوَّل.")
                        print(f"[auto-credit] ⚠️ عدم تطابق المبلغ للعملية {transaction_code}: مُدخل={_entered}, فعلي={confirmed_amount}")
                        return

                    # ✅ العملية صالحة — إضافة الرصيد فوراً
                    consume_pending_auto_deposit(transaction_code, user_id)
                    del user_states[user_id]
                    new_bal = update_user_balance(user_id, confirmed_amount)
                    # تسجيل الإيداع في قاعدة البيانات
                    user_info_auto = get_telegram_user_info(user_id)
                    add_deposit_request(
                        user_id, user_info_auto["username"], user_info_auto["name"],
                        method_title, confirmed_amount / (get_exchange_rate() or 1),
                        confirmed_amount, transaction_code
                    )
                    send_message(chat_id,
                        f"✅ <b>تم إضافة رصيدك تلقائياً!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔢 رقم العملية: <code>{transaction_code}</code>\n"
                        f"💰 المبلغ المُضاف: <b>{confirmed_amount:,.0f} ل.س</b>\n"
                        f"💼 رصيدك الآن: <b>{new_bal:,.0f} ل.س</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━")
                    print(f"[auto-credit] ✅ تم إيداع {confirmed_amount} ل.س للمستخدم {user_id} | عملية {transaction_code}")
                    return

                # لم يُعثر على رقم العملية ضمن الإيداعات التلقائية
                del user_states[user_id]
                send_message(chat_id,
                    "❌ <b>لم يتم العثور على عملية بهذا الرقم</b>\n"
                    f"🔢 رقم العملية: <code>{transaction_code}</code>\n"
                    "تأكد أن الرسالة وصلت إلى قناة الإيداع التلقائي، ثم انتظر بضع ثوانٍ وحاول مجدداً.\n"
                    "إن استمرّت المشكلة تواصل مع الدعم.")
                print(f"[auto-credit] ❌ لم يُعثر على العملية {transaction_code} للمستخدم {user_id}")
                return
            # =========================================
            # طريقة إيداع أخرى (يدوية) → تجاوز الفحص التلقائي وأرسل للمراجعة مباشرة

            user_info = get_telegram_user_info(user_id)

            request_id = add_deposit_request(user_id, user_info["username"], user_info["name"],
                               method_title, amount_usd, amount_syp, transaction_code)

            msg = f"⚡تم ارسال طلب ايداعك الى المراجعة⚡:\n"
            msg += f"🟡طريقة ايداعك: {method_title}\n"
            msg += f"🟡المبلغ: {amount_syp:,.2f} ليرة\n"
            msg += f"🟡رقم عملية تحويل: {transaction_code}\n"
            msg += f"💬سيتم اخبارك بنتيجة ايداعك ✅"
            send_message(chat_id, msg)

            admin_msg = f"📢 طلب ايداع جديد:\n"
            admin_msg += f"👤 الاسم: {user_info['name']}\n"
            admin_msg += f"🆔 الايدي الرقمي: {user_id}\n"
            admin_msg += f"🔖 معرف المستخدم: {user_info['username']}\n"
            admin_msg += f"----------------------\n"
            admin_msg += f"💳 طريقة الايداع: {method_title}\n"
            admin_msg += f"💰 المبلغ: {amount_syp:,.2f} ليرة\n"
            admin_msg += f"🔢 رقم العملية: {transaction_code}\n"
            admin_msg += f"💵 رصيد المستخدم: {get_user_balance(user_id):,.2f} ليرة"

            keyboard = {"inline_keyboard": [
                [{"text": "🟢 قبول الايداع", "callback_data": f"accept_deposit_{request_id}", "color": "success"},
                 {"text": "🔴 رفض الايداع", "callback_data": f"reject_deposit_{request_id}", "color": "danger"}]
            ]}

            if DEPOSIT_CHANNEL_ID:
                send_message(DEPOSIT_CHANNEL_ID, admin_msg, reply_markup=keyboard)
            else:
                send_message(ADMIN_CHAT_ID, admin_msg, reply_markup=keyboard)
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_support_username":
            username = text.strip().lstrip('@')
            set_support_username(username)
            send_message(chat_id, f"✅ تم تغيير معرف الدعم إلى: @{username}")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_deposit_channel_id":
            channel_id = text.strip()
            if channel_id == "مسح":
                channel_id = ""
                set_deposit_channel(channel_id)
                send_message(chat_id, "✅ تم مسح قناة الإيداعات")
            else:
                set_deposit_channel(channel_id)
                send_message(chat_id, f"✅ تم حفظ قناة الإيداعات: {channel_id}")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_api_orders_channel_id":
            channel_id = text.strip()
            if channel_id == "مسح":
                channel_id = ""
                set_api_orders_channel(channel_id)
                send_message(chat_id, "✅ تم مسح قناة طلبات API")
            else:
                set_api_orders_channel(channel_id)
                send_message(chat_id, f"✅ تم حفظ قناة طلبات API: {channel_id}")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_exchange_rate":
            try:
                rate_text = text.strip().replace(',', '').replace(' ', '').replace('،', '')
                rate = float(rate_text)
                set_exchange_rate(rate)
                send_message(chat_id, f"✅ تم وضع سعر صرف بنجاح\n💰 سعر الصرف الجديد: {rate:,.2f} ليرة = 1$")
                del user_states[user_id]
            except ValueError:
                send_message(chat_id, f"❌ السعر غير صحيح: '{text}'\nأرسل رقم فقط مثل 10000")
            return

        elif state.get("action") == "waiting_auto_interval":
            try:
                minutes = max(1, int(float(text.strip().replace(',', '').replace('،', ''))))
                set_auto_price_interval(minutes)
                hours_label = f" ({minutes // 60} ساعة)" if minutes >= 60 else ""
                send_message(chat_id,
                    f"✅ تم ضبط فترة التحديث التلقائي\n"
                    f"⏱️ ستُحدَّث الأسعار كل <b>{minutes} دقيقة</b>{hours_label}")
                del user_states[user_id]
            except (ValueError, TypeError):
                send_message(chat_id, f"❌ قيمة غير صحيحة: '{text}'\nأرسل رقم صحيح مثل 30 أو 60")
            return
        
        elif state.get("action") == "waiting_api_product_id":
            try:
                api_product_id = int(text)
                product_id = state.get("product_id")
                category_id = state.get("category_id")
                product_name = state.get("product_name")
                category_name = state.get("category_name")
                
                api_product = get_api_product_by_id(api_product_id)
                if api_product:
                    api_name = api_product.get("name", "غير معروف")
                    api_price = api_product.get("price", 0)
                    
                    link_product(product_id, category_id, api_product_id, api_name, api_price)
                    
                    msg = f"✅ تم ربط المنتج بنجاح:\n"
                    msg += f"📦 المنتج: {product_name}\n"
                    msg += f"🏷️ الفئة: {category_name}\n"
                    msg += f"🔗 الفئةApi: {api_name}\n"
                    msg += f"💰 السعرApi: {api_price:.2f}$\n"
                    msg += f"⚡يمكن الان للمستخدمين الشحن تلقائية🎉"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "❌ لا يوجد منتج بهذا الرقم في API")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الرقم غير صحيح، أرسل رقم المنتج من API")
            return
        
        elif state.get("action") == "waiting_search_term":
            search_term = text
            results = search_api_products(search_term)
            if results:
                for product in results[:10]:
                    product_id = product.get("id", "?")
                    name = product.get("name", "غير معروف")
                    price = product.get("price", 0)
                    product_type = product.get("product_type", "غير معروف")
                    available = product.get("available", False)
                    available_text = "✅ متاح" if available else "❌ غير متاح"
                    params = ", ".join(product.get("params", []))
                    
                    msg = f"🔶اسم المنتج: {name}\n"
                    msg += f"🔶سعر المنتج: {float(price):.2f}$\n"
                    msg += f"<code>🔷ايدي المنتج: {product_id}</code>\n"
                    msg += f"🔷نوع المنتج: {product_type}\n"
                    msg += f"🔶حالة المنتج: {available_text}\n"
                    msg += f"🔶متطلبات شراء المنتج: {params}\n"
                    msg += "━━━━━━━━━━━━━━━━━━━━\n"
                    send_message(chat_id, msg)
            else:
                send_message(chat_id, "❌ لا توجد منتجات مطابقة للبحث")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_discount_user_id":
            try:
                target_user_id = int(text)
                user_states[user_id]["target_user_id"] = target_user_id
                user_states[user_id]["action"] = "waiting_discount_name"
                send_message(chat_id, "🎁 ضع اسم الخصم الذي تريده:")
            except:
                send_message(chat_id, "❌ الايدي غير صحيح، أرسل رقم صحيح")
            return
        
        elif state.get("action") == "waiting_discount_name":
            user_states[user_id]["discount_name"] = text
            user_states[user_id]["action"] = "waiting_discount_percent"
            send_message(chat_id, "📊 اكتب نسبة الخصم التي تريدها (رقم فقط):")
            return
        
        elif state.get("action") == "waiting_discount_percent":
            try:
                percent = float(text.replace(',', '.').replace(' ', ''))
                if percent <= 0 or percent > 100:
                    send_message(chat_id, "❌ النسبة يجب ان تكون بين 1 و 100")
                    return
                target_user_id = user_states[user_id]["target_user_id"]
                discount_name = user_states[user_id]["discount_name"]
                set_user_discount(target_user_id, discount_name, percent)
                send_message(chat_id, f"✅ تم تطبيق الخصم بنجاح✅\n🎁 {discount_name}: {percent}%")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ النسبة غير صحيحة، أرسل رقم فقط")
            return
        
        elif state.get("action") == "waiting_remove_discount_user_id":
            try:
                target_user_id = int(text)
                remove_user_discount(target_user_id)
                send_message(chat_id, "✅ تم حذف الخصم بنجاح✅✅✅")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_welcome_message":
            set_welcome_message(text)
            send_message(chat_id, "✅ تم تغيير رسالة الترحيب بنجاح")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_user_id_for_add_balance":
            try:
                target_user_id = int(text)
                user_states[user_id]["target_user_id"] = target_user_id
                user_states[user_id]["action"] = "waiting_add_balance_amount"
                send_message(chat_id, "💰 اكتب المبلغ الذي تريد اضافته بليرة:")
            except:
                send_message(chat_id, "❌ الايدي غير صحيح، أرسل رقم صحيح")
            return
        
        elif state.get("action") == "waiting_add_balance_amount":
            try:
                amount = float(text.replace(',', '.').replace(' ', ''))
                target_user_id = user_states[user_id]["target_user_id"]
                add_user_balance(target_user_id, amount)
                send_message(chat_id, f"✅ تم اضافة الرصيد للمستخدم بنجاح")
                
                user_msg = f"💸تم اضافة مبلغ: {amount:,.2f} ليرة\n🔶تم اضافة هذه المبلغ بواسطة ادارة البوت❄"
                send_message(target_user_id, user_msg)
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ المبلغ غير صحيح")
            return
        
        elif state.get("action") == "waiting_user_id_for_deduct_balance":
            try:
                target_user_id = int(text)
                user_states[user_id]["target_user_id"] = target_user_id
                user_states[user_id]["action"] = "waiting_deduct_balance_amount"
                send_message(chat_id, "💰 اكتب المبلغ الذي تريد خصمه بليرة:")
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_deduct_balance_amount":
            try:
                amount = float(text.replace(',', '.').replace(' ', ''))
                target_user_id = user_states[user_id]["target_user_id"]
                deduct_user_balance(target_user_id, amount)
                send_message(chat_id, f"✅ تم خصم الرصيد من المستخدم بنجاح")
                
                user_msg = f"⚠تم خصم مبلغ: {amount:,.2f} ليرة\n📌تم الخصم رصيدك من قبل الادمن🧩"
                send_message(target_user_id, user_msg)
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ المبلغ غير صحيح")
            return
        
        elif state.get("action") == "waiting_webapp_domain":
            raw = text.strip()
            # بناء الرابط الكامل
            if raw.startswith("http://") or raw.startswith("https://"):
                final_url = raw.rstrip("/") + ("/" if "/shop" not in raw else "")
                if "/shop/" not in final_url:
                    final_url = final_url.rstrip("/") + "/shop/"
            else:
                raw = raw.rstrip("/")
                final_url = "https://" + raw + "/shop/"
            _set_webapp_url_in_db(final_url)
            del user_states[user_id]
            send_message(chat_id,
                f"✅ تم ضبط رابط المتجر:\n<code>{final_url}</code>\n\n"
                "الآن يمكن للمستخدمين فتح المتجر عبر البوت.",
                reply_markup=get_general_settings_keyboard()
            )
            return

        elif state.get("action") == "waiting_user_id_for_add_admin":
            try:
                target_user_id = int(text)
                if target_user_id == MAIN_ADMIN_ID:
                    send_message(chat_id, "❌ هذا المستخدم هو المدير الرئيسي بالفعل")
                else:
                    add_admin(target_user_id)
                    send_message(chat_id, f"✅ تم اضافة المستخدم {target_user_id} لادمن بنجاح")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_user_id_for_remove_admin":
            try:
                target_user_id = int(text)
                if target_user_id == MAIN_ADMIN_ID:
                    send_message(chat_id, "عذرا لايمكن حذف المدير الرئيسي 😉")
                else:
                    remove_admin(target_user_id)
                    send_message(chat_id, f"✅ تم حذف المستخدم من الادمن ⚡✅")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_user_id_for_info":
            try:
                target_user_id = int(text)
                stats = get_user_stats(target_user_id)
                user_info = get_telegram_user_info(target_user_id)
                
                msg = f"♕⚡اسمك: {user_info['name']}\n"
                msg += f"♕⚡ايديك: {target_user_id}\n"
                msg += f"♕⚡معرفك: {user_info['username']}\n"
                msg += "------------------\n"
                msg += f"♕⚡اجمالي مصروفاتك: {stats['total_shop']:,.2f} ليرة\n"
                if stats['discount_name']:
                    msg += f"♕⚡رتبك (نسبة الخصم): {stats['discount_name']}\n"
                else:
                    msg += f"♕⚡رتبك (نسبة الخصم): 0%\n"
                msg += f"♕⚡رصيدك بليرة: {stats['balance']:,.2f} ليرة\n"
                
                keyboard = {"inline_keyboard": [
                    [{"text": "📋 طـلــباتــي", "callback_data": f"my_orders_{target_user_id}", "color": "danger"},
                     {"text": "💰 أيـــداعـاتـي", "callback_data": f"my_deposits_{target_user_id}", "color": "success"}]
                ]}
                send_message(chat_id, msg, reply_markup=keyboard)
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_user_id_for_block":
            try:
                target_user_id = int(text)
                if target_user_id == MAIN_ADMIN_ID:
                    send_message(chat_id, "❌ لا يمكن حظر المدير الرئيسي")
                else:
                    block_user(target_user_id)
                    send_message(chat_id, f"✅ تم حظر المستخدم {target_user_id}")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_user_id_for_unblock":
            try:
                target_user_id = int(text)
                unblock_user(target_user_id)
                send_message(chat_id, f"✅ تم فك حظر المستخدم {target_user_id}")
                del user_states[user_id]
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_description_category":
            user_states[user_id]["desc_category"] = text
            user_states[user_id]["action"] = "waiting_description_text"
            send_message(chat_id, "📝 ارسل وصف القسم الجديد:")
            return
        
        elif state.get("action") == "waiting_description_text":
            category_name = user_states[user_id]["desc_category"]
            description = text
            set_category_description(category_name, description)
            send_message(chat_id, f"✅ تم وضع الوصف للقسم بنجاح")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_product_image":
            product_id = state.get("product_id")
            return
        
        elif state.get("action") == "waiting_product_description":
            product_id = state.get("product_id")
            update_product_description(product_id, text)
            send_message(chat_id, f"✅ تم حفظ الوصف بنجاح")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_sms_chat_id":
            del user_states[user_id]
            if text.strip().lower() in ("مسح", "حذف", "إلغاء", "الغاء", "/cancel"):
                set_sms_forwarder("")
                send_message(chat_id, "✅ تم إلغاء ربط هاتف SMS.")
                return
            try:
                int(text.strip())
            except Exception:
                send_message(chat_id, "❌ chat_id يجب أن يكون رقماً.")
                return
            set_sms_forwarder(text.strip())
            send_message(chat_id, f"✅ تم ربط هاتف SMS بـ chat_id: <code>{text.strip()}</code>")
            return
        
        elif state.get("action") == "waiting_sms_credit_user_id":
            try:
                target_user_id = int(text.strip())
            except Exception:
                send_message(chat_id, "❌ ID غير صحيح، أرسل رقم فقط.")
                return
            amount_syp = state.get("sms_amount_syp", 0)
            del user_states[user_id]
            if amount_syp <= 0:
                send_message(chat_id, "❌ مبلغ غير صالح.")
                return
            try:
                add_user_balance(target_user_id, amount_syp)
                send_message(chat_id, f"✅ تم إضافة <b>{amount_syp:,} ل.س</b> لرصيد المستخدم <code>{target_user_id}</code>.")
                try:
                    send_message(target_user_id, f"💰 تمت إضافة <b>{amount_syp:,} ل.س</b> إلى رصيدك بنجاح عبر إيداع SMS.")
                except Exception:
                    pass
            except Exception as e:
                send_message(chat_id, f"❌ خطأ: {e}")
            return
        
        elif state.get("action") == "waiting_new_section_name":
            section_id = state.get("section_id")
            new_name = text
            update_section_name(section_id, new_name)
            send_message(chat_id, f"✅ تم تحديث الاسم بنجاح")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_new_section":
            section_name = text
            if add_section(section_name):
                send_message(chat_id, f"✅ تم اضافة القسم {section_name} بنجاح")
            else:
                send_message(chat_id, "❌ هذا القسم موجود مسبقاً")
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_channel_username":
            ok, msg = add_required_channel(text)
            send_message(chat_id, msg)
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_broadcast_message":
            msg = text
            send_message(chat_id, "📢 جار الاذاعة للمستخدمين في البوت...")
            success, failed, total = broadcast_to_all(msg, user_id)
            result_msg = f"✅ عدد الذين تم الارسال لهم: {success}\n❌ عدد الذين فشل الارسال لهم: {failed}\n🔶 المستخدمين الاجماليين: {total}"
            send_message(chat_id, result_msg)
            del user_states[user_id]
            return
        
        elif state.get("action") == "waiting_broadcast_user_id":
            try:
                target_user_id = int(text)
                user_states[user_id]["target_user_id"] = target_user_id
                user_states[user_id]["action"] = "waiting_broadcast_user_message"
                send_message(chat_id, "📝 ارسل الاذاعة:")
            except:
                send_message(chat_id, "❌ الايدي غير صحيح")
            return
        
        elif state.get("action") == "waiting_broadcast_user_message":
            target_user_id = user_states[user_id]["target_user_id"]
            msg = text
            if broadcast_to_user(target_user_id, msg):
                send_message(chat_id, "✅ تم الارسال بنجاح")
            else:
                send_message(chat_id, "❌ فشل الارسال")
            del user_states[user_id]
            return

        elif state.get("action") == "waiting_edit_price_dollar":
            cat_id = state.get("category_id")
            del user_states[user_id]
            try:
                new_price = float(text.replace(',', '.').strip())
                if new_price < 0:
                    raise ValueError("negative price")
            except Exception:
                send_message(chat_id, "❌ السعر غير صحيح، أرسل رقماً موجباً مثل 0.15")
                return
            try:
                with get_db() as _conn:
                    _c = _conn.cursor()
                    _c.execute("UPDATE categories SET price = ? WHERE id = ?", (new_price, cat_id))
                category = get_category_by_id(cat_id)
                cat_name = category[2] if category else "—"
                exchange_rate = get_exchange_rate()
                send_message(chat_id,
                    f"✅ تم تحديث سعر الفئة <b>{cat_name}</b>\n"
                    f"💵 السعر الجديد: <b>{new_price:.4f}$</b>\n"
                    f"💴 يعادل: <b>{new_price * exchange_rate:,.2f} ليرة</b>")
            except Exception as _e:
                send_message(chat_id, f"❌ خطأ في تحديث السعر: {_e}")
            return

    pass

def handle_callback(chat_id, message_id, callback_id, data, user_id):
    try:
        # منع معالجة نفس الضغطة مرتين (حل مشكلة الاستجابة المزدوجة)
        now = time.time()
        with _processed_callbacks_lock:
            stale = [k for k, v in list(_processed_callbacks.items()) if now - v > 15]
            for k in stale:
                del _processed_callbacks[k]
            if callback_id in _processed_callbacks:
                answer_callback(callback_id)
                return
            _processed_callbacks[callback_id] = now

        answer_callback(callback_id)
        is_admin_user = is_admin(user_id)
        
        if not check_user_subscribed(user_id) and user_id != MAIN_ADMIN_ID and not is_admin(user_id):
            msg = "⚠️ عذرا عليك الاشتراك بالقنوات التالية:"
            send_message(chat_id, msg, reply_markup=get_check_subscription_keyboard())
            return
        
        # ===== أزرار القائمة الرئيسية =====
        if data == "main_products":
            shop_url = get_webapp_url()
            if shop_url:
                keyboard = {"inline_keyboard": [
                    [{"text": "🛍️ فتح المتجر", "web_app": {"url": shop_url}}],
                    [{"text": "🔙 رجوع", "callback_data": "back_main"}]
                ]}
                send_message(chat_id, "🛍️ اضغط لفتح المتجر:", reply_markup=keyboard)
            else:
                sections = get_all_sections()
                if not sections:
                    send_message(chat_id, "🚫 لا توجد أقسام حالياً")
                else:
                    keyboard = {"inline_keyboard": []}
                    for sec in sections:
                        sec_id, sec_name, color, emoji, is_active = sec
                        keyboard["inline_keyboard"].append([{"text": sec_name, "callback_data": f"section_{sec_name}"}])
                    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "back_main"}])
                    send_message(chat_id, "🛍️ اختر القسم:", reply_markup=keyboard)

        elif data.startswith("section_"):
            sec_name = data[len("section_"):]
            kb, msg = get_products_buttons(sec_name)
            if kb:
                send_message(chat_id, msg, reply_markup=kb)
            else:
                send_message(chat_id, msg)

        elif data == "main_account":
            user_info = get_telegram_user_info(user_id)
            stats = get_user_stats(user_id)
            msg = f"♕⚡اسمك: {user_info['name']}\n"
            msg += f"♕⚡ايديك: {user_info['user_id']}\n"
            msg += f"♕⚡معرفك: {user_info['username']}\n"
            msg += "------------------\n"
            msg += f"♕⚡اجمالي مصروفاتك: {stats['total_shop']:,.2f} ليرة\n"
            if stats['discount_name']:
                msg += f"♕⚡رتبك (نسبة الخصم): {stats['discount_name']}\n"
            else:
                msg += f"♕⚡رتبك (نسبة الخصم): 0%\n"
            msg += f"♕⚡رصيدك بليرة: {stats['balance']:,.2f} ليرة\n"
            keyboard = {"inline_keyboard": [
                [{"text": "📋 طـلــباتــي", "callback_data": f"my_orders_{user_id}"},
                 {"text": "💰 أيـــداعـاتـي", "callback_data": f"my_deposits_{user_id}"}],
                [{"text": "🔙 رجوع", "callback_data": "back_main"}]
            ]}
            send_message(chat_id, msg, reply_markup=keyboard)

        elif data == "main_deposit":
            kb, msg = get_deposit_methods_buttons()
            if kb:
                send_message(chat_id, msg, reply_markup=kb)
            else:
                send_message(chat_id, msg)

        elif data == "main_support":
            sup = _get_support_username_from_db()
            support_link = f"https://t.me/{sup}" if sup else "https://t.me/AZM1STORE"
            keyboard = {"inline_keyboard": [
                [{"text": "📞 تواصل مع الدعم", "url": support_link}],
                [{"text": "🔙 رجوع", "callback_data": "back_main"}]
            ]}
            send_message(chat_id, f"💬 للتواصل مع فريق الدعم:\n\n📞 <b>@{sup or 'AZM1STORE'}</b>", reply_markup=keyboard)

        elif data == "main_help":
            help_text = (
                "📖 <b>تعليمات استخدام البوت</b>\n\n"
                "🛍️ <b>المنتجات</b> — تصفح الأقسام واختر المنتج الذي تريد شحنه\n\n"
                "💼 <b>معلومات الحساب</b> — عرض رصيدك وطلباتك وإيداعاتك\n\n"
                "🦉 <b>تواصل معنا</b> — تواصل مع فريق الدعم الفني\n\n"
                "📱 لربط رقم هاتفك للإيداع التلقائي: /setphone 09XXXXXXXX"
            )
            keyboard = {"inline_keyboard": [[{"text": "🔙 رجوع", "callback_data": "back_main"}]]}
            send_message(chat_id, help_text, reply_markup=keyboard)

        elif data == "main_admin" and is_admin_user:
            keyboard = {"inline_keyboard": [
                [{"text": "📂 إدارة الأقسام", "callback_data": "sections_management"}, {"text": "🔐 إدارة API", "callback_data": "admin_api"}],
                [{"text": "📦 إدارة المنتجات", "callback_data": "admin_products"}],
                [{"text": "💳 إدارة الايداعات", "callback_data": "deposit_management"}, {"text": "👥 إدارة المستخدمين", "callback_data": "user_management"}],
                [{"text": "🎁 إدارة الخصومات", "callback_data": "discount_management"}],
                [{"text": "📢 إدارة الاشتراك الاجباري", "callback_data": "subscription_management"}],
                [{"text": "📣 الاذاعة", "callback_data": "broadcast_menu"}, {"text": "⚙️ الاعدادات الاساسية", "callback_data": "basic_settings"}],
                [{"text": "🔄 الاعدادات العامة", "callback_data": "general_settings"}],
                [{"text": "💰 تعديل سعر صرف", "callback_data": "edit_exchange_rate"}, {"text": "✏️ تعديل أسعار المنتجات بالدولار", "callback_data": "edit_prices"}],
                [{"text": "➕ إعدادات API", "callback_data": "api_settings"}],
                [{"text": "🔄 استرجاع الطلبات المعلّقة", "callback_data": "recover_orders"}],
                [{"text": "📱 إعداد SMS سيريتل", "callback_data": "sms_setup"}],
                [{"text": "🔙 رجوع", "callback_data": "back_main"}]
            ]}
            send_message(chat_id, "🛡️ لوحة التحكم:", reply_markup=keyboard)

        # معالجة إعدادات API (متعدد المصادر)
        elif data == "api_settings" and is_admin_user:
            keyboard = get_api_settings_keyboard()
            edit_message(chat_id, message_id, "🔐 إعدادات API:\n\nاختر مصدراً لتعديله، أو فعّله ليصبح المصدر النشط.", reply_markup=keyboard)

        elif data.startswith("api_src_") and is_admin_user:
            src = data[len("api_src_"):]
            keyboard, msg = get_api_source_keyboard(src)
            edit_message(chat_id, message_id, msg, reply_markup=keyboard)

        elif data.startswith("set_api_url_") and is_admin_user:
            src = data[len("set_api_url_"):]
            user_states[user_id] = {"action": "waiting_api_base_url", "api_src": src}
            edit_message(chat_id, message_id, f"📎 ارسل رابط الـ API للمصدر ({src}):\n\nمثال:\nhttps://api.example.com")

        elif data.startswith("set_api_token_") and is_admin_user:
            src = data[len("set_api_token_"):]
            user_states[user_id] = {"action": "waiting_api_token", "api_src": src}
            edit_message(chat_id, message_id, f"🔑 ارسل توكن الـ API للمصدر ({src}):")

        elif data.startswith("use_api_") and is_admin_user:
            src = data[len("use_api_"):]
            if set_current_api_source(src):
                keyboard, msg = get_api_source_keyboard(src)
                edit_message(chat_id, message_id, f"✅ تم تفعيل ({src}) كمصدر API الحالي.\n\n" + msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, "❌ مصدر غير صالح")

        elif data == "noop":
            pass

        elif data == "no_webapp_url":
            answer_callback(callback_id)
            if is_admin_user:
                edit_message(chat_id, message_id,
                    "⚠️ رابط المتجر غير مضبوط بعد.\n\n"
                    "📌 <b>اضبطه الآن من لوحة التحكم:</b>\n"
                    "الإعدادات العامة → 🌐 رابط المتجر\n\n"
                    "أو اضغط الزر أدناه مباشرة:",
                    reply_markup={"inline_keyboard": [
                        [{"text": "🌐 ضبط رابط المتجر الآن", "callback_data": "set_webapp_domain"}],
                        [{"text": "🔙 رجوع", "callback_data": "back_main"}],
                    ]}
                )
            else:
                edit_message(chat_id, message_id,
                    "⚠️ المتجر غير متاح حالياً، تواصل مع المدير.",
                    reply_markup=get_main_keyboard(is_admin_user)
                )
            return

        elif data == "set_webapp_domain" and is_admin_user:
            user_states[user_id] = {"action": "waiting_webapp_domain", "msg_id": message_id}
            current = _get_webapp_url_from_db() or HOST_DOMAIN or ""
            hint = f"\nالرابط الحالي: <code>{current}</code>" if current else ""
            edit_message(chat_id, message_id,
                f"🌐 <b>ضبط رابط المتجر</b>{hint}\n\n"
                "أرسل دومين أو IP السيرفر:\n\n"
                "📌 <b>أمثلة:</b>\n"
                "• <code>mysite.com</code>\n"
                "• <code>123.45.67.89:8081</code>\n"
                "• <code>https://mysite.com/shop/</code>\n\n"
                "⚠️ تيليغرام يتطلب HTTPS لفتح المتجر.",
                reply_markup={"inline_keyboard": [
                    [{"text": "❌ إلغاء", "callback_data": "general_settings"}]
                ]}
            )
            return

        elif data == "add_api" and is_admin_user:
            # إبقاء التوافق مع الإصدار القديم — يضيف للمصدر الحالي
            user_states[user_id] = {"action": "waiting_api_token", "api_src": current_api_source}
            edit_message(chat_id, message_id, f"🔑 ارسل توكن الـ API للمصدر ({current_api_source}):")
        
        elif data.startswith("my_orders_"):
            target_user_id = int(data.split("_")[2])
            if target_user_id == user_id or is_admin_user:
                orders = get_user_orders(target_user_id)
                if not orders:
                    send_message(chat_id, "📋 لا توجد طلبات سابقة")
                else:
                    msg = "📋 <b>قائمة طلباتك:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    for order in orders[:20]:
                        product_name, category_name, price, player_id, qty, status, created_at = order
                        status_emoji = "✅" if status == "accepted" else ("⏳" if status == "pending" else "❌")
                        status_text = "مقبول" if status == "accepted" else ("قيد المعالجة" if status == "pending" else "مرفوض")
                        msg += f"{status_emoji} <b>{product_name}</b> - {category_name}\n"
                        msg += f"   💰 {price:,.2f} ليرة | 🆔 {player_id}\n"
                        msg += f"   📦 الكمية: {qty} | 📅 {created_at[:16]}\n"
                        msg += f"   📌 الحالة: {status_text}\n━━━━━━━━━━━━━━━━━━━━\n"
                    send_message(chat_id, msg)
            else:
                send_message(chat_id, "❌ لا يمكنك عرض طلبات مستخدم آخر")
        
        elif data.startswith("my_deposits_"):
            target_user_id = int(data.split("_")[2])
            if target_user_id == user_id or is_admin_user:
                deposits = get_user_deposits(target_user_id)
                if not deposits:
                    send_message(chat_id, "💰 لا توجد ايداعات سابقة")
                else:
                    msg = "💰 <b>قائمة ايداعاتك:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                    for dep in deposits[:20]:
                        method_title, amount_syp, transaction_code, status, created_at = dep
                        status_emoji = "✅" if status == "accepted" else ("⏳" if status == "pending" else "❌")
                        status_text = "مقبول" if status == "accepted" else ("قيد المراجعة" if status == "pending" else "مرفوض")
                        msg += f"{status_emoji} <b>{method_title}</b>\n"
                        msg += f"   💰 {amount_syp:,.2f} ليرة\n"
                        msg += f"   🔢 {transaction_code}\n"
                        msg += f"   📅 {created_at[:16]}\n"
                        msg += f"   📌 الحالة: {status_text}\n━━━━━━━━━━━━━━━━━━━━\n"
                    send_message(chat_id, msg)
            else:
                send_message(chat_id, "❌ لا يمكنك عرض ايداعات مستخدم آخر")
        
        elif data.startswith("show_product_"):
            parts = data.split("_")
            if len(parts) >= 3 and parts[2].isdigit():
                product_id = int(parts[2])
                product = get_product_by_id(product_id)
                if product:
                    keyboard, msg = get_categories_buttons(product_id, product[1])
                    product_image = get_product_image(product_id)
                    delete_message(chat_id, message_id)
                    if product_image:
                        send_message(chat_id, msg, reply_markup=keyboard, photo=product_image)
                    else:
                        send_message(chat_id, msg, reply_markup=keyboard)
        
        elif data.startswith("category_"):
            parts = data.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                category_id = int(parts[1])
                category = get_category_by_id(category_id)
                if category:
                    cat_id, product_id, cat_name, price_usd, cat_type, min_qty, max_qty = category
                    exchange_rate = get_exchange_rate()
                    balance = get_user_balance(user_id)
                    product = get_product_by_id(product_id)
                    
                    delete_message(chat_id, message_id)
                    
                    discount_name, discount_percent = get_user_discount(user_id)
                    price_before_discount_usd = price_usd
                    if discount_percent > 0:
                        price_usd = price_usd * (1 - discount_percent / 100)
                    
                    price_before_discount_syp = price_before_discount_usd * exchange_rate
                    price_after_discount_syp = price_usd * exchange_rate
                    
                    if cat_type == 'counter' or cat_type == 'limited':
                        msg = f"<b>⚡ المنتج: {product[1] if product else 'غير معروف'}</b>\n"
                        msg += f"<b>⚡ الفئة: {cat_name}</b>\n"
                        msg += f"<b>🔷 سعر الواحدة بدولار: {price_before_discount_usd:.2f}$</b>\n"
                        if discount_percent > 0:
                            msg += f"<b>🔷 سعر الواحدة بعد الخصم: {price_usd:.2f}$</b>\n"
                        msg += f"<b>🔷 سعر الواحدة بليرة: {price_before_discount_syp:,.2f} ليرة</b>\n"
                        if discount_percent > 0:
                            msg += f"<b>🔷 سعر الواحدة بعد الخصم بليرة: {price_after_discount_syp:,.2f} ليرة</b>\n"
                        msg += f"<b>⚡ الحد الادنى: {min_qty}</b>\n"
                        msg += f"<b>⚡ الحد الاعلى: {max_qty}</b>\n"
                        if discount_percent > 0:
                            msg += f"<b>🎁 خصم {discount_name}: {discount_percent}%</b>\n"
                        msg += f"<b>🔶ارسل متطلبات شراء الخدمة مثال ايدي الاعب🔶:</b>"
                        
                        user_states[user_id] = {
                            "action": "waiting_player_id",
                            "category_id": category_id,
                            "cat_type": cat_type,
                            "min_qty": min_qty,
                            "max_qty": max_qty,
                            "price_usd": price_usd
                        }
                        send_message(chat_id, msg)
                    else:
                        msg = f"<b>🔶 المنتج: {product[1] if product else 'غير معروف'}</b>\n\n"
                        msg += f"<b>🔶 الفئة: {cat_name}</b>\n\n"
                        msg += f"<b>🔶 السعر الاصلي بالدولار: {price_before_discount_usd:.2f}$</b>\n"
                        if discount_percent > 0:
                            msg += f"<b>🔶 السعر بعد الخصم بالدولار: {price_usd:.2f}$</b>\n"
                        msg += f"<b>🔶 السعر الاصلي بليرة: {price_before_discount_syp:,.2f} ليرة</b>\n"
                        if discount_percent > 0:
                            msg += f"<b>🔶 السعر بعد الخصم بليرة: {price_after_discount_syp:,.2f} ليرة</b>\n\n"
                        else:
                            msg += f"<b>🔶 السعر بليرة: {price_after_discount_syp:,.2f} ليرة</b>\n\n"
                        if discount_percent > 0:
                            msg += f"<b>🎁 خصم {discount_name}: {discount_percent}%</b>\n\n"
                        msg += f"<b>🔶 رصيدك الان: {balance:,.2f} ليرة</b>\n\n"
                        msg += f"<b>◽ ادخل ايدي الاعب لشحن المنتج او متطلبات الخدمة:</b>"
                        
                        user_states[user_id] = {"action": "waiting_player_id", "category_id": category_id}
                        send_message(chat_id, msg)
        
        elif data.startswith("deposit_") and data != "deposit_management":
            parts = data.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                method_id = int(parts[1])
                msg = get_deposit_method_details(method_id)
                user_states[user_id] = {"action": "waiting_deposit_amount", "method_id": method_id}
                cancel_keyboard = {"inline_keyboard": [
                    [{"text": "❌ إلغاء / رجوع", "callback_data": "cancel_deposit", "color": "danger"}]
                ]}
                edit_message(chat_id, message_id, msg, reply_markup=cancel_keyboard)

        elif data == "cancel_deposit":
            if user_id in user_states:
                del user_states[user_id]
            keyboard, msg = get_deposit_methods_buttons()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data.startswith("confirm_buy_"):
            buyer_id = int(data.split("_")[2])
            if user_id == buyer_id:
                if user_id in pending_orders:
                    order = pending_orders[user_id]
                    balance = get_user_balance(user_id)
                    
                    if balance >= order["price_syp"]:
                        new_balance = update_user_balance(user_id, -order["price_syp"])
                        
                        if order.get("linked"):
                            api_product_id = order["api_product_id"]
                            player_id = order["player_id"]
                            qty = order.get("qty", 1)
                            
                            start_time = time.time()
                            api_result = buy_from_api(api_product_id, player_id, qty)
                            
                            if isinstance(api_result, dict) and api_result.get("status") == "OK":
                                data_res = api_result.get("data", {})
                                order_id = data_res.get("order_id")
                                replay_api = data_res.get("replay_api", [])
                                replay_text = str(replay_api) if replay_api else "جاري المعالجة"
                                
                                order_db_id = save_api_order(user_id, 0, order_id, order["product_name"], order["cat_name"], order["price_syp"], player_id, qty, replay_text)
                                
                                success_msg = f"<b>✅ تم انشاء طلبك الان ⚡ :</b>\n"
                                success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                                success_msg += f"<b>🟡♕ المنتج: {order['product_name']}</b>\n"
                                success_msg += f"<b>🟡♕ الفئة: {order['cat_name']}</b>\n"
                                success_msg += f"<b>🟡♕ السعر: {order['price_syp']:,.2f} ليرة</b>\n"
                                success_msg += f"<b>🟡♕ الايدي: {player_id}</b>\n"
                                success_msg += f"<b>🟡♕ الكمية: {qty}</b>\n"
                                success_msg += f"<b>🟡♕ حالة الطلب: جاري المعالجة</b>\n"
                                success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                                success_msg += f"<b>❄سيتم ارسال الرد هنا عند اكتمال طلبك بنجاح ✅</b>"
                                send_message(user_id, success_msg)
                                
                                threading.Thread(target=monitor_api_order, args=(order_db_id, user_id, chat_id, start_time, order["price_syp"]), daemon=True).start()
                            else:
                                update_user_balance(user_id, order["price_syp"])
                                error_msg = str(api_result)
                                send_message(user_id, f"❌ فشل الاتصال بـ API، تم استرداد رصيدك\nالسبب: {error_msg}")
                        else:
                            with get_db() as conn:
                                c = conn.cursor()
                                c.execute('''INSERT INTO shop_orders (user_id, product_name, category_name, price, player_id, qty, status) 
                                    VALUES (?, ?, ?, ?, ?, ?, 'pending')''',
                                    (user_id, order["product_name"], order["cat_name"], order["price_syp"], order["player_id"], order.get("qty", 1)))
                                order_id_db = c.lastrowid
                            
                            success_msg = f"<b>✅ تم انشاء طلبك الان ⚡ :</b>\n"
                            success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                            success_msg += f"<b>🟡♕ المنتج: {order['product_name']}</b>\n"
                            success_msg += f"<b>🟡♕ الفئة: {order['cat_name']}</b>\n"
                            success_msg += f"<b>🟡♕ السعر: {order['price_syp']:,.2f} ليرة</b>\n"
                            success_msg += f"<b>🟡♕ الايدي: {order['player_id']}</b>\n"
                            success_msg += f"<b>🟡♕ الكمية: {order.get('qty', 1)}</b>\n"
                            success_msg += f"<b>🟡♕ حالة الطلب: جاري المعالجة</b>\n"
                            success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                            success_msg += f"<b>❄سيتم ارسال الرد هنا عند اكتمال طلبك بنجاح ✅</b>"
                            send_message(user_id, success_msg)
                            
                            user_info = get_telegram_user_info(user_id)
                            admin_msg = f"🔶طلب شحن منتج جديد🔶\n"
                            admin_msg += f"🔶المستخدم: {user_info['name']}\n"
                            admin_msg += f"🔶الايدي: {user_id}\n"
                            admin_msg += f"🔶المعرف: {user_info['username']}\n"
                            admin_msg += f"----------------------------\n"
                            admin_msg += f"🔶المنتج: {order['product_name']}\n"
                            admin_msg += f"🔶الفئة: {order['cat_name']}\n"
                            admin_msg += f"🔶سعر المنتج: {order['price_syp']:,.2f} ليرة\n"
                            admin_msg += f"🔶الكمية: {order.get('qty', 1)}\n"
                            admin_msg += f"🔶رصيده الان: {balance:,.2f} ليرة\n"
                            admin_msg += f"🔶رصيده بعد الطلب: {new_balance:,.2f} ليرة\n"
                            admin_msg += f"🔶ايدي الاعب: {order['player_id']}\n"
                            if order.get('discount_percent', 0) > 0:
                                admin_msg += f"🎁 الخصم: {order.get('discount_name', '')} {order.get('discount_percent', 0)}%\n"
                            if order.get('qty', 1) > 1:
                                admin_msg += f"🔶الحد الادنى: {order.get('min_qty', 'N/A')}\n"
                                admin_msg += f"🔶الحد الاعلى: {order.get('max_qty', 'N/A')}\n"
                            
                            keyboard = {"inline_keyboard": [
                                [{"text": "🟢 قبول الشحن", "callback_data": f"accept_order_{order_id_db}", "color": "success"},
                                 {"text": "🔴 رفض الشحن", "callback_data": f"reject_order_{order_id_db}", "color": "danger"}]
                            ]}
                            send_message(ADMIN_CHAT_ID, admin_msg, reply_markup=keyboard)
                        
                        del pending_orders[user_id]
                        edit_message(chat_id, message_id, "✅ تم تأكيد الطلب بنجاح")
                    else:
                        send_message(user_id, "❌ رصيدك غير كافي لإتمام العملية")
                        edit_message(chat_id, message_id, "❌ رصيد غير كافي")
                else:
                    edit_message(chat_id, message_id, "❌ لا يوجد طلب معلق")
        
        elif data == "change_player_id":
            edit_message(chat_id, message_id, "🎮 ارسل ايدي اللاعب الجديد:")
        
        elif data == "cancel_buy":
            if user_id in pending_orders:
                del pending_orders[user_id]
            edit_message(chat_id, message_id, "❌ تم الغاء عملية الشراء")
        
        elif data == "back_to_categories":
            if user_id in pending_orders:
                order = pending_orders[user_id]
                product = get_product_by_id(order["product_id"])
                if product:
                    keyboard, msg = get_categories_buttons(order["product_id"], product[1])
                    product_image = get_product_image(order["product_id"])
                    delete_message(chat_id, message_id)
                    if product_image:
                        send_message(chat_id, msg, reply_markup=keyboard, photo=product_image)
                    else:
                        send_message(chat_id, msg, reply_markup=keyboard)
                del pending_orders[user_id]
        
        elif data.startswith("accept_order_"):
            if is_admin_user:
                order_id = int(data.split("_")[2])
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute("SELECT user_id, product_name, category_name, price, player_id, qty, created_at FROM shop_orders WHERE id = ?", (order_id,))
                    order = c.fetchone()
                    if order:
                        target_user_id, product_name, category_name, price, player_id, qty, created_at = order
                        c.execute("UPDATE shop_orders SET status = 'accepted' WHERE id = ?", (order_id,))
                        
                        created_time = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                        elapsed_time = time.time() - created_time.timestamp()
                        
                        success_msg = f"<b>✅ تم تنفيذ طلبك بنجاح🎉:</b>\n"
                        success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>\n"
                        success_msg += f"<b>🟢⚡ المنتج: {product_name}</b>\n"
                        success_msg += f"<b>🟢⚡ الفئة: {category_name}</b>\n"
                        success_msg += f"<b>🟢⚡ السعر: {price:,.2f} ليرة</b>\n"
                        success_msg += f"<b>🟢⚡ الايدي: {player_id}</b>\n"
                        success_msg += f"<b>🟢⚡ الكمية: {qty if qty else 1}</b>\n"
                        success_msg += f"<b>🟢⚡ الوقت المستغرق: {elapsed_time:.2f} ثانية</b>\n"
                        success_msg += f"<b>🟢⚡ رد النظام: تم القبول بنجاح✅️</b>\n"
                        success_msg += f"<b>━━━━━━━━━━━━━━━━━━━━</b>"
                        send_message(target_user_id, success_msg)
                        
                        edit_message(chat_id, message_id, "✅ تم قبول طلب الشحن")
        
        elif data.startswith("reject_order_"):
            if is_admin_user:
                order_id = int(data.split("_")[2])
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute("SELECT user_id, price FROM shop_orders WHERE id = ?", (order_id,))
                    order = c.fetchone()
                    if order:
                        target_user_id, price = order
                        update_user_balance(target_user_id, price)
                        c.execute("UPDATE shop_orders SET status = 'rejected' WHERE id = ?", (order_id,))
                        send_message(target_user_id, "❌ تم رفض طلب الشحن، وتم استرداد رصيدك")
                edit_message(chat_id, message_id, "❌ تم رفض طلب الشحن")
        
        elif data.startswith("accept_deposit_"):
            if is_admin_user:
                request_id = int(data.split("_")[2])
                success, req_user_id, amount = accept_deposit_request(request_id)
                if success:
                    user_msg = f"🎉تم معالجة ايداعك بنجاح✅:\n"
                    user_msg += f"⚡المبلغ: {amount:,.2f} ليرة\n"
                    user_msg += f"❤تم قبول الايداع بنجاح نتمنة لك تجربة ممتعة❤"
                    send_message(req_user_id, user_msg)
                    edit_message(chat_id, message_id, f"✅ تم قبول طلب الإيداع بنجاح\n💰 المبلغ: {amount:,.2f} ليرة")
                else:
                    edit_message(chat_id, message_id, "❌ فشل في قبول الطلب")
        
        elif data.startswith("reject_deposit_"):
            if is_admin_user:
                request_id = int(data.split("_")[2])
                reject_deposit_request(request_id)
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute("SELECT user_id FROM deposit_requests WHERE id = ?", (request_id,))
                    result = c.fetchone()
                    if result:
                        target_user_id = result[0]
                        reject_msg = f"❌ تم رفض ايداعك من قبل الادارة❌\n\n⚠يرجى تأكد من المعلومات وتواصل مع دعم⚠"
                        send_message(target_user_id, reject_msg)
                edit_message(chat_id, message_id, "❌ تم رفض طلب الإيداع")
        
        elif data == "back_to_products":
            sections = get_all_sections()
            keyboard = {"inline_keyboard": []}
            for section in sections:
                sec_id, sec_name, color, emoji, is_active = section
                keyboard["inline_keyboard"].append([{"text": sec_name, "callback_data": f"back_to_{sec_name}"}])
            edit_message(chat_id, message_id, "اختر القسم:", reply_markup=keyboard)
        
        elif data.startswith("back_to_"):
            section_name = data.split("_")[2]
            keyboard, msg = get_products_buttons(section_name)
            edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data == "admin_products" and is_admin_user:
            keyboard = get_admin_products_keyboard()
            edit_message(chat_id, message_id, "📦 إدارة المنتجات:", reply_markup=keyboard)
        
        elif data == "manage_description" and is_admin_user:
            keyboard = get_description_management_keyboard()
            edit_message(chat_id, message_id, "📝 إدارة وصف الأقسام:", reply_markup=keyboard)
        
        elif data.startswith("desc_section_"):
            section_name = data[len("desc_section_"):]
            user_states[user_id] = {"action": "waiting_description_text", "desc_category": section_name}
            edit_message(chat_id, message_id, f"📝 قسم {section_name}\nارسل وصف القسم الجديد:")
        
        elif data == "manage_images" and is_admin_user:
            keyboard, msg = get_products_for_image_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data == "product_description_menu" and is_admin_user:
            keyboard, msg = get_products_for_product_desc_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data == "delete_category_menu" and is_admin_user:
            keyboard, msg = get_delete_category_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data.startswith("delete_category_"):
            if is_admin_user:
                category_id = int(data.split("_")[2])
                delete_category(category_id)
                edit_message(chat_id, message_id, "✅ تم حذف الفئة بنجاح")
        
        elif data == "add_counter_category" and is_admin_user:
            products = get_all_products()
            if not products:
                edit_message(chat_id, message_id, "🚫 لا توجد منتجات")
            else:
                keyboard = {"inline_keyboard": []}
                for product in products:
                    product_id, name, category, emoji, desc, img = product
                    display_name = f"{emoji} {name}" if emoji else name
                    keyboard["inline_keyboard"].append([{"text": f"{display_name} ({category})", "callback_data": f"counter_category_{product_id}"}])
                keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
                edit_message(chat_id, message_id, "اختر المنتج لإضافة فئة عداد:", reply_markup=keyboard)
        
        elif data == "add_limited_category" and is_admin_user:
            products = get_all_products()
            if not products:
                edit_message(chat_id, message_id, "🚫 لا توجد منتجات")
            else:
                keyboard = {"inline_keyboard": []}
                for product in products:
                    product_id, name, category, emoji, desc, img = product
                    display_name = f"{emoji} {name}" if emoji else name
                    keyboard["inline_keyboard"].append([{"text": f"{display_name} ({category})", "callback_data": f"limited_category_{product_id}"}])
                keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "admin_products"}])
                edit_message(chat_id, message_id, "اختر المنتج لإضافة فئة (كمية محددة):", reply_markup=keyboard)
        
        elif data.startswith("counter_category_"):
            if is_admin_user:
                product_id = int(data.split("_")[2])
                product = get_product_by_id(product_id)
                if product:
                    user_states[user_id] = {"action": "waiting_category_name", "product_id": product_id, "product_name": product[1], "cat_type": "counter"}
                    edit_message(chat_id, message_id, f"📝 {product[1]}\nاكتب اسم الفئة:")
        
        elif data.startswith("limited_category_"):
            if is_admin_user:
                product_id = int(data.split("_")[2])
                product = get_product_by_id(product_id)
                if product:
                    user_states[user_id] = {"action": "waiting_category_name", "product_id": product_id, "product_name": product[1], "cat_type": "limited"}
                    edit_message(chat_id, message_id, f"📝 {product[1]}\nاكتب اسم الفئة:")
        
        elif data.startswith("image_product_"):
            product_id = int(data.split("_")[2])
            product = get_product_by_id(product_id)
            if product:
                user_states[user_id] = {"action": "waiting_product_image", "product_id": product_id}
                edit_message(chat_id, message_id, f"🖼️ المنتج: {product[1]}\nارسل الصورة (ليس رابط، ارسل الصورة كصورة):")
        
        elif data.startswith("desc_product_"):
            product_id = int(data.split("_")[2])
            product = get_product_by_id(product_id)
            if product:
                user_states[user_id] = {"action": "waiting_product_description", "product_id": product_id}
                edit_message(chat_id, message_id, f"📝 المنتج: {product[1]}\nارسل وصف المنتج الجديد:")
        
        elif data == "deposit_management" and is_admin_user:
            keyboard = get_deposit_management_keyboard()
            edit_message(chat_id, message_id, "💳 إدارة الايداعات:", reply_markup=keyboard)
        
        elif data == "add_deposit" and is_admin_user:
            user_states[user_id] = {"action": "waiting_deposit_title"}
            edit_message(chat_id, message_id, "✏️ اكتب اسم طريقة الإيداع:")
        
        elif data == "edit_deposit" and is_admin_user:
            keyboard, msg = get_deposit_methods_for_edit()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data.startswith("edit_method_"):
            if is_admin_user:
                parts = data.split("_")
                if len(parts) >= 3 and parts[2].isdigit():
                    method_id = int(parts[2])
                    method = get_deposit_method_by_id(method_id)
                    if method:
                        # لا يُسمح بتغيير عنوان "سيريتل كاش تلقائي" حفاظاً على عمل الميزة التلقائية
                        if method[1] == 'سيريتل كاش تلقائي':
                            user_states[user_id] = {
                                "action": "waiting_edit_description",
                                "method_id": method_id,
                                "new_title": method[1],
                            }
                            edit_message(
                                chat_id, message_id,
                                f"✏️ تعديل الطريقة: {method[1]}\n"
                                f"(لا يمكن تغيير الاسم لهذه الطريقة)\n\n"
                                f"اكتب الوصف الجديد:"
                            )
                        else:
                            user_states[user_id] = {"action": "waiting_edit_title", "method_id": method_id}
                            edit_message(chat_id, message_id, f"✏️ تعديل الطريقة: {method[1]}\n\nاكتب الاسم الجديد:")
        
        elif data == "delete_deposit" and is_admin_user:
            keyboard, msg = get_deposit_methods_for_delete()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data.startswith("delete_method_"):
            if is_admin_user:
                parts = data.split("_")
                if len(parts) >= 3 and parts[2].isdigit():
                    method_id = int(parts[2])
                    delete_deposit_method(method_id)
                    edit_message(chat_id, message_id, "✅ تم حذف طريقة الإيداع")
        
        elif data == "add_product" and is_admin_user:
            user_states[user_id] = {"action": "waiting_product_name"}
            edit_message(chat_id, message_id, "✏️ أرسل اسم المنتج:")
        
        elif data.startswith("save_product_"):
            if is_admin_user:
                section_name = data.split("_")[2]
                if user_id in user_states:
                    add_product(user_states[user_id]["product_name"], section_name, "", "", "")
                    del user_states[user_id]
                    edit_message(chat_id, message_id, f"✅ تم اضافة المنتج لقسم {section_name}")
        
        elif data == "add_category_menu" and is_admin_user:
            keyboard, msg = get_products_for_category_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data.startswith("select_product_"):
            if is_admin_user:
                parts = data.split("_")
                if len(parts) >= 3 and parts[2].isdigit():
                    product_id = int(parts[2])
                    product = get_product_by_id(product_id)
                    if product:
                        user_states[user_id] = {"action": "waiting_category_name", "product_id": product_id, "product_name": product[1]}
                        edit_message(chat_id, message_id, f"📝 {product[1]}\nاكتب اسم الفئة:")
        
        elif data == "delete_product_menu" and is_admin_user:
            keyboard, msg = get_delete_products_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data.startswith("delete_product_"):
            if is_admin_user:
                parts = data.split("_")
                if len(parts) >= 3 and parts[2].isdigit():
                    product_id = int(parts[2])
                    delete_product(product_id)
                    edit_message(chat_id, message_id, "✅ تم حذف المنتج")
        
        elif data == "list_products" and is_admin_user:
            products = get_all_products()
            if not products:
                edit_message(chat_id, message_id, "📋 لا توجد منتجات")
            else:
                msg = "📋 المنتجات:\n\n"
                for p in products:
                    product_id, name, category, emoji, desc, img = p
                    display_name = f"{emoji} {name}" if emoji else name
                    msg += f"🆔 {product_id} | {display_name} | {category}\n"
                    if desc:
                        msg += f"   📝 وصف: {desc[:50]}...\n"
                    if img:
                        msg += f"   🖼️ صورة: موجودة\n"
                    for cat in get_categories_by_product(product_id):
                        cat_id, cat_name, price_usd, cat_type, min_qty, max_qty = cat
                        if cat_type == 'counter':
                            msg += f"   └─ {cat_name} (عداد) - {price_usd:.2f}$ (حد: {min_qty}-{max_qty})\n"
                        elif cat_type == 'limited':
                            msg += f"   └─ {cat_name} (كمية محددة) - {price_usd:.2f}$ (حد: {min_qty}-{max_qty})\n"
                        else:
                            msg += f"   └─ {cat_name} - {price_usd:.2f}$\n"
                edit_message(chat_id, message_id, msg[:4000])
        
        elif data == "sections_management" and is_admin_user:
            keyboard, msg = get_sections_management_keyboard()
            edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data == "add_section" and is_admin_user:
            user_states[user_id] = {"action": "waiting_new_section"}
            edit_message(chat_id, message_id, "📝 اكتب اسم القسم الجديد:")
        
        elif data.startswith("delete_section_"):
            if is_admin_user:
                section_id = int(data.split("_")[2])
                delete_section(section_id)
                edit_message(chat_id, message_id, "✅ تم حذف القسم بنجاح")
        
        elif data.startswith("color_section_"):
            if is_admin_user:
                section_id = int(data.split("_")[2])
                keyboard = get_color_selection_keyboard(section_id)
                edit_message(chat_id, message_id, "🎨 اختر لون القسم:", reply_markup=keyboard)
        
        elif data.startswith("set_color_"):
            if is_admin_user:
                parts = data.split("_")
                section_id = int(parts[2])
                color = parts[3]
                update_section_color(section_id, color)
                section = get_section_by_id(section_id)
                if section:
                    color_emoji = "🟢" if color == "success" else ("🔴" if color == "danger" else "🔵")
                    edit_message(chat_id, message_id, f"✅ تم تغيير لون القسم {section[1]} إلى {color_emoji}")
        
        elif data == "admin_api" and is_admin_user:
            interval_now = get_auto_price_interval()
            keyboard = {"inline_keyboard": [
                [{"text": "✅ اختبار الربط", "callback_data": "test_api"}],
                [{"text": "❄️ معلومات الربط", "callback_data": "api_info"}],
                [{"text": "🔄 ربط المنتجات", "callback_data": "link_products"}],
                [{"text": "🔓 فك ربط المنتجات", "callback_data": "unlink_products"}],
                [{"text": "🔍 بحث بين المنتجات", "callback_data": "search_products"}],
                [{"text": "💱 جلب أسعار المرتبطة", "callback_data": "refresh_linked_prices"}],
                [{"text": f"⏱️ فترة التحديث: {interval_now} دقيقة", "callback_data": "set_auto_interval"}],
                [{"text": "🔙 رجوع", "callback_data": "back_admin"}]
            ]}
            edit_message(chat_id, message_id, "🔐 إدارة API:", reply_markup=keyboard)
        
        elif data == "test_api" and is_admin_user:
            profile = get_profile()
            if profile:
                balance = profile.get("balance", profile.get("credit", profile.get("wallet", "—")))
                email   = profile.get("email", profile.get("username", profile.get("name", "لا يوجد")))
                source  = current_api_source
                text = (
                    f"✅ الاتصال مع API ناجح!\n\n"
                    f"🌐 المصدر: {source}\n"
                    f"📧 البريد/الاسم: {email}\n"
                    f"💰 الرصيد: {balance}"
                )
                edit_message(chat_id, message_id, text)
            else:
                edit_message(chat_id, message_id, "❌ فشل الاتصال — تحقق من التوكن ورابط API")
        
        elif data == "api_info" and is_admin_user:
            profile = get_profile()
            if profile:
                balance = profile.get("balance", profile.get("credit", profile.get("wallet", "—")))
                email = profile.get("email", profile.get("username", profile.get("name", "لا يوجد")))
                text = f"❄ معلومات ربطك:\n💰 رصيدك: {balance}\n📧 ايميلك/اسمك: {email}"
                edit_message(chat_id, message_id, text)
            else:
                edit_message(chat_id, message_id, "❌ لا توجد معلومات — تحقق من صحة التوكن ورابط الـ API")
        
        elif data == "refresh_linked_prices" and is_admin_user:
            edit_message(chat_id, message_id, "⏳ جاري جلب أسعار المنتجات المرتبطة من API...")
            import threading
            def _do_refresh():
                results = refresh_linked_prices_from_api()
                if not results:
                    send_message(chat_id, "⚠️ لا توجد منتجات مرتبطة أو لم يتم تحديث أي سعر.")
                else:
                    summary = "\n".join(results)
                    msg = f"✅ تم تحديث أسعار المنتجات المرتبطة:\n\n{summary}"
                    if len(msg) > 4000:
                        msg = msg[:3990] + "\n..."
                    send_message(chat_id, msg)
            threading.Thread(target=_do_refresh, daemon=True).start()

        elif data == "link_products" and is_admin_user:
            keyboard, msg = get_link_products_menu()
            edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data.startswith("link_section_"):
            if is_admin_user:
                section_name = data.split("_")[2]
                products = get_products_by_category(section_name)
                if not products:
                    edit_message(chat_id, message_id, f"🚫 لا توجد منتجات في قسم {section_name}")
                else:
                    keyboard = {"inline_keyboard": []}
                    for product in products:
                        product_id, product_name, emoji = product
                        display_name = f"{emoji} {product_name}" if emoji else product_name
                        categories = get_categories_by_product(product_id)
                        for cat in categories:
                            cat_id, cat_name, price_usd, cat_type, min_qty, max_qty = cat
                            if cat_type == 'default':
                                keyboard["inline_keyboard"].append([{"text": f"{display_name} - {cat_name}", "callback_data": f"link_category_{cat_id}"}])
                    keyboard["inline_keyboard"].append([{"text": "🔙 رجوع", "callback_data": "link_products"}])
                    edit_message(chat_id, message_id, "اختر الفئة لربطها بمنتج من API:", reply_markup=keyboard)
        
        elif data.startswith("link_category_"):
            if is_admin_user:
                category_id = int(data.split("_")[2])
                category = get_category_by_id(category_id)
                if category:
                    cat_id, product_id, cat_name, price_usd, cat_type, min_qty, max_qty = category
                    product = get_product_by_id(product_id)
                    msg = f"🔶المنتج: {product[1]}\n"
                    msg += f"🔶الفئة: {cat_name}\n"
                    msg += f"🔷ارسل ايدي المنتج من الApi لربطه:"
                    user_states[user_id] = {"action": "waiting_api_product_id", "product_id": product_id, "category_id": category_id, "product_name": product[1], "category_name": cat_name}
                    edit_message(chat_id, message_id, msg)
        
        elif data == "unlink_products" and is_admin_user:
            keyboard, msg = get_unlink_products_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data.startswith("unlink_"):
            if is_admin_user:
                link_id = int(data.split("_")[1])
                unlink_product(link_id)
                edit_message(chat_id, message_id, "✅ تم فك ربط المنتج بنجاح")
        
        elif data == "search_products" and is_admin_user:
            user_states[user_id] = {"action": "waiting_search_term"}
            edit_message(chat_id, message_id, "✏️ اكتب اسم المنتج للبحث عنه:")
        
        elif data == "set_auto_interval" and is_admin_user:
            current_interval = get_auto_price_interval()
            user_states[user_id] = {"action": "waiting_auto_interval"}
            edit_message(chat_id, message_id,
                f"⏱️ فترة التحديث التلقائي الحالية: <b>{current_interval} دقيقة</b>\n\n"
                f"أرسل الفترة الجديدة بالدقائق (الحد الأدنى 1 دقيقة):\n"
                f"مثال: 30 (كل نصف ساعة) أو 60 (كل ساعة) أو 120 (كل ساعتين)")

        elif data == "edit_exchange_rate" and is_admin_user:
            current_rate = get_exchange_rate()
            user_states[user_id] = {"action": "waiting_exchange_rate"}
            edit_message(chat_id, message_id, f"💰 سعر الصرف الحالي: {current_rate:,.2f} ليرة = 1$\n\nارسل سعر الصرف الجديد بليرة سورية:")
        
        elif data == "user_management" and is_admin_user:
            keyboard = get_user_management_keyboard()
            edit_message(chat_id, message_id, "👥 إدارة المستخدمين:", reply_markup=keyboard)
        
        elif data == "add_balance" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_add_balance"}
            edit_message(chat_id, message_id, "📝 ارسل ايدي المستخدم لاضافة رصيد:")
        
        elif data == "deduct_balance" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_deduct_balance"}
            edit_message(chat_id, message_id, "📝 ارسل ايدي المستخدم لخصم رصيد:")
        
        elif data == "add_admin" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_add_admin"}
            edit_message(chat_id, message_id, "👑 ارسل ايدي المستخدم لاضافته للادمن:")
        
        elif data == "remove_admin" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_remove_admin"}
            edit_message(chat_id, message_id, "🗑 ارسل ايدي المستخدم لحذفه من الادمن:")
        
        elif data == "user_info" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_info"}
            edit_message(chat_id, message_id, "🔍 ارسل ايدي المستخدم للكشف:")
        
        elif data == "block_user" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_block"}
            edit_message(chat_id, message_id, "🚫 ارسل ايدي المستخدم لحظره:")
        
        elif data == "unblock_user" and is_admin_user:
            user_states[user_id] = {"action": "waiting_user_id_for_unblock"}
            edit_message(chat_id, message_id, "🔓 ارسل ايدي المستخدم لفك حظره:")
        
        elif data == "general_stats" and is_admin_user:
            users_count = get_total_users_count()
            total_balance = get_total_balance()
            total_orders = get_total_orders()
            msg = f"📊 <b>الاحصائية العامة:</b>\n\n"
            msg += f"👥 <b>اجمالي المستخدمين المسجلين في البوت: {users_count}</b>\n"
            msg += f"💰 <b>اجمالي الارصدة بليرة: {total_balance:,.2f} ليرة</b>\n"
            msg += f"📦 <b>اجمالي الطلبات المنفذة: {total_orders}</b>"
            edit_message(chat_id, message_id, msg)
        
        elif data == "discount_management" and is_admin_user:
            keyboard = get_discount_management_keyboard()
            edit_message(chat_id, message_id, "🎁 إدارة الخصومات:", reply_markup=keyboard)
        
        elif data == "add_discount" and is_admin_user:
            user_states[user_id] = {"action": "waiting_discount_user_id"}
            edit_message(chat_id, message_id, "🎁 ارسل ايدي المستخدم لوضع خصم له:")
        
        elif data == "remove_discount" and is_admin_user:
            user_states[user_id] = {"action": "waiting_remove_discount_user_id"}
            edit_message(chat_id, message_id, "🗑 ارسل ايدي المستخدم لحذف الخصم:")
        
        elif data == "list_discount_users" and is_admin_user:
            users_with_discount = get_all_users_with_discount()
            if not users_with_discount:
                edit_message(chat_id, message_id, "📋 لا يوجد مستخدمين لديهم خصومات حالياً")
            else:
                msg = "🎁 <b>المستخدمين المميزين (لديهم خصم):</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                for user in users_with_discount:
                    uid, dname, dpercent = user
                    user_info = get_telegram_user_info(uid)
                    msg += f"👤 المستخدم: {user_info['name']}\n"
                    msg += f"🆔 الايدي: {uid}\n"
                    msg += f"🎁 اسم الخصم: {dname}\n"
                    msg += f"📊 نسبة الخصم: {dpercent}%\n"
                    msg += "━━━━━━━━━━━━━━━━━━━━\n"
                edit_message(chat_id, message_id, msg)
        
        elif data == "subscription_management" and is_admin_user:
            keyboard, msg = get_subscription_management_keyboard()
            edit_message(chat_id, message_id, msg, reply_markup=keyboard)
        
        elif data == "add_channel" and is_admin_user:
            user_states[user_id] = {"action": "waiting_channel_username"}
            edit_message(chat_id, message_id, "📢 ارسل معرف قناة الاشتراك الاجباري:\n\n• قناة عامة: @username\n• قناة خاصة: المعرف الرقمي مثل -1001234567890\n• أو رابط https://t.me/...\n\n⚠️ يجب أن يكون البوت مشرفاً في القناة")
        
        elif data.startswith("delete_channel_"):
            if is_admin_user:
                channel_id = int(data.split("_")[2])
                delete_required_channel(channel_id)
                edit_message(chat_id, message_id, "✅ تم حذف القناة بنجاح")
        
        elif data == "check_sub":
            if check_user_subscribed(user_id):
                send_message(chat_id, "✅ تم التحقق من اشتراكك، مرحبا بك في البوت", reply_markup=get_main_keyboard(is_admin(user_id)))
                delete_message(chat_id, message_id)
            else:
                send_message(chat_id, "⚠️ عذرا عليك الاشتراك بالقنوات التالية:", reply_markup=get_check_subscription_keyboard())
        
        elif data == "broadcast_menu" and is_admin_user:
            keyboard = get_broadcast_keyboard()
            edit_message(chat_id, message_id, "📣 الاذاعة:", reply_markup=keyboard)
        
        elif data == "broadcast_all" and is_admin_user:
            user_states[user_id] = {"action": "waiting_broadcast_message"}
            edit_message(chat_id, message_id, "📝 اكتب الاذاعة التي تريد اذاعتها لجميع المستخدمين:")
        
        elif data == "broadcast_user" and is_admin_user:
            user_states[user_id] = {"action": "waiting_broadcast_user_id"}
            edit_message(chat_id, message_id, "🆔 ارسل ايدي المستخدم لارسال اذاعة له:")
        
        elif data == "basic_settings" and is_admin_user:
            keyboard = get_basic_settings_keyboard()
            edit_message(chat_id, message_id, "⚙️ الاعدادات الاساسية:", reply_markup=keyboard)
        
        elif data == "rename_sections" and is_admin_user:
            keyboard, msg = get_rename_sections_menu()
            if keyboard:
                edit_message(chat_id, message_id, msg, reply_markup=keyboard)
            else:
                edit_message(chat_id, message_id, msg)
        
        elif data.startswith("rename_section_"):
            if is_admin_user:
                section_id = int(data.split("_")[2])
                section = get_section_by_id(section_id)
                if section:
                    user_states[user_id] = {"action": "waiting_new_section_name", "section_id": section_id}
                    edit_message(chat_id, message_id, f"✏️ تعديل اسم القسم: {section[1]}\n\nاكتب اسم القسم الجديد:")
        
        elif data == "general_settings" and is_admin_user:
            keyboard = get_general_settings_keyboard()
            edit_message(chat_id, message_id, "🔄 الاعدادات العامة:", reply_markup=keyboard)
        
        elif data == "toggle_bot" and is_admin_user:
            new_status = not _get_bot_enabled_from_db()
            set_bot_status(new_status)
            status_text = "🟢 تم تشغيل البوت" if new_status else "🔴 تم ايقاف البوت"
            edit_message(chat_id, message_id, f"✅ {status_text}")
        
        elif data == "change_welcome" and is_admin_user:
            user_states[user_id] = {"action": "waiting_welcome_message"}
            edit_message(chat_id, message_id, "📝 ارسل رسالة ترحيب الجديدة:")
        
        elif data == "change_support" and is_admin_user:
            user_states[user_id] = {"action": "waiting_support_username"}
            current = support_username if support_username else "غير محدد"
            edit_message(chat_id, message_id, f"👨‍💻 معرف الدعم الحالي: @{current}\n\nارسل معرف الدعم الجديد (بدون @):")
        
        elif data == "set_deposit_channel" and is_admin_user:
            user_states[user_id] = {"action": "waiting_deposit_channel_id"}
            current = DEPOSIT_CHANNEL_ID if DEPOSIT_CHANNEL_ID else "غير محدد"
            edit_message(chat_id, message_id, f"💳 قناة الإيداعات الحالية: {current}\n\nارسل معرف القناة (مثال: -1001234567890 أو @channel_name):\nأو اكتب 'مسح' لإلغاء القناة:")
        
        elif data == "recover_orders" and is_admin_user:
            edit_message(chat_id, message_id, "🔄 جاري فحص الطلبات المعلّقة من المزود... سيصلك تقرير مفصّل عند الانتهاء، وستصل التحديثات للمستخدمين والقناة عند اكتمال أو رفض كل طلب.")
            threading.Thread(target=recover_pending_api_orders, kwargs={"triggered_by": chat_id}, daemon=True).start()

        elif data == "sms_setup" and is_admin_user:
            current = SMS_FORWARDER_CHAT_ID if SMS_FORWARDER_CHAT_ID else "غير محدد"
            msg_txt = (
                f"📱 <b>إعداد الإيداع التلقائي - سيريتل كاش</b>\n\n"
                f"🔗 القناة الحالية المراقبة: <code>{current}</code>\n\n"
                f"📋 <b>كيف يعمل النظام:</b>\n"
                f"1️⃣ تطبيق SMS Forwarder يستقبل رسائل سيريتل ويرسلها إلى قناة تيليغرام\n"
                f"2️⃣ البوت يراقب تلك القناة ويستخرج المبلغ ورقم المرسل\n"
                f"3️⃣ يضيف الرصيد تلقائياً للمستخدم الذي سجّل هذا الرقم عبر /setphone\n\n"
                f"📌 <b>أرسل الآن Chat ID للقناة التي ترسل إليها تطبيق SMS Forwarder</b>\n"
                f"مثال: <code>-1001234567890</code>\n\n"
                f"أو اكتب <b>مسح</b> لإلغاء الربط."
            )
            user_states[user_id] = {"action": "waiting_sms_chat_id"}
            edit_message(chat_id, message_id, msg_txt)

        elif data.startswith("sms_credit_") and is_admin_user:
            try:
                amount_syp = int(data.split("_")[2])
            except Exception:
                amount_syp = 0
            user_states[user_id] = {"action": "waiting_sms_credit_user_id", "sms_amount_syp": amount_syp}
            edit_message(chat_id, message_id, f"💰 المبلغ: {amount_syp:,} ل.س\n\n📌 أرسل ID المستخدم في تيليغرام:")

        elif data == "sms_ignore" and is_admin_user:
            edit_message(chat_id, message_id, "🗑 تم تجاهل الإيداع.")

        elif data == "set_api_orders_channel" and is_admin_user:
            user_states[user_id] = {"action": "waiting_api_orders_channel_id"}
            current = API_ORDERS_CHANNEL_ID if API_ORDERS_CHANNEL_ID else "غير محدد"
            edit_message(chat_id, message_id, f"🔗 قناة طلبات API الحالية: {current}\n\nارسل معرف القناة (مثال: -1001234567890 أو @channel_name):\nأو اكتب 'مسح' لإلغاء القناة:")
        
        elif data == "edit_prices" and is_admin_user:
            # عرض قائمة بجميع المنتجات/الفئات مع أسعارها بالدولار
            try:
                with get_db() as _conn:
                    _c = _conn.cursor()
                    _c.execute("""
                        SELECT c.id, p.name, c.name, c.price
                        FROM categories c
                        JOIN products p ON c.product_id = p.id
                        ORDER BY p.name, c.name
                    """)
                    all_cats = _c.fetchall()
            except Exception as _e:
                edit_message(chat_id, message_id, f"❌ خطأ في جلب المنتجات: {_e}")
                return
            if not all_cats:
                edit_message(chat_id, message_id, "❌ لا توجد فئات/أسعار بعد")
                return
            rows = []
            for cat_id, prod_name, cat_name, price in all_cats:
                label = f"{prod_name} ← {cat_name}: {price:.2f}$"
                rows.append([{"text": label, "callback_data": f"edit_price_cat_{cat_id}"}])
            rows.append([{"text": "🔙 رجوع", "callback_data": "back_admin"}])
            edit_message(chat_id, message_id, "✏️ اختر الفئة لتعديل سعرها بالدولار:", reply_markup={"inline_keyboard": rows})

        elif data.startswith("edit_price_cat_") and is_admin_user:
            try:
                cat_id = int(data.split("edit_price_cat_")[1])
            except Exception:
                edit_message(chat_id, message_id, "❌ فئة غير صحيحة")
                return
            category = get_category_by_id(cat_id)
            if not category:
                edit_message(chat_id, message_id, "❌ الفئة غير موجودة")
                return
            cat_id_db, product_id, cat_name, price_usd, cat_type, min_qty, max_qty = category
            product = get_product_by_id(product_id)
            prod_name = product[1] if product else "غير معروف"
            exchange_rate = get_exchange_rate()
            user_states[user_id] = {"action": "waiting_edit_price_dollar", "category_id": cat_id}
            edit_message(chat_id, message_id,
                f"✏️ <b>تعديل سعر الفئة</b>\n"
                f"🔷 المنتج: {prod_name}\n"
                f"🔷 الفئة: {cat_name}\n"
                f"🔷 السعر الحالي: <b>{price_usd:.4f}$</b>\n"
                f"   (يعادل: {price_usd * exchange_rate:,.2f} ليرة)\n\n"
                f"📝 أرسل السعر الجديد بالدولار (مثال: 0.15):")

        elif data == "back_admin" and is_admin_user:
            keyboard = {"inline_keyboard": [
                [{"text": "📂 إدارة الأقسام", "callback_data": "sections_management"}, {"text": "🔐 إدارة API", "callback_data": "admin_api"}],
                [{"text": "📦 إدارة المنتجات", "callback_data": "admin_products"}],
                [{"text": "💳 إدارة الايداعات", "callback_data": "deposit_management"}, {"text": "👥 إدارة المستخدمين", "callback_data": "user_management"}],
                [{"text": "🎁 إدارة الخصومات", "callback_data": "discount_management"}],
                [{"text": "📢 إدارة الاشتراك الاجباري", "callback_data": "subscription_management"}],
                [{"text": "📣 الاذاعة", "callback_data": "broadcast_menu"}, {"text": "⚙️ الاعدادات الاساسية", "callback_data": "basic_settings"}],
                [{"text": "🔄 الاعدادات العامة", "callback_data": "general_settings"}],
                [{"text": "💰 تعديل سعر صرف", "callback_data": "edit_exchange_rate"}, {"text": "✏️ تعديل أسعار المنتجات بالدولار", "callback_data": "edit_prices"}],
                [{"text": "➕ إعدادات API", "callback_data": "api_settings"}],
                [{"text": "🔄 استرجاع الطلبات المعلّقة", "callback_data": "recover_orders"}],
                [{"text": "📱 إعداد SMS سيريتل", "callback_data": "sms_setup"}],
                [{"text": "🔙 رجوع", "callback_data": "back_main"}]
            ]}
            edit_message(chat_id, message_id, "🛡️ لوحة التحكم:", reply_markup=keyboard)
        
        elif data == "back_main":
            send_message(chat_id, _get_welcome_message_from_db(), reply_markup=get_main_keyboard(is_admin(user_id)))
    
    except Exception as e:
        print(f"Error in handle_callback: {e}")
        traceback.print_exc()
        send_message(chat_id, f"❌ خطأ: {str(e)}")

DB_FILE = _DB_PATH


def init_files():
    """ينشئ كل الملفات التي يحتاجها البوت تلقائياً عند بدء التشغيل."""
    db_existed = os.path.exists(DB_FILE)
    init_db()  # ينشئ products.db والجداول والقيم الافتراضية إن لم تكن موجودة
    if not db_existed:
        print(f"📁 تم إنشاء قاعدة البيانات: {DB_FILE}")
    else:
        print(f"📁 قاعدة البيانات موجودة: {DB_FILE}")
    print(f"🔗 المصدر النشط: مهد")
    u = STORE_API_URLS.get('source1', "") or "—"
    print(f"   ✅ مهد: {u}")
    print(f"📝 لتعديل رابط أو توكن مهد: استخدم لوحة الأدمن (إعدادات API).")
    print(f"💳 قناة الإيداعات: {DEPOSIT_CHANNEL_ID or 'غير محددة'}")
    print(f"📦 قناة طلبات API/الشحن: {API_ORDERS_CHANNEL_ID or 'غير محددة'}")
    print(f"🤖 قناة الإيداع التلقائي (سيريتل كاش): {AUTO_CREDIT_CHANNEL_ID}")


def delete_webhook_and_drop_updates():
    global last_update_id
    try:
        # ملاحظة مهمة: drop_pending_updates=false كي لا نفقد رسائل القناة المُتراكمة
        # أثناء إعادة التشغيل (channel_post) — البوت سيلتقطها فور بدء الاستطلاع.
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=false"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("ok"):
            print("✅ تم حذف Webhook (مع الاحتفاظ بالتحديثات المعلّقة)")
        else:
            print(f"⚠️ deleteWebhook: {data}")
    except Exception as e:
        print(f"⚠️ تعذر حذف Webhook: {e}")
    # نُمرِّر allowed_updates ضمن نفس طلب getUpdates في حلقة الاستطلاع — لا حاجة لتجاهل التحديثات هنا


def _run_flask_keepalive():
    """محذوف — الخادم الحقيقي يُشغَّل من main.py أو server.py"""
    pass


_LOCK_FILE = None
_LOCK_FD = None

def acquire_single_instance_lock():
    global _LOCK_FILE, _LOCK_FD
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
    try:
        _LOCK_FD = open(lock_path, "w")
        if _HAS_FCNTL:
            _fcntl_module.flock(_LOCK_FD, _fcntl_module.LOCK_EX | _fcntl_module.LOCK_NB)
        _LOCK_FD.write(str(os.getpid()))
        _LOCK_FD.flush()
        print(f"🔒 قفل النسخة الواحدة: {lock_path} (PID={os.getpid()})")
        return True
    except (IOError, OSError):
        print("❌ نسخة أخرى من البوت تعمل بالفعل! لا يمكن تشغيل نسختين.")
        return False


def _auto_price_update_loop():
    """خيط خلفي: يحدّث أسعار المنتجات المرتبطة تلقائياً كل N دقيقة."""
    import time as _time
    # انتظر دقيقتين عند البدء لإعطاء البوت وقتاً للتهيئة
    _time.sleep(120)
    while True:
        try:
            interval_min = get_auto_price_interval()
            results = refresh_linked_prices_from_api()
            if results:
                count_ok  = sum(1 for r in results if r.startswith("✅"))
                count_err = len(results) - count_ok
                print(f"[auto_price] ✅ تحديث تلقائي: {count_ok} منتج، {count_err} خطأ")
            else:
                print("[auto_price] لا توجد منتجات مرتبطة — تم تخطي التحديث")
        except Exception as e:
            print(f"[auto_price] خطأ في التحديث التلقائي: {e}")
        # انتظر الفترة المحددة قبل الدورة القادمة
        interval_sec = get_auto_price_interval() * 60
        _time.sleep(max(60, interval_sec))


def main():
    global last_update_id
    if not acquire_single_instance_lock():
        sys.exit(1)
    init_files()
    delete_webhook_and_drop_updates()
    print("✅ البوت يعمل...")
    # فحص الطلبات المعلقة من قبل التحديث في الخلفية
    threading.Thread(target=recover_pending_api_orders, kwargs={"startup_delay": True}, daemon=True).start()
    # تحديث تلقائي دوري لأسعار المنتجات المرتبطة
    threading.Thread(target=_auto_price_update_loop, daemon=True).start()
    
    while True:
        try:
            url = (
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
                f"?timeout=30&offset={last_update_id + 1}"
                f"&allowed_updates=%5B%22message%22%2C%22edited_message%22%2C%22channel_post%22%2C%22edited_channel_post%22%2C%22callback_query%22%5D"
            )
            r = requests.get(url, timeout=35)
            updates = r.json()
            
            if updates.get("ok") and updates.get("result"):
                for update in updates["result"]:
                    update_id = update["update_id"]
                    last_update_id = update_id

                    # ===== تسجيل تشخيصي: نوع التحديث الوارد =====
                    _kinds = [k for k in update.keys() if k != "update_id"]
                    print(f"[RAW_UPDATE] update_id={update_id} kinds={_kinds}")

                    # ===== حماية قاعدة البيانات: منع معالجة نفس التحديث مرتين =====
                    if not claim_update(update_id):
                        print(f"[SKIP] update_id={update_id} مُعالَج مسبقاً")
                        continue

                    if "message" in update:
                        msg = update["message"]
                        chat_id = msg["chat"]["id"]
                        user_id = msg["from"]["id"]
                        print(f"[UPDATE] update_id={update_id} user_id={user_id} text={repr(msg.get('text',''))}")

                        # ===== استقبال رسائل SMS من هاتف إعادة التوجيه =====
                        if "text" in msg and try_handle_sms_forward(chat_id, msg["text"]):
                            continue
                        
                        if "text" in msg:
                            txt = msg["text"]
                            if txt == "/recover_orders" and is_admin(user_id):
                                send_message(chat_id, "🔄 جاري فحص الطلبات المعلّقة... سيصلك تقرير مفصّل عند الانتهاء.")
                                threading.Thread(target=recover_pending_api_orders, kwargs={"triggered_by": chat_id}, daemon=True).start()
                            elif txt == "/start":
                                if not _get_bot_enabled_from_db() and user_id != MAIN_ADMIN_ID and not is_admin(user_id):
                                    send_message(chat_id, "⚠️ الــبــوت مــتــوقــف حــالــيًــا عــن الــعــمــل .⚠️")
                                else:
                                    send_message(chat_id, _get_welcome_message_from_db(), reply_markup=get_main_keyboard(is_admin(user_id)))
                            elif txt == "/deposit":
                                # استرجاع طريقة "سيريتل كاش تلقائي" من قاعدة البيانات
                                _auto_method = None
                                try:
                                    with get_db() as _conn:
                                        _c = _conn.cursor()
                                        _c.execute(
                                            "SELECT id, title, description, code, exchange_rate FROM deposit_methods WHERE title = ?",
                                            ('سيريتل كاش تلقائي',)
                                        )
                                        _auto_method = _c.fetchone()
                                except Exception:
                                    _auto_method = None

                                if _auto_method:
                                    _, _title, _description, _code, _rate = _auto_method
                                    send_message(chat_id,
                                        f"💳 <b>{_title}</b>\n\n"
                                        f"📝 {_description}\n\n"
                                        f"1️⃣ حوّل المبلغ إلى رقم: <code>{_code}</code>\n"
                                        f"2️⃣ سعر الصرف: <b>{_rate:,.0f} ل.س لكل 1$</b>\n"
                                        f"3️⃣ بعد التحويل أرسل رقم العملية ليُضاف رصيدك تلقائياً")
                                else:
                                    send_message(chat_id,
                                        "💳 <b>طريقة الإيداع عبر سيريتل كاش</b>\n\n"
                                        f"1️⃣ حوّل المبلغ إلى رقم: <code>0{SYRIATEL_SENDER_NUMBER}</code>\n"
                                        "2️⃣ بعد التحويل أرسل رقم العملية ليُضاف رصيدك تلقائياً")
                            elif txt.startswith("/setphone"):
                                parts = txt.split()
                                if len(parts) < 2:
                                    send_message(chat_id, "📱 أرسل رقم هاتفك بعد الأمر:\nمثال: /setphone 0991234567")
                                else:
                                    raw = parts[1].strip()
                                    digits = re.sub(r"\D", "", raw)
                                    if len(digits) < 8:
                                        send_message(chat_id, "❌ رقم الهاتف غير صحيح. مثال: /setphone 0991234567")
                                    else:
                                        link_phone(user_id, digits)
                                        send_message(chat_id, f"✅ تم ربط رقم <code>{raw}</code> بحسابك.\nستُضاف إيداعاتك تلقائياً عند استلام رسالة التأكيد.")
                            else:
                                handle_message(chat_id, user_id, txt)
                        elif "photo" in msg and user_id in user_states and user_states[user_id].get("action") == "waiting_product_image":
                            file_id = msg["photo"][-1]["file_id"]
                            product_id = user_states[user_id]["product_id"]
                            update_product_image(product_id, file_id)
                            send_message(chat_id, "✅ تم وضع صورة بنجاح وحفظها")
                            del user_states[user_id]
                    
                    elif "channel_post" in update or "edited_channel_post" in update:
                        cpost = update.get("channel_post") or update.get("edited_channel_post")
                        cp_chat_id = cpost["chat"]["id"]
                        cp_text = cpost.get("text") or cpost.get("caption") or ""
                        kind = "CHANNEL_POST" if "channel_post" in update else "EDITED_CHANNEL_POST"
                        print(f"[{kind}] chat_id={cp_chat_id} text={repr(cp_text[:120])}")
                        if cp_text:
                            handle_auto_credit_channel_post(cp_chat_id, cp_text)
                            try_handle_sms_forward(cp_chat_id, cp_text)

                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        chat_id = cb["message"]["chat"]["id"]
                        message_id = cb["message"]["message_id"]
                        callback_id = cb["id"]
                        data = cb["data"]
                        user_id = cb["from"]["id"]
                        handle_callback(chat_id, message_id, callback_id, data, user_id)
        except Exception as e:
            print(f"Main error: {e}")
            traceback.print_exc()
            time.sleep(2)

if __name__ == "__main__":
    main()