import asyncio
import html
import logging
import os
import random

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- SOZLAMALAR
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "8975803698").replace(" ", "").split(",") if x
}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "sheyxoga").lstrip("@")
CHANNEL = os.getenv("CHANNEL", "").strip()  # majburiy obuna kanali, masalan @mening_kanalim
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # sayt manzili, masalan https://xxx.up.railway.app
SITE_NAME = os.getenv("SITE_NAME", "AniWorld")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "anime.db")
POSTER_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "posters")
os.makedirs(POSTER_DIR, exist_ok=True)

PAGE_EPS = 30
PAGE_LIST = 10
BOT_USERNAME = ""

B_SEARCH = "🔍 Qidirish"
B_RANDOM = "🎲 Tasodifiy"
B_LIST = "📚 Ro'yxat"
B_SITE = "🌐 Sayt"
B_ADMIN = "👤 Admin bilan aloqa"


def esc(s):
    return html.escape(str(s or ""))


# ---------------------------------------------------------------- BAZA
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT
            );
            CREATE TABLE IF NOT EXISTS anime(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                genre TEXT,
                poster TEXT
            );
            CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                ftype TEXT DEFAULT 'video'
            );
            """
        )
        await db.commit()


async def db_exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def db_all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


async def db_one(sql, args=()):
    rows = await db_all(sql, args)
    return rows[0] if rows else None


# ---------------------------------------------------------------- OBUNA
async def is_subscribed(bot: Bot, uid: int) -> bool:
    if not CHANNEL or uid in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("Obunani tekshirib bo'lmadi (bot kanalda admin emasmi?): %s", e)
        return True


def sub_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Kanalga obuna bo'lish", url=f"https://t.me/{CHANNEL.lstrip('@')}")
    kb.button(text="✅ Tekshirish", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


async def blocked(event, bot: Bot) -> bool:
    """Obuna bo'lmagan bo'lsa True qaytaradi va xabar yuboradi."""
    if await is_subscribed(bot, event.from_user.id):
        return False
    msg = event.message if isinstance(event, CallbackQuery) else event
    await msg.answer(
        "Botdan foydalanish uchun avval kanalimizga obuna bo'ling 👇", reply_markup=sub_kb()
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
    return True


# ---------------------------------------------------------------- KLAVIATURA
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=B_SEARCH), KeyboardButton(text=B_RANDOM)],
            [KeyboardButton(text=B_LIST), KeyboardButton(text=B_SITE)],
            [KeyboardButton(text=B_ADMIN)],
        ],
        resize_keyboard=True,
    )


# ---------------------------------------------------------------- ANIME KO'RINISHI
async def anime_view(aid: int, page: int = 0):
    a = await db_one("SELECT * FROM anime WHERE id=?", (aid,))
    if not a:
        return None
    eps = await db_all(
        "SELECT id, number FROM episodes WHERE anime_id=? ORDER BY number", (aid,)
    )
    cap = f"🎬 <b>{esc(a['title'])}</b>\n"
    if a["genre"]:
        cap += f"🎭 {esc(a['genre'])}\n"
    cap += f"📺 Qismlar: {len(eps)}\n\n{esc((a['description'] or '')[:600])}"
    kb = InlineKeyboardBuilder()
    for e in eps[page * PAGE_EPS : (page + 1) * PAGE_EPS]:
        kb.button(text=str(e["number"]), callback_data=f"ep:{e['id']}")
    kb.adjust(5)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pg:{aid}:{page - 1}"))
    if (page + 1) * PAGE_EPS < len(eps):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pg:{aid}:{page + 1}"))
    if nav:
        kb.row(*nav)
    return a, cap, kb.as_markup()


async def send_anime(target: Message, aid: int):
    res = await anime_view(aid)
    if not res:
        await target.answer("Anime topilmadi 😕")
        return
    a, cap, kb = res
    if a["poster"]:
        await target.answer_photo(a["poster"], caption=cap, reply_markup=kb)
    else:
        await target.answer(cap, reply_markup=kb)


async def send_episode(target: Message, eid: int):
    e = await db_one(
        "SELECT e.*, a.title FROM episodes e JOIN anime a ON a.id=e.anime_id WHERE e.id=?",
        (eid,),
    )
    if not e:
        await target.answer("Qism topilmadi 😕")
        return
    nxt = await db_one(
        "SELECT id FROM episodes WHERE anime_id=? AND number>? ORDER BY number LIMIT 1",
        (e["anime_id"], e["number"]),
    )
    kb = InlineKeyboardBuilder()
    if nxt:
        kb.button(text="➡️ Keyingi qism", callback_data=f"ep:{nxt['id']}")
    kb.button(text="📋 Barcha qismlar", callback_data=f"all:{e['anime_id']}")
    kb.adjust(1)
    cap = f"🎬 <b>{esc(e['title'])}</b> — {e['number']}-qism\n\n{SITE_NAME} 🍿"
    if e["ftype"] == "document":
        await target.answer_document(e["file_id"], caption=cap, reply_markup=kb.as_markup())
    else:
        await target.answer_video(e["file_id"], caption=cap, reply_markup=kb.as_markup())


def results_kb(rows):
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=r["title"][:60], callback_data=f"a:{r['id']}")
    kb.adjust(1)
    return kb


# ---------------------------------------------------------------- FOYDALANUVCHI
user_router = Router()


@user_router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot):
    await db_exec(
        "INSERT OR IGNORE INTO users(id, name) VALUES(?, ?)", (m.from_user.id, m.from_user.full_name)
    )
    if await blocked(m, bot):
        return
    arg = command.args or ""
    if arg.startswith("a_") and arg[2:].isdigit():
        return await send_anime(m, int(arg[2:]))
    if arg.startswith("e_") and arg[2:].isdigit():
        return await send_episode(m, int(arg[2:]))
    await m.answer(
        f"🍥 <b>{esc(SITE_NAME)}</b> anime botiga xush kelibsiz!\n\n"
        "Anime nomini yozing yoki pastdagi tugmalardan foydalaning.",
        reply_markup=main_menu(),
    )


@user_router.callback_query(F.data == "check_sub")
async def cb_check(cb: CallbackQuery, bot: Bot):
    if await blocked(cb, bot):
        return
    await cb.message.answer("✅ Rahmat! Endi anime nomini yozing.", reply_markup=main_menu())
    await cb.answer()


@user_router.message(F.text == B_SEARCH)
async def btn_search(m: Message, bot: Bot):
    if await blocked(m, bot):
        return
    await m.answer("Anime nomini yozing 👇")


@user_router.message(F.text == B_RANDOM)
async def btn_random(m: Message, bot: Bot):
    if await blocked(m, bot):
        return
    rows = await db_all("SELECT id FROM anime")
    if not rows:
        return await m.answer("Hozircha anime qo'shilmagan.")
    await send_anime(m, random.choice(rows)["id"])


async def list_page(page: int):
    total = (await db_one("SELECT COUNT(*) AS c FROM anime"))["c"]
    rows = await db_all(
        "SELECT id, title FROM anime ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_LIST, page * PAGE_LIST),
    )
    kb = results_kb(rows)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"list:{page - 1}"))
    if (page + 1) * PAGE_LIST < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"list:{page + 1}"))
    if nav:
        kb.row(*nav)
    return total, kb.as_markup()


@user_router.message(F.text == B_LIST)
async def btn_list(m: Message, bot: Bot):
    if await blocked(m, bot):
        return
    total, kb = await list_page(0)
    if not total:
        return await m.answer("Hozircha anime qo'shilmagan.")
    await m.answer(f"📚 Barcha animelar ({total} ta):", reply_markup=kb)


@user_router.callback_query(F.data.startswith("list:"))
async def cb_list(cb: CallbackQuery, bot: Bot):
    if await blocked(cb, bot):
        return
    total, kb = await list_page(int(cb.data.split(":")[1]))
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()


@user_router.message(F.text == B_SITE)
async def btn_site(m: Message, bot: Bot):
    if await blocked(m, bot):
        return
    if not BASE_URL:
        return await m.answer("Sayt hali ulanmagan.")
    if BASE_URL.startswith("https://"):
        btn = InlineKeyboardButton(text=f"🌐 {SITE_NAME} saytini ochish", web_app=WebAppInfo(url=BASE_URL))
    else:
        btn = InlineKeyboardButton(text=f"🌐 {SITE_NAME} saytini ochish", url=BASE_URL)
    await m.answer("Saytimizga o'ting 👇", reply_markup=InlineKeyboardBuilder().row(btn).as_markup())


@user_router.message(F.text == B_ADMIN)
async def btn_admin(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Adminga yozish", url=f"https://t.me/{ADMIN_USERNAME}")
    await m.answer(f"Savol yoki taklif bo'lsa: @{ADMIN_USERNAME}", reply_markup=kb.as_markup())


@user_router.callback_query(F.data.startswith("a:"))
async def cb_anime(cb: CallbackQuery, bot: Bot):
    if await blocked(cb, bot):
        return
    await send_anime(cb.message, int(cb.data.split(":")[1]))
    await cb.answer()


@user_router.callback_query(F.data.startswith("all:"))
async def cb_all(cb: CallbackQuery, bot: Bot):
    if await blocked(cb, bot):
        return
    await send_anime(cb.message, int(cb.data.split(":")[1]))
    await cb.answer()


@user_router.callback_query(F.data.startswith("pg:"))
async def cb_page(cb: CallbackQuery):
    _, aid, page = cb.data.split(":")
    res = await anime_view(int(aid), int(page))
    if res:
        await cb.message.edit_reply_markup(reply_markup=res[2])
    await cb.answer()


@user_router.callback_query(F.data.startswith("ep:"))
async def cb_ep(cb: CallbackQuery, bot: Bot):
    if await blocked(cb, bot):
        return
    await send_episode(cb.message, int(cb.data.split(":")[1]))
    await cb.answer()


@user_router.message(F.text, StateFilter(None), ~F.text.startswith("/"))
async def search(m: Message, bot: Bot):
    if await blocked(m, bot):
        return
    q = m.text.strip()
    rows = await db_all(
        "SELECT id, title FROM anime WHERE title LIKE ? ORDER BY id DESC LIMIT 10", (f"%{q}%",)
    )
    if not rows:
        return await m.answer("Hech narsa topilmadi 😕 Boshqa nom bilan urinib ko'ring.")
    await m.answer("🔎 Topilganlar:", reply_markup=results_kb(rows).as_markup())


# ---------------------------------------------------------------- ADMIN
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddAnime(StatesGroup):
    title = State()
    desc = State()
    genre = State()
    poster = State()


class AddEp(StatesGroup):
    video = State()


class Broadcast(StatesGroup):
    msg = State()


def admin_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Anime qo'shish", callback_data="adm:addanime")
    kb.button(text="🎞 Qism qo'shish", callback_data="adm:addep")
    kb.button(text="🗑 Anime o'chirish", callback_data="adm:del")
    kb.button(text="📊 Statistika", callback_data="adm:stats")
    kb.button(text="📢 Xabar yuborish", callback_data="adm:bc")
    kb.adjust(2)
    return kb.as_markup()


@admin_router.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


@admin_router.message(Command("cancel"))
async def admin_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=admin_kb())


# --- anime qo'shish
@admin_router.callback_query(F.data == "adm:addanime")
async def add_anime_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddAnime.title)
    await cb.message.answer("Anime nomini yozing:\n(bekor qilish: /cancel)")
    await cb.answer()


@admin_router.message(AddAnime.title, F.text)
async def add_anime_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AddAnime.desc)
    await m.answer("Qisqacha tavsif yozing:")


@admin_router.message(AddAnime.desc, F.text)
async def add_anime_desc(m: Message, state: FSMContext):
    await state.update_data(desc=m.text.strip())
    await state.set_state(AddAnime.genre)
    await m.answer("Janrlarini yozing (masalan: Action, Drama) yoki /skip:")


@admin_router.message(AddAnime.genre, F.text)
async def add_anime_genre(m: Message, state: FSMContext):
    g = "" if m.text.strip() == "/skip" else m.text.strip()
    await state.update_data(genre=g)
    await state.set_state(AddAnime.poster)
    await m.answer("Poster rasmini yuboring yoki /skip:")


@admin_router.message(AddAnime.poster)
async def add_anime_poster(m: Message, state: FSMContext):
    fid = m.photo[-1].file_id if m.photo else None
    if not fid and (m.text or "").strip() != "/skip":
        return await m.answer("Rasm yuboring yoki /skip yozing.")
    d = await state.get_data()
    aid = await db_exec(
        "INSERT INTO anime(title, description, genre, poster) VALUES(?,?,?,?)",
        (d["title"], d["desc"], d.get("genre", ""), fid),
    )
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🎞 Qism qo'shish", callback_data=f"epa:{aid}")
    await m.answer(f"✅ «{esc(d['title'])}» qo'shildi (ID: {aid}).", reply_markup=kb.as_markup())


# --- qism qo'shish
@admin_router.callback_query(F.data == "adm:addep")
async def add_ep_pick(cb: CallbackQuery):
    rows = await db_all("SELECT id, title FROM anime ORDER BY id DESC LIMIT 40")
    if not rows:
        await cb.message.answer("Avval anime qo'shing.")
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=r["title"][:60], callback_data=f"epa:{r['id']}")
    kb.adjust(1)
    await cb.message.answer("Qaysi animega qism qo'shamiz?", reply_markup=kb.as_markup())
    await cb.answer()


@admin_router.callback_query(F.data.startswith("epa:"))
async def add_ep_chosen(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    await state.set_state(AddEp.video)
    await state.update_data(aid=aid)
    row = await db_one("SELECT COALESCE(MAX(number),0)+1 AS n FROM episodes WHERE anime_id=?", (aid,))
    await cb.message.answer(
        f"Videolarni ketma-ket yuboring (yoki saqlash kanalidan forward qiling).\n"
        f"Keyingi qism raqami: {row['n']}\nTugatish: /done"
    )
    await cb.answer()


@admin_router.message(AddEp.video, Command("done"))
async def add_ep_done(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("✅ Tayyor.", reply_markup=admin_kb())


@admin_router.message(AddEp.video, F.video | F.document)
async def add_ep_save(m: Message, state: FSMContext):
    aid = (await state.get_data())["aid"]
    row = await db_one("SELECT COALESCE(MAX(number),0)+1 AS n FROM episodes WHERE anime_id=?", (aid,))
    n = row["n"]
    if m.video:
        fid, ft = m.video.file_id, "video"
    else:
        fid, ft = m.document.file_id, "document"
    await db_exec(
        "INSERT INTO episodes(anime_id, number, file_id, ftype) VALUES(?,?,?,?)", (aid, n, fid, ft)
    )
    await m.answer(f"✅ {n}-qism saqlandi. Keyingisini yuboring yoki /done")


@admin_router.message(AddEp.video)
async def add_ep_other(m: Message):
    await m.answer("Video yuboring yoki /done yozing.")


# --- o'chirish
@admin_router.callback_query(F.data == "adm:del")
async def del_pick(cb: CallbackQuery):
    rows = await db_all("SELECT id, title FROM anime ORDER BY id DESC LIMIT 40")
    if not rows:
        await cb.message.answer("O'chiradigan anime yo'q.")
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🗑 {r['title'][:50]}", callback_data=f"del:{r['id']}")
    kb.adjust(1)
    await cb.message.answer("Qaysi animeni o'chiramiz?", reply_markup=kb.as_markup())
    await cb.answer()


@admin_router.callback_query(F.data.startswith("del:"))
async def del_confirm(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"delok:{aid}")
    kb.button(text="❌ Yo'q", callback_data="delno")
    kb.adjust(2)
    await cb.message.answer("Anime va uning barcha qismlari o'chadi. Davom etamizmi?", reply_markup=kb.as_markup())
    await cb.answer()


@admin_router.callback_query(F.data.startswith("delok:"))
async def del_ok(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    await db_exec("DELETE FROM episodes WHERE anime_id=?", (aid,))
    await db_exec("DELETE FROM anime WHERE id=?", (aid,))
    try:
        os.remove(os.path.join(POSTER_DIR, f"{aid}.jpg"))
    except OSError:
        pass
    await cb.message.answer("🗑 O'chirildi.")
    await cb.answer()


@admin_router.callback_query(F.data == "delno")
async def del_no(cb: CallbackQuery):
    await cb.message.answer("Bekor qilindi.")
    await cb.answer()


# --- statistika
@admin_router.callback_query(F.data == "adm:stats")
async def stats(cb: CallbackQuery):
    u = (await db_one("SELECT COUNT(*) AS c FROM users"))["c"]
    a = (await db_one("SELECT COUNT(*) AS c FROM anime"))["c"]
    e = (await db_one("SELECT COUNT(*) AS c FROM episodes"))["c"]
    await cb.message.answer(f"📊 Foydalanuvchilar: {u}\n🎬 Animelar: {a}\n📺 Qismlar: {e}")
    await cb.answer()


# --- xabar yuborish
@admin_router.callback_query(F.data == "adm:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await cb.message.answer("Hammaga yuboriladigan xabarni yuboring (matn, rasm yoki video).\nBekor qilish: /cancel")
    await cb.answer()


@admin_router.message(Broadcast.msg)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    users = await db_all("SELECT id FROM users")
    ok = 0
    for u in users:
        try:
            await m.copy_to(u["id"])
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📢 Yuborildi: {ok}/{len(users)}")


# ---------------------------------------------------------------- SAYT
SITE_HTML = """<!doctype html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0f1117;--card:#1a1d27;--tx:#eef0f6;--mu:#9aa0b4;--ac:#ff5c8a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;z-index:5;border-bottom:1px solid #262a38}
h1{margin:0 0 10px;font-size:22px}h1 span{color:var(--ac)}
input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #2b3042;background:var(--card);color:var(--tx);font-size:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;padding:16px}
.card{background:var(--card);border-radius:14px;overflow:hidden;cursor:pointer}
.card img,.ph{width:100%;aspect-ratio:2/3;object-fit:cover;display:block;background:#222637}
.t{padding:8px 10px;font-size:14px;font-weight:600}
.t small{display:block;color:var(--mu);font-weight:400;margin-top:2px}
.empty{color:var(--mu);padding:20px;text-align:center;grid-column:1/-1}
.modal{position:fixed;inset:0;background:#000b;display:none;align-items:flex-end;justify-content:center;z-index:10}
.modal.on{display:flex}
.sheet{background:var(--card);width:100%;max-width:640px;max-height:90vh;overflow:auto;border-radius:20px 20px 0 0;padding:18px}
.sheet h2{margin:0 0 6px}.mu{color:var(--mu);font-size:14px;line-height:1.5}
.eps{display:grid;grid-template-columns:repeat(auto-fill,minmax(52px,1fr));gap:8px;margin:14px 0}
.ep,.btn{display:block;text-align:center;padding:10px 0;border-radius:10px;background:#262a3b;color:var(--tx);cursor:pointer;border:0;font-size:15px}
.btn{background:var(--ac);font-weight:700;padding:13px;width:100%;margin-top:6px}
.close{float:right;background:none;border:0;color:var(--mu);font-size:24px;cursor:pointer}
</style></head><body>
<header><h1>🍥 <span>__NAME__</span></h1><input id="q" placeholder="Anime qidirish..."></header>
<div class="grid" id="grid"></div>
<div class="modal" id="m" onclick="if(event.target===this)closeM()"><div class="sheet" id="sheet"></div></div>
<script>
const BOT="__BOT__";
const tg=window.Telegram&&window.Telegram.WebApp;
if(tg){tg.ready();tg.expand();}
const $=s=>document.querySelector(s);
function esc(s){return String(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function go(p){const u="https://t.me/"+BOT+"?start="+p;if(tg&&tg.openTelegramLink)tg.openTelegramLink(u);else window.open(u,"_blank");}
async function load(q){
  const r=await fetch("/api/anime?q="+encodeURIComponent(q||""));
  const d=await r.json();
  $("#grid").innerHTML=d.length?d.map(a=>
    '<div class="card" onclick="openA('+a.id+')">'+
    (a.has_poster?'<img loading="lazy" src="/poster/'+a.id+'">':'<div class="ph"></div>')+
    '<div class="t">'+esc(a.title)+'<small>'+a.eps+' qism</small></div></div>').join("")
    :'<p class="empty">Hozircha anime topilmadi</p>';
}
async function openA(id){
  const r=await fetch("/api/anime/"+id);const a=await r.json();
  $("#sheet").innerHTML='<button class="close" onclick="closeM()">×</button><h2>'+esc(a.title)+'</h2>'+
    '<div class="mu">'+esc(a.genre)+'</div><p class="mu">'+esc(a.description)+'</p>'+
    '<b>Qismlar</b><div class="eps">'+
    (a.episodes.length?a.episodes.map(e=>'<button class="ep" onclick="go(\\'e_'+e.id+'\\')">'+e.number+'</button>').join(""):'<span class="mu">Qismlar tez orada</span>')+
    '</div><button class="btn" onclick="go(\\'a_'+a.id+'\\')">Botda ochish</button>';
  $("#m").classList.add("on");
}
function closeM(){$("#m").classList.remove("on");}
let t;$("#q").addEventListener("input",e=>{clearTimeout(t);t=setTimeout(()=>load(e.target.value),300);});
load("");
</script></body></html>"""


async def page_index(request):
    page = SITE_HTML.replace("__NAME__", esc(SITE_NAME)).replace("__BOT__", BOT_USERNAME)
    return web.Response(text=page, content_type="text/html")


async def api_list(request):
    q = request.query.get("q", "").strip()
    rows = await db_all(
        "SELECT a.id, a.title, a.genre, (a.poster IS NOT NULL) AS has_poster, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.anime_id=a.id) AS eps "
        "FROM anime a WHERE a.title LIKE ? ORDER BY a.id DESC LIMIT 100",
        (f"%{q}%",),
    )
    return web.json_response(rows)


async def api_detail(request):
    aid = request.match_info["id"]
    if not aid.isdigit():
        raise web.HTTPNotFound()
    a = await db_one("SELECT id, title, description, genre FROM anime WHERE id=?", (int(aid),))
    if not a:
        raise web.HTTPNotFound()
    a["episodes"] = await db_all(
        "SELECT id, number FROM episodes WHERE anime_id=? ORDER BY number", (int(aid),)
    )
    return web.json_response(a)


async def api_poster(request):
    aid = request.match_info["id"]
    if not aid.isdigit():
        raise web.HTTPNotFound()
    path = os.path.join(POSTER_DIR, f"{aid}.jpg")
    if not os.path.exists(path):
        a = await db_one("SELECT poster FROM anime WHERE id=?", (int(aid),))
        if not a or not a["poster"]:
            raise web.HTTPNotFound()
        bot: Bot = request.app["bot"]
        f = await bot.get_file(a["poster"])
        await bot.download_file(f.file_path, destination=path)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def health(request):
    return web.Response(text="ok")


def make_app(bot: Bot):
    app = web.Application()
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/", page_index),
            web.get("/health", health),
            web.get("/api/anime", api_list),
            web.get("/api/anime/{id}", api_detail),
            web.get("/poster/{id}", api_poster),
        ]
    )
    return app


# ---------------------------------------------------------------- ISHGA TUSHIRISH
async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. Railway Variables yoki .env ga qo'shing.")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username

    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Sayt ishga tushdi: port %s", PORT)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
