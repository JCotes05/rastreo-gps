/*
 * esp32_sensores.ino — Orbita P3-S3 · ESP32 de sensores de la carga
 * Versión del firmware: 1.0  (protocolo VER:1, el mismo que ya entienden la app y el servidor)
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * QUÉ HACE
 *   Va en la zona de la carga del camión. No usa Wi-Fi: habla por Bluetooth
 *   clásico (SPP, con BluetoothSerial) con el celular del conductor, y el celular
 *   reenvía todo al servidor con su GPS y la hora.
 *     - GY-521 (MPU-6050) por I2C ........ frenadas bruscas, huecos y curvas bruscas.
 *     - Fotoresistencia (LDR) en GPIO 34 .. luz dentro de la carga = carga ABIERTA.
 *
 * PLACA
 *   ESP32 DevKit normal (ESP32-WROOM-32). NO sirve en ESP32-S3/C3 (no tienen
 *   Bluetooth clásico) y NO es para el ESP32-CAM.
 *   Arduino IDE: placa "ESP32 Dev Module", núcleo "esp32 by Espressif" 3.x
 *   (compilado sin errores con la 3.3.11; con el esquema de particiones por
 *   defecto ocupa ~84 % de la memoria de programa, así que no hay que cambiarlo).
 *
 * CONEXIONES (ver LEEME.txt para el diagrama)
 *   GY-521: VCC→3V3, GND→GND, SDA→GPIO 21, SCL→GPIO 22 (AD0 sin conectar = 0x68).
 *   LDR:    3V3 ─[LDR]─┬─[10 kΩ]─ GND, y el punto del medio ─→ GPIO 34 (ADC1).
 *
 * PROTOCOLO (NO CAMBIAR: la app y el servidor ya lo usan)
 *   Una línea por mensaje, terminada en \n; campos CLAVE:valor separados por ';'.
 *   Del celular al ESP:
 *     PING               → PONG
 *     INICIO;ENVIO:<id>  → empieza el viaje: responde HOLA y el estado actual de la LUZ;
 *                          desde aquí se mandan EVENTO y los cambios de LUZ.
 *     FIN                → termina el viaje: deja de mandar EVENTO y LUZ.
 *   Del ESP al celular:
 *     HOLA;DISP:SENSORES;VER:1                       al conectarse un cliente
 *     EVENTO;TIPO:FRENADA;G:0.62;SEQ:<n>             G = desaceleración máxima (g)
 *     EVENTO;TIPO:HUECO;G:1.10;SEQ:<n>               G = |az − 1 g| máximo
 *     EVENTO;TIPO:CURVA;G:0.48;SEQ:<n>               G = aceleración lateral máxima
 *     LUZ;ESTADO:ABIERTA|CERRADA;NIVEL:<0-4095>;SEQ:<n>   solo cuando cambia (con histéresis)
 *     LATIDO;LUZ:<nivel>;ESTADO:<ABIERTA|CERRADA>    cada 10 s con un cliente conectado
 *   SEQ sube en 1 con cada EVENTO o LUZ que se manda.
 *   Sin viaje (antes de INICIO o después de FIN) no se mandan EVENTO ni LUZ:
 *   solo se muestran en el monitor serie, igual que el simulador en Python.
 *
 * MONITOR SERIE (115200 baudios, "Nueva línea")
 *   1) Modo prueba sin celular: escriba los mismos mensajes del celular
 *      (PING, INICIO;ENVIO:1, FIN) y verá las respuestas y los eventos.
 *      Las líneas del protocolo salen tal cual; los avisos para personas
 *      empiezan con "# " para no confundirlos.
 *   2) Calibración: AYUDA, CAL, VER, UMBRAL, EJE, RECAL, OSCURO, CLARO,
 *      PRUEBA, FABRICA. Los umbrales se guardan en la memoria (Preferences)
 *      y sobreviven al apagado: no hay que volver a compilar.
 *
 * LIBRERÍAS: solo las que trae la placa (Wire, BluetoothSerial, Preferences).
 * Sin Wi-Fi y sin contraseñas.
 * ─────────────────────────────────────────────────────────────────────────────
 */

#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <stdarg.h>
#include <math.h>
#include "BluetoothSerial.h"
#include "esp_gap_bt_api.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error "El Bluetooth no está habilitado. En Arduino IDE elija la placa 'ESP32 Dev Module' (ESP32 normal, no S3/C3)."
#endif
#if !defined(CONFIG_BT_SPP_ENABLED)
#error "Bluetooth SPP no disponible: esta placa no tiene Bluetooth clásico. Use un ESP32 normal (WROOM)."
#endif

// ═════════════════════════════ CONFIGURACIÓN FIJA ═════════════════════════════
const char* NOMBRE_BT  = "ORBITA-SENSORES";   // así aparece en el Bluetooth del celular
const char* VERSION_FW = "1.0";

// Pines
const int PIN_SDA = 21;
const int PIN_SCL = 22;
const int PIN_LDR = 34;            // ADC1 canal 6. Los pines del ADC2 NO sirven con la radio encendida.
const int PIN_LED = 2;             // LED azul del DevKit: fijo = celular conectado; parpadea = calibrando

// Tiempos
const uint32_t LATIDO_MS        = 10000;  // LATIDO cada 10 s
const uint32_t MUESTREO_MS      = 5;      // acelerómetro a 200 muestras por segundo
const uint32_t LUZ_CADA_MS      = 50;     // la luz se lee 20 veces por segundo
const uint32_t CAL_CADA_MS      = 100;    // en modo CAL se imprime 10 veces por segundo
const uint32_t REINTENTO_MPU_MS = 3000;   // si el GY-521 no responde, se reintenta cada 3 s
const uint32_t EPISODIO_MAX_MS  = 2000;   // una frenada/curva que dura más se reporta igual a los 2 s

// Filtros (media móvil)
const int MEDIA_HORIZONTAL = 10;   // 10 muestras × 5 ms = 50 ms para frenada y curva
const int MEDIA_VERTICAL   = 2;    // 2 muestras = 10 ms para el hueco (es un golpe corto, no se puede suavizar mucho)
const float HISTERESIS_SALIDA = 0.85;  // un episodio termina cuando baja del 85 % del umbral (evita cortes por ruido)

// Calibración de la gravedad al arrancar
const uint32_t CALIB_MS       = 2000;  // 2 s quieto
const float    CALIB_RANGO_G  = 0.06;  // si algún eje varía más que esto, se movió: se vuelve a empezar
const int      CALIB_INTENTOS = 5;     // después de 5 intentos se acepta igual (con aviso)

// MPU-6050
const uint8_t MPU_DIR       = 0x68;
const float   MPU_LSB_POR_G = 4096.0;  // rango ±8 g (los golpes en la mesa pasan de 4 g)

// ═══════════════ UMBRALES (por defecto; se cambian con UMBRAL y se guardan) ═══════════════
// Valores iniciales pedidos en el proyecto. Después de calibrar, anote los suyos en LEEME.txt.
float uFrenadaG     = 0.40;   // frenada: aceleración longitudinal por debajo de −0,40 g...
float uFrenadaMs    = 200;    // ...durante 200 ms o más
float uHuecoG       = 0.80;   // hueco: pico vertical |az − 1 g| de más de 0,8 g...
float uHuecoMs      = 150;    // ...que dure menos de 150 ms (si dura más es una loma o una rampa)
float uCurvaG       = 0.40;   // curva: aceleración lateral de más de 0,40 g...
float uCurvaMs      = 100;    // ...sostenida al menos 100 ms (filtra golpes laterales)
float uAntirrebote  = 2000;   // 2 s sin repetir el mismo tipo de evento
float uLuzAbre      = 400;    // nivel (0-4095) por ENCIMA del cual la carga pasa a ABIERTA
float uLuzCierra    = 250;    // nivel por DEBAJO del cual vuelve a CERRADA (entre ambos: histéresis)
float uLuzMs        = 300;    // el cambio de luz debe sostenerse 300 ms (evita destellos)
int8_t ejeAdelante  = 1;      // eje del módulo que apunta hacia ADELANTE: 1=X+ -1=X- 2=Y+ -2=Y- 3=Z+ -3=Z-

struct Parametro {
  const char* nombre;   // como se escribe en el comando UMBRAL
  const char* clave;    // clave en Preferences (máx. 15 letras)
  float* valor;
  float defecto, minimo, maximo;
  const char* unidad;
  const char* ayuda;
};

Parametro PARAMETROS[] = {
  {"FRENADA",        "frenada_g",  &uFrenadaG,    0.40, 0.05, 4.0,   "g",  "desaceleracion minima"},
  {"FRENADA_MS",     "frenada_ms", &uFrenadaMs,   200,  20,   2000,  "ms", "duracion minima de la frenada"},
  {"HUECO",          "hueco_g",    &uHuecoG,      0.80, 0.10, 6.0,   "g",  "pico vertical minimo |az-1g|"},
  {"HUECO_MS",       "hueco_ms",   &uHuecoMs,     150,  10,   1000,  "ms", "duracion maxima del golpe"},
  {"CURVA",          "curva_g",    &uCurvaG,      0.40, 0.05, 4.0,   "g",  "aceleracion lateral minima"},
  {"CURVA_MS",       "curva_ms",   &uCurvaMs,     100,  0,    2000,  "ms", "duracion minima de la curva"},
  {"ANTIRREBOTE_MS", "antirrebote",&uAntirrebote, 2000, 0,    60000, "ms", "pausa entre eventos del mismo tipo"},
  {"LUZ_ABRE",       "luz_abre",   &uLuzAbre,     400,  1,    4095,  "",   "nivel para ABIERTA"},
  {"LUZ_CIERRA",     "luz_cierra", &uLuzCierra,   250,  0,    4094,  "",   "nivel para CERRADA"},
  {"LUZ_MS",         "luz_ms",     &uLuzMs,       300,  0,    5000,  "ms", "tiempo que debe durar el cambio de luz"},
};
const int N_PARAMETROS = sizeof(PARAMETROS) / sizeof(PARAMETROS[0]);

// ═════════════════════════════ ESTADO DEL PROGRAMA ═════════════════════════════
BluetoothSerial bt;
Preferences prefs;

// Canales por donde sale un mensaje del protocolo
const uint8_t CANAL_SERIE = 1;
const uint8_t CANAL_BT    = 2;
const uint8_t AMBOS       = CANAL_SERIE | CANAL_BT;

bool btIniciado = false;
bool btConectado = false;
uint32_t ultimoIntentoBt = 0;

bool viajeActivo = false;
String envioActual = "";
uint32_t seq = 0;

// Acelerómetro
bool mpuOk = false;
uint8_t mpuQuienSoy = 0;
int fallosSeguidos = 0;
uint32_t ultimoIntentoMpu = 0;
float acel[3] = {0, 0, 1};          // última lectura cruda, en g, ejes del módulo

// Gravedad calibrada y ejes del camión
bool calibrando = false;
bool gravedadOk = false;
int intentosCalib = 0;
uint32_t calibDesde = 0;
long calibN = 0;
float calibSuma[3], calibMin[3], calibMax[3];
float gravedad[3] = {0, 0, 1};      // vector medido en reposo (apunta hacia ARRIBA)
float gNorma = 1.0;                 // su tamaño (≈ 1 g)
float arriba[3]   = {0, 0, 1};      // vector unitario vertical
float adelante[3] = {1, 0, 0};      // vector unitario hacia adelante (perpendicular a "arriba")
float izquierda[3] = {0, 1, 0};     // vector unitario lateral
int8_t ejeUsado = 1;                // eje adelante que se está usando de verdad (puede diferir si el elegido quedó vertical)

// Medias móviles
float bufLon[MEDIA_HORIZONTAL], bufLat[MEDIA_HORIZONTAL], bufVer[MEDIA_VERTICAL];
int posH = 0, posV = 0, llenosH = 0, llenosV = 0;
float lonF = 0, latF = 0, verF = 0;  // señales filtradas, en g, SIN la gravedad

// Episodios (un "episodio" es el rato en que la señal pasa el umbral)
struct Episodio {
  bool activo = false;
  bool reportado = false;
  uint32_t desde = 0;
  float pico = 0;
};
Episodio epFrenada, epHueco, epCurva;
enum TipoEvento { FRENADA = 0, HUECO = 1, CURVA = 2 };
const char* NOMBRE_EVENTO[3] = {"FRENADA", "HUECO", "CURVA"};
uint32_t ultimoEvento[3] = {0, 0, 0};
bool huboEvento[3] = {false, false, false};

// Luz
int nivelLuz = 0;
bool luzAbierta = false;
uint32_t cambioLuzDesde = 0;
int nivelOscuro = -1, nivelClaro = -1;   // medidos con OSCURO y CLARO

// Monitor serie
bool modoCal = false;
String bufSerie = "", bufBt = "";
uint32_t ultimoCaracterSerie = 0;

uint32_t ultimaMuestra = 0, ultimaLuz = 0, ultimaCal = 0, ultimoLatido = 0;

// ═════════════════════════════ UTILIDADES ═════════════════════════════
// Aviso para personas (solo monitor serie). Empieza con "# " para no confundirse con el protocolo.
void info(const char* formato, ...) {
  char texto[220];
  va_list args;
  va_start(args, formato);
  vsnprintf(texto, sizeof(texto), formato, args);
  va_end(args);
  Serial.print("# ");
  Serial.println(texto);
}

// Manda una línea del protocolo por los canales indicados.
void enviar(const String& linea, uint8_t canales) {
  if (canales & CANAL_SERIE) Serial.println(linea);
  if ((canales & CANAL_BT) && btIniciado && bt.hasClient()) {
    bt.print(linea);
    bt.print('\n');
  }
}

// Saca el valor de un campo "CLAVE:valor" de una línea del protocolo.
String campo(const String& linea, const char* nombre) {
  String buscado = String(";") + nombre + ":";
  String l = String(";") + linea;
  String mayus = l;
  mayus.toUpperCase();                                 // acepta "envio:7" y "ENVIO:7"
  int i = mayus.indexOf(buscado);
  if (i < 0) return "";
  int inicio = i + buscado.length();
  int fin = l.indexOf(';', inicio);
  String v = l.substring(inicio, fin < 0 ? l.length() : fin);
  v.trim();
  return v;
}

const char* textoEje(int8_t e) {
  switch (e) {
    case 1: return "X+"; case -1: return "X-";
    case 2: return "Y+"; case -2: return "Y-";
    case 3: return "Z+"; case -3: return "Z-";
  }
  return "?";
}

float punto(const float a[3], const float b[3]) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

const char* textoLuz() { return luzAbierta ? "ABIERTA" : "CERRADA"; }

// ═════════════════════════════ PREFERENCIAS ═════════════════════════════
void cargarAjustes() {
  prefs.begin("orbita-sens", true);   // solo lectura
  for (int i = 0; i < N_PARAMETROS; i++) {
    *PARAMETROS[i].valor = prefs.getFloat(PARAMETROS[i].clave, PARAMETROS[i].defecto);
  }
  ejeAdelante = prefs.getChar("eje", 1);
  prefs.end();
  if (uLuzAbre <= uLuzCierra) {        // por si quedó algo inválido guardado
    uLuzAbre = 400; uLuzCierra = 250;
  }
  if (ejeAdelante == 0 || ejeAdelante < -3 || ejeAdelante > 3) ejeAdelante = 1;
}

void guardarParametro(const Parametro& p) {
  prefs.begin("orbita-sens", false);
  prefs.putFloat(p.clave, *p.valor);
  prefs.end();
}

void guardarEje() {
  prefs.begin("orbita-sens", false);
  prefs.putChar("eje", ejeAdelante);
  prefs.end();
}

void valoresDeFabrica() {
  prefs.begin("orbita-sens", false);
  prefs.clear();
  prefs.end();
  for (int i = 0; i < N_PARAMETROS; i++) *PARAMETROS[i].valor = PARAMETROS[i].defecto;
  ejeAdelante = 1;
  calcularEjes();
  info("Valores de fabrica restaurados (y borrados de la memoria).");
}

// ═════════════════════════════ MPU-6050 (GY-521) ═════════════════════════════
bool escribirMpu(uint8_t registro, uint8_t valor) {
  Wire.beginTransmission(MPU_DIR);
  Wire.write(registro);
  Wire.write(valor);
  return Wire.endTransmission() == 0;
}

bool leerRegistroMpu(uint8_t registro, uint8_t& valor) {
  Wire.beginTransmission(MPU_DIR);
  Wire.write(registro);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_DIR, (uint8_t)1) != 1) return false;
  valor = Wire.read();
  return true;
}

bool iniciarMpu() {
  uint8_t quien = 0;
  if (!leerRegistroMpu(0x75, quien)) return false;     // WHO_AM_I
  mpuQuienSoy = quien;
  if (!escribirMpu(0x6B, 0x80)) return false;          // reinicio interno del sensor
  delay(100);
  if (!escribirMpu(0x6B, 0x01)) return false;          // despertar, reloj del giroscopio X (más estable)
  delay(10);
  bool ok = escribirMpu(0x1C, 0x10)                    // acelerómetro a ±8 g
         && escribirMpu(0x1A, 0x03)                    // filtro digital interno ≈ 44 Hz
         && escribirMpu(0x19, 0x04);                   // muestreo interno 200 Hz
  uint8_t verifica = 0;
  if (!ok || !leerRegistroMpu(0x1C, verifica) || verifica != 0x10) return false;
  return true;
}

bool leerAcelerometro() {
  Wire.beginTransmission(MPU_DIR);
  Wire.write(0x3B);                                    // ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_DIR, (uint8_t)6) != 6) return false;
  for (int i = 0; i < 3; i++) {
    int16_t alto = Wire.read();
    int16_t bajo = Wire.read();
    int16_t crudo = (int16_t)((alto << 8) | (bajo & 0xFF));
    acel[i] = crudo / MPU_LSB_POR_G;
  }
  return true;
}

void intentarMpu() {
  ultimoIntentoMpu = millis();
  if (iniciarMpu()) {
    mpuOk = true;
    fallosSeguidos = 0;
    if (mpuQuienSoy == 0x68) info("GY-521 listo (MPU-6050, WHO_AM_I=0x68).");
    else info("GY-521 responde con WHO_AM_I=0x%02X (no es 0x68: puede ser un clon MPU-6500). Se usa igual.", mpuQuienSoy);
    empezarCalibracion();
  } else {
    mpuOk = false;
    info("ATENCION: el GY-521 no responde. Revise VCC=3V3, GND, SDA=GPIO21, SCL=GPIO22. La luz sigue funcionando; reintento cada %lu s.",
         (unsigned long)(REINTENTO_MPU_MS / 1000));
  }
}

// ═════════════════════════════ GRAVEDAD Y EJES ═════════════════════════════
void empezarCalibracion() {
  calibrando = true;
  calibDesde = millis();
  calibN = 0;
  for (int i = 0; i < 3; i++) { calibSuma[i] = 0; calibMin[i] = 99; calibMax[i] = -99; }
  if (intentosCalib == 0) info("Calibrando la gravedad: deje el sensor QUIETO 2 s...");
}

// Índice (0=X, 1=Y, 2=Z) del eje del módulo que quedó más vertical.
int indiceVertical() {
  int v = 0;
  for (int i = 1; i < 3; i++) if (fabsf(gravedad[i]) > fabsf(gravedad[v])) v = i;
  return v;
}

// Con la gravedad medida, arma los ejes del camión: arriba, adelante e izquierda.
void calcularEjes() {
  int indice = abs(ejeAdelante) - 1;
  float signo = ejeAdelante > 0 ? 1.0 : -1.0;
  float base[3] = {0, 0, 0};
  base[indice] = signo;
  // Se quita de "base" la parte vertical, para que "adelante" quede horizontal.
  float p = punto(base, arriba);
  float f[3] = {base[0] - p * arriba[0], base[1] - p * arriba[1], base[2] - p * arriba[2]};
  float n = sqrtf(punto(f, f));
  ejeUsado = ejeAdelante;
  if (n < 0.5) {
    // El eje elegido está casi vertical: se usa por ahora el eje horizontal más cercano.
    int mejor = 0;
    for (int i = 1; i < 3; i++) if (fabsf(arriba[i]) < fabsf(arriba[mejor])) mejor = i;
    float b2[3] = {0, 0, 0};
    b2[mejor] = 1;
    p = punto(b2, arriba);
    for (int i = 0; i < 3; i++) f[i] = b2[i] - p * arriba[i];
    n = sqrtf(punto(f, f));
    ejeUsado = mejor + 1;
    int otro = 3 - indiceVertical() - mejor;           // el tercer eje (también horizontal)
    info("ATENCION: el eje adelante %s quedo casi vertical. Por ahora uso %s. Corrija con EJE %s, EJE %s (o con signo -) segun hacia donde apunte la flecha.",
         textoEje(ejeAdelante), textoEje(ejeUsado), textoEje(mejor + 1), textoEje(otro + 1));
  }
  for (int i = 0; i < 3; i++) adelante[i] = f[i] / n;
  // izquierda = arriba × adelante
  izquierda[0] = arriba[1] * adelante[2] - arriba[2] * adelante[1];
  izquierda[1] = arriba[2] * adelante[0] - arriba[0] * adelante[2];
  izquierda[2] = arriba[0] * adelante[1] - arriba[1] * adelante[0];
}

void terminarCalibracion() {
  calibrando = false;
  for (int i = 0; i < 3; i++) gravedad[i] = calibSuma[i] / calibN;
  gNorma = sqrtf(punto(gravedad, gravedad));
  if (gNorma < 0.5) gNorma = 1.0;                      // lectura absurda: se evita dividir por casi cero
  for (int i = 0; i < 3; i++) arriba[i] = gravedad[i] / gNorma;
  gravedadOk = true;
  calcularEjes();
  // Reinicia filtros y episodios con la nueva referencia.
  llenosH = llenosV = posH = posV = 0;
  epFrenada = Episodio(); epHueco = Episodio(); epCurva = Episodio();
  int vertical = indiceVertical();
  int8_t ejeVert = (vertical + 1) * (gravedad[vertical] >= 0 ? 1 : -1);
  info("Gravedad: x=%+.3f y=%+.3f z=%+.3f g (total %.3f g). Eje vertical: %s. Adelante: %s.",
       gravedad[0], gravedad[1], gravedad[2], gNorma, textoEje(ejeVert), textoEje(ejeUsado));
  if (gNorma < 0.85 || gNorma > 1.15)
    info("ATENCION: la gravedad medida deberia ser cerca de 1,00 g. Revise el modulo o repita con RECAL.");
  intentosCalib = 0;
}

void pasoCalibracion() {
  for (int i = 0; i < 3; i++) {
    calibSuma[i] += acel[i];
    calibMin[i] = min(calibMin[i], acel[i]);
    calibMax[i] = max(calibMax[i], acel[i]);
  }
  calibN++;
  bool seMovio = false;
  for (int i = 0; i < 3; i++) if (calibMax[i] - calibMin[i] > CALIB_RANGO_G) seMovio = true;
  if (seMovio && intentosCalib < CALIB_INTENTOS - 1) {
    intentosCalib++;
    info("Se movio durante la calibracion (intento %d de %d). Dejelo quieto...", intentosCalib, CALIB_INTENTOS);
    empezarCalibracion();
    return;
  }
  if (millis() - calibDesde >= CALIB_MS && calibN > 50) {
    if (seMovio) info("ATENCION: no se logro quietud total; se usa la medida igual. Repita con RECAL si da falsos eventos.");
    terminarCalibracion();
  }
}

// ═════════════════════════════ DETECCIÓN DE EVENTOS ═════════════════════════════
void reportarEvento(TipoEvento tipo, float g) {
  uint32_t ahora = millis();
  if (huboEvento[tipo] && ahora - ultimoEvento[tipo] < (uint32_t)uAntirrebote) {
    if (modoCal) info("(antirrebote) %s de %.2f g ignorada", NOMBRE_EVENTO[tipo], g);
    return;
  }
  huboEvento[tipo] = true;
  ultimoEvento[tipo] = ahora;
  if (g > 16.0) g = 16.0;
  if (!viajeActivo) {
    info("(sin viaje) %s de %.2f g: no se manda; escriba INICIO;ENVIO:1 para probar.", NOMBRE_EVENTO[tipo], g);
    return;
  }
  seq++;
  enviar(String("EVENTO;TIPO:") + NOMBRE_EVENTO[tipo] + ";G:" + String(g, 2) + ";SEQ:" + String(seq), AMBOS);
}

// Frenada y curva: la señal debe pasar el umbral durante un tiempo MÍNIMO.
void revisarSostenido(Episodio& ep, float valor, float umbral, float minimoMs, TipoEvento tipo) {
  uint32_t ahora = millis();
  if (!ep.activo) {
    if (valor > umbral) { ep.activo = true; ep.reportado = false; ep.desde = ahora; ep.pico = valor; }
    return;
  }
  if (valor > ep.pico) ep.pico = valor;
  uint32_t dura = ahora - ep.desde;
  if (valor < umbral * HISTERESIS_SALIDA) {            // terminó el episodio
    if (!ep.reportado && dura >= (uint32_t)minimoMs) reportarEvento(tipo, ep.pico);
    else if (!ep.reportado && modoCal) info("(muy corta) %s de %.2f g duro %lu ms; minimo %.0f ms",
                                            NOMBRE_EVENTO[tipo], ep.pico, (unsigned long)dura, minimoMs);
    ep.activo = false;
  } else if (!ep.reportado && dura >= EPISODIO_MAX_MS) {
    reportarEvento(tipo, ep.pico);                     // muy larga: se reporta ya (y una sola vez)
    ep.reportado = true;
  }
}

// Hueco: pico vertical que debe ser CORTO (más corto que el máximo).
void revisarHueco(float valor) {
  uint32_t ahora = millis();
  Episodio& ep = epHueco;
  if (!ep.activo) {
    if (valor > uHuecoG) { ep.activo = true; ep.desde = ahora; ep.pico = valor; }
    return;
  }
  if (valor > ep.pico) ep.pico = valor;
  if (valor < uHuecoG * HISTERESIS_SALIDA) {
    uint32_t dura = ahora - ep.desde;
    if (dura <= (uint32_t)uHuecoMs) reportarEvento(HUECO, ep.pico);
    else if (modoCal) info("(no es hueco) pico de %.2f g duro %lu ms; maximo %.0f ms",
                           ep.pico, (unsigned long)dura, uHuecoMs);
    ep.activo = false;
  }
}

float media(float* buf, int llenos) {
  float s = 0;
  for (int i = 0; i < llenos; i++) s += buf[i];
  return llenos ? s / llenos : 0;
}

void procesarMuestra() {
  // Aceleración sin la gravedad, proyectada en los ejes del camión.
  float d[3] = {acel[0] - gravedad[0], acel[1] - gravedad[1], acel[2] - gravedad[2]};
  float lon = punto(d, adelante);                     // + acelera, − frena
  float lat = punto(d, izquierda);                    // + izquierda, − derecha
  float ver = punto(acel, arriba) - gNorma;           // ≈ az − 1 g

  bufLon[posH] = lon; bufLat[posH] = lat;
  posH = (posH + 1) % MEDIA_HORIZONTAL;
  if (llenosH < MEDIA_HORIZONTAL) llenosH++;
  bufVer[posV] = ver;
  posV = (posV + 1) % MEDIA_VERTICAL;
  if (llenosV < MEDIA_VERTICAL) llenosV++;

  lonF = media(bufLon, llenosH);
  latF = media(bufLat, llenosH);
  verF = media(bufVer, llenosV);
  if (llenosH < MEDIA_HORIZONTAL) return;             // espera a llenar el filtro

  revisarSostenido(epFrenada, -lonF, uFrenadaG, uFrenadaMs, FRENADA);
  revisarSostenido(epCurva, fabsf(latF), uCurvaG, uCurvaMs, CURVA);
  revisarHueco(fabsf(verF));
}

void muestrearMovimiento() {
  if (!mpuOk) {
    if (millis() - ultimoIntentoMpu >= REINTENTO_MPU_MS) intentarMpu();
    return;
  }
  if (!leerAcelerometro()) {
    if (++fallosSeguidos >= 20) {                      // 100 ms sin lecturas: se perdió el sensor
      mpuOk = false;
      gravedadOk = false;
      calibrando = false;
      ultimoIntentoMpu = millis();
      info("ATENCION: se perdio el GY-521 (¿cable flojo?). Sigo con la luz y reintento cada %lu s.",
           (unsigned long)(REINTENTO_MPU_MS / 1000));
    }
    return;
  }
  fallosSeguidos = 0;
  if (calibrando) { pasoCalibracion(); return; }
  if (gravedadOk) procesarMuestra();
}

// ═════════════════════════════ LUZ ═════════════════════════════
int leerLuz() {
  long suma = 0;
  for (int i = 0; i < 8; i++) suma += analogRead(PIN_LDR);
  return (int)(suma / 8);
}

void enviarLuz(uint8_t canales) {
  seq++;
  enviar(String("LUZ;ESTADO:") + textoLuz() + ";NIVEL:" + String(nivelLuz) + ";SEQ:" + String(seq), canales);
}

void revisarLuz() {
  nivelLuz = leerLuz();
  bool pideCambio = luzAbierta ? (nivelLuz < uLuzCierra) : (nivelLuz > uLuzAbre);
  uint32_t ahora = millis();
  if (!pideCambio) { cambioLuzDesde = 0; return; }
  if (cambioLuzDesde == 0) { cambioLuzDesde = ahora; if (uLuzMs > 0) return; }
  if (ahora - cambioLuzDesde < (uint32_t)uLuzMs) return;
  cambioLuzDesde = 0;
  luzAbierta = !luzAbierta;
  if (viajeActivo) enviarLuz(AMBOS);
  else info("(sin viaje) carga %s, nivel %d: no se manda (solo va en el LATIDO).", textoLuz(), nivelLuz);
}

// ═════════════════════════════ PROTOCOLO ═════════════════════════════
// Atiende un mensaje del celular (o del monitor serie en modo prueba).
// Devuelve false si la línea no es del protocolo.
bool atenderProtocolo(const String& linea, uint8_t origen) {
  String tipo = linea;
  int pc = tipo.indexOf(';');
  if (pc >= 0) tipo = tipo.substring(0, pc);
  tipo.trim();
  tipo.toUpperCase();

  if (tipo == "PING") {
    enviar("PONG", origen);
  } else if (tipo == "INICIO") {
    envioActual = campo(linea, "ENVIO");
    if (envioActual.length() == 0) envioActual = "?";
    viajeActivo = true;
    info("Viaje ENV-%s iniciado: desde ahora se mandan eventos.", envioActual.c_str());
    enviar("HOLA;DISP:SENSORES;VER:1", origen);
    enviarLuz(origen);                                 // estado actual (por si la carga ya estaba abierta)
  } else if (tipo == "FIN") {
    if (viajeActivo) info("Viaje ENV-%s terminado.", envioActual.c_str());
    viajeActivo = false;
    envioActual = "";
  } else if (tipo == "HOLA") {
    enviar("HOLA;DISP:SENSORES;VER:1", origen);
  } else {
    return false;                                      // otros mensajes se ignoran (como el simulador)
  }
  return true;
}

// ═════════════════════════════ COMANDOS DEL MONITOR SERIE ═════════════════════════════
void mostrarAyuda() {
  info("──── Comandos (monitor serie a 115200, 'Nueva linea') ────");
  info("Protocolo (modo prueba): PING | INICIO;ENVIO:1 | FIN | HOLA");
  info("AYUDA            esta lista");
  info("CAL              muestra en vivo ax ay az, lon lat vert (g) y la luz. CAL otra vez para salir");
  info("VER              estado, gravedad, ejes y umbrales");
  info("UMBRAL           lista los umbrales;  UMBRAL <NOMBRE> <valor>  cambia y guarda uno");
  info("                 ej.: UMBRAL FRENADA 0.35   UMBRAL HUECO_MS 120   UMBRAL LUZ_ABRE 600");
  info("EJE <X+|X-|Y+|Y-|Z+|Z->  eje del modulo que apunta hacia ADELANTE (se guarda)");
  info("RECAL            vuelve a medir la gravedad (dejelo quieto 2 s)");
  info("OSCURO / CLARO   mide la luz tapada / destapada y ajusta LUZ_ABRE y LUZ_CIERRA");
  info("PRUEBA <FRENADA|HUECO|CURVA>  manda un evento de prueba (necesita viaje)");
  info("FABRICA          vuelve a los umbrales iniciales");
}

void mostrarUmbrales() {
  for (int i = 0; i < N_PARAMETROS; i++) {
    const Parametro& p = PARAMETROS[i];
    bool entero = strcmp(p.unidad, "g") != 0;
    if (entero) info("  %-15s %8.0f %-2s (%s; inicial %.0f)", p.nombre, *p.valor, p.unidad, p.ayuda, p.defecto);
    else        info("  %-15s %8.2f %-2s (%s; inicial %.2f)", p.nombre, *p.valor, p.unidad, p.ayuda, p.defecto);
  }
  info("  %-15s %8s    (eje del modulo hacia adelante; inicial X+)", "EJE", textoEje(ejeAdelante));
}

void mostrarEstado() {
  info("──── ORBITA sensores · firmware %s · protocolo VER:1 ────", VERSION_FW);
  info("Bluetooth '%s': %s", NOMBRE_BT,
       !btIniciado ? "NO INICIADO (ver avisos)" : (bt.hasClient() ? "celular CONECTADO" : "esperando conexion"));
  info("Viaje: %s · SEQ=%lu", viajeActivo ? (String("ENV-") + envioActual).c_str() : "ninguno", (unsigned long)seq);
  if (!mpuOk) info("GY-521: NO RESPONDE (reintento cada %lu s)", (unsigned long)(REINTENTO_MPU_MS / 1000));
  else if (calibrando) info("GY-521: calibrando la gravedad...");
  else info("GY-521: OK (0x%02X). Gravedad x=%+.3f y=%+.3f z=%+.3f (%.3f g)",
            mpuQuienSoy, gravedad[0], gravedad[1], gravedad[2], gNorma);
  info("Luz: nivel %d -> carga %s (abre > %.0f, cierra < %.0f)", nivelLuz, textoLuz(), uLuzAbre, uLuzCierra);
  if (nivelOscuro >= 0 || nivelClaro >= 0) info("Medidos: oscuro=%d claro=%d", nivelOscuro, nivelClaro);
  info("Encendido hace %lu s", (unsigned long)(millis() / 1000));
  info("Umbrales:");
  mostrarUmbrales();
}

void cambiarUmbral(String resto) {
  resto.trim();
  if (resto.length() == 0) { mostrarUmbrales(); return; }
  int esp = resto.indexOf(' ');
  if (esp < 0) { info("Falta el valor. Ejemplo: UMBRAL FRENADA 0.35"); return; }
  String nombre = resto.substring(0, esp);
  String valorTxt = resto.substring(esp + 1);
  valorTxt.trim();
  valorTxt.replace(',', '.');                          // acepta 0,35 y 0.35
  for (int i = 0; i < N_PARAMETROS; i++) {
    Parametro& p = PARAMETROS[i];
    if (!nombre.equalsIgnoreCase(p.nombre)) continue;
    float v = valorTxt.toFloat();
    if (valorTxt.length() == 0 || (v == 0 && valorTxt[0] != '0') || v < p.minimo || v > p.maximo) {
      info("Valor invalido para %s: use un numero entre %.2f y %.2f", p.nombre, p.minimo, p.maximo);
      return;
    }
    if (&p.valor[0] == &uLuzAbre && v <= uLuzCierra) { info("LUZ_ABRE debe ser MAYOR que LUZ_CIERRA (%.0f).", uLuzCierra); return; }
    if (&p.valor[0] == &uLuzCierra && v >= uLuzAbre) { info("LUZ_CIERRA debe ser MENOR que LUZ_ABRE (%.0f).", uLuzAbre); return; }
    *p.valor = v;
    guardarParametro(p);
    info("%s = %.2f %s (guardado)", p.nombre, v, p.unidad);
    return;
  }
  info("No conozco '%s'. Nombres validos:", nombre.c_str());
  mostrarUmbrales();
}

void cambiarEje(String resto) {
  resto.trim();
  resto.toUpperCase();
  const char* nombres[6] = {"X+", "X-", "Y+", "Y-", "Z+", "Z-"};
  const int8_t valores[6] = {1, -1, 2, -2, 3, -3};
  for (int i = 0; i < 6; i++) {
    if (resto == nombres[i]) {
      ejeAdelante = valores[i];
      guardarEje();
      if (gravedadOk) calcularEjes();
      info("Eje adelante = %s (guardado). Pruebe empujando el modulo en esa direccion con CAL: lon debe subir.", nombres[i]);
      return;
    }
  }
  info("Uso: EJE X+  (o X-, Y+, Y-, Z+, Z-). Es la flecha del modulo que apunta hacia ADELANTE.");
}

void medirLuzReferencia(bool oscuro) {
  long suma = 0;
  for (int i = 0; i < 32; i++) { suma += analogRead(PIN_LDR); delay(3); }
  int nivel = suma / 32;
  if (oscuro) nivelOscuro = nivel; else nivelClaro = nivel;
  info("Luz %s medida: %d", oscuro ? "TAPADA (oscuro)" : "DESTAPADA (claro)", nivel);
  if (nivelOscuro < 0 || nivelClaro < 0) {
    info("Ahora %s la LDR y escriba %s.", oscuro ? "destape" : "tape", oscuro ? "CLARO" : "OSCURO");
    return;
  }
  int dif = nivelClaro - nivelOscuro;
  if (dif < 100) {
    info("ATENCION: casi no hay diferencia entre oscuro (%d) y claro (%d). Revise el divisor (LDR a 3V3, 10 kOhm a GND, punto medio a GPIO 34).",
         nivelOscuro, nivelClaro);
    return;
  }
  uLuzAbre = nivelOscuro + dif * 0.50;                 // abre a mitad de camino
  uLuzCierra = nivelOscuro + dif * 0.30;               // cierra un poco más abajo (histéresis)
  for (int i = 0; i < N_PARAMETROS; i++)
    if (PARAMETROS[i].valor == &uLuzAbre || PARAMETROS[i].valor == &uLuzCierra) guardarParametro(PARAMETROS[i]);
  info("Listo: LUZ_ABRE=%.0f y LUZ_CIERRA=%.0f (guardados).", uLuzAbre, uLuzCierra);
}

void prueba(String resto) {
  resto.trim();
  resto.toUpperCase();
  const float valores[3] = {0.62, 1.10, 0.48};
  for (int t = 0; t < 3; t++) {
    if (resto == NOMBRE_EVENTO[t]) {
      huboEvento[t] = false;                           // la prueba no espera el antirrebote
      reportarEvento((TipoEvento)t, valores[t]);
      return;
    }
  }
  info("Uso: PRUEBA FRENADA  (o HUECO, CURVA). Debe haber un viaje: INICIO;ENVIO:1");
}

void atenderSerie(String linea) {
  linea.trim();
  if (linea.length() == 0) return;
  if (atenderProtocolo(linea, CANAL_SERIE)) return;   // modo prueba: el monitor hace de celular

  String cmd = linea, resto = "";
  int esp = linea.indexOf(' ');
  if (esp >= 0) { cmd = linea.substring(0, esp); resto = linea.substring(esp + 1); }
  cmd.toUpperCase();

  if (cmd == "AYUDA" || cmd == "?") mostrarAyuda();
  else if (cmd == "CAL") {
    modoCal = !modoCal;
    if (modoCal) info("CAL: cada 100 ms -> ax ay az (crudo) | lon lat vert (sin gravedad) | luz. Escriba CAL para salir.");
    else info("Fin de CAL.");
  }
  else if (cmd == "VER") mostrarEstado();
  else if (cmd == "UMBRAL" || cmd == "UMBRALES") cambiarUmbral(resto);
  else if (cmd == "EJE") cambiarEje(resto);
  else if (cmd == "RECAL") {
    if (!mpuOk) info("El GY-521 no responde: no se puede calibrar.");
    else { intentosCalib = 0; empezarCalibracion(); }
  }
  else if (cmd == "OSCURO") medirLuzReferencia(true);
  else if (cmd == "CLARO") medirLuzReferencia(false);
  else if (cmd == "PRUEBA") prueba(resto);
  else if (cmd == "FABRICA") valoresDeFabrica();
  else info("No entiendo '%s'. Escriba AYUDA.", linea.c_str());
}

// ═════════════════════════════ BLUETOOTH ═════════════════════════════
// Algunos celulares (HyperOS de Xiaomi/Poco) esconden los equipos Bluetooth que no dicen qué
// son: el ESP32-CAM no aparecía hasta declararlo como cámara. Este se declara como periférico
// "dispositivo de medición" (sensing device) y se fuerza el modo visible y conectable.
void hacerVisible() {
  esp_bt_cod_t clase = {};
  clase.major = ESP_BT_COD_MAJOR_DEV_PERIPHERAL;
  clase.minor = 0x04;                                  // periférico > dispositivo de medición
  clase.service = ESP_BT_COD_SRVC_NONE;
  esp_bt_gap_set_cod(clase, ESP_BT_SET_COD_ALL);
  esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
}

void iniciarBluetooth() {
  ultimoIntentoBt = millis();
  btIniciado = bt.begin(NOMBRE_BT);                    // modo esclavo: espera que el celular se conecte
  if (btIniciado) hacerVisible();
  if (btIniciado) info("Bluetooth listo como '%s'. Vinculelo desde el celular (sin clave).", NOMBRE_BT);
  else info("ATENCION: no arranco el Bluetooth. Reintento en 5 s.");
}

void revisarConexion() {
  if (!btIniciado) {
    if (millis() - ultimoIntentoBt >= 5000) iniciarBluetooth();
    return;
  }
  bool ahora = bt.hasClient();
  if (ahora == btConectado) return;
  btConectado = ahora;
  bufBt = "";
  if (ahora) {
    info("Celular conectado.");
    enviar("HOLA;DISP:SENSORES;VER:1", CANAL_BT);
    ultimoLatido = millis();
  } else {
    // El viaje NO se borra: el celular, al reconectarse, manda otra vez PING e INICIO.
    // BluetoothSerial vuelve a aceptar conexiones solo; no hay que hacer nada más.
    info("Celular desconectado. Esperando que se vuelva a conectar...");
  }
}

void leerBluetooth() {
  if (!btIniciado) return;
  while (bt.available()) {
    char c = (char)bt.read();
    if (c == '\n') {
      bufBt.trim();
      if (bufBt.length()) {
        Serial.print("# <- ");
        Serial.println(bufBt);
        atenderProtocolo(bufBt, CANAL_BT);
      }
      bufBt = "";
    } else if (c != '\r') {
      if (bufBt.length() < 120) bufBt += c;
      else bufBt = "";                                 // línea demasiado larga: se descarta
    }
  }
}

void leerSerie() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    ultimoCaracterSerie = millis();
    if (c == '\n' || c == '\r') {
      if (bufSerie.length()) atenderSerie(bufSerie);
      bufSerie = "";
    } else if (bufSerie.length() < 80) {
      bufSerie += c;
    }
  }
  // Por si el monitor está en "Sin ajuste de línea": se procesa tras 0,5 s sin teclas.
  if (bufSerie.length() && millis() - ultimoCaracterSerie > 500) {
    atenderSerie(bufSerie);
    bufSerie = "";
  }
}

void actualizarLed() {
  uint32_t t = millis();
  if (calibrando) digitalWrite(PIN_LED, (t / 100) % 2 ? HIGH : LOW);          // parpadeo rápido: calibrando
  else if (!mpuOk) digitalWrite(PIN_LED, (t % 2000) < 150 ? HIGH : LOW);     // destello cada 2 s: falta el GY-521
  else digitalWrite(PIN_LED, btConectado ? HIGH : LOW);                       // fijo: celular conectado
}

// ═════════════════════════════ ARRANQUE Y BUCLE ═════════════════════════════
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  info("ORBITA - ESP32 de sensores de la carga · firmware %s · protocolo VER:1", VERSION_FW);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  cargarAjustes();

  // Luz: ADC de 12 bits (0-4095), rango completo hasta ~3,3 V.
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_LDR, ADC_11db);
  nivelLuz = leerLuz();
  luzAbierta = nivelLuz > uLuzAbre;                    // estado inicial, sin mensaje (no hay viaje)
  info("Luz inicial: nivel %d -> carga %s", nivelLuz, textoLuz());

  // Acelerómetro. Si no responde, el programa sigue con la luz y lo reintenta.
  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  Wire.setTimeOut(20);                                 // que un cable flojo no congele el programa
  intentarMpu();

  iniciarBluetooth();
  info("Escriba AYUDA para ver los comandos.");
  ultimaMuestra = ultimaLuz = ultimoLatido = millis();
}

void loop() {
  uint32_t ahora = millis();

  leerBluetooth();
  leerSerie();
  revisarConexion();

  if (ahora - ultimaMuestra >= MUESTREO_MS) {
    ultimaMuestra += MUESTREO_MS;
    if (ahora - ultimaMuestra > 50) ultimaMuestra = ahora;   // si se atrasó mucho, no intenta "ponerse al día"
    muestrearMovimiento();
  }

  if (ahora - ultimaLuz >= LUZ_CADA_MS) {
    ultimaLuz = ahora;
    revisarLuz();
  }

  if (modoCal && ahora - ultimaCal >= CAL_CADA_MS) {
    ultimaCal = ahora;
    if (!mpuOk) Serial.printf("# CAL GY-521 sin respuesta | luz=%4d %s\n", nivelLuz, textoLuz());
    else if (calibrando) Serial.printf("# CAL calibrando gravedad... | luz=%4d %s\n", nivelLuz, textoLuz());
    else Serial.printf("# CAL ax=%+.2f ay=%+.2f az=%+.2f | lon=%+.2f lat=%+.2f vert=%+.2f | luz=%4d %s\n",
                       acel[0], acel[1], acel[2], lonF, latF, verF, nivelLuz, textoLuz());
  }

  // LATIDO cada 10 s: al celular si está conectado, y siempre al monitor serie (modo prueba).
  if (ahora - ultimoLatido >= LATIDO_MS) {
    ultimoLatido = ahora;
    enviar(String("LATIDO;LUZ:") + String(nivelLuz) + ";ESTADO:" + textoLuz(), AMBOS);
  }

  actualizarLed();
  delay(1);
}
