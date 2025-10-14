import asyncio, os, sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, WebAppInfo, FSInputFile

TOKEN = os.environ.get("BOT_TOKEN")  # передаем в Render
WEBAPP_URL = os.environ.get("WEBAPP_URL")  # URL WebApp
DB_PATH = "products.db"

# ----------------- Создание базы и таблицы -----------------
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS products (
    name TEXT PRIMARY KEY,
    price REAL,
    stock INTEGER,
    category TEXT
)
""")
conn.commit()
conn.close()
# ----------------------------------------------------------

bot = Bot(token=TOKEN)
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
def admin_panel_kb():
    kb = InlineKeyboardBuilder()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock, category FROM products")
    for name, price, stock, category in c.fetchall():
        kb.button(
            text=f"{name} (${price}, {stock} шт., Категория: {category})",
            callback_data=f"edit_{name}"
        )
    conn.close()
    kb.button(text="➕ Добавить новый товар", callback_data="add_new")
    return kb.as_markup()

async def save_product(name, price, stock, category):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    INSERT OR REPLACE INTO products (name, price, stock, category)
    VALUES (?, ?, ?, ?)
    """, (name, price, stock, category))
    conn.commit()
    conn.close()

async def update_stock(name, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE products SET stock = stock + ? WHERE name = ?", (amount, name))
    conn.commit()
    conn.close()

async def update_price(name, price):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE products SET price = ? WHERE name = ?", (price, name))
    conn.commit()
    conn.close()

async def delete_product(name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE name = ?", (name,))
    conn.commit()
    conn.close()

# ---------------- START / WEBAPP ----------------
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

# ---------------- ADMIN LOGIN ----------------
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
        await message.answer("Вы авторизованы ✅", reply_markup=admin_panel_kb())
    else:
        await message.answer("Неверный логин или пароль ❌")
    await state.clear()

# ---------------- ADMIN PANEL CALLBACKS ----------------
@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def edit_item(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    if user_id not in admins:
        await callback_query.answer("Доступ запрещен ❌")
        return

    item_name = callback_query.data[5:]
    kb = InlineKeyboardBuilder()
    kb.button(text="Пополнить", callback_data=f"restock_{item_name}")
    kb.button(text="Изменить цену", callback_data=f"price_{item_name}")
    kb.button(text="Удалить", callback_data=f"delete_{item_name}")
    await callback_query.message.answer(f"Управление товаром: {item_name}", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data.startswith("restock_"))
async def restock_item_callback(callback_query: types.CallbackQuery, state: FSMContext):
    item_name = callback_query.data[8:]
    await state.update_data(item_name=item_name)
    await state.set_state(AdminAction.waiting_for_restock)
    await callback_query.message.answer(f"Введите количество для пополнения товара {item_name}:")

@dp.message(AdminAction.waiting_for_restock)
async def process_restock(message: Message, state: FSMContext):
    data = await state.get_data()
    item_name = data["item_name"]
    try:
        amount = int(message.text)
        await update_stock(item_name, amount)
        await message.answer(f"Товар {item_name} пополнен на {amount} шт. ✅", reply_markup=admin_panel_kb())
    except:
        await message.answer("Ошибка! Введите число.")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("price_"))
async def change_price_callback(callback_query: types.CallbackQuery, state: FSMContext):
    item_name = callback_query.data[6:]
    await state.update_data(item_name=item_name)
    await state.set_state(AdminAction.waiting_for_new_price)
    await callback_query.message.answer(f"Введите новую цену для товара {item_name}:")

@dp.message(AdminAction.waiting_for_new_price)
async def process_new_price(message: Message, state: FSMContext):
    data = await state.get_data()
    item_name = data["item_name"]
    try:
        price = float(message.text)
        await update_price(item_name, price)
        await message.answer(f"Цена товара {item_name} обновлена на ${price} ✅", reply_markup=admin_panel_kb())
    except:
        await message.answer("Ошибка! Введите число.")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_item_callback(callback_query: types.CallbackQuery):
    item_name = callback_query.data[7:]
    await delete_product(item_name)
    await callback_query.message.answer(f"Товар {item_name} удален ✅", reply_markup=admin_panel_kb())

@dp.callback_query(lambda c: c.data == "add_new")
async def add_new_item_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminAction.waiting_for_new_item)
    await callback_query.message.answer(
        "Введите новый товар в формате: Название,Цена,Количество,Категория"
    )

@dp.message(AdminAction.waiting_for_new_item)
async def process_new_item(message: Message, state: FSMContext):
    try:
        name, price, stock, category = message.text.split(",")
        await save_product(name.strip(), float(price.strip()), int(stock.strip()), category.strip())
        await message.answer(
            f"Товар {name.strip()} добавлен ✅ (Категория: {category.strip()})",
            reply_markup=admin_panel_kb()
        )
    except:
        await message.answer("Ошибка! Используйте формат: Название,Цена,Количество,Категория")
    await state.clear()

# ---------------- MAIN ----------------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
