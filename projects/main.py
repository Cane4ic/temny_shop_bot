import os
from flask import Flask, jsonify, send_file
from threading import Thread
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile
import openpyxl
import asyncio

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
EXCEL_FILE = "products.xlsx"

# ---------- FLASK ----------
app = Flask(__name__)

def read_products():
    wb = openpyxl.load_workbook(EXCEL_FILE)
    sheet = wb.active
    products = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        name, price, stock, category = row
        products[name] = {"price": price, "stock": stock, "category": category}
    return products

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    products = read_products()
    return jsonify(products)

def run_flask():
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

# --- BOT HANDLERS ---
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
        await message.answer("Вы авторизованы ✅")
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
