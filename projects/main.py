import os
import asyncio
from threading import Thread
from flask import Flask, jsonify, send_file, request
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile
from aiogram.filters import Command, StateFilter
from psycopg2.extras import RealDictCursor
import psycopg2
from dotenv import load_dotenv

# Telethon для чтения сообщений от CryptoBot
from telethon import TelegramClient, events

# ---------- LOAD CONFIG ----------
load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")

# Telethon config
API_ID = int(os.environ.get("TG_API_ID"))           # Telegram API ID
API_HASH = os.environ.get("TG_API_HASH")           # Telegram API HASH
PHONE = os.environ.get("TG_PHONE")                 # Телефон аккаунта для мониторинга CryptoBot

# ---------- DATABASE ----------
def get_db_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        port=os.environ.get("DB_PORT", 5432),
        cursor_factory=RealDictCursor
    )

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0,
            category TEXT DEFAULT 'Other'
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            user_id BIGINT UNIQUE NOT NULL,
            username TEXT,
            balance REAL DEFAULT 0
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Database initialized successfully!")

def fetch_products_from_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products ORDER BY id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def add_product_to_db(name, price, stock, category):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO products (name, price, stock, category) VALUES (%s, %s, %s, %s);",
                (name, price, stock, category))
    conn.commit()
    cur.close()
    conn.close()

def update_product_in_db(product_name, field, new_value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE products SET {field} = %s WHERE name = %s;", (new_value, product_name))
    conn.commit()
    cur.close()
    conn.close()

def delete_product_from_db(name):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE name = %s;", (name,))
    conn.commit()
    cur.close()
    conn.close()

def get_user_balance(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = %s;", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, balance) VALUES (%s, 0);", (user_id,))
        conn.commit()
        balance = 0
    else:
        balance = row["balance"]
    cur.close()
    conn.close()
    return balance

def update_user_balance(user_id, new_balance):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = %s WHERE user_id = %s;", (new_balance, user_id))
    conn.commit()
    cur.close()
    conn.close()

# ---------- FLASK ----------
app = Flask(__name__)
bot_loop = None

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    products = fetch_products_from_db()
    return jsonify(products)

@app.route("/get_balance")
def get_balance():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"balance": 0})
    balance = get_user_balance(user_id)
    return jsonify({"balance": balance})

@app.route("/buy_product", methods=["POST"])
def buy_product():
    data = request.json
    user_id = data.get("telegram_user_id")
    product_name = data.get("product_name")
    price = float(data.get("price", 0))

    if not all([user_id, product_name]):
        return jsonify({"status": "error", "error": "Missing fields"}), 400

    current_balance = get_user_balance(user_id)
    if current_balance < price:
        return jsonify({"status": "error", "error": "Недостаточно средств"}), 400

    future = asyncio.run_coroutine_threadsafe(send_product(int(user_id), product_name), bot_loop)
    try:
        future.result(timeout=5)
    except Exception as e:
        print(f"Error sending product notification: {e}")
        return jsonify({"status": "error", "error": "Failed to send notification"}), 500

    update_user_balance(user_id, current_balance - price)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE products SET stock = GREATEST(stock - 1, 0) WHERE name = %s;", (product_name,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# ---------- TELEGRAM BOT ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

admins = set()
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "1234"

class AdminLogin(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

class AddProduct(StatesGroup):
    name = State()
    price = State()
    stock = State()
    category = State()

# ---------- HANDLERS ----------
@dp.message(Command("start"))
async def start(message: Message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING;",
        (message.from_user.id, message.from_user.username)
    )
    conn.commit()
    cur.close()
    conn.close()

    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Открыть TEMNY SHOP", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)

    banner = FSInputFile("banner.png")
    caption = (
        "✨ <b>Добро пожаловать в</b> <i>TEMNY SHOP</i> ✨\n\n"
        "🖤 Магазин премиум-товаров и цифровых сервисов.\n"
        "🔥 Всё быстро, безопасно и анонимно.\n\n"
        "👇 Нажми на кнопку ниже, чтобы открыть магазин:"
    )

    await message.answer_photo(
        photo=banner,
        caption=caption,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

# --- Admin handlers omitted for brevity (оставляем как есть) ---

# --- Send product notification ---
async def send_product(user_id: int, product_name: str):
    try:
        await bot.send_message(user_id, f"✅ Оплата получена! Ваш товар <b>{product_name}</b> готов.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке товара: {e}")

# ---------- CRYPTOBOT PAYMENT CHECK ----------
crypto_client = TelegramClient("cryptobot_session", API_ID, API_HASH)

@crypto_client.on(events.NewMessage(from_users="CryptoBot"))
async def handle_payment(event):
    msg = event.message.message
    # Пример: "Вы пополнили баланс на $10"
    if "Вы пополнили баланс на $" in msg:
        import re
        m = re.search(r"\$([0-9]+(?:\.[0-9]{1,2})?)", msg)
        if m:
            amount = float(m.group(1))
            # Получаем telegram_id из сообщения (можно через reply или username)
            if event.message.is_reply:
                user_id = event.message.reply_to_msg_id  # или другой способ
            else:
                # Для простоты возьмем свой ID, можно потом уточнить
                user_id = None
            if user_id:
                current_balance = get_user_balance(user_id)
                update_user_balance(user_id, current_balance + amount)
                print(f"💰 Баланс пользователя {user_id} обновлен на +{amount}$")

async def start_cryptobot_monitor():
    await crypto_client.start(phone=PHONE)
    print("✅ CryptoBot monitor started")
    await crypto_client.run_until_disconnected()

# ---------- MAIN ----------
async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    t1 = Thread(target=run_flask, daemon=True)
    t1.start()
    t2 = Thread(target=lambda: asyncio.run(start_cryptobot_monitor()), daemon=True)
    t2.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    init_db()
    asyncio.run(main())
