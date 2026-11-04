from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
from dotenv import load_dotenv
import sqlite3
import requests
import threading
import time
import os

load_dotenv()

app = Flask(__name__)

# --- НАЛАШТУВАННЯ TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SOIL_CRITICAL_THRESHOLD = float(os.getenv("SOIL_CRITICAL_THRESHOLD", 30))
LIGHT_CRITICAL_THRESHOLD = float(os.getenv("LIGHT_CRITICAL_THRESHOLD", 25))

DEFAULT_START_HOUR = int(os.getenv("LIGHT_ALERT_START_HOUR", 8))
DEFAULT_END_HOUR = int(os.getenv("LIGHT_ALERT_END_HOUR", 20))

alert_start_hour = DEFAULT_START_HOUR
alert_end_hour = DEFAULT_END_HOUR

cooldown_minutes = int(os.getenv("ALERT_COOLDOWN_MINUTES", 30))
ALERT_COOLDOWN = timedelta(minutes=cooldown_minutes)

last_soil_alert = datetime.min
last_light_alert = datetime.min

DB_NAME = os.getenv("DB_NAME", "plant_monitor.db")

latest_metrics = {
    "temperature": "--",
    "humidity": "--",
    "soil_moisture": "--",
    "light_level": "--",
    "last_updated": "Дані ще не надходили"
}

def init_db():
    global alert_start_hour, alert_end_hour
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                display_time TEXT NOT NULL,
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                soil_moisture REAL NOT NULL,
                light_level REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('light_start_hour', ?)", (str(DEFAULT_START_HOUR),))
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('light_end_hour', ?)", (str(DEFAULT_END_HOUR),))
        conn.commit()

        cursor.execute("SELECT value FROM settings WHERE key = 'light_start_hour'")
        alert_start_hour = int(cursor.fetchone()[0])
        cursor.execute("SELECT value FROM settings WHERE key = 'light_end_hour'")
        alert_end_hour = int(cursor.fetchone()[0])

def update_alert_hours_in_db(start_hour: int, end_hour: int):
    global alert_start_hour, alert_end_hour
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'light_start_hour'", (str(start_hour),))
        cursor.execute("UPDATE settings SET value = ? WHERE key = 'light_end_hour'", (str(end_hour),))
        conn.commit()
    alert_start_hour = start_hour
    alert_end_hour = end_hour

def save_metric_to_db(display_time, temp, hum, soil, light):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO metrics (display_time, temperature, humidity, soil_moisture, light_level)
            VALUES (?, ?, ?, ?, ?)
        """, (display_time, temp, hum, soil, light))
        cursor.execute("DELETE FROM metrics WHERE created_at < datetime('now', '-7 days')")
        conn.commit()

def get_last_hour_metrics():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT display_time, temperature, humidity, soil_moisture, light_level
            FROM metrics
            WHERE created_at >= datetime('now', '-1 hour')
            ORDER BY id ASC
        """)
        rows = cursor.fetchall()
    return [
        {
            "time": row[0],
            "temperature": round(row[1], 1),
            "humidity": round(row[2], 1),
            "soil_moisture": round(row[3], 1),
            "light_level": round(row[4], 1)
        }
        for row in rows
    ]

def get_last_24h_aggregated_metrics():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                strftime('%H:', created_at, 'localtime') || printf('%02d', (cast(strftime('%M', created_at) as integer) / 10) * 10) as time_slot,
                AVG(temperature) as avg_temp,
                AVG(humidity) as avg_hum,
                AVG(soil_moisture) as avg_soil,
                AVG(light_level) as avg_light
            FROM metrics
            WHERE created_at >= datetime('now', '-24 hours')
            GROUP BY strftime('%Y-%m-%d %H', created_at), (cast(strftime('%M', created_at) as integer) / 10)
            ORDER BY MIN(id) ASC
        """)
        rows = cursor.fetchall()
    return [
        {
            "time": row[0],
            "temperature": round(row[1], 1),
            "humidity": round(row[2], 1),
            "soil_moisture": round(row[3], 1),
            "light_level": round(row[4], 1)
        }
        for row in rows
    ]

def send_telegram_alert(message: str, target_chat_id=None, reply_markup=None):
    chat_id = target_chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or "ВАШ_" in TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram] Помилка з'єднання: {e}")

STATUS_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Отримати статус"}],
        [{"text": "⏰ Години сповіщень"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

def handle_telegram_command(chat_id: str, text: str):
    global alert_start_hour, alert_end_hour
    text = text.strip()

    if text.startswith("/status") or text == "📊 Отримати статус":
        msg = (
            f"🌿 <b>Поточний стан рослини:</b>\n\n"
            f"🌡 <b>Температура повітря:</b> {latest_metrics['temperature']} °C\n"
            f"💧 <b>Вологість повітря:</b> {latest_metrics['humidity']} %\n"
            f"🌱 <b>Вологість ґрунту:</b> {latest_metrics['soil_moisture']} %\n"
            f"☀️ <b>Рівень освітленості:</b> {latest_metrics['light_level']} %\n\n"
            f"🕒 <i>Оновлено: {latest_metrics['last_updated']}</i>"
        )
        send_telegram_alert(msg, target_chat_id=chat_id, reply_markup=STATUS_KEYBOARD)

    elif text.startswith("/start"):
        welcome_msg = (
            f"👋 <b>Привіт! Я бот моніторингу твоєї рослини.</b>\n\n"
            f"Я автоматично сповіщу тебе про критичні показники.\n\n"
            f"Щоб дізнатися про стан рослини натискай кнопку \n<b>📊 Отримати статус</b> внизу, або відправляй команду <code>/status</code>.\n\n"
            f"Щоб переглянути поточні активні години натискай кнопку \n<b>⏰ Години сповіщень</b> внизу, або відправляй команду <code>/hours</code>.\n\n"
            f"⚙️ <b>Команда зміни годин для сповіщень:</b>\n"
            f"<code>/set_hours ПОЧАТОК КІНЕЦЬ</code>\n"
            f"<i>Приклад:</i> <code>/set_hours 8 20</code> (з 08:00 до 20:00)\n"
            f"<code>/set_hours 0 0</code> (цілодобово)\n"
        )
        send_telegram_alert(welcome_msg, target_chat_id=chat_id, reply_markup=STATUS_KEYBOARD)

    elif text == "⏰ Години сповіщень" or text == "/hours" or text == "/set_hours":
        msg = (
            f"⏰ <b>Поточні години сповіщень:</b> з {alert_start_hour:02d}:00 до {alert_end_hour:02d}:00\n\n"
            f"Сповіщення про вологість ґрунту та освітленість надсилаються лише в цьому інтервалі.\n\n"
            f"Щоб змінити, відправте команду: <code>/set_hours ПОЧАТОК КІНЕЦЬ</code>\n"
            f"<i>Приклад:</i> <code>/set_hours 8 20</code> (з 08:00 до 20:00)"
            f"<code>/set_hours 0 0</code> (цілодобово)\n"
        )
        send_telegram_alert(msg, target_chat_id=chat_id, reply_markup=STATUS_KEYBOARD)

    elif text.startswith("/set_hours"):
        parts = text.split()
        if len(parts) == 3:
            try:
                start_h = int(parts[1])
                end_h = int(parts[2])
                if 0 <= start_h <= 23 and 0 <= end_h <= 24:
                    end_saved = 0 if end_h == 24 else end_h
                    update_alert_hours_in_db(start_h, end_saved)
                    
                    if start_h == end_saved or (start_h == 0 and end_h == 24):
                        msg = "✅ Години сповіщень оновлено:\n<b>Сповіщення активні цілодобово (24/7)</b>"
                    else:
                        msg = f"✅ Години сповіщень успішно оновлено:\n<b>з {start_h:02d}:00 до {end_saved:02d}:00</b>"
                else:
                    msg = "⚠️ Помилка: години мають бути від 0 до 23 (<code>/set_hours 0 0</code> (цілодобові сповіщення))."
            except ValueError:
                msg = "⚠️ Некоректні параметри! Приклад: <code>/set_hours 8 20</code>"
        else:
            msg = "⚠️ Некоректний формат команди. Приклад: <code>/set_hours 8 20</code>"
        send_telegram_alert(msg, target_chat_id=chat_id, reply_markup=STATUS_KEYBOARD)

def telegram_polling_loop():
    if not TELEGRAM_BOT_TOKEN or "ВАШ_" in TELEGRAM_BOT_TOKEN:
        return

    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 20}
            resp = requests.get(url, params=params, timeout=25)
            
            if resp.status_code == 200:
                updates = resp.json().get("result", [])
                for update in updates:
                    last_update_id = update["update_id"]
                    msg = update.get("message")
                    if msg and "text" in msg:
                        sender_chat_id = str(msg["chat"]["id"])
                        handle_telegram_command(sender_chat_id, msg["text"])
        except Exception:
            time.sleep(2)
        time.sleep(0.5)

def is_hour_in_alert_range(current_hour: int, start_h: int, end_h: int) -> bool:
    end_norm = 0 if end_h == 24 else end_h

    if start_h == end_norm or (start_h == 0 and end_h == 24):
        return True
    
    if start_h < end_norm:
        return start_h <= current_hour < end_norm
    else:
        return current_hour >= start_h or current_hour < end_norm

@app.route('/')
def dashboard():
    return render_template('index.html')

@app.route('/api/metrics', methods=['POST'])
def receive_metrics():
    global latest_metrics, last_soil_alert, last_light_alert, alert_start_hour, alert_end_hour
    data = request.get_json()
    now = datetime.now()
    time_str = now.strftime("%H:%M:%S")

    latest_metrics["temperature"] = data.get("temperature", "--")
    latest_metrics["humidity"] = data.get("humidity", "--")
    latest_metrics["soil_moisture"] = data.get("soil_moisture", "--")
    latest_metrics["light_level"] = data.get("light_level", "--")
    latest_metrics["last_updated"] = time_str

    try:
        temp = float(latest_metrics["temperature"])
        hum = float(latest_metrics["humidity"])
        soil = float(latest_metrics["soil_moisture"])
        light = float(latest_metrics["light_level"])

        save_metric_to_db(time_str, temp, hum, soil, light)

        is_alert_time = is_hour_in_alert_range(now.hour, alert_start_hour, alert_end_hour)

        if is_alert_time and soil < SOIL_CRITICAL_THRESHOLD:
            if now - last_soil_alert > ALERT_COOLDOWN:
                last_soil_alert = now
                msg = (
                    f"🚨 <b>Увага: Потрібен полив!</b>\n"
                    f"Вологість ґрунту впала до <b>{soil}%</b>.\n"
                    f"Час: {latest_metrics['last_updated']}"
                )
                send_telegram_alert(msg)

        if is_alert_time and light < LIGHT_CRITICAL_THRESHOLD:
            if now - last_light_alert > ALERT_COOLDOWN:
                last_light_alert = now
                msg = (
                    f"☀️ <b>Увага: Недостатньо світла!</b>\n"
                    f"Рівень освітленості: <b>{light}%</b>.\n"
                    f"Переставте рослину або увімкніть фітолампу.\n"
                    f"Час: {latest_metrics['last_updated']}"
                )
                send_telegram_alert(msg)

    except (ValueError, TypeError):
        pass

    return jsonify({"status": "success"}), 200

@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    return jsonify(latest_metrics), 200

@app.route('/api/history', methods=['GET'])
def get_history():
    time_range = request.args.get('range', '1h')
    if time_range == '24h':
        history = get_last_24h_aggregated_metrics()
    else:
        history = get_last_hour_metrics()
    return jsonify(history), 200

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify({
        "light_alert_start_hour": alert_start_hour,
        "light_alert_end_hour": alert_end_hour
    }), 200

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.get_json() or {}
    try:
        start_h = int(data.get("light_alert_start_hour"))
        end_h = int(data.get("light_alert_end_hour"))

        if not (0 <= start_h <= 23 and 0 <= end_h <= 24):
            return jsonify({"error": "Години мають бути від 0 до 23 (кінцева година може бути 24)"}), 400

        end_saved = 0 if end_h == 24 else end_h
        update_alert_hours_in_db(start_h, end_saved)

        return jsonify({
            "status": "success",
            "light_alert_start_hour": alert_start_hour,
            "light_alert_end_hour": alert_end_hour
        }), 200
    except (ValueError, TypeError):
        return jsonify({"error": "Некоректні дані"}), 400

if __name__ == '__main__':
    init_db()
    polling_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
    polling_thread.start()
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)