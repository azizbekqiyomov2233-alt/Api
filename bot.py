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

ADMIN_ID = 8404969600  # xabarnoma yuboriladigan admin Telegram ID

DB_PATH = "hamyon.db"  # SQLite fayli - alohida baza server kerak emas

# --- Hamyon API sozlamalari ---
HAMYON_API_BASE = "https://hamyon-api.uz"
SHOP_ID = 443
SHOP_KEY = "15089791c7ea"

# To'lov qabul qilinadigan karta raqami (foydalanuvchiga ko'rsatiladi)
PAYMENT_CARD = "5614 6867 0787 6770"

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


# ------------------------------------------------------------------ MENUS

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Hisobni to'ldirish")],
            [KeyboardButton(text="💰 Balansim")],
        ],
        resize_keyboard=True,
    )


def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Orqaga")]],
        resize_keyboard=True,
    )


# ---------------------------------------------------------------- HANDLERS

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.clear()
    get_or_create_user(msg.from_user.id)
    await msg.answer(
        "👋 Salom! Bu Hamyon API to'lov boti.\n"
        "💵 Balansni to'ldirish uchun pastdagi tugmadan foydalaning.",
        reply_markup=main_menu(),
    )


@dp.message(lambda m: m.text == "💰 Balansim")
async def show_balance(msg: types.Message):
    balance = get_or_create_user(msg.from_user.id)
    await msg.answer(f"💰 Sizning balansingiz: <b>{balance} so'm</b>", reply_markup=main_menu())


@dp.message(lambda m: m.text == "🔙 Orqaga")
async def go_back(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("🏠 Bosh menyu.", reply_markup=main_menu())


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
                    "shop_id": SHOP_ID,
                    "shop_key": SHOP_KEY,
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
    card = data.get("card") or PAYMENT_CARD

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
        reply_markup=main_menu(),
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
                                    reply_markup=main_menu(),
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
                                reply_markup=main_menu(),
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
