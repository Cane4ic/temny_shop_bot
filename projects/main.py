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
    # Новая таблица accounts: хранит отдельные аккаунты для каждого товара
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
    """
    accounts: list of tuples (login, password)
    """
    conn = get_db_connection()
    cur = conn.cursor()
    # Получаем product_id
    cur.execute("SELECT id FROM products WHERE name = %s;", (product_name,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise ValueError("Product not found")
    product_id = row['id']
    # Вставляем аккаунты
    for login, password in accounts:
        cur.execute("INSERT INTO accounts (product_id, login, password, used) VALUES (%s, %s, %s, FALSE);",
                    (product_id, login, password))
    # Обновим stock: установим stock = count of unused accounts (или +N)
    cur.execute("UPDATE products SET stock = (SELECT COUNT(*) FROM accounts WHERE product_id = products.id AND used = FALSE) WHERE id = %s;", (product_id,))
    conn.commit()
    cur.close()
    conn.close()

def fetch_and_mark_account(product_name: str):
    """
    Атомарно выбирает один неиспользованный аккаунт для данного товара,
    помечает его как used и возвращает (login, password).
    Использует транзакцию и SELECT ... FOR UPDATE SKIP LOCKED, чтобы избежать race.
    Возвращает None если аккаунтов нет.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Получаем product_id
        cur.execute("SELECT id FROM products WHERE name = %s FOR SHARE;", (product_name,))
        prod = cur.fetchone()
        if not prod:
            cur.close()
            conn.close()
            return None
        product_id = prod['id']

        # Начинаем транзакцию: выбираем случайный доступный аккаунт и блокируем его
        # (обход конкурентных выборок — SKIP LOCKED)
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

        # Помечаем аккаунт как использованный
        cur.execute("UPDATE accounts SET used = TRUE WHERE id = %s;", (account_id,))

        # Уменьшаем stock у товара (гарантируем неотрицательное)
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

    # Попытаемся получить аккаунт из базы для этого товара
    account = fetch_and_mark_account(product_name)
    if not account:
        return jsonify({"status": "error", "error": "Нет доступных аккаунтов для данного товара"}), 400

    # Отправляем аккаунт через бота (асинхронно в loop)
    future = asyncio.run_coroutine_threadsafe(send_product(user_id, product_name, account), bot_loop)
    try:
        future.result(timeout=10)
    except Exception as e:
        print(f"Error sending product notification: {e}")
        # В случае ошибки отправки — можно вернуть аккаунт обратно (optional)
        # Но проще: сообщим об ошибке и оставим аккаунт помеченным. Можно реализовать rollback при необходимости.
        return jsonify({"status": "error", "error": "Failed to send notification"}), 500

    # Списываем деньги у пользователя
    update_user_balance(user_id, current_balance - price)

    return jsonify({"status": "ok"})

# Новый endpoint для админа — загрузить аккаунты в базу (можешь вызвать через curl или admin-бот)
@app.route("/admin/add_accounts", methods=["POST"])
def admin_add_accounts():
    data = request.json
    product_name = data.get("product_name")
    accounts_text = data.get("accounts_text")  # строки "email:password\n..."
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

class TopUpUser(StatesGroup):
    select_user = State()
    enter_amount = State()

# Новый FSM для загрузки аккаунтов
class UploadAccounts(StatesGroup):
    select_product = State()
    accounts_text = State()

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
        kb.button(text="⬆️ Загрузить аккаунты к товару", callback_data="upload_accounts")  # новая кнопка
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
    for u in rows:
        username = u['username'] or "—"
        text += f"• {username} ({u['user_id']}): ${u['balance']}\n"
    text += "\nВведите ID пользователя, чтобы пополнить его баланс:"
    await callback.message.answer(text)
    await state.set_state(TopUpUser.select_user)

# Загрузить аккаунты к товару (через бота)
@dp.callback_query(lambda c: c.data == "upload_accounts")
async def upload_accounts_cb(callback: types.CallbackQuery, state: FSMContext):
    products = fetch_products_from_db()
    if not products:
        await callback.message.answer("Список товаров пуст. Сначала добавьте товар.")
        return
    text = "Введите название товара, к которому хотите загрузить аккаунты:\n\n"
    for p in products:
        text += f"• {p['name']} | Остаток: {p['stock']}\n"
    await callback.message.answer(text)
    await state.set_state(UploadAccounts.select_product)

@dp.message(StateFilter(UploadAccounts.select_product))
async def upload_accounts_select_product(message: Message, state: FSMContext):
    product_name = message.text.strip()
    products = fetch_products_from_db()
    if not any(p['name'] == product_name for p in products):
        await message.answer("❌ Товар с таким названием не найден. Попробуйте снова.")
        return
    await state.update_data(product_name=product_name)
    await message.answer("Теперь пришлите список аккаунтов в формате `email:password` построчно. Отправьте `Готово` когда закончите.")
    await state.set_state(UploadAccounts.accounts_text)

@dp.message(StateFilter(UploadAccounts.accounts_text))
async def upload_accounts_receive(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.lower() == "готово":
        data = await state.get_data()
        accounts_text = data.get("accounts_text_raw", "")
        product_name = data.get("product_name")
        if not accounts_text:
            await message.answer("❌ Список аккаунтов пуст. Операция отменена.")
            await state.clear()
            return
        lines = [l.strip() for l in accounts_text.splitlines() if l.strip()]
        accounts = []
        for line in lines:
            if ":" in line:
                login, password = line.split(":", 1)
                accounts.append((login.strip(), password.strip()))
        try:
            add_accounts_to_db(product_name, accounts)
            await message.answer(f"✅ Добавлено {len(accounts)} аккаунтов к товару {product_name}.")
        except Exception as e:
            await message.answer(f"❌ Ошибка при добавлении: {e}")
        await state.clear()
        return

    # Накопление многстрочного текста (пользователь может слать много сообщений)
    data = await state.get_data()
    prev = data.get("accounts_text_raw", "")
    new_val = prev + ("\n" if prev else "") + text
    await state.update_data(accounts_text_raw=new_val)
    await message.answer("Добавлено. Пришлите ещё строки или отправьте `Готово`, чтобы завершить.")

# ---------- TOP UP, ADD, EDIT, DELETE PRODUCT (как было) ----------
@dp.callback_query(lambda c: c.data == "add_product")
async def add_product_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddProduct.name)
    await callback.message.answer("Введите название товара:")

@dp.message(StateFilter(AddProduct.name))
async def add_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите цену товара:")
    await state.set_state(AddProduct.price)

@dp.message(StateFilter(AddProduct.price))
async def add_product_price(message: Message, state: FSMContext):
    await state.update_data(price=float(message.text.strip()))
    await message.answer("Введите количество на складе (это значение будет перезаписано количеством неиспользованных аккаунтов, если вы загрузите их):")
    await state.set_state(AddProduct.stock)

@dp.message(StateFilter(AddProduct.stock))
async def add_product_stock(message: Message, state: FSMContext):
    await state.update_data(stock=int(message.text.strip()))
    await message.answer("Введите категорию товара:")
    await state.set_state(AddProduct.category)

@dp.message(StateFilter(AddProduct.category))
async def add_product_category(message: Message, state: FSMContext):
    data = await state.get_data()
    category = message.text.strip()
    # stock может быть перезаписан позднее при загрузке аккаунтов
    add_product_to_db(data['name'], data['price'], data['stock'], category)
    await message.answer(f"✅ Товар {data['name']} добавлен! Чтобы загрузить аккаунты к товару, используйте кнопку '⬆️ Загрузить аккаунты к товару'.")
    await state.clear()

@dp.callback_query(lambda c: c.data == "edit_product")
async def edit_product_cb(callback: types.CallbackQuery, state: FSMContext):
    products = fetch_products_from_db()
    if not products:
        await callback.message.answer("Список товаров пуст. Нечего редактировать.")
        return
    await state.set_state(EditProduct.select_product)
    text = "Введите название товара, который хотите редактировать:\n\n"
    for p in products:
        text += f"• {p['name']} | Цена: ${p['price']} | Остаток: {p['stock']} | Категория: {p['category']}\n"
    await callback.message.answer(text)

@dp.message(StateFilter(EditProduct.select_product))
async def edit_product_select(message: Message, state: FSMContext):
    product_name = message.text.strip()
    products = fetch_products_from_db()
    if not any(p['name'] == product_name for p in products):
        await message.answer("❌ Товар с таким названием не найден. Попробуйте снова.")
        return
    await state.update_data(product_name=product_name)
    await message.answer("Какое поле вы хотите изменить? (name, price, stock, category)")
    await state.set_state(EditProduct.field)

@dp.message(StateFilter(EditProduct.field))
async def edit_product_field(message: Message, state: FSMContext):
    field = message.text.strip().lower()
    if field not in ['name', 'price', 'stock', 'category']:
        await message.answer("❌ Неверное поле. Введите одно из: name, price, stock, category")
        return
    await state.update_data(field=field)
    await message.answer(f"Введите новое значение для {field}:")
    await state.set_state(EditProduct.new_value)

@dp.message(StateFilter(EditProduct.new_value))
async def edit_product_new_value(message: Message, state: FSMContext):
    data = await state.get_data()
    product_name = data['product_name']
    field = data['field']
    new_value = message.text.strip()

    if field in ['price']:
        try:
            new_value = float(new_value)
        except ValueError:
            await message.answer("❌ Введите корректное число для цены.")
            return
    elif field in ['stock']:
        try:
            new_value = int(new_value)
        except ValueError:
            await message.answer("❌ Введите корректное число для количества на складе.")
            return

    update_product_in_db(product_name, field, new_value)
    await message.answer(f"✅ Товар {product_name} успешно обновлен! Поле {field} теперь: {new_value}")
    await state.clear()

@dp.callback_query(lambda c: c.data == "delete_product")
async def delete_product_cb(callback: types.CallbackQuery, state: FSMContext):
    products = fetch_products_from_db()
    if not products:
        await callback.message.answer("Список товаров пуст. Нечего удалять.")
        return
    await state.set_state(DeleteProduct.select_product)
    text = "Введите название товара, который хотите удалить:\n\n"
    for p in products:
        text += f"• {p['name']} | Цена: ${p['price']} | Остаток: {p['stock']} | Категория: {p['category']}\n"
    await callback.message.answer(text)

@dp.message(StateFilter(DeleteProduct.select_product))
async def delete_product_confirm(message: Message, state: FSMContext):
    product_name = message.text.strip()
    products = fetch_products_from_db()
    if not any(p['name'] == product_name for p in products):
        await message.answer("❌ Товар с таким названием не найден. Попробуйте снова.")
        return
    delete_product_from_db(product_name)
    await message.answer(f"✅ Товар {product_name} удалён!")
    await state.clear()

# ---------- SEND PRODUCT NOTIFICATION ----------
async def send_product(user_id: int, product_name: str, account: dict):
    """
    account: {"login": "...", "password": "..."}
    Отправляет пользователю данные аккаунта в приватное сообщение.
    """
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
