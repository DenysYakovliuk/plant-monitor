#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>

// Конфігурація пінів
#define DHT_PIN 15         // Цифровий пін DHT22 (DATA)
#define SOIL_PIN 34        // Аналоговий пін потенціометра (ґрунт)
#define LDR_PIN 35         // Аналоговий пін модуля фоторезистора (AO)

// Параметри Wi-Fi для Wokwi
const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";

// Адреса бекенду
const char* SERVER_URL = "http://192.168.0.212:5000/api/metrics";

DHTesp dhtSensor;

// Інтервал відправки даних на сервер (наприклад, кожні 5 секунд)
unsigned long lastSendTime = 0;
const unsigned long sendInterval = 5000;

void sendMetricsToServer(float temp, float hum, int soil, int light) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[HTTP] Помилка: немає з'єднання з Wi-Fi");
    return;
  }

  WiFiClient client;
  HTTPClient http;

  // Ініціалізація підключення
  http.begin(client, SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  // Формування JSON-документа
  JsonDocument doc;
  doc["temperature"] = serialized(String(temp, 1));
  doc["humidity"] = serialized(String(hum, 1));
  doc["soil_moisture"] = soil;
  doc["light_level"] = light;

  String requestBody;
  serializeJson(doc, requestBody);

  Serial.print("[HTTP] Відправка POST запиту: ");
  Serial.println(requestBody);

  // Відправка HTTP POST
  int httpResponseCode = http.POST(requestBody);

  if (httpResponseCode > 0) {
    Serial.printf("[HTTP] Код відповіді: %d\n", httpResponseCode);
    String response = http.getString();
    Serial.printf("[HTTP] Тіло відповіді: %s\n", response.c_str());
  } else {
    Serial.printf("[HTTP] Помилка відправки: %s\n", http.errorToString(httpResponseCode).c_str());
  }

  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(500);

  dhtSensor.setup(DHT_PIN, DHTesp::DHT22);
  pinMode(SOIL_PIN, INPUT);
  pinMode(LDR_PIN, INPUT);

  Serial.print("Connecting to Wi-Fi");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(200);
    Serial.print(".");
  }
  Serial.println("\nWi-Fi Connected!");
}

void loop() {
  unsigned long currentMillis = millis();

  if (currentMillis - lastSendTime >= sendInterval) {
    lastSendTime = currentMillis;

    // Зчитування DHT22
    TempAndHumidity dhtData = dhtSensor.getTempAndHumidity();
    if (isnan(dhtData.temperature) || isnan(dhtData.humidity)) {
      Serial.println("[Датчик] Помилка зчитування DHT22!");
      return;
    }

    // Зчитування аналогових пінів
    int rawSoil = analogRead(SOIL_PIN);
    int rawLight = analogRead(LDR_PIN);

    // Нормалізація значень у діапазон 0-100%
    int soilPercent = constrain(map(rawSoil, 0, 4095, 0, 100), 0, 100);
    // Інверсія для LDR: 4095 (темно) -> 0%, 0 (світло) -> 100%
    int lightPercent = constrain(map(rawLight, 4095, 0, 0, 100), 0, 100);

    // Відправка сформованого пакета на бекенд
    sendMetricsToServer(dhtData.temperature, dhtData.humidity, soilPercent, lightPercent);
  }
}