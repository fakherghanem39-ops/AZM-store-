-- AZM Store — D1 Database Schema
-- قم بتشغيل هذا الأمر بعد إنشاء D1 database:
--   wrangler d1 execute azm_store --file=schema.sql
-- أو افتح https://your-domain.pages.dev/api/init-db

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL UNIQUE,
  full_name TEXT,
  username TEXT,
  balance REAL NOT NULL DEFAULT 0,
  first_seen TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  emoji TEXT DEFAULT '',
  image TEXT,
  sort_order INTEGER DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS providers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  api_url TEXT NOT NULL DEFAULT 'https://mhd-game.com/api',
  api_token TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER REFERENCES sections(id),
  name TEXT NOT NULL,
  description TEXT,
  image TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  provider_id INTEGER REFERENCES providers(id),
  provider_product_id TEXT,
  product_type TEXT DEFAULT 'fixed'
);

CREATE TABLE IF NOT EXISTS product_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER REFERENCES products(id),
  name TEXT NOT NULL,
  price REAL NOT NULL,
  player_id_label TEXT DEFAULT 'معرف اللاعب',
  min_qty INTEGER DEFAULT 1,
  max_qty INTEGER DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  price_usd REAL,
  cat_type TEXT DEFAULT 'fixed'
);

CREATE TABLE IF NOT EXISTS deposit_methods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT,
  code TEXT,
  image TEXT,
  exchange_rate REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deposits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  method_id INTEGER,
  method_title TEXT,
  amount_syp REAL NOT NULL,
  amount_usd REAL,
  transaction_code TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  full_name TEXT,
  username TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  product_id INTEGER,
  category_id INTEGER,
  product_name TEXT,
  category_name TEXT,
  player_id TEXT,
  qty INTEGER NOT NULL DEFAULT 1,
  price REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY,
  welcome_message TEXT DEFAULT '⚡ أهلاً بك في متجر AZM Store ⚡

مرحباً {name}! 🎉',
  support_username TEXT DEFAULT 'support',
  exchange_rate REAL DEFAULT 15000,
  bg_image TEXT,
  theme_json TEXT
);

INSERT OR IGNORE INTO settings (id) VALUES (1);

-- Migrations for existing databases (run safely):
-- ALTER TABLE sections ADD COLUMN emoji TEXT DEFAULT '';
-- ALTER TABLE sections ADD COLUMN image TEXT;
-- ALTER TABLE product_categories ADD COLUMN player_id_label TEXT DEFAULT 'معرف اللاعب';
-- ALTER TABLE product_categories ADD COLUMN min_qty INTEGER DEFAULT 1;
-- ALTER TABLE product_categories ADD COLUMN max_qty INTEGER DEFAULT 1;
