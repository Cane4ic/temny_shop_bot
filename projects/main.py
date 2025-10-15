import os
import json
from flask import Flask, jsonify, send_file
from threading import Thread
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import asyncio
import gspread
from google.oauth2.service_account import Credentials

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "TEMNY SHOP")

# ---------- GOOGLE SHEETS ----------
def get_google_sheet():
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
    
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1

def fetch_products_from_google_sheet():
    sheet = get_google_sheet()
    data = sheet.get_all_records()
    products = {}
    for row in data:
        name = row.get("Name")
        price = row.get("Price", "0")
        stock = row.get("Stock", "0")
        category = row.get("Category", "Other")
        if name:
            products[name] = {"price": price, "stock": stock, "category": category}
    return products

def update_product_in_sheet(product_name, field, new_value):
    sheet = get_google_sheet()
    data = sheet.get_all_records()
    for idx, row in enumerate(data, start=2):  # строка 2 — после заголовков
        if row.get("Name") == product_name:
            col_map = {"Name": 1, "Price": 2, "Stock": 3, "Category": 4}
            if field in col_map:
                sheet.update_cell(idx, col_map[field], new_value)
            break

# ---------- FLASK ----------
app = Flask(__name__)

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    products = fetch_products_from_google_sheet()
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

class EditProduct(StatesGroup):
    waiting_for_product_choice = State()
    waiting_for_field_choice = State()
    waiting_for_new_value = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()

# --- BOT HANDLERS ---
@dp.message(lambda m: m.text == "/start")
async def start(message: Message):
    # сохраняем пользователя в Google Sheets
    try:
        creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
        creds = Credentials.from_service_account_info(creds_json)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME)
        try:
            users_sheet = sheet.worksheet("Users")
        except gspread.WorksheetNotFound:
            users_sheet = sheet.add_worksheet(title="Users", rows="1000", cols="2")
            users_sheet.append_row(["UserID", "Username"])

        users = [str(row[0]) for row in users_sheet.get_all_values()[1:]]
        if str(message.from_user.id) not in users:
            users_sheet.append_row([message.from_user.id, message.from_user.username or ""])
    except Exception as e:
        print("Ошибка добавления пользователя:", e)

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

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать товар", callback_data="edit_product")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="send_broadcast")]
        ])

        await message.answer(
            "👑 <b>Добро пожаловать в Admin Panel</b>\n\n"
            "Пожалуйста, выберите действие, которое хотите сделать ниже:",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await message.answer("Неверный логин или пароль ❌")
    await state.clear()

@dp.message(Command("check_sheets"))
async def check_sheets(message: types.Message):
    try:
        sheet = get_google_sheet()
        values = sheet.row_values(1)
        if values:
            await message.answer(f"✅ Доступ к Google Sheets есть!\nПервая строка:\n<code>{', '.join(values)}</code>", parse_mode="HTML")
        else:
            await message.answer("✅ Доступ к Google Sheets есть, но таблица пуста.")
    except Exception as e:
        await message.answer(f"❌ Ошибка доступа к Google Sheets:\n<code>{e}</code>", parse_mode="HTML")

# ---------- CALLBACK: РЕДАКТИРОВАНИЕ ----------
@dp.callback_query(lambda c: c.data == "edit_product")
async def start_edit_product(callback: CallbackQuery, state: FSMContext):
    products = fetch_products_from_google_sheet()
    keyboard = InlineKeyboardBuilder()
    for name in products.keys():
        keyboard.button(text=name, callback_data=f"choose_product:{name}")
    keyboard.adjust(2)
    await callback.message.answer("Выберите товар для редактирования:", reply_markup=keyboard.as_markup())
    await state.set_state(EditProduct.waiting_for_product_choice)

@dp.callback_query(lambda c: c.data.startswith("choose_product:"))
async def choose_product(callback: CallbackQuery, state: FSMContext):
    product_name = callback.data.split(":", 1)[1]
    await state.update_data(product_name=product_name)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Название", callback_data="edit_field:Name")],
        [InlineKeyboardButton(text="💵 Цена", callback_data="edit_field:Price")],
        [InlineKeyboardButton(text="📦 Количество", callback_data="edit_field:Stock")],
        [InlineKeyboardButton(text="🏷 Категория", callback_data="edit_field:Category")]
    ])
    await callback.message.answer(f"Что хотите изменить в <b>{product_name}</b>?", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(EditProduct.waiting_for_field_choice)

@dp.callback_query(lambda c: c.data.startswith("edit_field:"))
async def choose_field(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    await state.update_data(field=field)
    await callback.message.answer(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(EditProduct.waiting_for_new_value)

@dp.message(EditProduct.waiting_for_new_value)
async def set_new_value(message: Message, state: FSMContext):
    data = await state.get_data()
    product_name = data["product_name"]
    field = data["field"]
    new_value = message.text

    update_product_in_sheet(product_name, field, new_value)
    await message.answer(f"✅ Поле <b>{field}</b> товара <b>{product_name}</b> успешно обновлено!", parse_mode="HTML")
    await state.clear()

# ---------- CALLBACK: РАССЫЛКА ----------
@dp.callback_query(lambda c: c.data == "send_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(Broadcast.waiting_for_text)

@dp.message(Broadcast.waiting_for_text)
async def send_broadcast(message: Message, state: FSMContext):
    text = message.text
    try:
        creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
        creds = Credentials.from_service_account_info(creds_json)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Users")
        users = sheet.get_all_records()

        count = 0
        for user in users:
            try:
                await bot.send_message(user["UserID"], text)
                count += 1
            except:
                pass

        await message.answer(f"📢 Рассылка завершена!\n✅ Успешно отправлено {count} сообщений.")
    except Exception as e:
        await message.answer(f"Ошибка при рассылке: {e}")
    await state.clear()

# ---------- MAIN ----------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    t = Thread(target=run_flask)
    t.start()
    asyncio.run(main())

