import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fazauc_bot")

# ---------------------------------------------------------------- sozlamalar
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "8599508712").replace(" ", "").split(",") if x
]
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@fazaboipmm")
CARD_NUMBER = os.getenv("CARD_NUMBER", "").strip()
CARD_OWNER = os.getenv("CARD_OWNER", "").strip()
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "orders.db")

# (UC miqdori, narx so'mda)
PACKAGES = [
    (60, 13000),
    (120, 26000),
    (180, 39000),
    (335, 55000),
    (385, 60000),
    (660, 110000),
    (720, 115000),
    (985, 155000),
    (1045, 165000),
    (1320, 205000),
    (1950, 260000),
    (2155, 305000),
    (2460, 350000),
    (4000, 495000),
    (4510, 590000),
    (5650, 735000),
    (8400, 895000),
    (9900, 1125000),
]
PRICES = dict(PACKAGES)


def money(n: int) -> str:
    return f"{n:,}".replace(",", ".") + " so'm"


def prices_text() -> str:
    lines = [
        "⚡️ <b>YANGI NARXLARIMIZ</b> ⚡️",
        "",
        "❗️ Eng arzon UC bizda!",
        "❗️ ID orqali UC tushiramiz!",
        "",
        "🇺🇿 <b>O'zbekistondan to'lov qiladiganlar uchun</b>",
        "",
    ]
    for uc, price in PACKAGES:
        lines.append(f"🔥 {uc} UC — {money(price)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- baza
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                uc INTEGER NOT NULL,
                price INTEGER NOT NULL,
                pubg_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )


def create_order(user, uc: int, pubg_id: str) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO orders (user_id, username, full_name, uc, price, pubg_id, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                user.id,
                user.username,
                user.full_name,
                uc,
                PRICES[uc],
                pubg_id,
                "awaiting_payment",
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        return cur.lastrowid


def get_order(order_id: int):
    with db() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def set_status(order_id: int, status: str):
    with db() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


def latest_awaiting(user_id: int):
    with db() as c:
        return c.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='awaiting_payment'"
            " ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()


# ---------------------------------------------------------------- klaviaturalar
BTN_BUY = "🛒 UC sotib olish"
BTN_PRICES = "💰 Narxlar"
BTN_CONTACT = "📞 Admin bilan aloqa"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_BUY)],
        [KeyboardButton(text=BTN_PRICES), KeyboardButton(text=BTN_CONTACT)],
    ],
    resize_keyboard=True,
)


def packages_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for uc, price in PACKAGES:
        row.append(
            InlineKeyboardButton(text=f"{uc} UC • {price // 1000}k", callback_data=f"pkg:{uc}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel:{order_id}")]]
    )


def admin_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{order_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"no:{order_id}"),
            ]
        ]
    )


class Buy(StatesGroup):
    waiting_id = State()


dp = Dispatcher(storage=MemoryStorage())


# ---------------------------------------------------------------- foydalanuvchi
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        f"Assalomu alaykum, <b>{html.escape(m.from_user.first_name or 'do‘st')}</b>! 👋\n\n"
        "PUBG Mobile UC ni <b>eng arzon narxlarda</b>, o'yin ID orqali tushirib beramiz.\n"
        "Boshlash uchun «🛒 UC sotib olish» tugmasini bosing.",
        reply_markup=main_kb,
    )


@dp.message(F.text == BTN_PRICES)
async def show_prices(m: Message):
    await m.answer(prices_text())


@dp.message(F.text == BTN_CONTACT)
async def contact(m: Message):
    await m.answer(f"Savol yoki muammo bo'lsa, adminga yozing: {ADMIN_USERNAME}")


@dp.message(F.text == BTN_BUY)
async def buy(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Kerakli UC paketini tanlang 👇", reply_markup=packages_kb())


@dp.callback_query(F.data.startswith("pkg:"))
async def choose_package(c: CallbackQuery, state: FSMContext):
    uc = int(c.data.split(":")[1])
    if uc not in PRICES:
        await c.answer("Paket topilmadi", show_alert=True)
        return
    await state.set_state(Buy.waiting_id)
    await state.update_data(uc=uc)
    await c.message.answer(
        f"✅ Tanlandi: <b>{uc} UC</b> — {money(PRICES[uc])}\n\n"
        "Endi <b>PUBG Mobile ID</b> raqamingizni yuboring (faqat raqamlar).",
    )
    await c.answer()


@dp.message(Buy.waiting_id)
async def got_id(m: Message, state: FSMContext):
    pubg_id = (m.text or "").strip()
    if not pubg_id.isdigit() or not (5 <= len(pubg_id) <= 15):
        await m.answer("❗️ ID faqat raqamlardan iborat bo'lishi kerak. Qaytadan yuboring.")
        return
    data = await state.get_data()
    uc = data.get("uc")
    if uc not in PRICES:
        await state.clear()
        await m.answer("Xatolik. Iltimos, qaytadan boshlang.", reply_markup=main_kb)
        return
    await state.clear()
    order_id = create_order(m.from_user, uc, pubg_id)

    if CARD_NUMBER:
        pay = f"💳 Karta: <code>{html.escape(CARD_NUMBER)}</code>\n"
        if CARD_OWNER:
            pay += f"👤 Egasi: {html.escape(CARD_OWNER)}\n"
    else:
        pay = f"💳 Karta raqami uchun adminga yozing: {ADMIN_USERNAME}\n"

    await m.answer(
        f"🧾 <b>Buyurtma #{order_id}</b>\n"
        f"UC: <b>{uc}</b>\n"
        f"PUBG ID: <code>{pubg_id}</code>\n"
        f"To'lov summasi: <b>{money(PRICES[uc])}</b>\n\n"
        f"{pay}\n"
        "To'lovni qilgach, <b>chek rasmini (skrinshot)</b> shu yerga yuboring. "
        "Admin tekshirib, UC ni tushiradi.",
        reply_markup=cancel_kb(order_id),
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_order(c: CallbackQuery):
    order_id = int(c.data.split(":")[1])
    o = get_order(order_id)
    if not o or o["user_id"] != c.from_user.id or o["status"] != "awaiting_payment":
        await c.answer("Bu buyurtmani bekor qilib bo'lmaydi", show_alert=True)
        return
    set_status(order_id, "cancelled")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"Buyurtma #{order_id} bekor qilindi.", reply_markup=main_kb)
    await c.answer()


@dp.message(F.photo | F.document)
async def got_receipt(m: Message, bot: Bot):
    o = latest_awaiting(m.from_user.id)
    if not o:
        await m.answer(
            "Faol buyurtma topilmadi. Avval «🛒 UC sotib olish» orqali buyurtma bering.",
            reply_markup=main_kb,
        )
        return
    set_status(o["id"], "checking")
    uname = f"@{m.from_user.username}" if m.from_user.username else "username yo'q"
    caption = (
        f"🆕 <b>Yangi buyurtma #{o['id']}</b>\n"
        f"👤 {html.escape(m.from_user.full_name)} ({html.escape(uname)})\n"
        f"🆔 Telegram ID: <code>{m.from_user.id}</code>\n"
        f"🎮 PUBG ID: <code>{o['pubg_id']}</code>\n"
        f"💎 UC: <b>{o['uc']}</b>\n"
        f"💰 Summa: <b>{money(o['price'])}</b>\n"
        f"🕒 {o['created_at']}"
    )
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await m.copy_to(admin_id, caption=caption, reply_markup=admin_kb(o["id"]))
            sent += 1
        except Exception as e:
            log.warning("Adminga (%s) yuborib bo'lmadi: %s", admin_id, e)
    if sent == 0:
        set_status(o["id"], "awaiting_payment")
        await m.answer(f"⚠️ Chekni adminga yetkazib bo'lmadi. Iltimos {ADMIN_USERNAME} ga yozing.")
        return
    await m.answer(
        f"✅ Chek qabul qilindi. Buyurtma #{o['id']} tekshirilmoqda.\n"
        "Odatda bir necha daqiqada UC tushiriladi. Rahmat! 🙏"
    )


# ---------------------------------------------------------------- admin
@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def admin_decision(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Bu tugma faqat admin uchun", show_alert=True)
        return
    action, oid = c.data.split(":")
    order_id = int(oid)
    o = get_order(order_id)
    if not o or o["status"] != "checking":
        await c.answer("Bu buyurtma allaqachon ko'rib chiqilgan", show_alert=True)
        return
    if action == "ok":
        set_status(order_id, "done")
        mark, user_text = "✅ TASDIQLANDI", (
            f"🎉 Buyurtma #{order_id} tasdiqlandi!\n"
            f"<b>{o['uc']} UC</b> ID <code>{o['pubg_id']}</code> ga tushirildi. Xarid uchun rahmat!"
        )
    else:
        set_status(order_id, "rejected")
        mark, user_text = "❌ RAD ETILDI", (
            f"😔 Buyurtma #{order_id} rad etildi (to'lov tasdiqlanmadi).\n"
            f"Muammo bo'lsa, adminga yozing: {ADMIN_USERNAME}"
        )
    try:
        await c.message.edit_caption(
            caption=(c.message.caption or "") + f"\n\n<b>{mark}</b>", reply_markup=None
        )
    except Exception as e:
        log.warning("Xabarni tahrirlab bo'lmadi: %s", e)
    try:
        await bot.send_message(o["user_id"], user_text)
    except Exception as e:
        log.warning("Mijozga yuborib bo'lmadi: %s", e)
    await c.answer("Bajarildi")


@dp.message(Command("orders"))
async def pending_orders(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE status='checking' ORDER BY id DESC LIMIT 20"
        ).fetchall()
    if not rows:
        await m.answer("Tekshirilishi kutilayotgan buyurtma yo'q.")
        return
    text = "⏳ <b>Kutilayotgan buyurtmalar:</b>\n\n" + "\n".join(
        f"#{r['id']} • {r['uc']} UC • ID {r['pubg_id']} • {money(r['price'])}" for r in rows
    )
    await m.answer(text)


@dp.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    with db() as c:
        done = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(price),0) s, COALESCE(SUM(uc),0) u FROM orders WHERE status='done'"
        ).fetchone()
        wait = c.execute("SELECT COUNT(*) n FROM orders WHERE status='checking'").fetchone()
    await m.answer(
        "📊 <b>Statistika</b>\n"
        f"Bajarilgan: {done['n']} ta\n"
        f"Sotilgan UC: {done['u']}\n"
        f"Tushum: {money(done['s'])}\n"
        f"Kutilmoqda: {wait['n']} ta"
    )


@dp.message()
async def fallback(m: Message):
    await m.answer("Quyidagi tugmalardan foydalaning 👇", reply_markup=main_kb)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. Railway Variables ga BOT_TOKEN qo'shing.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        log.info("TOKEN TO'G'RI: @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("fazauc_bot ishga tushdi")
        await dp.start_polling(bot)
    except Exception as e:
        log.error("XATO TURI: %s", type(e).__name__)
        log.error("XATO MATNI: %s", e)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
