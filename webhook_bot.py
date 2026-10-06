#!/usr/bin/env python3
"""
Professional Telegram Shop Bot — Complete Version
==================================================
Full purchase flow: Brands → Products → Quantity → Payment → Confirmation

Deploy: Railway (or any VPS with public HTTPS)
Env vars (Railway dashboard):
  BOT_TOKEN       Telegram bot token
  SHOP_API_KEY    Shop Reseller API X-API-Key
  SHOP_BASE_URL   Reseller API host (default: worker-production-53ca.up.railway.app)
  PUBLIC_URL      e.g. https://shop-webhook-bot-production.up.railway.app
  WEBHOOK_SECRET  random string (e.g. openssl rand -hex 16)
  ADMIN_CHAT_ID   Admin Telegram chat ID (order notifications)
  MARGIN          Customer price multiplier (default 1.30 = 30%)
  SUPPORT_USER    Support username without @ (default: hamzaazemat105)

Payment methods are configured via env as JSON:
  PAYMENT_METHODS_JSON  e.g. [{"key":"binance","name":"Binance Pay","instructions":"..."}, ...]

Flow:
  /start → main keyboard
  🛍️ المتجر → brand grid (with stock indicators)
  → brand → product list (✅ available / ❌ out of stock)
  → product → detail + image + [🛒 شراء]
  → quantity selection (1/2/3/5/10/custom)
  → payment method selection
  → order summary + payment instructions + [❌ إلغاء]
  → user sends payment screenshot
  → admin gets approve/reject buttons
  → on approve: auto-buy from supplier → deliver items to user
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------- config ---

def clean(v):
    return "".join(c for c in (v or "").strip() if ord(c) < 128 and not c.isspace())

BOT_TOKEN   = clean(os.environ["BOT_TOKEN"])
SHOP_API_KEY = clean(os.environ["SHOP_API_KEY"])
SHOP_BASE_URL = os.environ.get("SHOP_BASE_URL", "worker-production-53ca.up.railway.app").rstrip("/")
PUBLIC_URL  = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
MARGIN = float(os.environ.get("MARGIN", "1.30"))
SUPPORT_USER = os.environ.get("SUPPORT_USER", "hamzaazemat105").lstrip("@")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Smartshob_bot").lstrip("@")

try:
    PAYMENT_METHODS = json.loads(os.environ.get("PAYMENT_METHODS_JSON", "[]"))
except Exception:
    PAYMENT_METHODS = []
if not PAYMENT_METHODS:
    # Only Binance Pay + USDT (Cash Plus and Bank removed per Hamza 2026-10-05)
    PAYMENT_METHODS = [
        {"key": "binance", "name": "Binance Pay", "emoji": "🟡",
         "instructions": "🟡 <b>Binance Pay ID:</b> <code>718842303</code>\n\nحوّل المبلغ الدقيق إلى هاد الـID، ثم اضغط زر \"✅ تم الدفع\" وأرسل لقطة الشاشة أو رقم العملية."},
        {"key": "usdt_bep20", "name": "USDT (BEP20)", "emoji": "💵",
         "instructions": "💵 <b>USDT BEP20 (BSC):</b>\n<code>0x95d047dcb7fa90fd97a2f04965c8d71fa4b4aebb</code>\n\nحوّل المبلغ الدقيق إلى هاد العنوان، ثم اضغط زر \"✅ تم الدفع\" وأرسل لقطة الشاشة أو رقم العملية (TxID)."},
        {"key": "usdt_trc20", "name": "USDT (TRC20)", "emoji": "💵",
         "instructions": "💵 <b>USDT TRC20 (TRX):</b>\n<code>TMRLAQXPECALME55ZGZm52D6jSyAtfxkSu</code>\n\nحوّل المبلغ الدقيق إلى هاد العنوان، ثم اضغط زر \"✅ تم الدفع\" وأرسل لقطة الشاشة أو رقم العملية (TxID)."},
    ]

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
SHOP = f"https://{SHOP_BASE_URL}"

# ---------------------------------------------------------------- state ----

ORDERS = {}          # oid -> order dict
USER_STATE = {}      # chat_id -> {"step":..., "product_id":..., "qty":...}
PRODUCTS = {"items": [], "ts": 0}

# ------------------------------------------------- persistence -----------
# Orders survive Railway restarts/redeploys via a JSON file on disk.
ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.json")

def save_orders():
    try:
        tmp = ORDERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ORDERS, f, ensure_ascii=False)
        os.replace(tmp, ORDERS_FILE)
    except Exception as e:
        print("save_orders error:", e)

def load_orders():
    try:
        with open(ORDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            ORDERS.update(data)
            print(f"loaded {len(ORDERS)} orders from disk")
    except FileNotFoundError:
        pass
    except Exception as e:
        print("load_orders error:", e)

load_orders()

# ---------------------------------------------------------------- helpers --

def tg(method, params=None):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{TG}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def shop(path, method="GET", body=None):
    req = urllib.request.Request(f"{SHOP}{path}", method=method,
                                 headers={"X-API-Key": SHOP_API_KEY})
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30, data=data) as r:
        return json.loads(r.read())

def esc(s):
    return html.escape(str(s), quote=False)

def send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        return tg("sendMessage", params)
    except Exception as e:
        print("SEND FAILED:", e)

def send_photo(chat_id, photo_bytes, caption, reply_markup=None):
    import uuid
    boundary = uuid.uuid4().hex
    body = b""
    def field(name, value):
        nonlocal body
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    field("chat_id", chat_id); field("caption", caption); field("parse_mode", "HTML")
    if reply_markup:
        field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
             f'filename="p.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode()
    body += photo_bytes + b"\r\n" + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{TG}/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print("PHOTO FAILED:", e); return None

# ---------------------------------------------------------------- products -

def refresh_products(force=False):
    if not force and time.time() - PRODUCTS["ts"] < 300:
        return PRODUCTS["items"]
    try:
        res = shop("/api/products")
        items = res.get("products", res if isinstance(res, list) else [])
        PRODUCTS["items"] = items; PRODUCTS["ts"] = time.time()
    except Exception as e:
        print("product refresh failed:", e)
    return PRODUCTS["items"]

def stock_of(p):
    for k in ("stock_count", "stockCount", "stock", "quantity"):
        v = p.get(k)
        if v is not None:
            try: return int(v)
            except: pass
    return 0

def cust_price(p):
    try: return round(float(p.get("price") or 0) * MARGIN, 2)
    except: return 0.0

def prod_name(p):
    return p.get("name_en") or p.get("name") or f"#{p.get('id')}"

def brand_of(p):
    """Extract brand from product name (first meaningful word)."""
    name = prod_name(p).lower()
    # known brands
    brands = ["chatgpt", "gemini", "capcut", "canva", "notion", "figma",
              "duolingo", "youtube", "netflix", "spotify", "adobe", "miro",
              "fortnite", "tiktok", "instagram", "telegram", "microsoft",
              "lovable", "cursor", "jetbrains", "edx", "udemy", "coursera",
              "midjourney", "claude", "deepseek", "grok", "perplexity",
              "autodesk", "ilovepdf", "nordvpn", "expressvpn", "disney",
              "prime", "shahid", "osn", "anghami", "deezer", "xbox",
              "playstation", "steam", "api"]
    for b in brands:
        if b in name:
            return b.capitalize()
    # fallback: first word
    w = re.split(r'[\s\-_]+', prod_name(p).strip())
    return w[0][:12].capitalize() if w else "?"

def brand_emoji(brand):
    b = brand.lower()
    m = {"chatgpt": "🤖", "gemini": "✨", "capcut": "🎬", "canva": "🎨",
         "notion": "📝", "figma": "🎯", "duolingo": "🦉", "youtube": "📺",
         "netflix": "🎬", "spotify": "🎵", "adobe": "🎨", "miro": "📌",
         "fortnite": "🎮", "tiktok": "🎵", "instagram": "📸",
         "microsoft": "💼", "lovable": "💜", "jetbrains": "💻",
         "autodesk": "🏗️", "api": "🔑"}
    return m.get(b, "📦")

IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img")
IMG_MAP = [
    ("gemini", "ai.jpg"), ("chatgpt", "ai.jpg"), ("claude", "ai.jpg"),
    ("midjourney", "ai.jpg"), ("lovable", "ai.jpg"), ("deepseek", "ai.jpg"),
    ("fortnite", "game.jpg"), ("xbox", "game.jpg"), ("playstation", "game.jpg"),
    ("steam", "game.jpg"), ("youtube", "video.jpg"), ("netflix", "video.jpg"),
    ("disney", "video.jpg"), ("prime", "video.jpg"),
    ("spotify", "music.jpg"), ("anghami", "music.jpg"),
    ("canva", "design.jpg"), ("miro", "design.jpg"), ("figma", "design.jpg"),
    ("notion", "productivity.jpg"), ("microsoft", "productivity.jpg"),
    ("instagram", "social.jpg"), ("tiktok", "social.jpg"),
    ("capcut", "editing.jpg"), ("duolingo", "education.jpg"),
    ("coursera", "education.jpg"), ("udemy", "education.jpg"),
    ("bot", "bot.jpg"), ("api", "bot.jpg"),
]
def product_image(name):
    n = (name or "").lower()
    for kw, fn in IMG_MAP:
        if kw in n:
            p = os.path.join(IMG_DIR, fn)
            if os.path.exists(p): return p
    p = os.path.join(IMG_DIR, "generic.jpg")
    return p if os.path.exists(p) else None

# ---------------------------------------------------------------- keyboards

WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://muse.ai/s/space-2-ld5xytfxovkxaxt")

def main_keyboard():
    """Main menu as 2-column inline grid matching the reference design."""
    return {"inline_keyboard": [
        [{"text": "🌐 فتح المتجر الملون 🎨", "web_app": {"url": WEBAPP_URL}}],
        [{"text": "🛍️ المتجر", "callback_data": "menu:shop"},
         {"text": "📦 طلباتي", "callback_data": "menu:orders"}],
        [{"text": "⏳ حجوزاتي", "callback_data": "menu:reservations"},
         {"text": "💳 شحن الرصيد", "callback_data": "menu:topup"}],
        [{"text": "🔗 رابط الإحالة", "callback_data": "menu:referral"},
         {"text": "🎧 الدعم", "callback_data": "menu:support"}],
        [{"text": "🌐 Language / اللغة", "callback_data": "menu:language"},
         {"text": "🔑 بوابة الموزعين", "callback_data": "menu:reseller"}],
    ]}

def show_main_menu(chat_id, name=""):
    send(chat_id,
         f"🛍️ <b>مرحباً بك في المتجر!</b>\n\nأهلاً {esc(name)}! 👋\nاختار من القائمة 👇",
         main_keyboard())

def brand_keyboard():
    """3-column brand grid with stock indicators."""
    products = refresh_products()
    brands = {}
    for p in products:
        b = brand_of(p)
        if b not in brands:
            brands[b] = {"total": 0, "available": 0}
        brands[b]["total"] += 1
        if stock_of(p) > 0:
            brands[b]["available"] += 1
    kb, row = [], []
    for b in sorted(brands):
        info = brands[b]
        mark = "🟢" if info["available"] > 0 else "🔴"
        row.append({"text": f"{brand_emoji(b)} {b} · {info['total']}",
                    "callback_data": f"brand:{b}"})
        if len(row) == 3:
            kb.append(row); row = []
    if row: kb.append(row)
    kb.append([{"text": "🔄 تحديث", "callback_data": "brands_refresh"}])
    return kb, len(products), len(brands)

def show_brands(chat_id):
    kb, n_prod, n_brand = brand_keyboard()
    send(chat_id,
         f"🛍️ <b>اختار العلامة التجارية:</b>\n{n_prod} منتج · {n_brand} علامة\n\n"
         f"🟢 متوفر &nbsp;&nbsp; 🔴 غير متوفر",
         {"inline_keyboard": kb})

def show_brand_products(chat_id, brand):
    products = [p for p in refresh_products() if brand_of(p) == brand]
    if not products:
        send(chat_id, "لا توجد منتجات في هذه العلامة.")
        return
    kb = []
    for p in products:
        pid = p.get("id")
        st = stock_of(p)
        mark = "✅" if st > 0 else "❌"
        label = f"{mark} {prod_name(p)[:30]} — ${cust_price(p)}"
        kb.append([{"text": label, "callback_data": f"product:{pid}"}])
    kb.append([{"text": "⬅️ رجوع للعلامات", "callback_data": "back_brands"}])
    send(chat_id,
         f"{brand_emoji(brand)} <b>{esc(brand)}</b> — اختار المنتج:",
         {"inline_keyboard": kb})

def show_product_detail(chat_id, pid):
    p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
    if not p:
        send(chat_id, "⚠️ المنتج غير موجود."); return
    name, st, pr = prod_name(p), stock_of(p), cust_price(p)
    stock_txt = f"✅ متوفر ({st})" if st > 0 else "❌ غير متوفر"
    caption = (f"📦 <b>{esc(name)}</b>\n\n"
               f"💰 الثمن: <b>${pr}</b>\n"
               f"📊 المخزون: {stock_txt}\n")
    kb = {"inline_keyboard": [
        [{"text": "🛒 اشترِ الآن", "callback_data": f"buy:{pid}"}],
        [{"text": "⬅️ رجوع", "callback_data": f"brand:{brand_of(p)}"}],
    ]} if st > 0 else {"inline_keyboard": [
        [{"text": "⬅️ رجوع", "callback_data": f"brand:{brand_of(p)}"}]]}
    img = product_image(name)
    if img:
        try:
            with open(img, "rb") as f:
                if send_photo(chat_id, f.read(), caption, kb): return
        except Exception as e:
            print("detail photo failed:", e)
    send(chat_id, caption, kb)

def show_quantity(chat_id, pid):
    p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
    if not p: return
    st = stock_of(p)
    opts = [1, 2, 3, 5, 10]
    kb, row = [], []
    for q in opts:
        if q <= st or st >= 999:  # allow bulk for unlimited stock
            row.append({"text": f"{q}", "callback_data": f"qty:{pid}:{q}"})
            if len(row) == 3: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([{"text": "✏️ عدد مخصص", "callback_data": f"qtycustom:{pid}"}])
    kb.append([{"text": "⬅️ رجوع", "callback_data": f"product:{pid}"}])
    USER_STATE[str(chat_id)] = {"step": "qty", "product_id": pid}
    send(chat_id,
         f"🔢 <b>اختار العدد:</b>\n\n{esc(prod_name(p))}\n"
         f"💰 ${cust_price(p)} / للواحد\n📊 متوفر: {st}",
         {"inline_keyboard": kb})

def show_payment_methods(chat_id, pid, qty):
    p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
    if not p: return
    total = round(cust_price(p) * qty, 2)
    USER_STATE[str(chat_id)] = {"step": "pay", "product_id": pid, "qty": qty, "total": total}
    kb = []
    for m in PAYMENT_METHODS:
        kb.append([{"text": f"{m.get('emoji','💳')} {m['name']}",
                    "callback_data": f"pay:{pid}:{qty}:{m['key']}"}])
    kb.append([{"text": "⬅️ رجوع", "callback_data": f"buy:{pid}"}])
    send(chat_id,
         f"💳 <b>اختار طريقة الدفع:</b>\n\n"
         f"📦 {esc(prod_name(p))} × {qty}\n"
         f"💰 المجموع: <b>${total}</b>",
         {"inline_keyboard": kb})

def create_order(chat_id, user_name, pid, qty, pay_key):
    p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
    if not p: return
    m = next((x for x in PAYMENT_METHODS if x["key"] == pay_key), None)
    if not m: return
    total = round(cust_price(p) * qty, 2)
    oid = f"{chat_id}:{pid}:{qty}:{int(time.time())}"
    ORDERS[oid] = {
        "user_chat_id": chat_id, "user_name": user_name,
        "product_id": p["id"], "product_name": prod_name(p),
        "qty": qty, "unit_price": cust_price(p), "total": total,
        "pay_method": m["name"], "pay_key": pay_key,
        "status": "awaiting_payment", "created_at": int(time.time()),
    }
    save_orders()
    USER_STATE.pop(str(chat_id), None)
    kb = {"inline_keyboard": [
        [{"text": "✅ تم الدفع", "callback_data": f"paid:{oid}"}],
        [{"text": "❌ إلغاء الطلب", "callback_data": f"cancel:{oid}"}]]}
    send(chat_id,
         f"🧾 <b>تأكيد الطلب</b>\n\n"
         f"📦 المنتج: <b>{esc(prod_name(p))}</b>\n"
         f"🔢 العدد: <b>{qty}</b>\n"
         f"💰 المجموع: <b>${total}</b>\n"
         f"💳 الدفع عبر: <b>{esc(m['name'])}</b>\n\n"
         f"📋 <b>التعليمات:</b>\n{esc(m.get('instructions',''))}\n\n"
         f"1️⃣ حوّل المبلغ الدقيق\n"
         f"2️⃣ اضغط زر <b>✅ تم الدفع</b>\n"
         f"3️⃣ أرسل <b>لقطة شاشة</b> أو <b>رقم العملية (TxID)</b>\n\n"
         f"سيتم إرسال المنتج إليك مباشرة بعد التأكيد.",
         kb)

# ---------------------------------------------------------------- handlers -

def handle_start(chat_id, name):
    if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
        send(chat_id, "👋 مرحباً أيها المدير!\nالبوت يعمل بالـ webhook ⚡", main_keyboard())
        return
    show_main_menu(chat_id, name)

def handle_menu(chat_id, section, user_name):
    if section == "shop":
        show_brands(chat_id)
    elif section == "orders":
        mine = [o for o in ORDERS.values() if str(o["user_chat_id"]) == str(chat_id)]
        if not mine:
            send(chat_id, "🧾 لا توجد طلبات بعد.", back_to_menu_kb())
        else:
            lines = []
            for o in sorted(mine, key=lambda x: -x["created_at"])[:10]:
                st_map = {"awaiting_payment": "⏳ بانتظار الدفع",
                          "awaiting_approval": "🔍 قيد المراجعة",
                          "delivered": "✅ تم التسليم",
                          "rejected": "❌ مرفوض", "cancelled": "🚫 ملغي"}
                lines.append(f"📦 {esc(o['product_name'])} × {o['qty']} — "
                             f"${o['total']} ({st_map.get(o['status'], o['status'])})")
            send(chat_id, "🧾 <b>طلباتي:</b>\n\n" + "\n\n".join(lines), back_to_menu_kb())
    elif section == "reservations":
        send(chat_id, "⏳ <b>حجوزاتي</b>\n\nليس لديك أي حجوزات نشطة حالياً.", back_to_menu_kb())
    elif section == "topup":
        send(chat_id,
             "💳 <b>شحن الرصيد</b>\n\nاختر طريقة الدفع وتواصل مع الإدارة:\n"
             f"👤 @{SUPPORT_USER}",
             {"inline_keyboard": [
                 [{"text": "💬 تواصل مع الدعم", "url": f"https://t.me/{SUPPORT_USER}"}],
                 [{"text": "⬅️ رجوع للقائمة", "callback_data": "menu:main"}]]})
    elif section == "referral":
        send(chat_id,
             f"🔗 <b>رابط الإحالة الخاص بك:</b>\n\n"
             f"<code>https://t.me/{esc(BOT_USERNAME)}?start=ref_{chat_id}</code>\n\n"
             f"شاركه مع أصدقائك واربح عمولة على كل عملية شراء! 🎁",
             back_to_menu_kb())
    elif section == "support":
        send(chat_id,
             f"🎧 <b>الدعم</b>\n\nللتواصل المباشر مع الإدارة:",
             {"inline_keyboard": [
                 [{"text": "💬 تواصل مع الدعم", "url": f"https://t.me/{SUPPORT_USER}"}],
                 [{"text": "⬅️ رجوع للقائمة", "callback_data": "menu:main"}]]})
    elif section == "language":
        send(chat_id, "🌐 <b>الرجاء اختيار اللغة:</b>",
             {"inline_keyboard": [
                 [{"text": "🇺🇸 English", "callback_data": "lang:en"},
                  {"text": "🇸🇦 العربية", "callback_data": "lang:ar"}],
                 [{"text": "🇷🇺 Русский", "callback_data": "lang:ru"},
                  {"text": "🇫🇷 Français", "callback_data": "lang:fr"}],
                 [{"text": "🇨🇳 中文", "callback_data": "lang:zh"},
                  {"text": "⬅️ رجوع للقائمة", "callback_data": "menu:main"}]]})
    elif section == "reseller":
        send(chat_id,
             "🔑 <b>بوابة الموزعين</b>\n\nهذه المنطقة مخصصة للموزعين المعتمدين.\n"
             f"للاستفسار تواصل مع: @{SUPPORT_USER}",
             back_to_menu_kb())
    elif section == "main":
        show_main_menu(chat_id, user_name)

def back_to_menu_kb():
    return {"inline_keyboard": [[{"text": "⬅️ رجوع للقائمة", "callback_data": "menu:main"}]]}

def notify_admin_proof(o, oid, user_name, chat_id, proof_block):
    if not ADMIN_CHAT_ID:
        print("NO ADMIN; awaiting:", oid); return
    kb = {"inline_keyboard": [[
        {"text": "✅ تأكيد", "callback_data": f"approve:{oid}"},
        {"text": "❌ رفض", "callback_data": f"reject:{oid}"}]]}
    send(ADMIN_CHAT_ID,
         f"🔔 <b>طلب جديد بانتظار التأكيد</b>\n\n"
         f"👤 الزبون: {esc(user_name)} (<code>{chat_id}</code>)\n"
         f"📦 المنتج: <b>{esc(o['product_name'])}</b> × {o['qty']}\n"
         f"💰 المجموع: <b>${o['total']}</b>\n"
         f"💳 الدفع: {esc(o['pay_method'])}\n"
         f"🔖 الرقم: <code>{esc(oid)}</code>\n\n{proof_block}", kb)

def handle_photo(chat_id, message_id, user_name):
    pend = [(oid, o) for oid, o in ORDERS.items()
            if str(o["user_chat_id"]) == str(chat_id)
            and o["status"] in ("awaiting_payment", "awaiting_proof")]
    if not pend:
        send(chat_id, "ليس لديك طلب بانتظار الدفع. ابدأ من 🛍️ المتجر.", main_keyboard())
        return
    oid, o = pend[-1]
    o["status"] = "awaiting_approval"
    o["proof_type"] = "photo"
    save_orders()
    USER_STATE.pop(str(chat_id), None)
    send(chat_id, "✅ توصلنا بإثبات الدفع. سيتم مراجعة طلبك قريباً ⏳")
    notify_admin_proof(o, oid, user_name, chat_id, "📸 إثبات الدفع (صورة) 👇")
    tg("forwardMessage", {"chat_id": ADMIN_CHAT_ID, "from_chat_id": chat_id,
                          "message_id": message_id})

def admin_decision(chat_id, oid, approve):
    if not ADMIN_CHAT_ID or str(chat_id) != str(ADMIN_CHAT_ID):
        return "⛔ غير مصرح."
    o = ORDERS.get(oid)
    if not o or o["status"] != "awaiting_approval":
        return "⚠️ الطلب غير موجود أو تمت معالجته."
    if not approve:
        o["status"] = "rejected"
        save_orders()
        send(o["user_chat_id"], "❌ تم رفض طلبك. تواصل مع الدعم للمزيد من المعلومات.")
        return "تم رفض الطلب."
    # auto-buy from supplier
    try:
        res = shop("/api/buy", "POST", {"product_id": o["product_id"], "quantity": o["qty"]})
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not res.get("ok"):
        send(o["user_chat_id"], "⚠️ حدث خطأ أثناء تجهيز طلبك. سيتواصل معك المدير قريباً.")
        return f"⚠️ فشل الشراء من المزود: {esc(res.get('error',''))}"
    o["status"] = "delivered"
    save_orders()
    items = res.get("items", [])
    if items:
        body = "\n\n".join(f"<code>{esc(i)}</code>" for i in items)
        send(o["user_chat_id"],
             f"🎉 <b>تم تأكيد طلبك!</b>\n\n"
             f"📦 {esc(o['product_name'])} × {o['qty']}\n\n"
             f"🔑 بيانات المنتج:\n{body}\n\nشكراً لثقتك! 🙏")
    else:
        send(o["user_chat_id"], f"🎉 تم تأكيد طلبك! سيتواصل معك المدير.")
    return "✅ تم التأكيد والتسليم."

def handle_update(u):
    cb = u.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        name = cb.get("from", {}).get("first_name", "")
        answer = "تم ✅"
        try:
            if data.startswith("menu:"):
                handle_menu(chat_id, data[5:], name)
            elif data.startswith("lang:"):
                lang_names = {"en": "English", "ar": "العربية", "ru": "Русский",
                              "fr": "Français", "zh": "中文"}
                send(chat_id, f"✅ تم اختيار اللغة: {lang_names.get(data[5:], data[5:])}",
                     back_to_menu_kb())
                answer = "تم ✅"
            elif data == "brands_refresh" or data == "back_brands":
                show_brands(chat_id)
            elif data.startswith("brand:"):
                show_brand_products(chat_id, data[6:])
            elif data.startswith("product:"):
                show_product_detail(chat_id, data[8:])
            elif data.startswith("buy:"):
                show_quantity(chat_id, data[4:])
            elif data.startswith("qty:"):
                _, pid, q = data.split(":")
                show_payment_methods(chat_id, pid, int(q))
            elif data.startswith("qtycustom:"):
                pid = data[10:]
                USER_STATE[str(chat_id)] = {"step": "qty_custom", "product_id": pid}
                send(chat_id, "✏️ <b>اكتب العدد المطلوب</b> (رقم فقط):")
                answer = "اكتب العدد"
            elif data.startswith("pay:"):
                _, pid, q, key = data.split(":")
                create_order(chat_id, name, pid, int(q), key)
            elif data.startswith("paid:"):
                oid = data[5:]
                o = ORDERS.get(oid)
                if o and str(o["user_chat_id"]) == str(chat_id):
                    if o["status"] == "awaiting_payment":
                        o["status"] = "awaiting_proof"
                        save_orders()
                        USER_STATE[str(chat_id)] = {"step": "proof", "order_id": oid}
                        send(chat_id,
                             "📸 <b>أرسل دليل الدفع:</b>\n\n"
                             "• لقطة شاشة للتحويل، <b>أو</b>\n"
                             "• رقم العملية (TxID / 🆔)\n\n"
                             "اكتب الرقم أو أرسل الصورة هنا 👇")
                        answer = "أرسل الدليل"
                    else:
                        answer = "الطلب قيد المعالجة."
                else:
                    answer = "الطلب غير موجود."
            elif data.startswith("cancel:"):
                oid = data[7:]
                o = ORDERS.get(oid)
                if o and str(o["user_chat_id"]) == str(chat_id):
                    o["status"] = "cancelled"; save_orders(); answer = "تم إلغاء الطلب ❌"
                else:
                    answer = "الطلب غير موجود."
            elif data.startswith("approve:"):
                answer = admin_decision(chat_id, data[8:], True)
            elif data.startswith("reject:"):
                answer = admin_decision(chat_id, data[7:], False)
        except Exception as e:
            print("callback error:", e); answer = "⚠️ حدث خطأ"
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": answer[:190]})
        return

    msg = u.get("message") or {}
    if not msg: return
    chat_id = msg["chat"]["id"]
    user_name = msg.get("from", {}).get("first_name", "")
    text = (msg.get("text") or "").strip()
    print("INCOMING:", repr(text), "chat:", chat_id)

    # custom quantity input
    st = USER_STATE.get(str(chat_id))
    if st and st.get("step") == "qty_custom" and text.isdigit():
        q = int(text)
        if q < 1 or q > 999:
            send(chat_id, "⚠️ العدد يجب أن يكون بين 1 و 999."); return
        pid = st["product_id"]
        p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
        if p and q > stock_of(p) and stock_of(p) < 999:
            send(chat_id, f"⚠️ المخزون المتوفر: {stock_of(p)} فقط."); return
        show_payment_methods(chat_id, pid, q)
        return

    # transaction ID / proof text input
    st = USER_STATE.get(str(chat_id))
    if st and st.get("step") == "proof" and text:
        oid = st.get("order_id")
        o = ORDERS.get(oid)
        if o and o["status"] == "awaiting_proof":
            o["status"] = "awaiting_approval"
            o["proof_type"] = "txid"
            o["proof_text"] = text[:200]
            save_orders()
            USER_STATE.pop(str(chat_id), None)
            send(chat_id, "✅ توصلنا برقم العملية. سيتم مراجعة طلبك قريباً ⏳")
            notify_admin_proof(o, oid, user_name, chat_id, f"🆔 رقم العملية:\n<code>{esc(text[:200])}</code>")
            return

    if text == "/myid":
        send(chat_id, f"🆔 <code>{chat_id}</code>"); return
    if text.startswith("/start"):
        handle_start(chat_id, user_name); return
    if "المتجر" in text:
        show_brands(chat_id); return
    if "طلبات" in text:
        mine = [o for o in ORDERS.values() if str(o["user_chat_id"]) == str(chat_id)]
        if not mine:
            send(chat_id, "🧾 لا توجد طلبات بعد.")
        else:
            lines = []
            for o in sorted(mine, key=lambda x: -x["created_at"])[:10]:
                st_map = {"awaiting_payment": "⏳ بانتظار الدفع",
                          "awaiting_approval": "🔍 قيد المراجعة",
                          "delivered": "✅ تم التسليم",
                          "rejected": "❌ مرفوض", "cancelled": "🚫 ملغي"}
                lines.append(f"📦 {esc(o['product_name'])} × {o['qty']} — "
                             f"${o['total']} ({st_map.get(o['status'], o['status'])})")
            send(chat_id, "🧾 <b>طلباتي:</b>\n\n" + "\n\n".join(lines))
        return
    if "شحن" in text:
        kb = {"inline_keyboard": [
            [{"text": f"{m.get('emoji','💳')} {m['name']}", "callback_data": "topup_info"}]
            for m in PAYMENT_METHODS]}
        send(chat_id,
             "💳 <b>شحن الرصيد</b>\n\nاختر طريقة الدفع وتواصل مع الإدارة:\n"
             f"👤 @{SUPPORT_USER}",
             {"inline_keyboard": [[{"text": f"💬 تواصل مع الدعم",
                                    "url": f"https://t.me/{SUPPORT_USER}"}]]})
        return
    if "مساعدة" in text:
        send(chat_id,
             f"❓ <b>مساعدة</b>\n\n"
             f"🛍️ المتجر — تصفح المنتجات والشراء\n"
             f"🧾 طلباتي — تتبع طلباتك\n"
             f"💳 شحن الرصيد — طرق الدفع\n\n"
             f"للتواصل المباشر: @{SUPPORT_USER}",
             {"inline_keyboard": [[{"text": "💬 الدعم",
                                    "url": f"https://t.me/{SUPPORT_USER}"}]]})
        return
    if msg.get("photo"):
        handle_photo(chat_id, msg["message_id"], user_name); return
    pend = [o for o in ORDERS.values()
            if str(o["user_chat_id"]) == str(chat_id) and o["status"] == "awaiting_payment"]
    if pend:
        send(chat_id, "📸 أرسل لقطة شاشة للتحويل لإتمام طلبك.")
    else:
        send(chat_id, "🛍️ مرحباً بك!\nاختر من القائمة بالأسفل 👇", main_keyboard())

# ------------------------------------------------------------------ server -

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != f"/webhook/{WEBHOOK_SECRET}":
            self.send_response(403); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        update = json.loads(self.rfile.read(length))
        threading.Thread(target=process, args=(update,), daemon=True).start()
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a): pass

def process(update):
    try: handle_update(update)
    except Exception as e: print("handler error:", e)

def main():
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
    print("setWebhook:", tg("setWebhook", {"url": url}))
    refresh_products(force=True)
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
