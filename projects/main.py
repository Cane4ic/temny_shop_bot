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
from telethon import TelegramClient, events

# ---------- LOAD CONFIG ----------
load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
API_ID = int(os.environ.get("TG_API_ID"))
API_HASH = os.environ.get("TG_API_HASH")
PHONE = os.environ.get("TG_PHONE")

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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
            login TEXT NOT NULL,
            password TEXT NOT NULL,
            used BOOLEAN DEFAULT FALSE,
            added_at TIMESTAMP DEFAULT now()
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
    cur.execute("INSERT INTO products (name, price, stock, category) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET price = EXCLUDED.price, stock = EXCLUDED.stock, category = EXCLUDED.category;",
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

def get_user_balance(user_id: int):
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

def update_user_balance(user_id: int, new_balance):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = %s WHERE user_id = %s;", (new_balance, user_id))
    conn.commit()
    cur.close()
    conn.close()

# ---------- Accounts helpers ----------
def add_accounts_to_db(product_name: str, accounts: list):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM products WHERE name = %s;", (product_name,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise ValueError("Product not found")
    product_id = row['id']
    for login, password in accounts:
        cur.execute("INSERT INTO accounts (product_id, login, password, used) VALUES (%s, %s, %s, FALSE);",
                    (product_id, login, password))
    cur.execute("UPDATE products SET stock = (SELECT COUNT(*) FROM accounts WHERE product_id = products.id AND used = FALSE) WHERE id = %s;", (product_id,))
    conn.commit()
    cur.close()
    conn.close()

def fetch_and_mark_account(product_name: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM products WHERE name = %s FOR SHARE;", (product_name,))
        prod = cur.fetchone()
        if not prod:
            cur.close()
            conn.close()
            return None
        product_id = prod['id']
        cur.execute("""
            SELECT id, login, password FROM accounts
            WHERE product_id = %s AND used = FALSE
            ORDER BY random()
            FOR UPDATE SKIP LOCKED
            LIMIT 1;
        """, (product_id,))
        acc = cur.fetchone()
        if not acc:
            cur.close()
            conn.commit()
            conn.close()
            return None
        account_id = acc['id']
        login = acc['login']
        password = acc['password']
        cur.execute("UPDATE accounts SET used = TRUE WHERE id = %s;", (account_id,))
        cur.execute("UPDATE products SET stock = GREATEST(stock - 1, 0) WHERE id = %s;", (product_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {"login": login, "password": password}
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        print(f"[DB ERROR fetch_and_mark_account] {e}")
        return None

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
    try:
        balance = get_user_balance(int(user_id))
        return jsonify({"balance": balance})
    except Exception as e:
        print(f"[Flask ERROR] {e}")
        return jsonify({"balance": 0})

@app.route("/buy_product", methods=["POST"])
def buy_product():
    data = request.json
    user_id = int(data.get("telegram_user_id", 0))
    product_name = data.get("product_name")
    price = float(data.get("price", 0))
    if not all([user_id, product_name]):
        return jsonify({"status": "error", "error": "Missing fields"}), 400
    current_balance = get_user_balance(user_id)
    if current_balance < price:
        return jsonify({"status": "error", "error": "Недостаточно средств"}), 400
    account = fetch_and_mark_account(product_name)
    if not account:
        return jsonify({"status": "error", "error": "Нет доступных аккаунтов для данного товара"}), 400
    future = asyncio.run_coroutine_threadsafe(send_product(user_id, product_name, account), bot_loop)
    try:
        future.result(timeout=10)
    except Exception as e:
        print(f"Error sending product notification: {e}")
        return jsonify({"status": "error", "error": "Failed to send notification"}), 500
    update_user_balance(user_id, current_balance - price)
    return jsonify({"status": "ok"})

@app.route("/admin/add_accounts", methods=["POST"])
def admin_add_accounts():
    data = request.json
    product_name = data.get("product_name")
    accounts_text = data.get("accounts_text")
    if not product_name or not accounts_text:
        return jsonify({"status": "error", "error": "Missing fields"}), 400
    lines = [l.strip() for l in accounts_text.splitlines() if l.strip()]
    accounts = []
    for line in lines:
        if ":" in line:
            login, password = line.split(":", 1)
            accounts.append((login.strip(), password.strip()))
    try:
        add_accounts_to_db(product_name, accounts)
        return jsonify({"status": "ok", "added": len(accounts)})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

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

class EditProduct(StatesGroup):
    select_product = State()
    field = State()
    new_value = State()

class DeleteProduct(StatesGroup):
    select_product = State()

# Новый FSM для загрузки аккаунтов
class UploadAccounts(StatesGroup):
    select_product = State()
    accounts_text = State()

# FSM для пополнения баланса
class TopUpUser(StatesGroup):
    enter_amount = State()

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
    user_id = message.from_user.id
    kb.button(
        text="🛍 Открыть TEMNY SHOP",
        web_app=WebAppInfo(url=f"{WEBAPP_URL}?user_id={user_id}")
    )
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

# ---------- ADMIN LOGIN ----------
@dp.message(Command("admin"))
async def admin_login(message: Message, state: FSMContext):
    await state.set_state(AdminLogin.waiting_for_login)
    await message.answer("Введите логин:")

@dp.message(StateFilter(AdminLogin.waiting_for_login))
async def process_login(message: Message, state: FSMContext):
    if message.text == ADMIN_LOGIN:
        await state.update_data(login=message.text)
        await state.set_state(AdminLogin.waiting_for_password)
        await message.answer("Введите пароль:")
    else:
        await message.answer("Неверный логин. Попробуйте снова.")

@dp.message(StateFilter(AdminLogin.waiting_for_password))
async def process_password(message: Message, state: FSMContext):
    if message.text == ADMIN_PASSWORD:
        admins.add(message.from_user.id)
        await message.answer("✅ Вы вошли как админ!")
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Добавить товар", callback_data="add_product")
        kb.button(text="📝 Редактировать товар", callback_data="edit_product")
        kb.button(text="❌ Удалить товар", callback_data="delete_product")
        kb.button(text="📦 Просмотреть товары", callback_data="list_products")
        kb.button(text="💰 Балансы пользователей", callback_data="user_balances")
        kb.button(text="⬆️ Загрузить аккаунты к товару", callback_data="upload_accounts")
        kb.adjust(1)
        await message.answer("Выберите действие:", reply_markup=kb.as_markup())
    else:
        await message.answer("Неверный пароль. Попробуйте снова.")

# ---------- CALLBACKS FOR ADMIN ----------
@dp.callback_query(lambda c: c.data == "list_products")
async def list_products_cb(callback: types.CallbackQuery):
    products = fetch_products_from_db()
    if not products:
        await callback.message.answer("Список товаров пуст.")
    else:
        text = "📦 Список товаров:\n\n"
        for p in products:
            text += f"• {p['name']} | Цена: ${p['price']} | Остаток: {p['stock']} | Категория: {p['category']}\n"
        await callback.message.answer(text)

# ---------- NEW TOP-UP WITH BUTTONS ----------
@dp.callback_query(lambda c: c.data == "user_balances")
async def user_balances_cb(callback: types.CallbackQuery, state: FSMContext):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, balance FROM users ORDER BY id;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        await callback.message.answer("Нет зарегистрированных пользователей.")
        return
    text = "💰 Балансы пользователей:\n\n"
    kb = InlineKeyboardBuilder()
    for u in rows:
        username = u['username'] or "—"
        text += f"• {username} ({u['user_id']}): ${u['balance']}\n"
        kb.button(text=f"Пополнить {username}", callback_data=f"topup_{u['user_id']}")
    kb.adjust(1)
    await callback.message.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("topup_"))
async def start_topup(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    await state.update_data(target_user_id=user_id)
    await callback.message.answer(f"Введите сумму для пополнения пользователя <code>{user_id}</code>:", parse_mode="HTML")
    await state.set_state(TopUpUser.enter_amount)

@dp.message(StateFilter(TopUpUser.enter_amount))
async def process_topup_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму (положительное число).")
        return
    data = await state.get_data()
    user_id = data.get("target_user_id")
    current_balance = get_user_balance(user_id)
    update_user_balance(user_id, current_balance + amount)
    try:
        await bot.send_message(
            user_id,
            f"💰 Ваш баланс был пополнен на ${amount:.2f}. Новый баланс: ${current_balance + amount:.2f}"
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление пользователю {user_id}: {e}")
    await message.answer(
        f"✅ Баланс пользователя <code>{user_id}</code> успешно пополнен на ${amount:.2f}. Новый баланс: ${current_balance + amount:.2f}",
        parse_mode="HTML"
    )
    await state.clear()

# ---------- SEND PRODUCT NOTIFICATION ----------
async def send_product(user_id: int, product_name: str, account: dict):
    try:
        text = (
            f"✅ Оплата получена! Ваш товар <b>{product_name}</b> готов.\n\n"
            f"🔐 Данные аккаунта:\n"
            f"Логин: <code>{account['login']}</code>\n"
            f"Пароль: <code>{account['password']}</code>\n\n"
            "Сохраните данные — они больше не будут доступны публично."
        )
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке товара: {e}")
        raise

# ---------- CRYPTOBOT ----------
crypto_client = TelegramClient("cryptobot_session", API_ID, API_HASH)

@crypto_client.on(events.NewMessage(from_users="CryptoBot"))
async def handle_payment(event):
    msg = event.raw_text
    if "Вы пополнили баланс на $" in msg:
        import re
        m = re.search(r"\$([0-9]+(?:\.[0-9]{1,2})?)", msg)
        if m:
            amount = float(m.group(1))
            user_id = None
            if event.message.is_reply and hasattr(event.message.reply_to_msg, 'from_id'):
                user_id = event.message.reply_to_msg.from_id.user_id if hasattr(event.message.reply_to_msg.from_id, 'user_id') else None
            if user_id:
                try:
                    current_balance = get_user_balance(user_id)
                    update_user_balance(user_id, current_balance + amount)
                    print(f"💰 Баланс пользователя {user_id} обновлен на +{amount}$")
                except Exception as e:
                    print(f"[CRYPTOBOT ERROR] {e}")

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
