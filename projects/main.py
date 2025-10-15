import os
import json
import base64
from threading import Thread
import asyncio
import requests

from flask import Flask, jsonify, send_file, request
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.filters import Command
import gspread
from google.oauth2.service_account import Credentials

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "TEMNYSHOP")
TRIBUTE_API_KEY = os.environ.get("TRIBUTE_API_KEY")
TRIBUTE_PROJECT_ID = os.environ.get("TRIBUTE_PROJECT_ID")

# ---------- GOOGLE SHEETS ----------
def get_google_sheet():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
    if not creds_b64:
        raise ValueError("Переменная окружения GOOGLE_CREDENTIALS_BASE64 не задана!")

    creds_json = json.loads(base64.b64decode(creds_b64))
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1

def fetch_products_from_google_sheet():
    try:
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
    except Exception as e:
        print("Ошибка получения товаров:", e)
        return {}

def update_product_in_sheet(product_name, field, new_value):
    try:
        sheet = get_google_sheet()
        data = sheet.get_all_records()
        for idx, row in enumerate(data, start=2):
            if row.get("Name") == product_name:
                col_map = {"Name": 1, "Price": 2, "Stock": 3, "Category": 4}
                if field in col_map:
                    sheet.update_cell(idx, col_map[field], new_value)
                break
    except Exception as e:
        print(f"Ошибка обновления {field} для {product_name}: {e}")

# ---------- USERS / BALANCE ----------
def get_users_sheet():
    sheet = get_google_sheet().parent
    try:
        return sheet.worksheet("Users")
    except gspread.WorksheetNotFound:
        users_sheet = sheet.add_worksheet(title="Users", rows="1000", cols="3")
        users_sheet.append_row(["UserID", "Username", "Balance"])
        return users_sheet

def get_user_balance(user_id):
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    for idx, row in enumerate(rows, start=2):
        if str(row.get("UserID")) == str(user_id):
            return float(row.get("Balance", 0))
    sheet.append_row([user_id, "", 0])
    return 0

def update_user_balance(user_id, new_balance):
    sheet = get_users_sheet()
    rows = sheet.get_all_records()
    for idx, row in enumerate(rows, start=2):
        if str(row.get("UserID")) == str(user_id):
            sheet.update_cell(idx, 3, new_balance)
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

    # списываем баланс
    update_user_balance(user_id, current_balance - price)

    # уменьшаем stock
    sheet = get_google_sheet()
    data_rows = sheet.get_all_records()
    for idx, row in enumerate(data_rows, start=2):
        if row.get("Name") == product_name:
            stock = int(row.get("Stock", 0))
            sheet.update_cell(idx, 3, max(stock - 1, 0))
            break

    # отправка товара пользователю через бота
    asyncio.create_task(send_product(int(user_id), product_name))
    return jsonify({"status": "ok"})

@app.route("/tribute_webhook", methods=["POST"])
def tribute_webhook():
    data = request.json
    print("Webhook received:", data)

    if data.get("status") == "paid":
        metadata = data.get("metadata", {})
        user_id = int(metadata.get("telegram_user_id", 0))
        product_name = metadata.get("product_name")
        if user_id and product_name:
            asyncio.create_task(send_product(user_id, product_name))
            # уменьшаем stock
            try:
                sheet = get_google_sheet()
                all_rows = sheet.get_all_records()
                for idx, row in enumerate(all_rows, start=2):
                    if row.get("Name") == product_name:
                        stock = int(row.get("Stock", 0))
                        if stock > 0:
                            sheet.update_cell(idx, 3, stock - 1)
                        break
            except Exception as e:
                print("Ошибка обновления Stock:", e)

    return {"status": "ok"}, 200

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

def admin_only(func):
    async def wrapper(message: Message, state: FSMContext):
        if message.from_user.id not in admins:
            await message.answer("❌ У вас нет прав администратора.")
            return
        return await func(message, state)
    return wrapper

class AdminLogin(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

class EditProduct(StatesGroup):
    waiting_for_product_choice = State()
    waiting_for_field_choice = State()
    waiting_for_new_value = State()

class Broadcast(StatesGroup):
    waiting_for_text = State()

# ---------- BOT HANDLERS ----------
@dp.message(lambda m: m.text == "/start")
async def start(message: Message):
    try:
        sheet = get_google_sheet().parent
        try:
            users_sheet = sheet.worksheet("Users")
        except gspread.WorksheetNotFound:
            users_sheet = sheet.add_worksheet(title="Users", rows="1000", cols="3")
            users_sheet.append_row(["UserID", "Username", "Balance"])

        all_rows = users_sheet.get_all_values()
        users = [str(row[0]) for row in all_rows[1:]] if len(all_rows) > 1 else []

        if str(message.from_user.id) not in users:
            users_sheet.append_row([message.from_user.id, message.from_user.username or "", 0])
    except Exception as e:
        print("Ошибка добавления пользователя:", e)

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

# ---------- Admin handlers ----------
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
@admin_only
async def check_sheets(message: types.Message, state: FSMContext):
    try:
        sheet = get_google_sheet()
        values = sheet.row_values(1)
        if values:
            await message.answer(f"✅ Доступ к Google Sheets есть!\nПервая строка:\n<code>{', '.join(values)}</code>", parse_mode="HTML")
        else:
            await message.answer("✅ Доступ к Google Sheets есть, но таблица пуста.")
    except Exception as e:
        await message.answer(f"❌ Ошибка доступа к Google Sheets:\n<code>{e}</code>", parse_mode="HTML")

# ---------- Callback: редактирование ----------
@dp.callback_query(lambda c: c.data == "edit_product")
@admin_only
async def start_edit_product(callback: CallbackQuery, state: FSMContext):
    products = fetch_products_from_google_sheet()
    keyboard = InlineKeyboardBuilder()
    for name in products.keys():
        keyboard.button(text=name, callback_data=f"choose_product:{name}")
    keyboard.adjust(2)
    await callback.message.answer("Выберите товар для редактирования:", reply_markup=keyboard.as_markup())
    await state.set_state(EditProduct.waiting_for_product_choice)

@dp.callback_query(lambda c: c.data.startswith("choose_product:"))
@admin_only
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
@admin_only
async def choose_field(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":", 1)[1]
    await state.update_data(field=field)
    await callback.message.answer(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(EditProduct.waiting_for_new_value)

@dp.message(EditProduct.waiting_for_new_value)
@admin_only
async def set_new_value(message: Message, state: FSMContext):
    data = await state.get_data()
    product_name = data["product_name"]
    field = data["field"]
    new_value = message.text
    update_product_in_sheet(product_name, field, new_value)
    await message.answer(f"✅ Поле <b>{field}</b> товара <b>{product_name}</b> успешно обновлено!", parse_mode="HTML")
    await state.clear()

# ---------- Callback: рассылка ----------
@dp.callback_query(lambda c: c.data == "send_broadcast")
@admin_only
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст рассылки:")
    await state.set_state(Broadcast.waiting_for_text)

@dp.message(Broadcast.waiting_for_text)
@admin_only
async def send_broadcast(message: Message, state: FSMContext):
    text = message.text
    try:
        sheet = get_google_sheet().parent.worksheet("Users")
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

# ---------- Отправка товара ----------
async def send_product(user_id: int, product_name: str):
    try:
        await bot.send_message(user_id, f"✅ Оплата получена! Ваш товар <b>{product_name}</b> готов.", parse_mode="HTML")
    except Exception as e:
        print("Ошибка при отправке товара:", e)

# ---------- MAIN ----------
if __name__ == "__main__":
    t = Thread(target=run_flask, daemon=True)
    t.start()
    asyncio.run(dp.start_polling(bot))
