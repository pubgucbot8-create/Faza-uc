import os
import asyncio
import logging
from urllib.parse import quote

import aiosqlite
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO)

# ================== SOZLAMALAR (.env / Railway Variables) ==================
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "8975803698").replace(" ", "").split(",") if x}
# Kanal: Railway Variables'da CHANNEL bo'lmasa ham shu kanal ishlaydi
CHANNEL = os.getenv("CHANNEL", "@fazauc_anime").strip()
DB_PATH = os.getenv("DB_PATH", "aniworld_v2.db").strip() or "aniworld_v2.db"


def _prepare_db_path(path: str) -> str:
    # papka yo'q bo'lsa yaratadi; yoza olmasa joriy papkadagi faylga o'tadi
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "a"):
            pass
        return path
    except Exception as e:
        logging.warning("DB_PATH=%s ishlamadi (%s). aniworld_v2.db ishlatiladi", path, e)
        return "aniworld_v2.db"


DB_PATH = _prepare_db_path(DB_PATH)
PORT = int(os.getenv("PORT", "8080"))
SITE_NAME = os.getenv("SITE_NAME", "AniWorld")

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
BOT_USERNAME = ""
ep_lock = asyncio.Lock()
poster_cache: dict[str, bytes] = {}

BTN_ADD = "➕ Anime qo'shish"
BTN_ADD_EP = "➕ Qism qo'shish"
BTN_DEL = "🗑 Anime o'chirish"
BTN_LIST = "📋 Animelar"
BTN_CANCEL = "❌ Bekor qilish"
BTN_DONE = "✅ Tugatish"


def is_admin(m) -> bool:
    return m.from_user and m.from_user.id in ADMINS


def admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_ADD_EP)],
            [KeyboardButton(text=BTN_DEL), KeyboardButton(text=BTN_LIST)],
        ],
        resize_keyboard=True,
    )


cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
done_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_DONE)]], resize_keyboard=True)


class AddAnime(StatesGroup):
    code = State()
    name = State()
    desc = State()
    poster = State()


class Eps(StatesGroup):
    ask_code = State()
    waiting = State()


class DelAnime(StatesGroup):
    code = State()


# ================== BAZA ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS anime ("
            "code TEXT PRIMARY KEY, name TEXT NOT NULL, poster TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS episodes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, "
            "num INTEGER NOT NULL, file_id TEXT NOT NULL, kind TEXT NOT NULL)"
        )
        # yangi ustunlar (eski baza buzilmaydi)
        try:
            await db.execute("ALTER TABLE anime ADD COLUMN descr TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE anime ADD COLUMN posted INTEGER DEFAULT 0")
            # eski animelar kanalga qayta chiqib ketmasin
            await db.execute("UPDATE anime SET posted = 1")
        except Exception:
            pass
        await db.commit()


async def get_anime(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, name, poster FROM anime WHERE code = ?", (code,))
        return await cur.fetchone()


async def get_desc(code: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(descr, '') FROM anime WHERE code = ?", (code,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def is_posted(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(posted, 0) FROM anime WHERE code = ?", (code,))
        row = await cur.fetchone()
        return bool(row and row[0])


async def mark_posted(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE anime SET posted = 1 WHERE code = ?", (code,))
        await db.commit()


async def ep_count(code: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(num), 0) FROM episodes WHERE code = ?", (code,))
        return (await cur.fetchone())[0]


async def ep_nums(code: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT num FROM episodes WHERE code = ? ORDER BY num", (code,))
        return [r[0] for r in await cur.fetchall()]


async def add_episode(code: str, file_id: str, kind: str, num: int | None = None) -> int:
    # lock: ko'p video birdan tashlansa ham raqamlar 1,2,3... bo'lib ketadi
    # num berilsa (izoh/caption orqali) shu raqamdagi qism almashtiriladi
    async with ep_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            if num is None:
                cur = await db.execute("SELECT COALESCE(MAX(num), 0) FROM episodes WHERE code = ?", (code,))
                num = (await cur.fetchone())[0] + 1
            else:
                await db.execute("DELETE FROM episodes WHERE code = ? AND num = ?", (code, num))
            await db.execute(
                "INSERT INTO episodes (code, num, file_id, kind) VALUES (?, ?, ?, ?)",
                (code, num, file_id, kind),
            )
            await db.commit()
            return num


async def get_episode(code: str, num: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT file_id, kind FROM episodes WHERE code = ? AND num = ?", (code, num))
        return await cur.fetchone()


async def all_anime():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT a.code, a.name, a.poster, COALESCE(MAX(e.num), 0) "
            "FROM anime a LEFT JOIN episodes e ON e.code = a.code "
            "GROUP BY a.code ORDER BY a.rowid DESC"
        )
        return await cur.fetchall()


async def search_anime(q: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT code, name FROM anime WHERE code = ? OR name LIKE ? LIMIT 10",
            (q, f"%{q}%"),
        )
        return await cur.fetchall()


async def delete_anime(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM episodes WHERE code = ?", (code,))
        await db.execute("DELETE FROM anime WHERE code = ?", (code,))
        await db.commit()
    poster_cache.pop(code, None)


# ================== OBUNA TEKSHIRUVI ==================
async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL or user_id in ADMINS:
        return True
    try:
        mem = await bot.get_chat_member(CHANNEL, user_id)
        return mem.status in ("member", "administrator", "creator")
    except Exception as e:
        # bot kanalda admin bo'lmasa shu xato chiqadi
        logging.error("Obuna tekshiruvi ishlamadi (%s): %r — bot %s da admin ekanini tekshiring", user_id, e, CHANNEL)
        return True


async def ask_subscribe(chat_id: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga a'zo bo'lish", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="chk")],
    ])
    await bot.send_message(chat_id, "Botdan foydalanish uchun kanalga a'zo bo'ling 👇", reply_markup=kb)


# ================== KANALGA POST ==================
async def tg_api(method: str, payload: dict) -> dict:
    # to'g'ridan-to'g'ri Telegram API (tugma ranglari aiogram versiyasiga bog'liq bo'lmasin)
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    async with ClientSession() as s:
        async with s.post(url, json=payload) as r:
            return await r.json()


async def post_to_channel(code: str) -> str:
    """Kanalga post qo'yadi. Muvaffaqiyatli bo'lsa '' qaytaradi, bo'lmasa xato matni."""
    if not CHANNEL:
        return "CHANNEL sozlanmagan"
    row = await get_anime(code)
    if not row:
        return "anime topilmadi"
    _, name, poster = row
    desc = (await get_desc(code)).strip()
    n = await ep_count(code)

    caption = f"🎬 {name}\n\n"
    if desc:
        caption += desc[:650] + "\n\n"
    caption += f"📺 Qismlar: {n}"

    start_link = f"https://t.me/{BOT_USERNAME}?start={code}"
    share_link = (
        "https://t.me/share/url?url=" + quote(start_link, safe="")
        + "&text=" + quote(f"🎬 {name}", safe="")
    )

    def markup(styled: bool) -> dict:
        b1 = {"text": "▶️ Ko'rish", "url": start_link}
        b2 = {"text": "📤 Do'stlarga ulashish", "url": share_link}
        if styled:
            b1["style"] = "success"   # yashil
            b2["style"] = "primary"   # ko'k
        return {"inline_keyboard": [[b1], [b2]]}

    err = ""
    for styled in (True, False):
        payload = {"chat_id": CHANNEL, "reply_markup": markup(styled)}
        if poster:
            method = "sendPhoto"
            payload.update(photo=poster, caption=caption)
        else:
            method = "sendMessage"
            payload["text"] = caption
        try:
            res = await tg_api(method, payload)
        except Exception as e:
            err = repr(e)
            break
        if res.get("ok"):
            return ""
        err = res.get("description", "noma'lum xato")
        logging.error("Kanalga post xatosi (styled=%s): %s", styled, err)
    return err


# ================== ANIME KARTASI ==================
async def show_anime(chat_id: int, code: str):
    row = await get_anime(code)
    if not row:
        await bot.send_message(chat_id, "❌ Anime topilmadi.")
        return
    _, name, poster = row
    desc = (await get_desc(code)).strip()
    nums = await ep_nums(code)
    n = len(nums)
    caption = f"🎬 {name}\n"
    if desc:
        caption += f"\n{desc[:600]}\n"
    caption += f"\n📺 Qismlar: {n}"
    kb = None
    if n:
        b = InlineKeyboardBuilder()
        for i in nums:
            b.button(text=str(i), callback_data=f"ep:{code}:{i}")
        b.adjust(5)
        kb = b.as_markup()
        caption += "\n\nQismni tanlang 👇"
    else:
        caption += "\n\nQismlar hali yuklanmagan."
    if poster:
        await bot.send_photo(chat_id, poster, caption=caption, reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, reply_markup=kb)


async def send_episode(chat_id: int, code: str, num: int):
    row = await get_anime(code)
    ep = await get_episode(code, num)
    if not row or not ep:
        await bot.send_message(chat_id, "❌ Qism topilmadi.")
        return
    cap = f"🎬 {row[1]} — {num}-qism"
    file_id, kind = ep
    if kind == "video":
        await bot.send_video(chat_id, file_id, caption=cap)
    else:
        await bot.send_document(chat_id, file_id, caption=cap)


# ================== ADMIN ==================
@dp.message(F.func(is_admin), F.text == BTN_CANCEL)
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=admin_kb())


@dp.message(F.func(is_admin), F.text == BTN_ADD)
async def add_start(m: Message, state: FSMContext):
    await state.set_state(AddAnime.code)
    await m.answer("🔑 Anime kodini yuboring (faqat harf va raqam, masalan: 45):", reply_markup=cancel_kb)


@dp.message(AddAnime.code, F.text)
async def add_code(m: Message, state: FSMContext):
    code = m.text.strip()
    if not code.isalnum():
        await m.answer("Kod faqat harf va raqamdan iborat bo'lsin. Qayta yuboring:")
        return
    if await get_anime(code):
        await m.answer("Bu kod band. Boshqa kod yuboring:")
        return
    await state.update_data(code=code)
    await state.set_state(AddAnime.name)
    await m.answer("📝 Anime nomini yuboring:")


@dp.message(AddAnime.name, F.text)
async def add_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddAnime.desc)
    await m.answer("📄 Anime tavsifini yuboring (kanal posti va saytda chiqadi).\nTavsif kerak bo'lmasa «-» yuboring:")


@dp.message(AddAnime.desc, F.text)
async def add_desc(m: Message, state: FSMContext):
    t = m.text.strip()
    await state.update_data(desc="" if t == "-" else t[:1500])
    await state.set_state(AddAnime.poster)
    await m.answer("🖼 Anime rasmini (poster) yuboring:")


@dp.message(AddAnime.poster, F.photo)
async def add_poster(m: Message, state: FSMContext):
    data = await state.get_data()
    code, name, desc = data["code"], data["name"], data.get("desc", "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO anime (code, name, poster, descr, posted) VALUES (?, ?, ?, ?, 0)",
            (code, name, m.photo[-1].file_id, desc),
        )
        await db.commit()
    await state.clear()
    await state.set_state(Eps.waiting)
    await state.update_data(code=code)
    await m.answer(
        f"✅ «{name}» qo'shildi.\n\nEndi qismlarni ketma-ket tashlang (1-qismdan boshlab). "
        f"Raqamlar avtomatik 1, 2, 3... bo'lib qo'yiladi.\n"
        f"Tugagach «{BTN_DONE}» ni bosing — shunda kanalga post avtomatik chiqadi.",
        reply_markup=done_kb,
    )


@dp.message(AddAnime.poster)
async def add_poster_wrong(m: Message):
    await m.answer("Iltimos, rasm yuboring (surat sifatida).")


@dp.message(F.func(is_admin), F.text == BTN_ADD_EP)
async def addep_start(m: Message, state: FSMContext):
    await state.set_state(Eps.ask_code)
    await m.answer("🔑 Qaysi animega qism qo'shamiz? Kodini yuboring:", reply_markup=cancel_kb)


@dp.message(Eps.ask_code, F.text)
async def addep_code(m: Message, state: FSMContext):
    code = m.text.strip()
    row = await get_anime(code)
    if not row:
        await m.answer("Bunday kod topilmadi. Qayta yuboring:")
        return
    n = await ep_count(code)
    await state.set_state(Eps.waiting)
    await state.update_data(code=code)
    await m.answer(
        f"«{row[1]}» — hozir {n} qism bor. Yangi qismlarni tashlang, {n + 1} dan boshlab raqam qo'yiladi.",
        reply_markup=done_kb,
    )


@dp.message(Eps.waiting, F.text == BTN_DONE)
async def eps_done(m: Message, state: FSMContext):
    data = await state.get_data()
    code = data.get("code")
    row = await get_anime(code)
    n = await ep_count(code)
    await state.clear()

    note = ""
    # yangi anime bo'lsa va qismlar bor bo'lsa — kanalga bir marta post qo'yiladi
    if n and CHANNEL and not await is_posted(code):
        err = await post_to_channel(code)
        if err:
            note = f"\n\n⚠️ Kanalga post chiqmadi: {err}\nBot {CHANNEL} kanalida admin ekanini tekshiring."
        else:
            await mark_posted(code)
            note = f"\n\n📢 {CHANNEL} kanaliga post qo'yildi."
    await m.answer(f"✅ Tayyor! «{row[1]}»: {n} qism.{note}", reply_markup=admin_kb())


@dp.message(Eps.waiting, F.video | F.document)
async def eps_receive(m: Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    cap = (m.caption or "").strip()
    forced = int(cap) if cap.isdigit() and 0 < int(cap) < 10000 else None
    if m.video:
        num = await add_episode(code, m.video.file_id, "video", forced)
    else:
        num = await add_episode(code, m.document.file_id, "document", forced)
    if forced:
        await m.answer(f"♻️ {num}-qism joylandi (izohdagi raqam bo'yicha)")
    else:
        await m.answer(f"✅ {num}-qism qo'shildi")


@dp.message(F.func(is_admin), F.text == BTN_DEL)
async def del_start(m: Message, state: FSMContext):
    await state.set_state(DelAnime.code)
    await m.answer("🗑 O'chiriladigan anime kodini yuboring:", reply_markup=cancel_kb)


@dp.message(DelAnime.code, F.text)
async def del_code(m: Message, state: FSMContext):
    code = m.text.strip()
    row = await get_anime(code)
    if not row:
        await m.answer("Bunday kod topilmadi. Qayta yuboring:")
        return
    await delete_anime(code)
    await state.clear()
    await m.answer(f"🗑 «{row[1]}» o'chirildi.", reply_markup=admin_kb())


@dp.message(F.func(is_admin), F.text == BTN_LIST)
async def list_anime(m: Message):
    rows = await all_anime()
    if not rows:
        await m.answer("Hali anime yo'q.")
        return
    await m.answer("\n".join(f"{c} — {n} ({k} qism)" for c, n, _, k in rows)[:4000])


# ================== FOYDALANUVCHI ==================
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if is_admin(m):
        await m.answer("👑 Admin panel", reply_markup=admin_kb())
    if not await is_subscribed(m.from_user.id):
        await ask_subscribe(m.chat.id)
        return
    payload = (command.args or "").strip()
    if payload:
        if "-" in payload:
            code, _, num = payload.partition("-")
            if num.isdigit():
                await send_episode(m.chat.id, code, int(num))
                return
        await show_anime(m.chat.id, payload)
        return
    if not is_admin(m):
        await m.answer(
            f"🎌 {SITE_NAME} ga xush kelibsiz!\n\nAnime nomi yoki kodini yuboring.",
            reply_markup=ReplyKeyboardRemove(),
        )


@dp.callback_query(F.data == "chk")
async def chk(c: CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.answer("Rahmat! Endi anime nomi yoki kodini yuboring.")
    else:
        await c.answer("Hali a'zo bo'lmagansiz ❌", show_alert=True)


@dp.callback_query(F.data.startswith("ep:"))
async def cb_ep(c: CallbackQuery):
    if not await is_subscribed(c.from_user.id):
        await ask_subscribe(c.message.chat.id)
        await c.answer()
        return
    _, code, num = c.data.split(":")
    await c.answer()
    await send_episode(c.message.chat.id, code, int(num))


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(c: CallbackQuery):
    await c.answer()
    await show_anime(c.message.chat.id, c.data.split(":", 1)[1])


@dp.message(StateFilter(None), F.text)
async def search(m: Message):
    if not await is_subscribed(m.from_user.id):
        await ask_subscribe(m.chat.id)
        return
    rows = await search_anime(m.text.strip())
    if not rows:
        await m.answer("❌ Hech narsa topilmadi.")
        return
    if len(rows) == 1:
        await show_anime(m.chat.id, rows[0][0])
        return
    b = InlineKeyboardBuilder()
    for code, name in rows:
        b.button(text=name, callback_data=f"open:{code}")
    b.adjust(1)
    await m.answer("Topilganlar:", reply_markup=b.as_markup())


# ================== SAYT ==================
INDEX_HTML = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__SITE__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#10111a;color:#fff;font-family:Roboto,system-ui,sans-serif;padding-bottom:30px}
header{padding:14px 16px 12px;border-bottom:1px solid #22243a;position:sticky;top:0;background:#10111a;z-index:5}
h1{color:#ff4d82;font-size:26px;margin-bottom:10px}
input{width:100%;padding:14px 16px;border-radius:14px;border:1px solid #2a2d4a;background:#181a2b;color:#fff;font-size:16px;outline:none}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:16px}
.card{background:#1b1d2e;border-radius:18px;overflow:hidden;cursor:pointer}
.poster{aspect-ratio:2/3;background:#22253a center/cover no-repeat}
.info{padding:10px 12px}
.name{font-weight:700;font-size:15px;line-height:1.25;word-break:break-word}
.cnt{color:#9a9db8;font-size:13px;margin-top:3px}
.dsc{color:#9a9db8;font-size:12px;line-height:1.35;margin-top:6px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.empty{padding:40px 16px;text-align:center;color:#9a9db8}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:10;overflow:auto}
.box{background:#1b1d2e;margin:30px 14px;border-radius:20px;overflow:hidden}
.box .poster{aspect-ratio:16/11}
.box .body{padding:16px}
.box h2{font-size:20px;margin-bottom:4px}
.box .desc{color:#cfd2ee;font-size:14px;line-height:1.5;margin-top:10px;white-space:pre-line;word-break:break-word}
.eps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:14px}
.eps a{background:#2a2d4a;color:#fff;text-align:center;padding:11px 0;border-radius:10px;text-decoration:none;font-weight:700}
.close{width:100%;margin-top:16px;padding:12px;border:0;border-radius:12px;background:#ff4d82;color:#fff;font-size:16px;font-weight:700}
</style></head><body>
<header><h1>🍥 __SITE__</h1><input id="q" placeholder="Anime qidirish..." autocomplete="off"></header>
<div class="grid" id="grid"></div>
<div id="modal"><div class="box" id="box"></div></div>
<script>
const BOT="__BOT__";
const tg=window.Telegram&&window.Telegram.WebApp;
if(tg){tg.ready();tg.expand();}
let data=[];
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function link(code,n){
  const u="https://t.me/"+BOT+"?start="+code+(n?"-"+n:"");
  if(tg&&tg.openTelegramLink){tg.openTelegramLink(u);}else{window.open(u,"_blank");}
}
function render(list){
  const g=document.getElementById("grid");
  if(!list.length){g.innerHTML='<div class="empty" style="grid-column:1/-1">Anime topilmadi</div>';return;}
  g.innerHTML=list.map(a=>`<div class="card" onclick="openAnime('${a.code}')">
    <div class="poster" style="${a.poster?`background-image:url('${a.poster}')`:''}"></div>
    <div class="info"><div class="name">${esc(a.name)}</div><div class="cnt">${a.count} qism</div>
    ${a.desc?`<div class="dsc">${esc(a.desc)}</div>`:''}</div></div>`).join("");
}
function openAnime(code){
  const a=data.find(x=>x.code===code);if(!a)return;
  let eps="";for(const i of (a.eps||[])){eps+=`<a href="#" onclick="link('${a.code}',${i});return false">${i}</a>`;}
  document.getElementById("box").innerHTML=`
    <div class="poster" style="${a.poster?`background-image:url('${a.poster}')`:''}"></div>
    <div class="body"><h2>${esc(a.name)}</h2><div class="cnt">${a.count} qism</div>
    ${a.desc?`<div class="desc">${esc(a.desc)}</div>`:''}
    <div class="eps">${eps||'<span class="cnt">Qismlar hali yo\\'q</span>'}</div>
    <button class="close" onclick="link('${a.code}')">Botda ochish</button>
    <button class="close" style="background:#2a2d4a" onclick="closeM()">Yopish</button></div>`;
  document.getElementById("modal").style.display="block";
}
function closeM(){document.getElementById("modal").style.display="none";}
document.getElementById("q").addEventListener("input",e=>{
  const s=e.target.value.toLowerCase().trim();
  render(data.filter(a=>a.name.toLowerCase().includes(s)||a.code.toLowerCase()===s));
});
fetch("/api/animes").then(r=>r.json()).then(d=>{data=d;render(d);});
</script></body></html>"""


async def page_index(request):
    html = INDEX_HTML.replace("__BOT__", BOT_USERNAME).replace("__SITE__", SITE_NAME)
    return web.Response(text=html, content_type="text/html")


async def api_animes(request):
    rows = await all_anime()
    data = []
    for c, n, p, k in rows:
        nums = await ep_nums(c)
        data.append({
            "code": c, "name": n, "count": len(nums), "eps": nums,
            "desc": await get_desc(c),
            "poster": f"/poster/{c}" if p else None,
        })
    return web.json_response(data)


async def api_poster(request):
    code = request.match_info["code"]
    if code not in poster_cache:
        row = await get_anime(code)
        if not row or not row[2]:
            logging.warning("Poster: %s kodli animeda rasm yo'q", code)
            raise web.HTTPNotFound()
        try:
            f = await bot.get_file(row[2])
            url = f"https://api.telegram.org/file/bot{TOKEN}/{f.file_path}"
            async with ClientSession() as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        logging.error("Poster yuklanmadi: HTTP %s (kod %s)", r.status, code)
                        raise web.HTTPNotFound()
                    poster_cache[code] = await r.read()
        except web.HTTPNotFound:
            raise
        except Exception as e:
            logging.error("Poster xatosi (kod %s): %r", code, e)
            raise web.HTTPNotFound()
    return web.Response(
        body=poster_cache[code], content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def start_web():
    app = web.Application()
    app.router.add_get("/", page_index)
    app.router.add_get("/api/animes", api_animes)
    app.router.add_get("/poster/{code}", api_poster)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Sayt %s portda ishga tushdi", PORT)


# ================== ISHGA TUSHIRISH ==================
async def main():
    global BOT_USERNAME
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await start_web()
    await bot.delete_webhook(drop_pending_updates=True)
    # handle_as_tasks=False: xabarlar kelish tartibida birin-ketin ishlanadi (qismlar aralashmaydi)
    await dp.start_polling(bot, handle_as_tasks=False)


if __name__ == "__main__":
    asyncio.run(main())
