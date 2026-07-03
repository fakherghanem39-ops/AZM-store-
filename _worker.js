// ═══════════════════════════════════════════════════════════════════════
//  AZM Store — All-in-One Cloudflare Pages Worker
//  Bot + API + Database (D1) — كل شيء في ملف واحد
//
//  إعداد Cloudflare Pages:
//    1. ارفع مجلد /cxx/ على Cloudflare Pages
//    2. أنشئ D1 Database اسمها "azm_store"
//    3. اربطها في Settings → Functions → D1 Bindings باسم "DB"
//    4. افتح https://your-pages.domain.com/api/init-db  (مرة واحدة)
//    5. افتح https://your-pages.domain.com/setup-webhook (مرة واحدة)
//
//  متغيرات البيئة (Settings → Environment Variables):
//    BOT_TOKEN  — توكن البوت من @BotFather
//    ADMIN_IDS  — معرفات الأدمن مفصولة بفاصلة: 123456,789012
// ═══════════════════════════════════════════════════════════════════════

// ── ثوابت افتراضية (تُستبدل بمتغيرات البيئة عند وجودها) ───────────────
const DEFAULT_BOT_TOKEN = "8394331291:AAHpXxruLw3MirGetFaU4gmJgUpcopawqwk";
const DEFAULT_ADMIN_IDS = [6200238604, 7286288857];
const DEFAULT_MAIN_ADMIN = 6200238604;

function getToken(env)     { return env.BOT_TOKEN || DEFAULT_BOT_TOKEN; }
function getAdmins(env)    { return env.ADMIN_IDS ? env.ADMIN_IDS.split(",").map(Number) : DEFAULT_ADMIN_IDS; }
function getMainAdmin(env) { return env.MAIN_ADMIN ? Number(env.MAIN_ADMIN) : DEFAULT_MAIN_ADMIN; }
function getWebappUrl(env, request) {
  if (env.WEBAPP_URL) return env.WEBAPP_URL.replace(/\/$/, "");
  const u = new URL(request.url);
  return u.origin;
}

// ── مساعدات CORS ────────────────────────────────────────────────────────
function cors(res) {
  const h = new Headers(res.headers);
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS");
  h.set("Access-Control-Allow-Headers", "Content-Type");
  return new Response(res.body, { status: res.status, headers: h });
}
function json(data, status = 200) {
  return cors(Response.json(data, { status }));
}

// ── مساعدات تيليغرام ────────────────────────────────────────────────────
async function tg(method, body, env) {
  const r = await fetch(`https://api.telegram.org/bot${getToken(env)}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}
function sendMsg(chatId, text, extra = {}, env) {
  return tg("sendMessage", { chat_id: chatId, text, parse_mode: "HTML", ...extra }, env);
}
function sendPhoto(chatId, photo, caption, extra = {}, env) {
  return tg("sendPhoto", { chat_id: chatId, photo, caption, parse_mode: "HTML", ...extra }, env);
}
function editMsg(chatId, msgId, text, extra = {}, env) {
  return tg("editMessageText", { chat_id: chatId, message_id: msgId, text, parse_mode: "HTML", ...extra }, env);
}
function answerCb(id, text = "", env) {
  return tg("answerCallbackQuery", { callback_query_id: id, text }, env);
}

// ── إنشاء جداول D1 ───────────────────────────────────────────────────────
const SCHEMA = `
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
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER REFERENCES sections(id),
  name TEXT NOT NULL,
  description TEXT,
  image TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS product_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER REFERENCES products(id),
  name TEXT NOT NULL,
  price REAL NOT NULL,
  player_id_label TEXT DEFAULT 'معرف اللاعب',
  min_qty INTEGER DEFAULT 1,
  max_qty INTEGER DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1
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
CREATE TABLE IF NOT EXISTS providers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  api_url TEXT NOT NULL DEFAULT 'https://mhd-game.com/api',
  api_token TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY,
  welcome_message TEXT DEFAULT '⚡ أهلاً بك في متجر AZM Store ⚡\n\nمرحباً {name}! 🎉',
  support_username TEXT DEFAULT 'support',
  exchange_rate REAL DEFAULT 15000,
  bg_image TEXT,
  theme_json TEXT
);
INSERT OR IGNORE INTO settings (id) VALUES (1);
`;

async function initDb(db) {
  for (const stmt of SCHEMA.split(";").map(s => s.trim()).filter(Boolean)) {
    await db.prepare(stmt + ";").run();
  }
  // Migrations for existing databases
  const migrations = [
    "ALTER TABLE sections ADD COLUMN emoji TEXT DEFAULT ''",
    "ALTER TABLE sections ADD COLUMN image TEXT",
    "ALTER TABLE product_categories ADD COLUMN player_id_label TEXT DEFAULT 'معرف اللاعب'",
    "ALTER TABLE product_categories ADD COLUMN min_qty INTEGER DEFAULT 1",
    "ALTER TABLE product_categories ADD COLUMN max_qty INTEGER DEFAULT 1",
    "ALTER TABLE product_categories ADD COLUMN price_usd REAL",
    "ALTER TABLE product_categories ADD COLUMN cat_type TEXT DEFAULT 'fixed'",
    "ALTER TABLE products ADD COLUMN provider_id INTEGER",
    "ALTER TABLE products ADD COLUMN provider_product_id TEXT",
    "ALTER TABLE products ADD COLUMN product_type TEXT DEFAULT 'fixed'",
  ];
  for (const m of migrations) {
    try { await db.prepare(m).run(); } catch(_) {}
  }
}

// ── مساعدات D1 ───────────────────────────────────────────────────────────
async function dbAll(db, sql, params = []) {
  const { results } = await db.prepare(sql).bind(...params).all();
  return results || [];
}
async function dbFirst(db, sql, params = []) {
  return db.prepare(sql).bind(...params).first();
}
async function dbRun(db, sql, params = []) {
  return db.prepare(sql).bind(...params).run();
}

// ── مساعدات المستخدم ─────────────────────────────────────────────────────
async function getOrCreateUser(db, userId, fullName, username) {
  await dbRun(db,
    "INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
    [String(userId), fullName || null, username || null]
  );
  if (fullName) {
    await dbRun(db,
      "UPDATE users SET full_name=?, username=? WHERE user_id=?",
      [fullName, username || null, String(userId)]
    );
  }
  return dbFirst(db, "SELECT * FROM users WHERE user_id=?", [String(userId)]);
}

// ══════════════════════════════════════════════════════════════════════════
//  API HANDLERS
// ══════════════════════════════════════════════════════════════════════════

// ── /api/init-db ──────────────────────────────────────────────────────────
async function handleInitDb(db) {
  await initDb(db);
  return json({ ok: true, message: "Database initialized" });
}

// ── /api/healthz ─────────────────────────────────────────────────────────
function handleHealth() { return json({ status: "ok" }); }

// ── /api/shop/user/:id ────────────────────────────────────────────────────
async function handleGetUser(db, userId) {
  const user = await getOrCreateUser(db, userId, null, null);
  return json(user);
}

// ── /api/shop/settings ───────────────────────────────────────────────────
async function handleGetSettings(db) {
  const s = await dbFirst(db, "SELECT * FROM settings WHERE id=1");
  return json(s || {});
}
async function handlePatchSettings(db, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["welcome_message","support_username","exchange_rate","bg_image","theme_json"].includes(k)) {
      fields.push(`${k}=?`); vals.push(v);
    }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(1);
  await dbRun(db, `UPDATE settings SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM settings WHERE id=1"));
}

// ── /api/shop/sections ───────────────────────────────────────────────────
async function handleGetSections(db) {
  return json(await dbAll(db, "SELECT * FROM sections ORDER BY sort_order, id"));
}
async function handleCreateSection(db, body) {
  const { title, emoji = "", image = null, sort_order = 0, active = 1 } = body;
  if (!title) return json({ error: "title required" }, 400);
  const r = await dbRun(db, "INSERT INTO sections (title, emoji, image, sort_order, active) VALUES (?,?,?,?,?)", [title, emoji, image, sort_order, active ? 1 : 0]);
  return json(await dbFirst(db, "SELECT * FROM sections WHERE id=?", [r.meta.last_row_id]));
}
async function handlePatchSection(db, id, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["title","emoji","image","sort_order","active"].includes(k)) { fields.push(`${k}=?`); vals.push(v); }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(id);
  await dbRun(db, `UPDATE sections SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM sections WHERE id=?", [id]));
}
async function handleDeleteSection(db, id) {
  await dbRun(db, "DELETE FROM sections WHERE id=?", [id]);
  return json({ ok: true });
}

// ── /api/shop/products ───────────────────────────────────────────────────
async function handleGetProducts(db, sectionId) {
  if (sectionId) return json(await dbAll(db, "SELECT * FROM products WHERE section_id=?", [sectionId]));
  return json(await dbAll(db, "SELECT * FROM products ORDER BY id"));
}
async function handleGetProduct(db, id) {
  const p = await dbFirst(db, "SELECT * FROM products WHERE id=?", [id]);
  if (!p) return json({ error: "not found" }, 404);
  return json(p);
}
async function handleCreateProduct(db, body) {
  const { name, description, image, section_id, active = 1, provider_id, provider_product_id, product_type = "fixed" } = body;
  if (!name) return json({ error: "name required" }, 400);
  const r = await dbRun(db, "INSERT INTO products (name, description, image, section_id, active, provider_id, provider_product_id, product_type) VALUES (?,?,?,?,?,?,?,?)",
    [name, description || null, image || null, section_id || null, active ? 1 : 0, provider_id || null, provider_product_id || null, product_type]);
  return json(await dbFirst(db, "SELECT * FROM products WHERE id=?", [r.meta.last_row_id]));
}
async function handlePatchProduct(db, id, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["name","description","image","section_id","active","provider_id","provider_product_id","product_type"].includes(k)) { fields.push(`${k}=?`); vals.push(v); }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(id);
  await dbRun(db, `UPDATE products SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM products WHERE id=?", [id]));
}
async function handleDeleteProduct(db, id) {
  await dbRun(db, "DELETE FROM products WHERE id=?", [id]);
  return json({ ok: true });
}

// ── /api/shop/products/:pid/categories ───────────────────────────────────
async function handleGetCategories(db, pid) {
  return json(await dbAll(db, "SELECT * FROM product_categories WHERE product_id=?", [pid]));
}
async function handleCreateCategory(db, pid, body) {
  const { name, price, player_id_label = "معرف اللاعب", min_qty = 1, max_qty = 1, active = 1 } = body;
  if (!name || price === undefined) return json({ error: "name and price required" }, 400);
  const r = await dbRun(db, "INSERT INTO product_categories (product_id, name, price, player_id_label, min_qty, max_qty, active) VALUES (?,?,?,?,?,?,?)",
    [pid, name, price, player_id_label, min_qty, max_qty, active ? 1 : 0]);
  return json(await dbFirst(db, "SELECT * FROM product_categories WHERE id=?", [r.meta.last_row_id]));
}
async function handlePatchCategory(db, id, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["name","price","player_id_label","min_qty","max_qty","active"].includes(k)) { fields.push(`${k}=?`); vals.push(v); }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(id);
  await dbRun(db, `UPDATE product_categories SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM product_categories WHERE id=?", [id]));
}
async function handleDeleteCategory(db, id) {
  await dbRun(db, "DELETE FROM product_categories WHERE id=?", [id]);
  return json({ ok: true });
}

// ── /api/shop/deposit-methods ────────────────────────────────────────────
async function handleGetDepositMethods(db) {
  return json(await dbAll(db, "SELECT * FROM deposit_methods WHERE active=1"));
}
async function handleGetAllDepositMethods(db) {
  return json(await dbAll(db, "SELECT * FROM deposit_methods ORDER BY id"));
}
async function handleCreateDepositMethod(db, body) {
  const { title, description, code, image, exchange_rate = 0, active = 1 } = body;
  if (!title) return json({ error: "title required" }, 400);
  const r = await dbRun(db, "INSERT INTO deposit_methods (title, description, code, image, exchange_rate, active) VALUES (?,?,?,?,?,?)",
    [title, description || null, code || null, image || null, exchange_rate, active ? 1 : 0]);
  return json(await dbFirst(db, "SELECT * FROM deposit_methods WHERE id=?", [r.meta.last_row_id]));
}
async function handlePatchDepositMethod(db, id, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["title","description","code","image","exchange_rate","active"].includes(k)) { fields.push(`${k}=?`); vals.push(v); }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(id);
  await dbRun(db, `UPDATE deposit_methods SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM deposit_methods WHERE id=?", [id]));
}
async function handleDeleteDepositMethod(db, id) {
  await dbRun(db, "DELETE FROM deposit_methods WHERE id=?", [id]);
  return json({ ok: true });
}

// ── /api/shop/providers ──────────────────────────────────────────────────
async function handleGetProviders(db) {
  return json(await dbAll(db, "SELECT id,name,api_url,api_token,active,created_at FROM providers ORDER BY id"));
}
async function handleCreateProvider(db, body) {
  const { name, api_url = "https://mhd-game.com/api", api_token, active = 1 } = body;
  if (!name || !api_token) return json({ error: "name and api_token required" }, 400);
  const r = await dbRun(db, "INSERT INTO providers (name,api_url,api_token,active) VALUES (?,?,?,?)",
    [name, api_url, api_token, active ? 1 : 0]);
  return json(await dbFirst(db, "SELECT * FROM providers WHERE id=?", [r.meta.last_row_id]));
}
async function handlePatchProvider(db, id, body) {
  const fields = [], vals = [];
  for (const [k, v] of Object.entries(body)) {
    if (["name","api_url","api_token","active"].includes(k)) { fields.push(`${k}=?`); vals.push(v); }
  }
  if (!fields.length) return json({ error: "nothing to update" }, 400);
  vals.push(id);
  await dbRun(db, `UPDATE providers SET ${fields.join(",")} WHERE id=?`, vals);
  return json(await dbFirst(db, "SELECT * FROM providers WHERE id=?", [id]));
}
async function handleDeleteProvider(db, id) {
  await dbRun(db, "DELETE FROM providers WHERE id=?", [id]);
  return json({ ok: true });
}
async function handleTestProvider(db, id) {
  const prov = await dbFirst(db, "SELECT * FROM providers WHERE id=?", [id]);
  if (!prov) return json({ error: "provider not found" }, 404);
  const base = prov.api_url.replace(/\/$/, "");
  try {
    // Try account info first
    const acctResp = await fetch(`${base}/client/api/user`, { headers: { "api-token": prov.api_token } });
    const acctData = await acctResp.json().catch(() => null);
    if (acctData && (acctData.status === true || acctData.status === "OK" || acctData.ok)) {
      return json({ ok: true, account: acctData.data || acctData, message: "✅ الاتصال يعمل" });
    }
    // Fallback: check products endpoint
    const prodResp = await fetch(`${base}/client/api/products`, { headers: { "api-token": prov.api_token } });
    const prodData = await prodResp.json().catch(() => null);
    if (prodData) {
      const prods = Array.isArray(prodData) ? prodData : (Array.isArray(prodData?.data) ? prodData.data : null);
      if (prods) {
        return json({ ok: true, message: `✅ الاتصال يعمل — ${prods.length} منتج متاح`, account: acctData });
      }
    }
    return json({ ok: false, error: "فشل التحقق من الاتصال", raw: acctData }, 400);
  } catch(e) {
    return json({ ok: false, error: String(e.message || e) }, 400);
  }
}
async function handleGetProviderProducts(db, id) {
  const prov = await dbFirst(db, "SELECT * FROM providers WHERE id=?", [id]);
  if (!prov) return json({ error: "provider not found" }, 404);
  const base = prov.api_url.replace(/\/$/, "");
  try {
    const resp = await fetch(`${base}/client/api/products`, { headers: { "api-token": prov.api_token } });
    const data = await resp.json();
    const products = Array.isArray(data) ? data : (Array.isArray(data?.data) ? data.data : []);
    return json({ ok: true, products });
  } catch(e) {
    return json({ ok: false, error: String(e.message || e), products: [] }, 400);
  }
}
async function handleGetProviderProductDetail(db, id, productId) {
  const prov = await dbFirst(db, "SELECT * FROM providers WHERE id=?", [id]);
  if (!prov) return json({ error: "provider not found" }, 404);
  const base = prov.api_url.replace(/\/$/, "");
  try {
    const resp = await fetch(`${base}/client/api/products?products_id=${productId}`, { headers: { "api-token": prov.api_token } });
    const data = await resp.json();
    const products = Array.isArray(data) ? data : (Array.isArray(data?.data) ? data.data : []);
    const product = products.find(p => String(p.id) === String(productId)) || products[0] || null;
    return json({ ok: !!product, product });
  } catch(e) {
    return json({ ok: false, error: String(e.message || e) }, 400);
  }
}

// ── /api/shop/deposits ───────────────────────────────────────────────────
async function handleCreateDeposit(db, body, env) {
  const { user_id, method_id, amount_syp, transaction_code } = body;
  if (!user_id || !amount_syp) return json({ error: "missing fields" }, 400);
  const method = method_id ? await dbFirst(db, "SELECT * FROM deposit_methods WHERE id=?", [method_id]) : null;
  const rate = method?.exchange_rate || 15000;
  const amount_usd = parseFloat(amount_syp) / parseFloat(rate);
  const user = await dbFirst(db, "SELECT * FROM users WHERE user_id=?", [String(user_id)]);
  const r = await dbRun(db,
    "INSERT INTO deposits (user_id, method_id, method_title, amount_syp, amount_usd, transaction_code, full_name, username) VALUES (?,?,?,?,?,?,?,?)",
    [String(user_id), method_id || null, method?.title || null, amount_syp, amount_usd.toFixed(2), transaction_code || null,
     user?.full_name || null, user?.username || null]
  );
  const dep = await dbFirst(db, "SELECT * FROM deposits WHERE id=?", [r.meta.last_row_id]);
  // notify admins
  const admins = getAdmins(env);
  const msg = `🔔 <b>إيداع جديد #${dep.id}</b>\n\n👤 ${dep.full_name || dep.username || dep.user_id}\n🆔 <code>${dep.user_id}</code>\n💰 ${Number(dep.amount_syp).toLocaleString()} ل.س (${Number(dep.amount_usd).toFixed(2)}$)\n🏦 ${dep.method_title || "—"}\n📝 <code>${dep.transaction_code || "—"}</code>`;
  const kb = { inline_keyboard: [[{ text: "✅ قبول", callback_data: `accept_dep_${dep.id}` }, { text: "❌ رفض", callback_data: `reject_dep_${dep.id}` }]] };
  for (const a of admins) { try { await sendMsg(a, msg, { reply_markup: kb }, env); } catch (e) {} }
  return json({ ok: true, deposit: dep });
}
async function handleGetMyDeposits(db, userId) {
  return json({ deposits: await dbAll(db, "SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC", [String(userId)]) });
}
async function handleAdminGetDeposits(db, status) {
  if (status) return json(await dbAll(db, "SELECT * FROM deposits WHERE status=? ORDER BY created_at DESC", [status]));
  return json(await dbAll(db, "SELECT * FROM deposits ORDER BY created_at DESC"));
}
async function handleApproveDeposit(db, id, env) {
  const dep = await dbFirst(db, "SELECT * FROM deposits WHERE id=?", [id]);
  if (!dep) return json({ error: "not found" }, 404);
  await dbRun(db, "UPDATE deposits SET status='approved' WHERE id=?", [id]);
  await dbRun(db, "UPDATE users SET balance = balance + ? WHERE user_id=?", [dep.amount_usd, dep.user_id]);
  try {
    await sendMsg(dep.user_id,
      `✅ <b>تم قبول طلب الشحن!</b>\n\n💰 ${Number(dep.amount_syp).toLocaleString()} ل.س (${Number(dep.amount_usd).toFixed(2)}$)\n📝 <code>${dep.transaction_code || "—"}</code>\n✨ تم إضافة الرصيد إلى حسابك`,
      {}, env);
  } catch (e) {}
  return json({ ok: true });
}
async function handleRejectDeposit(db, id, env) {
  const dep = await dbFirst(db, "SELECT * FROM deposits WHERE id=?", [id]);
  if (!dep) return json({ error: "not found" }, 404);
  await dbRun(db, "UPDATE deposits SET status='rejected' WHERE id=?", [id]);
  try {
    await sendMsg(dep.user_id,
      `❌ <b>تم رفض طلب الشحن</b>\n\n💰 ${Number(dep.amount_syp).toLocaleString()} ل.س\n📝 <code>${dep.transaction_code || "—"}</code>\n⚠️ إذا كان هناك خطأ تواصل مع الدعم`,
      {}, env);
  } catch (e) {}
  return json({ ok: true });
}

// ── /api/shop/orders ─────────────────────────────────────────────────────
async function handleCreateOrder(db, body) {
  const { user_id, product_id, category_id, player_id, qty = 1 } = body;
  if (!user_id || !product_id || !category_id) return json({ error: "missing fields" }, 400);
  const cat = await dbFirst(db, "SELECT * FROM product_categories WHERE id=?", [category_id]);
  const prod = await dbFirst(db, "SELECT * FROM products WHERE id=?", [product_id]);
  const user = await dbFirst(db, "SELECT * FROM users WHERE user_id=?", [String(user_id)]);
  if (!cat || !prod) return json({ error: "product or category not found" }, 404);
  const total = parseFloat(cat.price) * qty;
  if (!user || parseFloat(user.balance) < total) return json({ error: "رصيد غير كافٍ" }, 400);
  await dbRun(db, "UPDATE users SET balance = balance - ? WHERE user_id=?", [total, String(user_id)]);
  const r = await dbRun(db,
    "INSERT INTO orders (user_id, product_id, category_id, product_name, category_name, player_id, qty, price) VALUES (?,?,?,?,?,?,?,?)",
    [String(user_id), product_id, category_id, prod.name, cat.name, player_id || null, qty, total]
  );
  return json({ ok: true, order: await dbFirst(db, "SELECT * FROM orders WHERE id=?", [r.meta.last_row_id]) });
}
async function handleGetMyOrders(db, userId) {
  return json({ orders: await dbAll(db, "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", [String(userId)]) });
}
async function handleAdminGetOrders(db) {
  return json(await dbAll(db, "SELECT * FROM orders ORDER BY created_at DESC"));
}

// ── /api/shop/admin/users ────────────────────────────────────────────────
async function handleAdminGetUsers(db) {
  return json(await dbAll(db, "SELECT * FROM users ORDER BY id DESC"));
}
async function handleAdminPatchUserBalance(db, userId, body) {
  const { balance } = body;
  if (balance === undefined) return json({ error: "balance required" }, 400);
  await dbRun(db, "UPDATE users SET balance=? WHERE user_id=?", [balance, String(userId)]);
  return json(await dbFirst(db, "SELECT * FROM users WHERE user_id=?", [String(userId)]));
}

// ── /api/shop/upload — يخزن الصورة كـ base64 في D1 (بديل R2) ────────────
async function handleUpload(db, request) {
  try {
    const fd = await request.formData();
    const file = fd.get("file");
    if (!file) return json({ error: "no file" }, 400);
    const buf = await file.arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(buf)));
    const mime = file.type || "image/jpeg";
    const dataUrl = `data:${mime};base64,${b64}`;
    return json({ url: dataUrl });
  } catch (e) {
    return json({ error: "upload failed" }, 500);
  }
}

// ══════════════════════════════════════════════════════════════════════════
//  BOT HANDLERS
// ══════════════════════════════════════════════════════════════════════════

function mainKeyboard(webappUrl, isAdmin) {
  const rows = [];
  if (webappUrl) rows.push([{ text: "🛒 فتح متجر AZM Store", web_app: { url: webappUrl } }]);
  rows.push([{ text: "💳 شحن رصيد", callback_data: "main_deposit" }, { text: "💼 حسابي", callback_data: "main_account" }]);
  rows.push([{ text: "📋 طلباتي", callback_data: "my_orders" }, { text: "💬 الدعم", callback_data: "main_support" }]);
  if (isAdmin) rows.push([{ text: "⚙️ لوحة الأدمن", callback_data: "admin_panel" }]);
  return { inline_keyboard: rows };
}
function backMain() { return { inline_keyboard: [[{ text: "🔙 القائمة الرئيسية", callback_data: "back_main" }]] }; }

async function handleStart(msg, db, env, request) {
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  const firstName = msg.from.first_name || "مستخدم";
  const fullName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(" ");
  const username = msg.from.username || "";
  const isAdmin = getAdmins(env).includes(userId);

  const isNew = !(await dbFirst(db, "SELECT id FROM users WHERE user_id=?", [String(userId)]));
  await getOrCreateUser(db, userId, fullName, username);

  const s = await dbFirst(db, "SELECT * FROM settings WHERE id=1");
  let welcome = s?.welcome_message || `⚡ أهلاً بك في <b>متجر AZM Store</b> ⚡\n\nمرحباً <b>${firstName}</b>! 🎉`;
  welcome = welcome.replace(/\{name\}/g, firstName);

  await sendMsg(chatId, welcome, { reply_markup: mainKeyboard(getWebappUrl(env, request), isAdmin) }, env);

  if (isNew && !isAdmin) {
    const adminMsg = `👤 <b>مستخدم جديد!</b>\nالاسم: <b>${fullName || firstName}</b>\n🆔 <code>${userId}</code>\n${username ? "@" + username : ""}`;
    try { await sendMsg(getMainAdmin(env), adminMsg, {}, env); } catch (e) {}
  }
}

async function handleDepositList(chatId, db, env) {
  const methods = await dbAll(db, "SELECT * FROM deposit_methods WHERE active=1");
  if (!methods.length) return sendMsg(chatId, "🚫 لا توجد طرق إيداع", { reply_markup: backMain() }, env);
  await sendMsg(chatId, "💳 <b>اختر طريقة الإيداع:</b>", {}, env);
  for (const m of methods) {
    const cap = `🏦 <b>${m.title}</b>\n${m.description || ""}\n━━━━━━━━━━━━\n📱 <b>رقم التحويل:</b>\n<code>${m.code || "—"}</code>\n💱 سعر الصرف: <b>${Number(m.exchange_rate || 0).toLocaleString()}</b> ل.س/دولار`;
    const kb = { inline_keyboard: [[{ text: `✅ اختيار — ${m.title}`, callback_data: `dep_sel_${m.id}` }]] };
    if (m.image && m.image.startsWith("http")) {
      try { await sendPhoto(chatId, m.image, cap, { reply_markup: kb }, env); continue; } catch (e) {}
    }
    await sendMsg(chatId, cap, { reply_markup: kb }, env);
  }
  await sendMsg(chatId, "⬆️ اختر من الطرق أعلاه", { reply_markup: backMain() }, env);
}

async function handleAccount(chatId, userId, db, env) {
  const user = await dbFirst(db, "SELECT * FROM users WHERE user_id=?", [String(userId)]);
  if (!user) return sendMsg(chatId, "❌ حسابك غير موجود، أرسل /start", {}, env);
  const msg = `💼 <b>معلومات حسابك</b>\n\n👤 <b>${user.full_name || "—"}</b>\n🆔 <code>${userId}</code>\n📱 ${user.username ? "@" + user.username : "—"}\n━━━━━━━━━━━━━━━━\n💰 رصيدك: <b>${parseFloat(user.balance || 0).toFixed(2)} $</b>`;
  const kb = { inline_keyboard: [[{ text: "📋 طلباتي", callback_data: "my_orders" }, { text: "💰 إيداعاتي", callback_data: "my_deposits" }], [{ text: "💳 شحن رصيد", callback_data: "main_deposit" }], [{ text: "🔙 القائمة الرئيسية", callback_data: "back_main" }]] };
  await sendMsg(chatId, msg, { reply_markup: kb }, env);
}

async function handleMyOrders(chatId, userId, db, env) {
  const orders = await dbAll(db, "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 15", [String(userId)]);
  if (!orders.length) return sendMsg(chatId, "📋 <b>لا توجد طلبات</b>", { reply_markup: backMain() }, env);
  const lbl = { accepted:"✅", pending:"⏳", rejected:"❌", completed:"✅" };
  let msg = "📋 <b>طلباتك:</b>\n━━━━━━━━━━━━━━━━\n";
  for (const o of orders) {
    msg += `${lbl[o.status]||"❓"} <b>${o.product_name}</b> — ${o.category_name}\n💰 ${Number(o.price).toFixed(2)}$ | 🆔 ${o.player_id||"—"}\n📅 ${(o.created_at||"").slice(0,10)} | <b>${o.status}</b>\n\n`;
  }
  await sendMsg(chatId, msg, { reply_markup: backMain() }, env);
}

async function handleMyDeposits(chatId, userId, db, env) {
  const deps = await dbAll(db, "SELECT * FROM deposits WHERE user_id=? ORDER BY created_at DESC LIMIT 15", [String(userId)]);
  if (!deps.length) return sendMsg(chatId, "💰 <b>لا توجد إيداعات</b>", { reply_markup: backMain() }, env);
  const lbl = { approved:"✅", pending:"⏳", rejected:"❌" };
  let msg = "💰 <b>إيداعاتك:</b>\n━━━━━━━━━━━━━━━━\n";
  for (const d of deps) {
    msg += `${lbl[d.status]||"❓"} ${d.method_title||"—"}\n💵 ${Number(d.amount_syp||0).toLocaleString()} ل.س`;
    if (d.amount_usd) msg += ` (${Number(d.amount_usd).toFixed(2)}$)`;
    if (d.transaction_code) msg += `\n📝 <code>${d.transaction_code}</code>`;
    msg += `\n📅 ${(d.created_at||"").slice(0,10)} | <b>${d.status}</b>\n\n`;
  }
  await sendMsg(chatId, msg, { reply_markup: backMain() }, env);
}

async function handleAdminPanel(chatId, db, env) {
  const pending = await dbAll(db, "SELECT COUNT(*) as c FROM deposits WHERE status='pending'");
  const users   = await dbAll(db, "SELECT COUNT(*) as c FROM users");
  const pCount  = pending[0]?.c || 0;
  const uCount  = users[0]?.c || 0;
  const msg = `⚙️ <b>لوحة تحكم AZM Store</b>\n\n💳 إيداعات معلّقة: <b>${pCount}</b>\n👥 المستخدمين: <b>${uCount}</b>`;
  const kb = { inline_keyboard: [[{ text: `💳 الإيداعات المعلّقة (${pCount})`, callback_data: "admin_deps" }], [{ text: "🔙 القائمة الرئيسية", callback_data: "back_main" }]] };
  await sendMsg(chatId, msg, { reply_markup: kb }, env);
}

async function handleAdminDeposits(chatId, db, env) {
  const deps = await dbAll(db, "SELECT * FROM deposits WHERE status='pending' ORDER BY created_at DESC LIMIT 10");
  if (!deps.length) return sendMsg(chatId, "✅ <b>لا توجد إيداعات معلّقة</b>", { reply_markup: { inline_keyboard: [[{ text: "🔙 رجوع", callback_data: "admin_panel" }]] } }, env);
  for (const d of deps) {
    const msg = `🔔 <b>إيداع #${d.id}</b>\n\n👤 <b>${d.full_name||d.username||"—"}</b>\n🆔 <code>${d.user_id}</code>\n💰 ${Number(d.amount_syp||0).toLocaleString()} ل.س (${Number(d.amount_usd||0).toFixed(2)}$)\n🏦 ${d.method_title||"—"}\n📝 <code>${d.transaction_code||"—"}</code>`;
    const kb = { inline_keyboard: [[{ text: "✅ قبول", callback_data: `accept_dep_${d.id}` }, { text: "❌ رفض", callback_data: `reject_dep_${d.id}` }]] };
    await sendMsg(chatId, msg, { reply_markup: kb }, env);
  }
}

async function handleBotCallback(cb, db, env, request) {
  const chatId  = cb.message.chat.id;
  const msgId   = cb.message.message_id;
  const cbId    = cb.id;
  const userId  = cb.from.id;
  const data    = cb.data;
  const isAdmin = getAdmins(env).includes(userId);
  await answerCb(cbId, "", env);

  if (data === "back_main") {
    await editMsg(chatId, msgId, "🏠 <b>القائمة الرئيسية</b>", { reply_markup: mainKeyboard(getWebappUrl(env, request), isAdmin) }, env);
  } else if (data === "main_deposit") {
    await handleDepositList(chatId, db, env);
  } else if (data.startsWith("dep_sel_")) {
    const mId = parseInt(data.replace("dep_sel_", ""));
    const m = await dbFirst(db, "SELECT * FROM deposit_methods WHERE id=?", [mId]);
    if (!m) return;
    const webappUrl = getWebappUrl(env, request);
    const rows = [];
    if (webappUrl) rows.push([{ text: "📲 فتح المتجر لإتمام الشحن", web_app: { url: webappUrl } }]);
    rows.push([{ text: "🔙 رجوع", callback_data: "main_deposit" }]);
    await sendMsg(chatId,
      `✅ <b>${m.title}</b>\n\n📱 رقم التحويل:\n<code>${m.code||"—"}</code>\n💱 سعر الصرف: ${Number(m.exchange_rate||0).toLocaleString()} ل.س = 1$\n\n1️⃣ حوّل المبلغ\n2️⃣ افتح المتجر وأدخل تفاصيل الإيداع\n3️⃣ انتظر تأكيد الأدمن`,
      { reply_markup: { inline_keyboard: rows } }, env);
  } else if (data === "main_account") {
    await handleAccount(chatId, userId, db, env);
  } else if (data === "my_orders") {
    await handleMyOrders(chatId, userId, db, env);
  } else if (data === "my_deposits") {
    await handleMyDeposits(chatId, userId, db, env);
  } else if (data === "main_support") {
    const s = await dbFirst(db, "SELECT * FROM settings WHERE id=1");
    const su = (s?.support_username || "support").replace("@", "");
    await sendMsg(chatId, `💬 <b>الدعم الفني</b>\n\n<a href="https://t.me/${su}">تواصل مع الدعم</a>`, { reply_markup: backMain() }, env);
  } else if (data === "admin_panel" && isAdmin) {
    await handleAdminPanel(chatId, db, env);
  } else if (data === "admin_deps" && isAdmin) {
    await handleAdminDeposits(chatId, db, env);
  } else if (data.startsWith("accept_dep_") && isAdmin) {
    const depId = parseInt(data.replace("accept_dep_", ""));
    await handleApproveDeposit(db, depId, env);
    try { await editMsg(chatId, msgId, `✅ <b>تم قبول الإيداع #${depId}</b>`, {}, env); } catch (e) {}
  } else if (data.startsWith("reject_dep_") && isAdmin) {
    const depId = parseInt(data.replace("reject_dep_", ""));
    await handleRejectDeposit(db, depId, env);
    try { await editMsg(chatId, msgId, `❌ <b>تم رفض الإيداع #${depId}</b>`, {}, env); } catch (e) {}
  }
}

// ── Webhook handler ──────────────────────────────────────────────────────
async function handleWebhook(request, db, env) {
  try {
    const update = await request.json();
    if (update.message) {
      const msg = update.message;
      const text = (msg.text || "").trim();
      if (text === "/start" || text.startsWith("/start ")) {
        await handleStart(msg, db, env, request);
      } else {
        const chatId = msg.chat.id;
        const isAdmin = getAdmins(env).includes(msg.from.id);
        await sendMsg(chatId, "اضغط على الزر أدناه للمتجر 👇", { reply_markup: mainKeyboard(getWebappUrl(env, request), isAdmin) }, env);
      }
    } else if (update.callback_query) {
      await handleBotCallback(update.callback_query, db, env, request);
    }
  } catch (e) {
    console.error("webhook error:", e);
  }
  return new Response("ok");
}

// ── Setup webhook ────────────────────────────────────────────────────────
async function handleSetupWebhook(request, env) {
  const origin = new URL(request.url).origin;
  const res = await tg("setWebhook", {
    url: `${origin}/webhook`,
    allowed_updates: ["message", "callback_query"],
    drop_pending_updates: true,
  }, env);
  return json({ webhookUrl: `${origin}/webhook`, result: res });
}

// ══════════════════════════════════════════════════════════════════════════
//  MAIN ROUTER
// ══════════════════════════════════════════════════════════════════════════
export default {
  async fetch(request, env, ctx) {
    const url    = new URL(request.url);
    const path   = url.pathname;
    const method = request.method;

    // CORS preflight
    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      }});
    }

    // Ensure DB is ready
    const db = env.DB;
    if (!db) return json({ error: "D1 database not configured. Bind a D1 database as 'DB' in Cloudflare Pages settings." }, 503);

    // ── Bot routes ────────────────────────────────────────────────────────
    if (method === "POST" && path === "/webhook") return handleWebhook(request, db, env);
    if (path === "/setup-webhook") return handleSetupWebhook(request, env);

    // ── Init DB ───────────────────────────────────────────────────────────
    if (path === "/api/init-db") return handleInitDb(db);

    // ── Health ────────────────────────────────────────────────────────────
    if (path === "/api/healthz") return handleHealth();

    // ── Shop API ─────────────────────────────────────────────────────────
    if (path.startsWith("/api/shop/")) {
      let body = {};
      if (method !== "GET" && method !== "DELETE") {
        const ct = request.headers.get("content-type") || "";
        if (ct.includes("multipart")) {
          // handled specially for upload
        } else {
          try { body = await request.json(); } catch (e) {}
        }
      }

      // settings
      if (path === "/api/shop/settings") {
        if (method === "GET")   return handleGetSettings(db);
        if (method === "PATCH") return handlePatchSettings(db, body);
      }

      // user
      const userMatch = path.match(/^\/api\/shop\/user\/([^/]+)$/);
      if (userMatch) return handleGetUser(db, userMatch[1]);

      // sections
      if (path === "/api/shop/sections" && method === "GET")          return handleGetSections(db);
      if (path === "/api/shop/sections/create" && method === "POST")  return handleCreateSection(db, body);
      const secMatch = path.match(/^\/api\/shop\/sections\/(\d+)$/);
      if (secMatch) {
        if (method === "PATCH")  return handlePatchSection(db, Number(secMatch[1]), body);
        if (method === "DELETE") return handleDeleteSection(db, Number(secMatch[1]));
      }

      // products
      if (path === "/api/shop/products" && method === "GET") {
        return handleGetProducts(db, url.searchParams.get("section_id"));
      }
      if (path === "/api/shop/products/create" && method === "POST") return handleCreateProduct(db, body);
      const prodMatch = path.match(/^\/api\/shop\/products\/(\d+)$/);
      if (prodMatch) {
        if (method === "GET")    return handleGetProduct(db, Number(prodMatch[1]));
        if (method === "PATCH")  return handlePatchProduct(db, Number(prodMatch[1]), body);
        if (method === "DELETE") return handleDeleteProduct(db, Number(prodMatch[1]));
      }

      // categories
      const catListMatch = path.match(/^\/api\/shop\/products\/(\d+)\/categories$/);
      if (catListMatch && method === "GET") return handleGetCategories(db, Number(catListMatch[1]));
      const catCreateMatch = path.match(/^\/api\/shop\/products\/(\d+)\/categories\/create$/);
      if (catCreateMatch && method === "POST") return handleCreateCategory(db, Number(catCreateMatch[1]), body);
      const catMatch = path.match(/^\/api\/shop\/categories\/(\d+)$/);
      if (catMatch) {
        if (method === "PATCH")  return handlePatchCategory(db, Number(catMatch[1]), body);
        if (method === "DELETE") return handleDeleteCategory(db, Number(catMatch[1]));
      }

      // deposit methods
      if (path === "/api/shop/deposit-methods" && method === "GET")         return handleGetDepositMethods(db);
      if (path === "/api/shop/deposit-methods/all" && method === "GET")     return handleGetAllDepositMethods(db);
      if (path === "/api/shop/deposit-methods/create" && method === "POST") return handleCreateDepositMethod(db, body);
      const dmMatch = path.match(/^\/api\/shop\/deposit-methods\/(\d+)$/);
      if (dmMatch) {
        if (method === "PATCH")  return handlePatchDepositMethod(db, Number(dmMatch[1]), body);
        if (method === "DELETE") return handleDeleteDepositMethod(db, Number(dmMatch[1]));
      }

      // providers
      if (path === "/api/shop/providers" && method === "GET")          return handleGetProviders(db);
      if (path === "/api/shop/providers/create" && method === "POST")  return handleCreateProvider(db, body);
      const provMatch = path.match(/^\/api\/shop\/providers\/(\d+)$/);
      if (provMatch) {
        if (method === "PATCH")  return handlePatchProvider(db, Number(provMatch[1]), body);
        if (method === "DELETE") return handleDeleteProvider(db, Number(provMatch[1]));
      }
      const provTestMatch = path.match(/^\/api\/shop\/providers\/(\d+)\/test$/);
      if (provTestMatch && method === "GET") return handleTestProvider(db, Number(provTestMatch[1]));
      const provProdsMatch = path.match(/^\/api\/shop\/providers\/(\d+)\/products$/);
      if (provProdsMatch && method === "GET") return handleGetProviderProducts(db, Number(provProdsMatch[1]));
      const provProdMatch = path.match(/^\/api\/shop\/providers\/(\d+)\/products\/([^/]+)$/);
      if (provProdMatch && method === "GET") return handleGetProviderProductDetail(db, Number(provProdMatch[1]), provProdMatch[2]);

      // deposits
      if (path === "/api/shop/deposits" && method === "POST") return handleCreateDeposit(db, body, env);
      if (path === "/api/shop/my-deposits" && method === "GET") {
        const uid = url.searchParams.get("userId") || url.searchParams.get("user_id");
        return handleGetMyDeposits(db, uid);
      }
      if (path === "/api/shop/admin/deposits" && method === "GET") {
        return handleAdminGetDeposits(db, url.searchParams.get("status"));
      }
      const appMatch = path.match(/^\/api\/shop\/admin\/deposits\/(\d+)\/(approve|reject)$/);
      if (appMatch && method === "POST") {
        return appMatch[2] === "approve"
          ? handleApproveDeposit(db, Number(appMatch[1]), env)
          : handleRejectDeposit(db, Number(appMatch[1]), env);
      }

      // orders
      if (path === "/api/shop/orders" && method === "POST")        return handleCreateOrder(db, body);
      if (path === "/api/shop/my-orders" && method === "GET") {
        const uid = url.searchParams.get("userId") || url.searchParams.get("user_id");
        return handleGetMyOrders(db, uid);
      }
      if (path === "/api/shop/admin/orders" && method === "GET")   return handleAdminGetOrders(db);

      // admin users
      if (path === "/api/shop/admin/users" && method === "GET")    return handleAdminGetUsers(db);
      const uBalMatch = path.match(/^\/api\/shop\/admin\/users\/([^/]+)\/balance$/);
      if (uBalMatch && method === "PATCH") return handleAdminPatchUserBalance(db, uBalMatch[1], body);

      // upload
      if (path === "/api/shop/upload" && method === "POST") return handleUpload(db, request);

      return json({ error: "not found" }, 404);
    }

    // ── Serve Mini App (index.html + assets) ─────────────────────────────
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return new Response("Mini App not configured", { status: 404 });
  },
};
