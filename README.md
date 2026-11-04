## ⚙️ Ключові алгоритми та логіка системи

### 1. Нормалізація та валідація сенсорних даних (ESP32 / C++)

На рівні мікроконтролера виконується первинна фільтрація хибних значень та масштабування аналогових сигналів (12-бітний АЦП: `0–4095`) у відносні фізичні одиниці (`0–100%`).

* **Захист від аномалій**: Перевірка `isnan()` гарантує відсікання помилок шини цифрового сенсора DHT22 до відправки пакету.
* **Інверсія аналогового сигналу**: Рівень освітленості трансформується у зворотній шкалі (`4095 -> 0%`, `0 -> 100%`), відображаючи реальний рівень світла при схемі підключення фоторезистора у подільнику напруги.
* **Обмеження діапазону**: Використання функції `constrain()` запобігає виходу значень за межі діапазону при апаратних флуктуаціях напруги.

```cpp
// Зчитування та перевірка цілісності даних DHT22
TempAndHumidity dhtData = dhtSensor.getTempAndHumidity();
if (isnan(dhtData.temperature) || isnan(dhtData.humidity)) {
    Serial.println("[Датчик] Помилка зчитування DHT22!");
    return;
}

// Зчитування сирих значень з 12-бітного АЦП (0 - 4095)
int rawSoil = analogRead(SOIL_PIN);
int rawLight = analogRead(LDR_PIN);

// Калібрування та масштабування у діапазон 0-100%
int soilPercent = constrain(map(rawSoil, 0, 4095, 0, 100), 0, 100);
int lightPercent = constrain(map(rawLight, 4095, 0, 0, 100), 0, 100);
```

### 2. Часова агрегація та автоматичне очищення даних (SQLite / Python)

Для оптимізації передачі метрик на клієнтський дашборд реалізовано дворівневе отримання історії: деталізоване за останню годину та усереднене за 24 години, а також механізм очищення застарілої інформації.

* **Rolling window retention**: При кожному записі нових показників автоматично видаляються дані, старші за 7 днів `(DELETE ... -7 days)`, що запобігає розростанню бази даних на вбудованих сховищах.
* **10-хвилинна інтервальна агрегація**: Запит за 24 години виконує кластеризацію та усереднення (`AVG()`) за часовими слотами у 10 хвилин, усуваючи надлишкове навантаження на мережу та прискорюючи рендеринг графіків Chart.js.
* **Корекція часового поясу**: Застосування модифікатора `'localtime'` нівелює розбіжності між UTC-мітками бази та локальним часом клієнта.

```python
def save_metric_to_db(display_time, temp, hum, soil, light):
    """Збереження метрик та автоматична ротація даних за 7 днів."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO metrics (display_time, temperature, humidity, soil_moisture, light_level)
            VALUES (?, ?, ?, ?, ?)
        """, (display_time, temp, hum, soil, light))
        cursor.execute("DELETE FROM metrics WHERE created_at < datetime('now', '-7 days')")
        conn.commit()

def get_last_hour_metrics():
    """Отримання деталізованих щохвилинних метрик за останню 1 годину."""
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
    """Отримання агрегованих (10-хвилинні інтервали) даних за останні 24 години."""
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
```

### 3. Розумні сповіщення з фільтрацією подій (Flask Backend)

Обробник маршруту телеметрії приймає дані від ESP32, оновлює стан системи в пам'яті та ініціює надсилання аварійних повідомлень у Telegram за багаторівневими умовами.

* **Часовий фільтр спокою**: Перевірка `is_alert_time` дозволяє сповіщення лише у встановлений денний інтервал (`alert_start_hour <= now.hour < alert_end_hour`), виключаючи турбування користувача вночі.
* **Захист від спаму сповіщень (Cooldown)**: Використання інтервалу затримки (`ALERT_COOLDOWN`) блокує багаторазову відправку повідомлень за однакової критичної події при кожній ітерації опитування датчиків.

```python
@app.route('/api/metrics', methods=['POST'])
def receive_metrics():
    """Прийом телеметрії від мікроконтролера та оцінка порогів небезпеки."""
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

        is_alert_time = alert_start_hour <= now.hour < alert_end_hour

        # Перевірка критичного висихання ґрунту
        if is_alert_time and soil < SOIL_CRITICAL_THRESHOLD:
            if now - last_soil_alert > ALERT_COOLDOWN:
                last_soil_alert = now
                msg = (
                    f"🚨 <b>Увага: Потрібен полив!</b>\n"
                    f"Вологість ґрунту впала до <b>{soil}%</b>.\n"
                    f"Час: {latest_metrics['last_updated']}"
                )
                send_telegram_alert(msg)

        # Перевірка недостатності освітлення
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
```

### 4. Асинхронне довге опитування Telegram (Long Polling)

Взаємодія з Telegram Bot API організована через безперервний цикл у фоновому потоці (`threading.Thread`), що позбавляє необхідності налаштовувати публічні білі IP чи зовнішні webhook-домени.

* **Економічний Long Polling**: Параметр `timeout=20` утримує відкрите HTTP-з'єднання на стороні сервера Telegram, віддаючи оновлення миттєво без постійного спаму запитами.
* **Відстеження зміщення (`offset`)**: Використання `last_update_id + 1` гарантує, що кожне вхідне повідомлення буде підтверджене та оброблене сервером рівно один раз.
* **Відмовостійкість**: Блок перехоплення винятків з тайм-аутом запобігає падінню фонового сервісу у випадку короткочасних розривів Інтернет-з'єднання.

```python
def telegram_polling_loop():
    """Фоновий цикл обробки команд Telegram без використання вебхуків."""
    if not TELEGRAM_BOT_TOKEN:
        return

    last_update_id = 0
    while True:
        try:
            url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/getUpdates"
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
```
