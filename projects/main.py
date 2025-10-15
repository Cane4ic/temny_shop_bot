import os
import json
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
WEBAPP_URL = os.environ.get("WEBAPP_URL") or (
    f"https://{os.environ.get('REPLIT_DEV_DOMAIN')}" if os.environ.get('REPLIT_DEV_DOMAIN') else None
)
if not WEBAPP_URL:
    raise ValueError("WEBAPP_URL not set! Please set WEBAPP_URL environment variable or ensure REPLIT_DEV_DOMAIN is available.")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "TEMNYSHOP")
TRIBUTE_API_KEY = os.environ.get("TRIBUTE_API_KEY")
TRIBUTE_PROJECT_ID = os.environ.get("TRIBUTE_PROJECT_ID")

# ---------- GOOGLE SHEETS ----------
_google_client = None
_google_sheet = None

def get_google_client():
    global _google_client
    if _google_client:
        return _google_client

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    # используем файл напрямую
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    _google_client = gspread.authorize(creds)
    return _google_client

def get_google_sheet():
    global _google_sheet
    if _google_sheet:
        return _google_sheet
    client = get_google_client()
    _google_sheet = client.open(GOOGLE_SHEET_NAME).sheet1
    return _google_sheet

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

def get_users_sheet():
    client = get_google_client()
    sheet_parent = client.open(GOOGLE_SHEET_NAME)
    try:
        return sheet_parent.worksheet("Users")
    except gspread.WorksheetNotFound:
        users_sheet = sheet_parent.add_worksheet(title="Users", rows="1000", cols="3")
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
bot_loop = None

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

    if not (bot_loop and bot_loop.is_running()):
        print("Bot loop not available, cannot process purchase")
        return jsonify({"status": "error", "error": "Bot not ready"}), 503

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

    sheet = get_google_sheet()
    data_rows = sheet.get_all_records()
    for idx, row in enumerate(data_rows, start=2):
        if row.get("Name") == product_name:
            stock = int(row.get("Stock", 0))
            sheet.update_cell(idx, 3, max(stock - 1, 0))
            break

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
            if bot_loop and bot_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(send_product(user_id, product_name), bot_loop)
                try:
                    future.result(timeout=5)
                except Exception as e:
                    print(f"Error sending product notification via webhook: {e}")
                    return {"status": "error", "error": "Failed to send notification"}, 500
            else:
                print("Bot loop not available for webhook notification")
                return {"status": "error", "error": "Bot not ready"}, 503
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
                return {"status": "error", "error": "Failed to update stock"}, 500

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
        sheet = get_google_client().open(GOOGLE_SHEET_NAME)
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

# ---------- Отправка товара ----------
async def send_product(user_id: int, product_name: str):
    try:
        await bot.send_message(user_id, f"✅ Оплата получена! Ваш товар <b>{product_name}</b> готов.", parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка при отправке товара: {e}")
        raise  # Re-raise so caller can handle

# ---------- MAIN ----------
async def main():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    # Start Flask in a separate thread
    t = Thread(target=run_flask, daemon=True)
    t.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
