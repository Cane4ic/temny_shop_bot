from flask import Flask, send_file, jsonify
import os, sqlite3

app = Flask(__name__)

DB_FILE = '/home/render/project/products.db'  # путь к SQLite базе на Render

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    if not os.path.exists(DB_FILE):
        return jsonify({})  # если база не создана

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name, price, stock, category FROM products")
    data = {row[0]: {"price": row[1], "stock": row[2], "category": row[3]} for row in cur.fetchall()}
    conn.close()
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
