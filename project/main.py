import os
import asyncio
import asyncpg
from flask import Flask, jsonify, send_file
from threading import Thread
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
DATABASE_URL = os.environ.get("DATABASE_URL")  # Render Postgres URL

# ---------- FLASK ----------
app = Flask(__name__)

async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            name TEXT PRIMARY KEY,
            price REAL,
            stock INTEGER,
            category TEXT
        )
    """)
    await conn.close()

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
async def get_products():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT name, price, stock, category FROM products")
    products = {r['name']: {'price': r['price'], 'stock': r['stock'], 'category': r['category']} for r in rows}
    await conn.close()
    return jsonify(products)

def run_flask():
    asyncio.run(init_db())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

# ---------- TELEGRAM BOT ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

admins = set()
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "1234"

# --- FSM States ---
class AdminLogin(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

class AdminAction(StatesGroup):
    waiting_for_new_item = State()
    waiting_for_restock = State()
    waiting_for_new_price = State()

# --- UTILS ---
async def save_product(name, price, stock, category):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO products(name, price, stock, category)
        VALUES($1,$2,$3,$4)
        ON CONFLICT (name) DO UPDATE
        SET price = EXCLUDED.price,
            stock = EXCLUDED.stock,
            category = EXCLUDED.category
    """, name, price, stock, category)
    await conn.close()

async def update_stock(name, amount):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE products SET stock = stock + $1 WHERE name = $2", amount, name)
    await conn.close()

async def update_price(name, price):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE products SET price = $1 WHERE name = $2", price, name)
    await conn.close()

async def delete_product(name):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM products WHERE name = $1", name)
    await conn.close()

async def get_all_products():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT name, price, stock, category FROM products")
    await conn.close()
    return rows

def admin_panel_kb_markup_sync():
    kb = InlineKeyboardBuilder()
    # async wrapper
    async def wrapper():
        rows = await get_all_products()
        for r in rows:
            kb.button(
                text=f"{r['name']} (${r['price']}, {r['stock']} шт., Категория: {r['category']})",
                callback_data=f"edit_{r['name']}"
            )
        kb.button(text="➕ Добавить новый товар", callback_data="add_new")
        return kb.as_markup()
    return wrapper

# ---------- BOT HANDLERS ----------
@dp.message(lambda m: m.text == "/start")
async def start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Открыть TEMNY SHOP", web_app=WebAppInfo(url=WEBAPP_URL))

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
@dp.message(lambda m: m.text == "/admin")
async def admin_command(message: Message, state: FSMContext):
    await message.answer("Введите логин администратора:")
    await state.set_state(AdminLogin.waiting_for_login)

@dp.message(AdminLogin.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    await state.update_data(login=message.text)
    await message.answer("Введите пароль:")
    await state.set_state(AdminLogin.waiting_for_password)

@dp.message(AdminLogin.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    login = data["login"]
    password = message.text
    user_id = message.from_user.id

    if login == ADMIN_LOGIN and password == ADMIN_PASSWORD:
        admins.add(user_id)
        kb_markup = await admin_panel_kb_markup_sync()()
        await message.answer("Вы авторизованы ✅", reply_markup=kb_markup)
    else:
        await message.answer("Неверный логин или пароль ❌")
    await state.clear()

# ---------- MAIN ----------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Run Flask in a separate thread
    t = Thread(target=run_flask)
    t.start()

    # Run bot in main thread
    asyncio.run(main())
