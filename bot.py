import asyncio
import logging
import datetime
import sqlite3
import json

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============================== SOZLAMALAR ==============================
# ⚠️ Faqat shu qiymatlarni o'zgartiring, kod qismiga tegmang.

BOT_TOKEN = "8429697464:AAHK3ahqA9Fcf2JnGdA4cXm1m-mq8QPLwMg"

ADMIN_ID = 8537782289  # kassalarni sozlash huquqiga ega bo'lgan admin Telegram ID

DB_PATH = "hamyon.db"  # SQLite fayli - alohida baza server kerak emas

# --- Hamyon API sozlamalari ---
HAMYON_API_BASE = "https://hamyon-api.uz"

# Boshlang'ich kassa (birinchi ishga tushirishda avtomatik "kassalar" ro'yxatiga qo'shiladi)
DEFAULT_SHOP_ID = 443
DEFAULT_SHOP_KEY = "15089791c7ea"
DEFAULT_PAYMENT_CARD = "5614 6867 0787 6770"

MIN_AMOUNT = 1000
MAX_AMOUNT = 10_000_000

# To'lov holatini necha soniyada bir tekshirish
CHECK_INTERVAL_SECONDS = 10
# ==========================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hamyon-bot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class TopUp(StatesGroup):
    waiting_amount = State()


class KassaSetup(StatesGroup):
    waiting_shop_id = State()
    waiting_shop_key = State()
    waiting_card = State()


# ---------------------------------------------------------------- DATABASE

def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = connect_db()
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_id TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kassas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT NOT NULL,
            shop_key TEXT NOT NULL,
            card TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    db.commit()

    # Agar hali birorta kassa qo'shilmagan bo'lsa - standart kassani avtomatik qo'shamiz
    cur.execute("SELECT COUNT(*) as cnt FROM kassas")
    if cur.fetchone()["cnt"] == 0:
        cur.execute(
            "INSERT INTO kassas (shop_id, shop_key, card, is_active) VALUES (?,?,?,1)",
            (str(DEFAULT_SHOP_ID), DEFAULT_SHOP_KEY, DEFAULT_PAYMENT_CARD),
        )
        db.commit()

    cur.close()
    db.close()


def get_or_create_user(user_id: int) -> int:
    db = connect_db()
    cur = db.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
        db.commit()
        balance = 0
    else:
        balance = row["balance"]
    cur.close()
    db.close()
    return balance


def add_balance(user_id: int, amount: int):
    db = connect_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id=?",
        (amount, user_id),
    )
    db.commit()
    cur.close()
    db.close()


# ------------------------------------------------------------------ KASSA

def list_kassas():
    db = connect_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM kassas ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    db.close()
    return rows


def add_kassa(shop_id: str, shop_key: str, card: str):
    db = connect_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO kassas (shop_id, shop_key, card, is_active) VALUES (?,?,?,1)",
        (shop_id, shop_key, card),
    )
    db.commit()
    cur.close()
    db.close()


def toggle_kassa(kassa_id: int):
    db = connect_db()
    cur = db.cursor()
    cur.execute("SELECT is_active FROM kassas WHERE id=?", (kassa_id,))
    row = cur.fetchone()
    if row is not None:
        new_state = 0 if row["is_active"] else 1
        cur.execute("UPDATE kassas SET is_active=? WHERE id=?", (new_state, kassa_id))
        db.commit()
    cur.close()
    db.close()


def delete_kassa(kassa_id: int):
    db = connect_db()
    cur = db.cursor()
    cur.execute("DELETE FROM kassas WHERE id=?", (kassa_id,))
    db.commit()
    cur.close()
    db.close()


def pick_kassa():
    """Faol kassalar orasidan navbat bilan (eng uzoq vaqt ishlatilmaganini) tanlaydi."""
    db = connect_db()
    cur = db.cursor()
    cur.execute(
        "SELECT * FROM kassas WHERE is_active=1 "
        "ORDER BY (last_used_at IS NOT NULL), last_used_at ASC, id ASC LIMIT 1"
    )
    row = cur.fetchone()
    if row is not None:
        cur.execute(
            "UPDATE kassas SET last_used_at=? WHERE id=?",
            (datetime.datetime.now().isoformat(), row["id"]),
        )
        db.commit()
    cur.close()
    db.close()
    return row


# ------------------------------------------------------------------ MENUS

def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="💳 Hisobni to'ldirish")],
        [KeyboardButton(text="💰 Balansim")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="⚙️ Kasa sozlash")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Orqaga")]],
        resize_keyboard=True,
    )


def kassa_list_markup(kassas) -> InlineKeyboardMarkup:
    rows = []
    for k in kassas:
        status = "✅" if k["is_active"] else "⛔️"
        rows.append([
            InlineKeyboardButton(
                text=f"{status} #{k['id']} | shop_id: {k['shop_id']} | {k['card']}",
                callback_data=f"kassa_toggle:{k['id']}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"kassa_delete:{k['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Yangi kassa qo'shish", callback_data="kassa_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------- HANDLERS

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.clear()
    get_or_create_user(msg.from_user.id)
    await msg.answer(
        "👋 Salom! Bu Hamyon API to'lov boti.\n"
        "💵 Balansni to'ldirish uchun pastdagi tugmadan foydalaning.",
        reply_markup=main_menu(msg.from_user.id),
    )


@dp.message(lambda m: m.text == "💰 Balansim")
async def show_balance(msg: types.Message):
    balance = get_or_create_user(msg.from_user.id)
    await msg.answer(
        f"💰 Sizning balansingiz: <b>{balance} so'm</b>",
        reply_markup=main_menu(msg.from_user.id),
    )


@dp.message(lambda m: m.text == "🔙 Orqaga")
async def go_back(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("🏠 Bosh menyu.", reply_markup=main_menu(msg.from_user.id))


# --------------------------------------------------------- KASSA SOZLASH (ADMIN)

@dp.message(lambda m: m.text == "⚙️ Kasa sozlash")
async def kassa_settings(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kassas = list_kassas()
    if not kassas:
        await msg.answer(
            "Hali birorta kassa qo'shilmagan.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Yangi kassa qo'shish", callback_data="kassa_add")]]
            ),
        )
        return
    await msg.answer(
        "⚙️ <b>Kassalar ro'yxati</b>\n"
        "✅ - faol, ⛔️ - o'chirilgan. Bosib holatini almashtirishingiz mumkin.\n"
        "🗑 - o'chirib tashlash.",
        reply_markup=kassa_list_markup(kassas),
    )


@dp.callback_query(lambda c: c.data == "kassa_add")
async def kassa_add_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await state.set_state(KassaSetup.waiting_shop_id)
    await call.message.answer(
        "🆔 Yangi kassa uchun <b>shop_id</b> ni kiriting:",
        reply_markup=back_menu(),
    )
    await call.answer()


@dp.callback_query(lambda c: c.data.startswith("kassa_toggle:"))
async def kassa_toggle_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    kassa_id = int(call.data.split(":", 1)[1])
    toggle_kassa(kassa_id)
    kassas = list_kassas()
    await call.message.edit_reply_markup(reply_markup=kassa_list_markup(kassas))
    await call.answer("Holat yangilandi")


@dp.callback_query(lambda c: c.data.startswith("kassa_delete:"))
async def kassa_delete_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    kassa_id = int(call.data.split(":", 1)[1])
    delete_kassa(kassa_id)
    kassas = list_kassas()
    if kassas:
        await call.message.edit_reply_markup(reply_markup=kassa_list_markup(kassas))
    else:
        await call.message.edit_text("Barcha kassalar o'chirildi.")
    await call.answer("Kassa o'chirildi")


@dp.message(KassaSetup.waiting_shop_id)
async def kassa_get_shop_id(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    if msg.text == "🔙 Orqaga":
        await state.clear()
        await msg.answer("🏠 Bosh menyu.", reply_markup=main_menu(msg.from_user.id))
        return
    await state.update_data(shop_id=msg.text.strip())
    await state.set_state(KassaSetup.waiting_shop_key)
    await msg.answer("🔑 Endi <b>shop_key</b> ni kiriting:")


@dp.message(KassaSetup.waiting_shop_key)
async def kassa_get_shop_key(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    if msg.text == "🔙 Orqaga":
        await state.clear()
        await msg.answer("🏠 Bosh menyu.", reply_markup=main_menu(msg.from_user.id))
        return
    await state.update_data(shop_key=msg.text.strip())
    await state.set_state(KassaSetup.waiting_card)
    await msg.answer("💳 Endi shu kassaga tegishli <b>karta raqamini</b> kiriting:")


@dp.message(KassaSetup.waiting_card)
async def kassa_get_card(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    if msg.text == "🔙 Orqaga":
        await state.clear()
        await msg.answer("🏠 Bosh menyu.", reply_markup=main_menu(msg.from_user.id))
        return

    data = await state.get_data()
    shop_id = data.get("shop_id")
    shop_key = data.get("shop_key")
    card = msg.text.strip()

    add_kassa(shop_id, shop_key, card)
    await state.clear()

    await msg.answer(
        f"✅ Yangi kassa qo'shildi!\n"
        f"🆔 shop_id: <code>{shop_id}</code>\n"
        f"💳 Karta: <code>{card}</code>",
        reply_markup=main_menu(msg.from_user.id),
    )
    kassas = list_kassas()
    await msg.answer("⚙️ Kassalar ro'yxati:", reply_markup=kassa_list_markup(kassas))


# ------------------------------------------------------------- HISOB TO'LDIRISH

@dp.message(lambda m: m.text == "💳 Hisobni to'ldirish")
async def ask_amount(msg: types.Message, state: FSMContext):
    await state.set_state(TopUp.waiting_amount)
    await msg.answer(
        "💵 Balansni necha so'mga to'ldirmoqchisiz?\n"
        f"📰 Minimal miqdor: {MIN_AMOUNT} so'm\n"
        f"📰 Maksimal miqdor: {MAX_AMOUNT} so'm",
        reply_markup=back_menu(),
    )


@dp.message(TopUp.waiting_amount)
async def create_payment(msg: types.Message, state: FSMContext):
    if not msg.text or not msg.text.isdigit():
        await msg.answer("❗ Iltimos, faqat raqam kiriting.")
        return

    amount = int(msg.text)
    if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
        await msg.answer(
            f"❌ Minimal {MIN_AMOUNT} so'm, maksimal {MAX_AMOUNT} so'm."
        )
        return

    kassa = pick_kassa()
    if kassa is None:
        await msg.answer("❌ Hozircha faol kassa mavjud emas. Admin bilan bog'laning.")
        return

    db = connect_db()
    cur = db.cursor()
    cur.execute(
        "SELECT * FROM payments WHERE user_id=? AND status='pending'",
        (msg.from_user.id,),
    )
    existing = cur.fetchone()
    if existing:
        cur.close()
        db.close()
        await msg.answer(
            "⚠️ Sizda allaqachon kutilayotgan to'lov mavjud. "
            "Avval uni bekor qiling yoki to'lovni yakunlang."
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{HAMYON_API_BASE}/payment/create",
                json={
                    "shop_id": kassa["shop_id"],
                    "shop_key": kassa["shop_key"],
                    "amount": amount,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                raw_text = await resp.text()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    log.error(
                        "Hamyon API returned non-JSON (status %s): %s",
                        resp.status, raw_text[:500],
                    )
                    cur.close()
                    db.close()
                    await msg.answer(
                        "❌ Hamyon API kutilmagan javob qaytardi. "
                        "Birozdan so'ng qayta urinib ko'ring yoki admin bilan bog'laning."
                    )
                    return
    except asyncio.TimeoutError:
        log.error("Hamyon API timeout on payment create")
        cur.close()
        db.close()
        await msg.answer("❌ Hamyon API javob bermadi (timeout). Birozdan so'ng qayta urinib ko'ring.")
        return
    except Exception:
        log.exception("Payment create failed")
        cur.close()
        db.close()
        await msg.answer("❌ To'lov yaratishda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring.")
        return

    payment_id = data.get("payment_id")
    card = data.get("card") or kassa["card"]

    if not payment_id:
        cur.close()
        db.close()
        reason = data.get("message") or data.get("error") or data
        log.error("Hamyon API rejected payment create: %s", data)
        await msg.answer(f"❌ To'lov yaratib bo'lmadi.\nSabab: <code>{reason}</code>")
        return

    now = datetime.datetime.now().isoformat()

    cur.execute(
        "INSERT INTO payments (user_id, payment_id, amount, status, created_at) "
        "VALUES (?,?,?,'pending',?)",
        (msg.from_user.id, payment_id, amount, now),
    )
    db.commit()
    cur.close()
    db.close()

    await state.clear()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ To'lovni bekor qilish", callback_data=f"cancel:{payment_id}")]
        ]
    )

    await msg.answer(
        f"✅ To'lov yaratildi!\n\n"
        f"🆔 To'lov ID: <code>{payment_id}</code>\n"
        f"💵 Miqdori: {amount} so'm\n"
        f"💳 To'lov uchun karta: <code>{card}</code>\n\n"
        f"⏰ 5 daqiqa ichida to'lovni amalga oshiring. "
        f"Pul kartaga tushgach, balansingiz avtomatik to'ldiriladi.",
        reply_markup=main_menu(msg.from_user.id),
    )
    await msg.answer("Yuqoridagi to'lovni istalgan vaqt bekor qilishingiz mumkin:", reply_markup=markup)


@dp.callback_query(lambda c: c.data.startswith("cancel:"))
async def cancel_payment(call: types.CallbackQuery):
    payment_id = call.data.split(":", 1)[1]
    db = connect_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE payments SET status='cancelled' WHERE payment_id=? AND status='pending'",
        (payment_id,),
    )
    db.commit()
    changed = cur.rowcount
    cur.close()
    db.close()

    if changed:
        await call.message.edit_text("❌ To'lov bekor qilindi.")
    else:
        await call.message.edit_text("ℹ️ Bu to'lov allaqachon yakunlangan yoki bekor qilingan.")
    await call.answer()


# ------------------------------------------------------------- BACKGROUND

async def check_payments_loop():
    while True:
        try:
            db = connect_db()
            cur = db.cursor()
            cur.execute("SELECT * FROM payments WHERE status='pending'")
            rows = cur.fetchall()
            cur.close()
            db.close()

            if rows:
                async with aiohttp.ClientSession() as session:
                    for row in rows:
                        try:
                            async with session.get(
                                f"{HAMYON_API_BASE}/merchant/{row['payment_id']}/json",
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as resp:
                                data = await resp.json()
                        except Exception:
                            log.exception("Status check failed for %s", row["payment_id"])
                            continue

                        status = data.get("status")
                        if status == "paid":
                            db2 = connect_db()
                            cur2 = db2.cursor()
                            cur2.execute(
                                "UPDATE payments SET status='paid' WHERE payment_id=? AND status='pending'",
                                (row["payment_id"],),
                            )
                            updated = cur2.rowcount
                            db2.commit()
                            cur2.close()
                            db2.close()

                            if updated:
                                add_balance(row["user_id"], row["amount"])
                                await bot.send_message(
                                    row["user_id"],
                                    f"✅ Hisobingizga {row['amount']} so'm qo'shildi.",
                                    reply_markup=main_menu(row["user_id"]),
                                )
                                if ADMIN_ID:
                                    await bot.send_message(
                                        ADMIN_ID,
                                        f"💰 To'lov qabul qilindi:\n"
                                        f"User ID: {row['user_id']}\n"
                                        f"Miqdor: {row['amount']} so'm\n"
                                        f"Payment ID: {row['payment_id']}",
                                    )
                        elif status in ("cancel", "cancelled", "expired"):
                            db2 = connect_db()
                            cur2 = db2.cursor()
                            cur2.execute(
                                "UPDATE payments SET status=? WHERE payment_id=? AND status='pending'",
                                (status, row["payment_id"]),
                            )
                            db2.commit()
                            cur2.close()
                            db2.close()
                            await bot.send_message(
                                row["user_id"],
                                f"❌ {row['amount']} so'mlik to'lov bekor qilindi / muddati tugadi.",
                                reply_markup=main_menu(row["user_id"]),
                            )
        except Exception:
            log.exception("check_payments_loop error")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ------------------------------------------------------------------- MAIN

async def main():
    init_db()
    asyncio.create_task(check_payments_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
