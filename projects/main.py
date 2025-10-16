import os
import asyncio
import requests
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

# ---------- LOAD CONFIG ----------
load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
TRIBUTE_API_KEY = os.environ.get("TRIBUTE_API_KEY")
TRIBUTE_PROJECT_ID = os.environ.get("TRIBUTE_PROJECT_ID")

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

@app.route("/create_payment", methods=["POST"])
def create_payment():
    data = request.json
    product_name = data.get("product_name")
    price = data.get("price")
    telegram_user_id = data.get("telegram_user_id")

    if not (product_name and price and telegram_user_id):
        return jsonify({"error": "Missing fields"}), 400

    headers = {
        "Authorization": f"Bearer {TRIBUTE_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "amount": int(float(price) * 100),
        "currency": "USD",
        "metadata": {
            "telegram_user_id": telegram_user_id,
            "product_name": product_name
        },
        "project_id": TRIBUTE_PROJECT_ID
    }

    try:
        resp = requests.post("https://api.tribute.io/v1/payments", json=payload, headers=headers)
        resp_data = resp.json()
        payment_url = resp_data.get("url")
        if not payment_url:
            return jsonify({"error": "Failed to create payment"}), 500
        return jsonify({"payment_url": payment_url})
    except Exception as e:
        print("Ошибка создания платежа:", e)
        return jsonify({"error": str(e)}), 500

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

# ---------- ADMIN HANDLERS ----------
@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    await message.answer("Введите логин администратора:")
    await state.set_state(AdminLogin.waiting_for_login)

@dp.message(StateFilter(AdminLogin.waiting_for_login))
async def admin_login_step1(message: Message, state: FSMContext):
    if message.text == ADMIN_LOGIN:
        await message.answer("Введите пароль:")
        await state.set_state(AdminLogin.waiting_for_password)
    else:
        await message.answer("❌ Неверный логин.")

@dp.message(StateFilter(AdminLogin.waiting_for_password))
async def admin_login_step2(message: Message, state: FSMContext):
    if message.text == ADMIN_PASSWORD:
        admins.add(message.from_user.id)
        await state.clear()
        await message.answer("✅ Авторизация успешна!")
        await show_admin_panel(message)
    else:
        await message.answer("❌ Неверный пароль.")

async def show_admin_panel(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить товар", callback_data="add_product")
    kb.button(text="✏️ Изменить цену", callback_data="edit_product")
    kb.button(text="❌ Удалить товар", callback_data="delete_product")
    kb.button(text="📋 Показать все", callback_data="list_products")
    kb.adjust(1)
    await message.answer("🛠 Админ-панель:", reply_markup=kb.as_markup())

# --- Add / Edit / Delete products ---
@dp.callback_query(lambda c: c.data == "add_product")
async def start_add_product(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in admins:
        return await callback.answer("Нет доступа")
    await callback.message.answer("Введите название товара:")
    await state.set_state(AddProduct.name)

@dp.message(StateFilter(AddProduct.name))
async def step_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите цену:")
    await state.set_state(AddProduct.price)

@dp.message(StateFilter(AddProduct.price))
async def step_price(message: Message, state: FSMContext):
    await state.update_data(price=float(message.text))
    await message.answer("Введите количество (stock):")
    await state.set_state(AddProduct.stock)

@dp.message(StateFilter(AddProduct.stock))
async def step_stock(message: Message, state: FSMContext):
    await state.update_data(stock=int(message.text))
    await message.answer("Введите категорию:")
    await state.set_state(AddProduct.category)

@dp.message(StateFilter(AddProduct.category))
async def step_category(message: Message, state: FSMContext):
    data = await state.get_data()
    add_product_to_db(data["name"], data["price"], data["stock"], message.text)
    await state.clear()
    await message.answer(f"✅ Товар <b>{data['name']}</b> успешно добавлен!", parse_mode="HTML")

# --- List / Delete / Edit products ---
@dp.callback_query(lambda c: c.data == "list_products")
async def list_products(callback: types.CallbackQuery):
    products = fetch_products_from_db()
    text = "📋 <b>Товары:</b>\n\n"
    for p in products:
        text += f"• {p['name']} — {p['price']}$ | Остаток: {p['stock']} | Категория: {p['category']}\n"
    await callback.message.answer(text, parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "delete_product")
async def delete_menu(callback: types.CallbackQuery):
    products = fetch_products_from_db()
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"❌ {p['name']}", callback_data=f"del_{p['name']}")
    kb.adjust(1)
    await callback.message.answer("Выберите товар для удаления:", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("del_"))
async def delete_item(callback: types.CallbackQuery):
    name = callback.data.split("_", 1)[1]
    delete_product_from_db(name)
    await callback.message.answer(f"🗑 Товар <b>{name}</b> удалён!", parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "edit_product")
async def edit_menu(callback: types.CallbackQuery):
    products = fetch_products_from_db()
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"✏️ {p['name']}", callback_data=f"edit_{p['name']}")
    kb.adjust(1)
    await callback.message.answer("Выберите товар для изменения цены:", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def edit_price(callback: types.CallbackQuery, state: FSMContext):
    name = callback.data.split("_", 1)[1]
    await state.update_data(edit_name=name)
    await callback.message.answer(f"Введите новую цену для <b>{name}</b>:", parse_mode="HTML")
    await state.set_state("editing_price")

@dp.message(StateFilter("editing_price"))
async def save_new_price(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["edit_name"]
    new_price = float(message.text)
    update_product_in_db(name, "price", new_price)
    await state.clear()
    await message.answer(f"✅ Цена товара <b>{name}</b> обновлена до {new_price}$", parse_mode="HTML")

# --- Send product notification ---
async def send_product(user_id: int, product_name: str):
    try:
        await bot.send_message(user_id, f"✅ Оплата получена! Ваш товар <b>{product_name}</b> готов.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке товара: {e}")

# --- MAIN ---
async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()
    t = Thread(target=run_flask, daemon=True)
    t.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    init_db()
    asyncio.run(main())
