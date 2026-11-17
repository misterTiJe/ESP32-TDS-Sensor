#include <Arduino.h>

#define TDS_PIN     34      // ADC1 (jangan pakai ADC2 kalau pakai WiFi)
#define VREF        3.3     // tegangan referensi ESP32
#define ADC_RES     4095.0  // 12-bit
#define SAMPLES     30      // jumlah sampel untuk median filter
#define K_FACTOR    0.5     // TDS = CE * 0.5 (skala NaCl), ubah 0.5-0.7 sesuai kalibrasi

#define SHOW_TEXT   1       // 1 = tampilkan baris teks mudah dibaca di Serial Monitor, 0 = hanya CSV

float temperature = 25.0;   // suhu air (°C); ganti dengan DS18B20 kalau ada

int readings[SAMPLES];

int getMedian(int arr[], int n) {
  int tmp[SAMPLES];
  memcpy(tmp, arr, sizeof(int) * n);
  for (int i = 0; i < n - 1; i++) {
    for (int j = 0; j < n - i - 1; j++) {
      if (tmp[j] > tmp[j + 1]) {
        int t = tmp[j]; tmp[j] = tmp[j + 1]; tmp[j + 1] = t;
      }
    }
  }
  return (n % 2) ? tmp[n / 2] : (tmp[n / 2] + tmp[n / 2 - 1]) / 2;
}

void setup() {
  Serial.begin(115200);
  pinMode(TDS_PIN, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(TDS_PIN, ADC_11db); // range ~0-3.3V

  // Baris diawali '#' = komentar/info, diabaikan oleh dashboard Python
  Serial.println("# TDS Meter siap. Format data: voltage,tds,ec");
}

void loop() {
  // Ambil sampel
  for (int i = 0; i < SAMPLES; i++) {
    readings[i] = analogRead(TDS_PIN);
    delay(40);
  }

  // Filter median -> tegangan
  float voltage = getMedian(readings, SAMPLES) * VREF / ADC_RES;

  // Kompensasi suhu
  float compCoef = 1.0 + 0.02 * (temperature - 25.0);
  float compVoltage = voltage / compCoef;

  // Polinomial dari DFRobot -> TDS (ppm)
  float tds = (133.42 * pow(compVoltage, 3)
             - 255.86 * pow(compVoltage, 2)
             + 857.39 * compVoltage) * K_FACTOR;
  if (tds < 0) tds = 0;

  // CE (µS/cm)
  float ec = tds / K_FACTOR;

#if SHOW_TEXT
  // Baris teks untuk dibaca manusia. Diawali '#' sehingga diabaikan dashboard Python.
  Serial.print("# Voltage: ");  Serial.print(voltage, 3);
  Serial.print(" V | TDS: ");   Serial.print(tds, 0);
  Serial.print(" ppm | CE: ");  Serial.print(ec, 0);
  Serial.println(" uS/cm");
#endif

  // Baris data CSV untuk dashboard: voltage,tds,ec  (contoh: 1.234,456.7,913.4)
  Serial.print(voltage, 3);
  Serial.print(',');
  Serial.print(tds, 1);
  Serial.print(',');
  Serial.println(ec, 1);

  delay(1000);
}
