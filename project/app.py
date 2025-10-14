from flask import Flask, send_file, jsonify
import os, json

app = Flask(__name__)

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/products")
def get_products():
    if os.path.exists("products.json"):
        with open("products.json", "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
    else:
        data = {}
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
