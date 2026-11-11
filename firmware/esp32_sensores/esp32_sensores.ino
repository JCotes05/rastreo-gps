/*
 * esp32_sensores.ino — Orbita P3-S3 · ESP32 de sensores de la carga (BORRADOR, sin probar en hardware)
 *
 * Placa: ESP32 normal (DevKit / ESP32-WROOM-32). NO es para el ESP32-CAM.
 * Va en la zona de la carga. No usa Wi-Fi: habla por Bluetooth clásico (SPP)
 * con el celular del conductor, y el celular manda todo al servidor.
 *
 * Sensores:
 *   - GY-521 (MPU-6050) por I2C: SDA = GPIO 21, SCL = GPIO 22, dirección 0x68.
 *       frenada brusca, hueco y curva brusca.
 *   - Fotoresistencia (LDR) en divisor con 10 kΩ a GPIO 34 (ADC1; funciona con Bluetooth activo).
 *       luz dentro de la carga = carga abierta.
 *
 * Protocolo (una línea por mensaje, terminada en \n; campos CLAVE:valor separados por ';'):
 *   Del celular:  PING | INICIO;ENVIO:<id> | FIN
 *   Al celular:   PONG
 *                 HOLA;DISP:SENSORES;VER:1                      (al conectar)
 *                 EVENTO;TIPO:FRENADA;G:0.62;SEQ:<n>            (también HUECO y CURVA; solo con viaje)
 *                 LUZ;ESTADO:ABIERTA;NIVEL:812;SEQ:<n>          (al cambiar, con histéresis; y al recibir INICIO)
 *                 LATIDO;LUZ:<nivel>;ESTADO:<ABIERTA|CERRADA>   (cada 10 s)
 *
 * Cómo se monta el GY-521 (importante para los ejes):
 *   - eje X del módulo apuntando hacia ADELANTE del camión,
 *   - eje Y hacia la izquierda,
 *   - eje Z hacia ARRIBA (el módulo plano, con los componentes hacia arriba).
 *   Si se monta distinto, cambie EJE_LONGITUDINAL / SIGNO_* abajo o use la calibración.
 *
 * Calibración por el monitor serie (115200 baudios, "Nueva línea"):
 *   escriba  CAL   -> muestra cada 100 ms ax, ay, az (en g) y la luz; otra vez CAL para salir.
 *   escriba  CERO  -> con el camión QUIETO, mide 2 s y guarda el desfase de los ejes (offset).
 *   escriba  UMBRALES -> muestra los umbrales actuales.
 *   escriba  PRUEBA   -> manda un EVENTO de prueba por Bluetooth (como si fuera una frenada).
 *
 * Librerías: solo las que trae el "esp32 by Espressif Systems" (BluetoothSerial y Wire).
 */

#include <Arduino.h>
#include <Wire.h>
#include "BluetoothSerial.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error "Bluetooth no está habilitado. En Arduino IDE elija la placa 'ESP32 Dev Module'."
#endif

// ======================= CONFIGURACIÓN (calibrar en la protoboard) =======================
const char* NOMBRE_BT = "Orbita-Sensores";   // así aparece en el Bluetooth del celular

// Pines
const int PIN_SDA = 21;
const int PIN_SCL = 22;
const int PIN_LDR = 34;                      // ADC1 (los pines ADC2 no sirven con Bluetooth/Wi-Fi)
const int PIN_LED = 2;                       // LED azul del DevKit: encendido = celular conectado

// Umbrales de eventos (en g)
const float UMBRAL_FRENADA_G   = 0.40;       // desaceleración longitudinal (aceleración hacia atrás)
const unsigned long DURACION_FRENADA_MS = 200;  // ...sostenida al menos este tiempo
const float UMBRAL_HUECO_G     = 0.80;       // pico vertical |az - 1 g|
const unsigned long DURACION_MAX_HUECO_MS = 150; // ...más corto que esto (si dura más, es una loma/rampa)
const float UMBRAL_CURVA_G     = 0.40;       // aceleración lateral
const unsigned long DURACION_CURVA_MS = 300;
const unsigned long ANTIRREBOTE_MS = 2000;   // después de un evento, 2 s sin repetir el mismo tipo

// Luz (ADC de 12 bits: 0 = oscuro ... 4095 = mucha luz, con la LDR arriba y 10 kΩ a tierra)
const int LUZ_ABRE   = 400;                  // por encima: carga ABIERTA
const int LUZ_CIERRA = 250;                  // por debajo: carga CERRADA (histéresis entre ambos)
const unsigned long CONFIRMAR_LUZ_MS = 500;  // el cambio debe sostenerse este tiempo

const unsigned long LATIDO_MS = 10000;
const unsigned long MUESTREO_MS = 10;        // 100 muestras por segundo

// Ejes del MPU-6050 según el montaje (ver arriba)
const int EJE_LONGITUDINAL = 0;              // 0 = X, 1 = Y, 2 = Z
const int EJE_LATERAL = 1;
const int EJE_VERTICAL = 2;
const float SIGNO_LONGITUDINAL = 1.0;        // +1 si X apunta hacia adelante
// ========================================================================================

const uint8_t MPU = 0x68;
BluetoothSerial bt;

float offset[3] = {0, 0, 0};                 // desfase medido con CERO
float filtrado[3] = {0, 0, 1};               // filtro pasa-bajas sencillo
bool mpuOk = false;

long seq = 0;
long envioActual = 0;                        // 0 = sin viaje
bool conectado = false;
bool modoCal = false;

bool luzAbierta = false;
int nivelLuz = 0;
unsigned long cambioLuzDesde = 0;

unsigned long ultimoEvento[3] = {0, 0, 0};   // FRENADA, HUECO, CURVA
unsigned long frenadaDesde = 0, curvaDesde = 0, huecoDesde = 0;
float picoFrenada = 0, picoCurva = 0, picoHueco = 0;
unsigned long ultimoLatido = 0, ultimaMuestra = 0;

String bufferBt = "", bufferSerie = "";

// ---------------------------------------------------------------- utilidades
void enviar(const String& linea) {
  Serial.println("-> " + linea);
  if (bt.hasClient()) {
    bt.print(linea);
    bt.print('\n');
  }
}

String campo(const String& linea, const String& nombre) {
  int i = linea.indexOf(nombre + ":");
  if (i < 0) return "";
  int fin = linea.indexOf(';', i);
  return linea.substring(i + nombre.length() + 1, fin < 0 ? linea.length() : fin);
}

// ---------------------------------------------------------------- MPU-6050
bool iniciarMpu() {
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  Wire.beginTransmission(MPU);
  Wire.write(0x6B); Wire.write(0x00);        // despertar (PWR_MGMT_1 = 0)
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(MPU);
  Wire.write(0x1C); Wire.write(0x08);        // acelerómetro a ±4 g (8192 LSB/g)
  Wire.endTransmission();
  Wire.beginTransmission(MPU);
  Wire.write(0x1A); Wire.write(0x03);        // filtro digital interno ~44 Hz
  Wire.endTransmission();
  return true;
}

bool leerAcel(float g[3]) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)MPU, 6) != 6) return false;
  for (int i = 0; i < 3; i++) {
    int16_t crudo = (Wire.read() << 8) | Wire.read();
    g[i] = crudo / 8192.0 - offset[i];
  }
  return true;
}

void medirCero() {
  Serial.println("Midiendo el cero: deje el sensor QUIETO 2 s...");
  float suma[3] = {0, 0, 0};
  float g[3];
  float viejo[3] = {offset[0], offset[1], offset[2]};
  offset[0] = offset[1] = offset[2] = 0;
  int n = 0;
  for (int k = 0; k < 200; k++) {
    if (leerAcel(g)) { for (int i = 0; i < 3; i++) suma[i] += g[i]; n++; }
    delay(10);
  }
  if (n < 100) {
    for (int i = 0; i < 3; i++) offset[i] = viejo[i];
    Serial.println("No se pudo leer el MPU-6050. Revise las conexiones.");
    return;
  }
  for (int i = 0; i < 3; i++) offset[i] = suma[i] / n;
  offset[EJE_VERTICAL] -= 1.0;               // en reposo el eje vertical debe marcar 1 g
  Serial.printf("Cero guardado: x=%.3f y=%.3f z=%.3f (g)\n", offset[0], offset[1], offset[2]);
}

// ---------------------------------------------------------------- eventos de movimiento
void evento(int tipo, const char* nombre, float g) {
  unsigned long t = millis();
  if (t - ultimoEvento[tipo] < ANTIRREBOTE_MS) return;
  ultimoEvento[tipo] = t;
  if (envioActual == 0) {                    // sin viaje no se manda (solo se muestra)
    Serial.printf("(sin viaje) %s de %.2f g\n", nombre, g);
    return;
  }
  enviar(String("EVENTO;TIPO:") + nombre + ";G:" + String(g, 2) + ";SEQ:" + String(++seq));
}

void revisarMovimiento() {
  float g[3];
  if (!mpuOk || !leerAcel(g)) return;
  for (int i = 0; i < 3; i++) filtrado[i] = 0.7 * filtrado[i] + 0.3 * g[i];
  float longitudinal = SIGNO_LONGITUDINAL * filtrado[EJE_LONGITUDINAL];
  float lateral = fabs(filtrado[EJE_LATERAL]);
  float vertical = fabs(g[EJE_VERTICAL] - 1.0);   // el hueco usa la señal SIN filtrar (es un golpe corto)
  unsigned long t = millis();

  if (modoCal) return;

  // Frenada: aceleración hacia atrás de más de 0,40 g durante 200 ms o más.
  if (-longitudinal > UMBRAL_FRENADA_G) {
    if (frenadaDesde == 0) { frenadaDesde = t; picoFrenada = 0; }
    picoFrenada = max(picoFrenada, -longitudinal);
  } else if (frenadaDesde != 0) {
    if (t - frenadaDesde >= DURACION_FRENADA_MS) evento(0, "FRENADA", picoFrenada);
    frenadaDesde = 0;
  }
  // Hueco: pico vertical de más de 0,8 g y de menos de 150 ms.
  if (vertical > UMBRAL_HUECO_G) {
    if (huecoDesde == 0) { huecoDesde = t; picoHueco = 0; }
    picoHueco = max(picoHueco, vertical);
  } else if (huecoDesde != 0) {
    if (t - huecoDesde <= DURACION_MAX_HUECO_MS) evento(1, "HUECO", picoHueco);
    huecoDesde = 0;
  }
  // Curva: aceleración lateral de más de 0,40 g sostenida.
  if (lateral > UMBRAL_CURVA_G) {
    if (curvaDesde == 0) { curvaDesde = t; picoCurva = 0; }
    picoCurva = max(picoCurva, lateral);
  } else if (curvaDesde != 0) {
    if (t - curvaDesde >= DURACION_CURVA_MS) evento(2, "CURVA", picoCurva);
    curvaDesde = 0;
  }
}

// ---------------------------------------------------------------- luz (con histéresis)
void mandarLuz() {
  enviar(String("LUZ;ESTADO:") + (luzAbierta ? "ABIERTA" : "CERRADA") + ";NIVEL:" + String(nivelLuz) + ";SEQ:" + String(++seq));
}

void revisarLuz() {
  // Promedio de 8 lecturas para quitar ruido.
  long suma = 0;
  for (int i = 0; i < 8; i++) suma += analogRead(PIN_LDR);
  nivelLuz = suma / 8;
  bool pideCambio = luzAbierta ? (nivelLuz < LUZ_CIERRA) : (nivelLuz > LUZ_ABRE);
  unsigned long t = millis();
  if (!pideCambio) { cambioLuzDesde = 0; return; }
  if (cambioLuzDesde == 0) { cambioLuzDesde = t; return; }
  if (t - cambioLuzDesde < CONFIRMAR_LUZ_MS) return;
  cambioLuzDesde = 0;
  luzAbierta = !luzAbierta;
  if (envioActual != 0) mandarLuz();         // sin viaje solo se informa en el latido
  else Serial.printf("(sin viaje) carga %s, nivel %d\n", luzAbierta ? "ABIERTA" : "CERRADA", nivelLuz);
}

// ---------------------------------------------------------------- mensajes
void atenderBt(const String& linea) {
  Serial.println("<- " + linea);
  String tipo = linea;
  int pc = tipo.indexOf(';');
  if (pc >= 0) tipo = tipo.substring(0, pc);
  tipo.trim();
  tipo.toUpperCase();
  if (tipo == "PING") {
    enviar("PONG");
  } else if (tipo == "INICIO") {
    envioActual = campo(linea, "ENVIO").toInt();
    if (envioActual <= 0) envioActual = -1;  // viaje sin número: igual se mandan eventos
    enviar("HOLA;DISP:SENSORES;VER:1");
    mandarLuz();                             // estado actual (por si ya estaba abierta)
  } else if (tipo == "FIN") {
    envioActual = 0;
  } else if (tipo == "HOLA") {
    enviar("HOLA;DISP:SENSORES;VER:1");
  }
}

void atenderSerie(String linea) {
  linea.trim();
  linea.toUpperCase();
  if (linea == "CAL") {
    modoCal = !modoCal;
    Serial.println(modoCal ? "Calibración: ax ay az (g) y luz cada 100 ms. CAL para salir." : "Fin de la calibración.");
  } else if (linea == "CERO") {
    medirCero();
  } else if (linea == "UMBRALES") {
    Serial.printf("Frenada > %.2f g por %lu ms · Hueco > %.2f g en < %lu ms · Curva > %.2f g por %lu ms\n",
                  UMBRAL_FRENADA_G, DURACION_FRENADA_MS, UMBRAL_HUECO_G, DURACION_MAX_HUECO_MS,
                  UMBRAL_CURVA_G, DURACION_CURVA_MS);
    Serial.printf("Luz: abre > %d, cierra < %d (ahora %d, carga %s)\n", LUZ_ABRE, LUZ_CIERRA, nivelLuz,
                  luzAbierta ? "ABIERTA" : "CERRADA");
  } else if (linea == "PRUEBA") {
    long antes = envioActual;
    if (envioActual == 0) envioActual = -1;
    ultimoEvento[0] = 0;
    evento(0, "FRENADA", 0.55);
    envioActual = antes;
  } else if (linea.length()) {
    Serial.println("Comandos: CAL, CERO, UMBRALES, PRUEBA");
  }
}

// ---------------------------------------------------------------- arranque y bucle
void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_LDR, ADC_11db);   // rango completo 0-3,3 V
  mpuOk = iniciarMpu();
  Serial.println(mpuOk ? "MPU-6050 listo." : "ATENCIÓN: no responde el MPU-6050 (revise SDA 21, SCL 22, 3V3 y GND).");
  if (mpuOk) medirCero();
  bt.begin(NOMBRE_BT);
  Serial.printf("Bluetooth listo como '%s'. Vincúlelo desde el celular.\n", NOMBRE_BT);
  revisarLuz();
}

void loop() {
  unsigned long t = millis();

  // Conexión del celular
  bool ahora = bt.hasClient();
  if (ahora != conectado) {
    conectado = ahora;
    digitalWrite(PIN_LED, conectado ? HIGH : LOW);
    Serial.println(conectado ? "Celular conectado." : "Celular desconectado.");
    if (conectado) enviar("HOLA;DISP:SENSORES;VER:1");
    // Si el celular se desconecta en pleno viaje NO se borra el viaje: al reconectar sigue igual.
  }

  // Mensajes del celular y del monitor serie
  while (bt.available()) {
    char c = bt.read();
    if (c == '\n') { bufferBt.trim(); if (bufferBt.length()) atenderBt(bufferBt); bufferBt = ""; }
    else if (c != '\r' && bufferBt.length() < 120) bufferBt += c;
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { atenderSerie(bufferSerie); bufferSerie = ""; }
    else if (c != '\r' && bufferSerie.length() < 40) bufferSerie += c;
  }

  // Sensores
  if (t - ultimaMuestra >= MUESTREO_MS) {
    ultimaMuestra = t;
    revisarMovimiento();
    static unsigned long ultimaLuz = 0;
    if (t - ultimaLuz >= 100) { ultimaLuz = t; revisarLuz(); }
    static unsigned long ultimaCal = 0;
    if (modoCal && t - ultimaCal >= 100) {
      ultimaCal = t;
      Serial.printf("ax=%+.2f ay=%+.2f az=%+.2f  luz=%4d\n", filtrado[0], filtrado[1], filtrado[2], nivelLuz);
    }
  }

  // Latido cada 10 s
  if (t - ultimoLatido >= LATIDO_MS) {
    ultimoLatido = t;
    enviar(String("LATIDO;LUZ:") + String(nivelLuz) + ";ESTADO:" + (luzAbierta ? "ABIERTA" : "CERRADA"));
  }
}
