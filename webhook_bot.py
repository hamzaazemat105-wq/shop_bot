#!/usr/bin/env python3
"""
Professional Telegram Shop Bot — Complete Version (i18n: ar/en/ru)
==================================================================
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
  → user taps [✅ تم الدفع] → sends screenshot or TxID
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
PUBLIC_URL  = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "devsecret")
ADMIN_CHAT_ID = (os.environ.get("ADMIN_CHAT_ID") or "").strip()
MARGIN = float(os.environ.get("MARGIN", "1.30"))
SUPPORT_USER = os.environ.get("SUPPORT_USER", "hamzaazemat105").lstrip("@")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
SHOP = f"https://{SHOP_BASE_URL}"

PAYMENT_METHODS = []
_raw_pm = os.environ.get("PAYMENT_METHODS_JSON", "").strip()
if _raw_pm:
    try:
        PAYMENT_METHODS = json.loads(_raw_pm)
    except Exception as e:
        print("PAYMENT_METHODS_JSON parse error:", e)

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

# ---------------------------------------------------------------- state ----

ORDERS = {}          # oid -> order dict
USER_STATE = {}      # chat_id -> {"step":..., "product_id":..., "qty":...}
PRODUCTS = {"items": [], "ts": 0}
LANGS = {}           # chat_id(str) -> "ar" | "en" | "ru"

# ------------------------------------------------- persistence -----------
# Orders + languages survive Railway restarts/redeploys via JSON files on disk.
_HERE = os.path.dirname(os.path.abspath(__file__))
ORDERS_FILE = os.path.join(_HERE, "orders.json")
LANGS_FILE = os.path.join(_HERE, "langs.json")

def _save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print("save error:", path, e)

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("load error:", path, e)
        return {}

def save_orders():
    _save_json(ORDERS_FILE, ORDERS)

def save_langs():
    _save_json(LANGS_FILE, LANGS)

ORDERS.update(_load_json(ORDERS_FILE))
LANGS.update(_load_json(LANGS_FILE))
print(f"loaded {len(ORDERS)} orders, {len(LANGS)} lang prefs from disk")

# ------------------------------------------------------------------- i18n ---

STRINGS = {
    "menu_shop":        {"ar": "🛍️ المتجر", "en": "🛍️ Shop", "ru": "🛍️ Магазин"},
    "menu_orders":      {"ar": "📦 طلباتي", "en": "📦 My orders", "ru": "📦 Мои заказы"},
    "menu_reserv":      {"ar": "⏳ حجوزاتي", "en": "⏳ My bookings", "ru": "⏳ Мои брони"},
    "menu_topup":       {"ar": "💳 شحن الرصيد", "en": "💳 Top up", "ru": "💳 Пополнить"},
    "menu_referral":    {"ar": "🔗 رابط الإحالة", "en": "🔗 Referral link", "ru": "🔗 Реферальная ссылка"},
    "menu_support":     {"ar": "🎧 الدعم", "en": "🎧 Support", "ru": "🎧 Поддержка"},
    "menu_lang":        {"ar": "🌐 Language / اللغة", "en": "🌐 Language", "ru": "🌐 Язык"},
    "menu_reseller":    {"ar": "🔑 بوابة الموزعين", "en": "🔑 Reseller portal", "ru": "🔑 Портал дилеров"},
    "welcome":          {"ar": "🛍️ <b>مرحباً بك في المتجر!</b>\n\nأهلاً {name}! 👋\nاختار من القائمة 👇",
                         "en": "🛍️ <b>Welcome to the store!</b>\n\nHello {name}! 👋\nChoose from the menu 👇",
                         "ru": "🛍️ <b>Добро пожаловать в магазин!</b>\n\nПривет, {name}! 👋\nВыберите из меню 👇"},
    "admin_hello":      {"ar": "👋 مرحباً أيها المدير!\nالبوت يعمل بالـ webhook ⚡"},
    "brands_title":     {"ar": "🛍️ <b>اختار العلامة التجارية:</b>",
                         "en": "🛍️ <b>Choose a brand:</b>",
                         "ru": "🛍️ <b>Выберите бренд:</b>"},
    "brands_count":     {"ar": "{np} منتج · {nb} علامة", "en": "{np} products · {nb} brands", "ru": "{np} товаров · {nb} брендов"},
    "stock_legend":     {"ar": "🟢 متوفر    🔴 غير متوفر", "en": "🟢 Available    🔴 Out of stock", "ru": "🟢 В наличии    🔴 Нет в наличии"},
    "choose_product":   {"ar": "اختار المنتج:", "en": "Choose a product:", "ru": "Выберите товар:"},
    "no_products":      {"ar": "لا توجد منتجات في هذه العلامة.", "en": "No products in this brand.", "ru": "В этом бренде нет товаров."},
    "back_brands":      {"ar": "⬅️ رجوع للعلامات", "en": "⬅️ Back to brands", "ru": "⬅️ Назад к брендам"},
    "product_missing":  {"ar": "⚠️ المنتج غير موجود.", "en": "⚠️ Product not found.", "ru": "⚠️ Товар не найден."},
    "price":            {"ar": "💰 الثمن:", "en": "💰 Price:", "ru": "💰 Цена:"},
    "stock_is":         {"ar": "📊 المخزون:", "en": "📊 Stock:", "ru": "📊 Остаток:"},
    "avail_n":          {"ar": "✅ متوفر ({n})", "en": "✅ Available ({n})", "ru": "✅ В наличии ({n})"},
    "not_avail":        {"ar": "❌ غير متوفر", "en": "❌ Out of stock", "ru": "❌ Нет в наличии"},
    "buy_now":          {"ar": "🛒 اشترِ الآن", "en": "🛒 Buy now", "ru": "🛒 Купить"},
    "back":             {"ar": "⬅️ رجوع", "en": "⬅️ Back", "ru": "⬅️ Назад"},
    "choose_qty":       {"ar": "🔢 <b>اختار العدد:</b>", "en": "🔢 <b>Choose quantity:</b>", "ru": "🔢 <b>Выберите количество:</b>"},
    "per_unit":         {"ar": "/ للواحد", "en": "each", "ru": "за шт."},
    "avail_label":      {"ar": "📊 متوفر:", "en": "📊 Available:", "ru": "📊 В наличии:"},
    "custom_qty":       {"ar": "✏️ عدد مخصص", "en": "✏️ Custom quantity", "ru": "✏️ Своё количество"},
    "write_qty":        {"ar": "✏️ <b>اكتب العدد المطلوب</b> (رقم فقط):",
                         "en": "✏️ <b>Type the quantity</b> (numbers only):",
                         "ru": "✏️ <b>Введите количество</b> (только цифры):"},
    "qty_range":        {"ar": "⚠️ العدد يجب أن يكون بين 1 و 999.", "en": "⚠️ Quantity must be between 1 and 999.", "ru": "⚠️ Количество должно быть от 1 до 999."},
    "stock_only":       {"ar": "⚠️ المخزون المتوفر: {n} فقط.", "en": "⚠️ Only {n} in stock.", "ru": "⚠️ В наличии только {n}."},
    "choose_pay":       {"ar": "💳 <b>اختار طريقة الدفع:</b>", "en": "💳 <b>Choose payment method:</b>", "ru": "💳 <b>Выберите способ оплаты:</b>"},
    "order_title":      {"ar": "🧾 <b>تأكيد الطلب</b>", "en": "🧾 <b>Order confirmation</b>", "ru": "🧾 <b>Подтверждение заказа</b>"},
    "f_product":        {"ar": "📦 المنتج:", "en": "📦 Product:", "ru": "📦 Товар:"},
    "f_qty":            {"ar": "🔢 العدد:", "en": "🔢 Quantity:", "ru": "🔢 Количество:"},
    "f_total":          {"ar": "💰 المجموع:", "en": "💰 Total:", "ru": "💰 Итого:"},
    "f_payvia":         {"ar": "💳 الدفع عبر:", "en": "💳 Pay via:", "ru": "💳 Оплата через:"},
    "f_instr":          {"ar": "📋 <b>التعليمات:</b>", "en": "📋 <b>Instructions:</b>", "ru": "📋 <b>Инструкции:</b>"},
    "step1":            {"ar": "1️⃣ حوّل المبلغ الدقيق", "en": "1️⃣ Send the exact amount", "ru": "1️⃣ Отправьте точную сумму"},
    "step2":            {"ar": "2️⃣ اضغط زر <b>✅ تم الدفع</b>", "en": "2️⃣ Tap <b>✅ Paid</b>", "ru": "2️⃣ Нажмите <b>✅ Оплачено</b>"},
    "step3":            {"ar": "3️⃣ أرسل <b>لقطة شاشة</b> أو <b>رقم العملية (TxID)</b>",
                         "en": "3️⃣ Send a <b>screenshot</b> or <b>transaction ID (TxID)</b>",
                         "ru": "3️⃣ Отправьте <b>скриншот</b> или <b>ID транзакции (TxID)</b>"},
    "deliver_after":    {"ar": "سيتم إرسال المنتج إليك مباشرة بعد التأكيد.",
                         "en": "The product will be delivered right after confirmation.",
                         "ru": "Товар будет доставлен сразу после подтверждения."},
    "paid_btn":         {"ar": "✅ تم الدفع", "en": "✅ Paid", "ru": "✅ Оплачено"},
    "cancel_btn":       {"ar": "❌ إلغاء الطلب", "en": "❌ Cancel order", "ru": "❌ Отменить заказ"},
    "cancelled":        {"ar": "تم إلغاء الطلب ❌", "en": "Order cancelled ❌", "ru": "Заказ отменён ❌"},
    "order_gone":       {"ar": "الطلب غير موجود.", "en": "Order not found.", "ru": "Заказ не найден."},
    "order_busy":       {"ar": "الطلب قيد المعالجة.", "en": "Order is being processed.", "ru": "Заказ обрабатывается."},
    "send_proof":       {"ar": "📸 <b>أرسل دليل الدفع:</b>", "en": "📸 <b>Send payment proof:</b>", "ru": "📸 <b>Отправьте подтверждение оплаты:</b>"},
    "proof_or":         {"ar": "• لقطة شاشة للتحويل، <b>أو</b>", "en": "• Transfer screenshot, <b>or</b>", "ru": "• Скриншот перевода <b>или</b>"},
    "proof_txid":       {"ar": "• رقم العملية (TxID / 🆔)", "en": "• Transaction ID (TxID / 🆔)", "ru": "• ID транзакции (TxID / 🆔)"},
    "proof_here":       {"ar": "اكتب الرقم أو أرسل الصورة هنا 👇", "en": "Type the ID or send the photo here 👇", "ru": "Введите ID или отправьте фото сюда 👇"},
    "proof_ok":         {"ar": "✅ توصلنا بإثبات الدفع. سيتم مراجعة طلبك قريباً ⏳",
                         "en": "✅ Payment proof received. Your order will be reviewed soon ⏳",
                         "ru": "✅ Подтверждение получено. Ваш заказ скоро будет проверен ⏳"},
    "txid_ok":          {"ar": "✅ توصلنا برقم العملية. سيتم مراجعة طلبك قريباً ⏳",
                         "en": "✅ Transaction ID received. Your order will be reviewed soon ⏳",
                         "ru": "✅ ID транзакции получен. Ваш заказ скоро будет проверен ⏳"},
    "no_pending":       {"ar": "ليس لديك طلب بانتظار الدفع. ابدأ من 🛍️ المتجر.",
                         "en": "You have no pending payment order. Start from 🛍️ Shop.",
                         "ru": "У вас нет ожидающих оплату заказов. Начните с 🛍️ Магазина."},
    "orders_title":     {"ar": "🧾 <b>طلباتي:</b>", "en": "🧾 <b>My orders:</b>", "ru": "🧾 <b>Мои заказы:</b>"},
    "no_orders":        {"ar": "🧾 لا توجد طلبات بعد.", "en": "🧾 No orders yet.", "ru": "🧾 Заказов пока нет."},
    "st_pay":           {"ar": "⏳ بانتظار الدفع", "en": "⏳ Awaiting payment", "ru": "⏳ Ожидает оплаты"},
    "st_review":        {"ar": "🔍 قيد المراجعة", "en": "🔍 Under review", "ru": "🔍 На проверке"},
    "st_done":          {"ar": "✅ تم التسليم", "en": "✅ Delivered", "ru": "✅ Доставлен"},
    "st_rejected":      {"ar": "❌ مرفوض", "en": "❌ Rejected", "ru": "❌ Отклонён"},
    "st_cancelled":     {"ar": "🚫 ملغي", "en": "🚫 Cancelled", "ru": "🚫 Отменён"},
    "reserv_title":     {"ar": "⏳ <b>حجوزاتي</b>\n\nليس لديك أي حجوزات نشطة حالياً.",
                         "en": "⏳ <b>My bookings</b>\n\nYou have no active bookings.",
                         "ru": "⏳ <b>Мои брони</b>\n\nУ вас нет активных броней."},
    "topup_title":      {"ar": "💳 <b>شحن الرصيد</b>\n\nاختر طريقة الدفع وتواصل مع الإدارة:",
                         "en": "💳 <b>Top up balance</b>\n\nChoose a payment method and contact support:",
                         "ru": "💳 <b>Пополнить баланс</b>\n\nВыберите способ оплаты и свяжитесь с поддержкой:"},
    "contact_support":  {"ar": "💬 تواصل مع الدعم", "en": "💬 Contact support", "ru": "💬 Связаться с поддержкой"},
    "back_menu":        {"ar": "⬅️ رجوع للقائمة", "en": "⬅️ Back to menu", "ru": "⬅️ Назад в меню"},
    "referral_title":   {"ar": "🔗 <b>رابط الإحالة الخاص بك:</b>", "en": "🔗 <b>Your referral link:</b>", "ru": "🔗 <b>Ваша реферальная ссылка:</b>"},
    "referral_share":   {"ar": "شاركه مع أصدقائك واربح عمولة على كل عملية شراء! 🎁",
                         "en": "Share it with friends and earn commission on every purchase! 🎁",
                         "ru": "Делитесь с друзьями и получайте комиссию с каждой покупки! 🎁"},
    "support_title":    {"ar": "🎧 <b>الدعم</b>\n\nللتواصل المباشر مع الإدارة:",
                         "en": "🎧 <b>Support</b>\n\nTo contact us directly:",
                         "ru": "🎧 <b>Поддержка</b>\n\nДля прямой связи:"},
    "choose_lang":      {"ar": "🌐 <b>الرجاء اختيار اللغة:</b>", "en": "🌐 <b>Please choose a language:</b>", "ru": "🌐 <b>Пожалуйста, выберите язык:</b>"},
    "lang_set":         {"ar": "✅ تم اختيار اللغة: العربية", "en": "✅ Language selected: English", "ru": "✅ Язык выбран: Русский"},
    "reseller_title":   {"ar": "🔑 <b>بوابة الموزعين</b>\n\nهذه المنطقة مخصصة للموزعين المعتمدين.",
                         "en": "🔑 <b>Reseller portal</b>\n\nThis area is for approved resellers.",
                         "ru": "🔑 <b>Портал дилеров</b>\n\nЭта зона для проверенных дилеров."},
    "reseller_contact": {"ar": "للاستفسار تواصل مع:", "en": "For inquiries contact:", "ru": "По вопросам обращайтесь:"},
    "refresh":          {"ar": "🔄 تحديث", "en": "🔄 Refresh", "ru": "🔄 Обновить"},
    "need_screenshot":  {"ar": "📸 أرسل لقطة شاشة للتحويل لإتمام طلبك.",
                         "en": "📸 Send a transfer screenshot to complete your order.",
                         "ru": "📸 Отправьте скриншот перевода для завершения заказа."},
    "hello_choose":     {"ar": "🛍️ مرحباً بك!\nاختر من القائمة بالأسفل 👇",
                         "en": "🛍️ Welcome!\nChoose from the menu below 👇",
                         "ru": "🛍️ Добро пожаловать!\nВыберите из меню ниже 👇"},
    "ans_proof":        {"ar": "أرسل الدليل", "en": "Send proof", "ru": "Отправьте подтверждение"},
    "ans_number":       {"ar": "اكتب العدد", "en": "Type the number", "ru": "Введите число"},
    "ans_done":         {"ar": "تم ✅", "en": "Done ✅", "ru": "Готово ✅"},
    "help_title":       {"ar": "❓ <b>مساعدة</b>", "en": "❓ <b>Help</b>", "ru": "❓ <b>Помощь</b>"},
    "help_shop":        {"ar": "🛍️ المتجر — تصفح المنتجات والشراء", "en": "🛍️ Shop — browse and buy products", "ru": "🛍️ Магазин — каталог и покупки"},
    "help_orders":      {"ar": "🧾 طلباتي — تتبع طلباتك", "en": "🧾 My orders — track your orders", "ru": "🧾 Мои заказы — отслеживание заказов"},
    "help_topup":       {"ar": "💳 شحن الرصيد — طرق الدفع", "en": "💳 Top up — payment methods", "ru": "💳 Пополнение — способы оплаты"},
    "help_contact":     {"ar": "للتواصل المباشر:", "en": "To contact us directly:", "ru": "Для прямой связи:"},
    "support_word":     {"ar": "💬 الدعم", "en": "💬 Support", "ru": "💬 Поддержка"},
    "rejected_msg":     {"ar": "❌ تم رفض طلبك. تواصل مع الدعم للمزيد من المعلومات.",
                         "en": "❌ Your order was rejected. Contact support for more info.",
                         "ru": "❌ Ваш заказ отклонён. Свяжитесь с поддержкой."},
    "buy_failed":       {"ar": "⚠️ حدث خطأ أثناء تجهيز طلبك. سيتواصل معك المدير قريباً.",
                         "en": "⚠️ An error occurred while processing your order. The manager will contact you soon.",
                         "ru": "⚠️ Произошла ошибка при обработке заказа. Менеджер скоро свяжется с вами."},
    "delivered_msg":    {"ar": "🎉 <b>تم تأكيد طلبك!</b>\n\n📦 {product} × {qty}\n\n🔑 بيانات المنتج:\n{body}\n\nشكراً لثقتك! 🙏",
                         "en": "🎉 <b>Your order is confirmed!</b>\n\n📦 {product} × {qty}\n\n🔑 Product details:\n{body}\n\nThank you for your trust! 🙏",
                         "ru": "🎉 <b>Ваш заказ подтверждён!</b>\n\n📦 {product} × {qty}\n\n🔑 Данные товара:\n{body}\n\nСпасибо за доверие! 🙏"},
    "delivered_short":  {"ar": "🎉 تم تأكيد طلبك! سيتواصل معك المدير.",
                         "en": "🎉 Your order is confirmed! The manager will contact you.",
                         "ru": "🎉 Ваш заказ подтверждён! Менеджер свяжется с вами."},
}

def get_lang(chat_id):
    return LANGS.get(str(chat_id), "ar")

def t(key, chat_id=None):
    lang = get_lang(chat_id) if chat_id is not None else "ar"
    entry = STRINGS.get(key, {})
    return entry.get(lang) or entry.get("ar") or key

def set_lang(chat_id, lang):
    LANGS[str(chat_id)] = lang
    save_langs()

def status_label(status, chat_id):
    return {"awaiting_payment": t("st_pay", chat_id),
            "awaiting_proof": t("st_pay", chat_id),
            "awaiting_approval": t("st_review", chat_id),
            "delivered": t("st_done", chat_id),
            "rejected": t("st_rejected", chat_id),
            "cancelled": t("st_cancelled", chat_id)}.get(status, status)

# ---------------------------------------------------------------- helpers --

def tg(method, params=None):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{TG}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def esc(s):
    return html.escape(str(s or ""), quote=False)

def send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
         "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return tg("sendMessage", p)

def send_photo(chat_id, photo_bytes, caption, reply_markup=None):
    import io
    boundary = "----botboundary1234"
    body = io.BytesIO()
    def field(n, v):
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())
    field("chat_id", str(chat_id)); field("caption", caption); field("parse_mode", "HTML")
    if reply_markup:
        field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"p.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
    body.write(photo_bytes); body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{TG}/sendPhoto", data=body.getvalue())
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("ok")
    except Exception as e:
        print("sendPhoto failed:", e); return False

def shop(path, method="GET", data=None):
    url = SHOP + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-API-Key", SHOP_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ---------------------------------------------------------------- products -

def refresh_products(force=False):
    if not force and time.time() - PRODUCTS["ts"] < 120 and PRODUCTS["items"]:
        return PRODUCTS["items"]
    try:
        res = shop("/api/products")
        items = res.get("products", res.get("items", [])) if isinstance(res, dict) else res
        PRODUCTS["items"] = items or []
        PRODUCTS["ts"] = time.time()
    except Exception as e:
        print("products fetch failed:", e)
    return PRODUCTS["items"]

def prod_name(p):
    return p.get("name") or p.get("title") or f"#{p.get('id')}"

def brand_of(p):
    b = (p.get("brand") or p.get("category") or "عام").strip()
    return b or "عام"

def stock_of(p):
    for k in ("stock", "quantity", "available", "count"):
        v = p.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(v)
    return 999

def cust_price(p):
    try:
        return round(float(p.get("price", 0)) * MARGIN, 2)
    except Exception:
        return 0.0

_BRAND_EMOJI = ["🏷️", "🛒", "🎮", "🎨", "📱", "💻", "🎬", "📚", "🎵", "👕"]
def brand_emoji(b):
    return _BRAND_EMOJI[abs(hash(b)) % len(_BRAND_EMOJI)]

def product_image(name):
    safe = re.sub(r"[^\w\-]+", "_", name, flags=re.U).strip("_")[:60]
    for ext in (".jpg", ".png", ".jpeg", ".webp"):
        for base in ("img", "images"):
            p = os.path.join(_HERE, base, safe + ext)
            if os.path.exists(p):
                return p
    return None

# ---------------------------------------------------------------- keyboards

def main_keyboard(chat_id):
    return {"inline_keyboard": [
        [{"text": t("menu_shop", chat_id), "callback_data": "menu:shop"},
         {"text": t("menu_orders", chat_id), "callback_data": "menu:orders"}],
        [{"text": t("menu_reserv", chat_id), "callback_data": "menu:reservations"},
         {"text": t("menu_topup", chat_id), "callback_data": "menu:topup"}],
        [{"text": t("menu_referral", chat_id), "callback_data": "menu:referral"},
         {"text": t("menu_support", chat_id), "callback_data": "menu:support"}],
        [{"text": t("menu_lang", chat_id), "callback_data": "menu:language"},
         {"text": t("menu_reseller", chat_id), "callback_data": "menu:reseller"}],
    ]}

def back_to_menu_kb(chat_id):
    return {"inline_keyboard": [[{"text": t("back_menu", chat_id), "callback_data": "menu:main"}]]}

def show_main_menu(chat_id, name=""):
    send(chat_id, t("welcome", chat_id).format(name=esc(name)), main_keyboard(chat_id))

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
        row.append({"text": f"{brand_emoji(b)} {b} · {info['total']}",
                    "callback_data": f"brand:{b}"})
        if len(row) == 3:
            kb.append(row); row = []
    if row: kb.append(row)
    return kb, len(products), len(brands)

def show_brands(chat_id):
    kb, n_prod, n_brand = brand_keyboard()
    kb.append([{"text": t("refresh", chat_id), "callback_data": "brands_refresh"}])
    send(chat_id,
         f"{t('brands_title', chat_id)}\n{t('brands_count', chat_id).format(np=n_prod, nb=n_brand)}\n\n"
         f"{t('stock_legend', chat_id)}",
         {"inline_keyboard": kb})

def show_brand_products(chat_id, brand):
    products = [p for p in refresh_products() if brand_of(p) == brand]
    if not products:
        send(chat_id, t("no_products", chat_id))
        return
    kb = []
    for p in products:
        pid = p.get("id")
        st = stock_of(p)
        mark = "✅" if st > 0 else "❌"
        label = f"{mark} {prod_name(p)[:30]} — ${cust_price(p)}"
        kb.append([{"text": label, "callback_data": f"product:{pid}"}])
    kb.append([{"text": t("back_brands", chat_id), "callback_data": "back_brands"}])
    send(chat_id,
         f"{brand_emoji(brand)} <b>{esc(brand)}</b> — {t('choose_product', chat_id)}",
         {"inline_keyboard": kb})

def show_product_detail(chat_id, pid):
    p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
    if not p:
        send(chat_id, t("product_missing", chat_id)); return
    name, st, pr = prod_name(p), stock_of(p), cust_price(p)
    stock_txt = t("avail_n", chat_id).format(n=st) if st > 0 else t("not_avail", chat_id)
    caption = (f"📦 <b>{esc(name)}</b>\n\n"
               f"{t('price', chat_id)} <b>${pr}</b>\n"
               f"{t('stock_is', chat_id)} {stock_txt}\n")
    kb = {"inline_keyboard": [
        [{"text": t("buy_now", chat_id), "callback_data": f"buy:{pid}"}],
        [{"text": t("back", chat_id), "callback_data": f"brand:{brand_of(p)}"}],
    ]} if st > 0 else {"inline_keyboard": [
        [{"text": t("back", chat_id), "callback_data": f"brand:{brand_of(p)}"}]]}
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
    kb.append([{"text": t("custom_qty", chat_id), "callback_data": f"qtycustom:{pid}"}])
    kb.append([{"text": t("back", chat_id), "callback_data": f"product:{pid}"}])
    USER_STATE[str(chat_id)] = {"step": "qty", "product_id": pid}
    send(chat_id,
         f"{t('choose_qty', chat_id)}\n\n{esc(prod_name(p))}\n"
         f"💰 ${cust_price(p)} {t('per_unit', chat_id)}\n{t('avail_label', chat_id)} {st}",
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
    kb.append([{"text": t("back", chat_id), "callback_data": f"buy:{pid}"}])
    send(chat_id,
         f"{t('choose_pay', chat_id)}\n\n"
         f"📦 {esc(prod_name(p))} × {qty}\n"
         f"{t('f_total', chat_id)} <b>${total}</b>",
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
        [{"text": t("paid_btn", chat_id), "callback_data": f"paid:{oid}"}],
        [{"text": t("cancel_btn", chat_id), "callback_data": f"cancel:{oid}"}]]}
    send(chat_id,
         f"{t('order_title', chat_id)}\n\n"
         f"{t('f_product', chat_id)} <b>{esc(prod_name(p))}</b>\n"
         f"{t('f_qty', chat_id)} <b>{qty}</b>\n"
         f"{t('f_total', chat_id)} <b>${total}</b>\n"
         f"{t('f_payvia', chat_id)} <b>{esc(m['name'])}</b>\n\n"
         f"{t('f_instr', chat_id)}\n{esc(m.get('instructions',''))}\n\n"
         f"{t('step1', chat_id)}\n"
         f"{t('step2', chat_id)}\n"
         f"{t('step3', chat_id)}\n\n"
         f"{t('deliver_after', chat_id)}",
         kb)

# ---------------------------------------------------------------- handlers -

def handle_start(chat_id, name):
    if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
        send(chat_id, t("admin_hello", chat_id), main_keyboard(chat_id))
        return
    show_main_menu(chat_id, name)

def handle_menu(chat_id, section, user_name):
    if section == "shop":
        show_brands(chat_id)
    elif section == "orders":
        mine = [o for o in ORDERS.values() if str(o["user_chat_id"]) == str(chat_id)]
        if not mine:
            send(chat_id, t("no_orders", chat_id), back_to_menu_kb(chat_id))
        else:
            lines = []
            for o in sorted(mine, key=lambda x: -x["created_at"])[:10]:
                lines.append(f"📦 {esc(o['product_name'])} × {o['qty']} — "
                             f"${o['total']} ({status_label(o['status'], chat_id)})")
            send(chat_id, t("orders_title", chat_id) + "\n\n" + "\n\n".join(lines),
                 back_to_menu_kb(chat_id))
    elif section == "reservations":
        send(chat_id, t("reserv_title", chat_id), back_to_menu_kb(chat_id))
    elif section == "topup":
        send(chat_id,
             f"{t('topup_title', chat_id)}\n👤 @{SUPPORT_USER}",
             {"inline_keyboard": [
                 [{"text": t("contact_support", chat_id), "url": f"https://t.me/{SUPPORT_USER}"}],
                 [{"text": t("back_menu", chat_id), "callback_data": "menu:main"}]]})
    elif section == "referral":
        send(chat_id,
             f"{t('referral_title', chat_id)}\n\n"
             f"<code>https://t.me/{esc(BOT_USERNAME)}?start=ref_{chat_id}</code>\n\n"
             f"{t('referral_share', chat_id)}",
             back_to_menu_kb(chat_id))
    elif section == "support":
        send(chat_id,
             f"{t('support_title', chat_id)}",
             {"inline_keyboard": [
                 [{"text": t("contact_support", chat_id), "url": f"https://t.me/{SUPPORT_USER}"}],
                 [{"text": t("back_menu", chat_id), "callback_data": "menu:main"}]]})
    elif section == "language":
        send(chat_id, t("choose_lang", chat_id),
             {"inline_keyboard": [
                 [{"text": "🇺🇸 English", "callback_data": "lang:en"},
                  {"text": "🇸🇦 العربية", "callback_data": "lang:ar"}],
                 [{"text": "🇷🇺 Русский", "callback_data": "lang:ru"},
                  {"text": "🇫🇷 Français", "callback_data": "lang:fr"}],
                 [{"text": "🇨🇳 中文", "callback_data": "lang:zh"},
                  {"text": t("back_menu", chat_id), "callback_data": "menu:main"}]]})
    elif section == "reseller":
        send(chat_id,
             f"{t('reseller_title', chat_id)}\n{t('reseller_contact', chat_id)} @{SUPPORT_USER}",
             back_to_menu_kb(chat_id))
    elif section == "main":
        show_main_menu(chat_id, user_name)

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
        send(chat_id, t("no_pending", chat_id), main_keyboard(chat_id))
        return
    oid, o = pend[-1]
    o["status"] = "awaiting_approval"
    o["proof_type"] = "photo"
    save_orders()
    USER_STATE.pop(str(chat_id), None)
    send(chat_id, t("proof_ok", chat_id))
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
        send(o["user_chat_id"], t("rejected_msg", o["user_chat_id"]))
        return "تم رفض الطلب."
    # auto-buy from supplier
    try:
        res = shop("/api/buy", "POST", {"product_id": o["product_id"], "quantity": o["qty"]})
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    if not res.get("ok"):
        send(o["user_chat_id"], t("buy_failed", o["user_chat_id"]))
        return f"⚠️ فشل الشراء من المزود: {esc(res.get('error',''))}"
    o["status"] = "delivered"
    save_orders()
    items = res.get("items", [])
    if items:
        body = "\n\n".join(f"<code>{esc(i)}</code>" for i in items)
        send(o["user_chat_id"],
             t("delivered_msg", o["user_chat_id"]).format(
                 product=esc(o['product_name']), qty=o['qty'], body=body))
    else:
        send(o["user_chat_id"], t("delivered_short", o["user_chat_id"]))
    return "✅ تم التأكيد والتسليم."

def handle_update(u):
    cb = u.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        name = cb.get("from", {}).get("first_name", "")
        answer = t("ans_done", chat_id)
        try:
            if data.startswith("menu:"):
                handle_menu(chat_id, data[5:], name)
            elif data.startswith("lang:"):
                lang = data[5:]
                if lang in ("ar", "en", "ru"):
                    set_lang(chat_id, lang)
                send(chat_id, t("lang_set", chat_id), back_to_menu_kb(chat_id))
                answer = t("ans_done", chat_id)
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
                send(chat_id, t("write_qty", chat_id))
                answer = t("ans_number", chat_id)
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
                             f"{t('send_proof', chat_id)}\n\n"
                             f"{t('proof_or', chat_id)}\n"
                             f"{t('proof_txid', chat_id)}\n\n"
                             f"{t('proof_here', chat_id)}")
                        answer = t("ans_proof", chat_id)
                    else:
                        answer = t("order_busy", chat_id)
                else:
                    answer = t("order_gone", chat_id)
            elif data.startswith("cancel:"):
                oid = data[7:]
                o = ORDERS.get(oid)
                if o and str(o["user_chat_id"]) == str(chat_id):
                    o["status"] = "cancelled"; save_orders(); answer = t("cancelled", chat_id)
                else:
                    answer = t("order_gone", chat_id)
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
            send(chat_id, t("qty_range", chat_id)); return
        pid = st["product_id"]
        p = next((x for x in refresh_products() if str(x.get("id")) == str(pid)), None)
        if p and q > stock_of(p) and stock_of(p) < 999:
            send(chat_id, t("stock_only", chat_id).format(n=stock_of(p))); return
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
            send(chat_id, t("txid_ok", chat_id))
            notify_admin_proof(o, oid, user_name, chat_id, f"🆔 رقم العملية:\n<code>{esc(text[:200])}</code>")
            return

    if text == "/myid":
        send(chat_id, f"🆔 <code>{chat_id}</code>"); return
    if text.startswith("/start"):
        handle_start(chat_id, user_name); return
    low = text.lower()
    if any(k in low for k in ("المتجر", "shop", "магазин")):
        show_brands(chat_id); return
    if any(k in low for k in ("طلبات", "order", "заказ")):
        mine = [o for o in ORDERS.values() if str(o["user_chat_id"]) == str(chat_id)]
        if not mine:
            send(chat_id, t("no_orders", chat_id))
        else:
            lines = []
            for o in sorted(mine, key=lambda x: -x["created_at"])[:10]:
                lines.append(f"📦 {esc(o['product_name'])} × {o['qty']} — "
                             f"${o['total']} ({status_label(o['status'], chat_id)})")
            send(chat_id, t("orders_title", chat_id) + "\n\n" + "\n\n".join(lines))
        return
    if any(k in low for k in ("شحن", "topup", "top up", "баланс", "пополн")):
        send(chat_id,
             f"{t('topup_title', chat_id)}\n👤 @{SUPPORT_USER}",
             {"inline_keyboard": [[{"text": t("contact_support", chat_id),
                                    "url": f"https://t.me/{SUPPORT_USER}"}]]})
        return
    if any(k in low for k in ("مساعدة", "help", "помощь")):
        send(chat_id,
             f"{t('help_title', chat_id)}\n\n"
             f"{t('help_shop', chat_id)}\n"
             f"{t('help_orders', chat_id)}\n"
             f"{t('help_topup', chat_id)}\n\n"
             f"{t('help_contact', chat_id)} @{SUPPORT_USER}",
             {"inline_keyboard": [[{"text": t("support_word", chat_id),
                                    "url": f"https://t.me/{SUPPORT_USER}"}]]})
        return
    if msg.get("photo"):
        handle_photo(chat_id, msg["message_id"], user_name); return
    pend = [o for o in ORDERS.values()
            if str(o["user_chat_id"]) == str(chat_id) and o["status"] == "awaiting_payment"]
    if pend:
        send(chat_id, t("need_screenshot", chat_id))
    else:
        send(chat_id, t("hello_choose", chat_id), main_keyboard(chat_id))

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
