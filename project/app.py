from flask import Flask, jsonify, send_file
import sqlite3, os

app = Flask(__name__)
DB_PATH = "products.db"

# ----------------- Создание таблицы, если её нет -----------------
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
# ----------------------------------------------------------------

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, price, stock, category FROM products")
    products = {}
    for name, price, stock, category in c.fetchall():
        products[name] = {
            "price": price,
            "stock": stock,
            "category": category
        }
    conn.close()
    return jsonify(products)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
