/*
  esp32cam_reconocimiento.ino — Orbita, P3-S3
  Equipo de reconocimiento de la cabina: ESP32-CAM que DETECTA el rostro del conductor
  y manda el rostro recortado al celular. El celular lo pasa al servidor, que lo RECONOCE
  con el mismo modelo SFace del computador de P3-S1 (esquema hibrido).

  Por que hibrido: el modelo de reconocimiento de esp-dl que cabe en los 4 MB de flash
  junto con Bluetooth (S8) tarda ~14 s por rostro en el ESP32; el rapido (S16) no cabe.
  Detectar, en cambio, toma ~0,1-0,2 s en la placa.

  Placa: ESP32-CAM AI-Thinker con camara GC2145 (RHYX M21-45) u OV2640.
  Nucleo: esp32 de Espressif **2.0.17** (el 3.x ya no trae los detectores de esp-dl).
  Detector: HumanFaceDetectMSR01 + HumanFaceDetectMNP01 (dos etapas, viene en el nucleo).

  Bluetooth SPP, nombre "Orbita-Cabina". Una linea por mensaje, campos CLAVE:valor con ';':

    Celular -> ESP        ESP -> Celular
    PING                  PONG
    IDENTIFICAR           IDENTIFICANDO                              (de inmediato)
                          ROSTRO;N:1;DE:3;ANCHO:152;JPG:<base64>      (1 a 3 rostros recortados)
                          FIN_ROSTROS;N:3                             (el celular los manda al servidor)
                          NO_RECONOCIDO;SIM:0.00;MOTIVO:...           (no se vio ningun rostro)
                          ERROR;MOTIVO:...
    (otra cosa)           ERROR;MOTIVO:comando desconocido

  El computador de P3-S1 sigue respondiendo CONDUCTOR;... directamente; la app acepta los dos.

  Comandos por el cable USB (115200 baudios), para preparar y revisar:
    CAPTURAR[;N:8]   manda N rostros (mismo formato ROSTRO) para registrar al conductor en el PC
                     (reconocimiento/capturar_esp.py los guarda y luego registrar.py --fotos)
    PROBAR           10 s mostrando si ve un rostro, su tamano y cuanto tarda
    FOTO             foto completa en JPEG (hexadecimal) para revisar el encuadre
    ESTADO           camara, memoria y Bluetooth
*/
#include <Arduino.h>
#include <BluetoothSerial.h>
#include <list>
#include <vector>
#include "esp_camera.h"
#include "img_converters.h"
#include "mbedtls/base64.h"
#include "esp_gap_bt_api.h"
#include "esp_bt_device.h"
#include "human_face_detect_msr01.hpp"
#include "human_face_detect_mnp01.hpp"

// ------------------------------------------------------------------
//  Parametros
// ------------------------------------------------------------------
const char *NOMBRE_BT = "Orbita-Cabina";
const int ROSTROS_POR_IDENTIFICACION = 3;   // el servidor exige que coincidan
const uint32_t SEGUNDOS_IDENTIFICAR = 10;   // la app espera en total hasta 40 s
const uint32_t MS_ENTRE_ROSTROS = 400;      // rostros de cuadros distintos, no del mismo instante
const int ANCHO_MIN_ROSTRO = 70;            // px en 320x240 (igual que ANCHO_MIN_RECONOCER de comun.py)
const float MARGEN_RECORTE = 0.45f;         // se agrega este tanto del ancho del rostro a cada lado
const int CALIDAD_JPEG = 90;
const int PIN_FLASH = 4;                    // LED blanco del ESP32-CAM: apagado (deslumbra)

// Pines del ESP32-CAM AI-Thinker
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

// Un rostro detectado: la caja [x1, y1, x2, y2] en el cuadro.
struct Rostro {
  bool hay = false;
  int x1 = 0, y1 = 0, x2 = 0, y2 = 0;
  int ancho() const { return x2 - x1; }
};

BluetoothSerial SerialBT;
HumanFaceDetectMSR01 detector1(0.1F, 0.5F, 10, 0.2F);
HumanFaceDetectMNP01 detector2(0.5F, 0.3F, 5);
bool camara_ok = false;

// ------------------------------------------------------------------
//  Utilidades
// ------------------------------------------------------------------
void enviar(Stream &canal, const String &texto) {
  canal.print(texto);
  canal.print("\n");
  if (&canal != &Serial) {                   // eco corto en el cable USB para depurar
    Serial.print("-> ");
    Serial.println(texto.length() > 90 ? texto.substring(0, 90) + "..." : texto);
  }
}

String campo(const String &linea, const String &clave) {   // lee CLAVE:valor por nombre
  int i = linea.indexOf(";" + clave + ":");
  if (i < 0) return "";
  int ini = i + clave.length() + 2;
  int fin = linea.indexOf(';', ini);
  String v = linea.substring(ini, fin < 0 ? linea.length() : fin);
  v.trim();
  return v;
}

// ------------------------------------------------------------------
//  Camara y detector
// ------------------------------------------------------------------
bool iniciar_camara() {
  camera_config_t c = {};
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM; c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d0 = Y2_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.xclk_freq_hz = 20000000; c.ledc_timer = LEDC_TIMER_0; c.ledc_channel = LEDC_CHANNEL_0;
  c.pixel_format = PIXFORMAT_RGB565;       // el detector trabaja sobre RGB565 (la GC2145 no da JPEG)
  c.frame_size = FRAMESIZE_QVGA;           // 320x240
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;
  esp_err_t e = ESP_FAIL;
  for (int n = 1; n <= 3 && e != ESP_OK; n++) {   // algunos sensores tardan en despertar
    e = esp_camera_init(&c);
    if (e != ESP_OK) { Serial.printf("camara: intento %d fallo 0x%x\n", n, e); delay(300); }
  }
  if (e != ESP_OK) return false;
  sensor_t *s = esp_camera_sensor_get();
  if (s->id.PID == OV2640_PID) s->set_vflip(s, 1);  // la OV2640 del AI-Thinker sale al reves
  Serial.printf("camara: OK, sensor PID 0x%x\n", s->id.PID);
  return true;
}

// El rostro mas grande del cuadro (el conductor es el mas cercano a la camara).
Rostro detectar(camera_fb_t *fb) {
  Rostro r;
  std::vector<int> forma = {(int)fb->height, (int)fb->width, 3};
  std::list<dl::detect::result_t> &candidatos = detector1.infer((uint16_t *)fb->buf, forma);
  std::list<dl::detect::result_t> &resultados = detector2.infer((uint16_t *)fb->buf, forma, candidatos);
  for (auto &res : resultados) {
    if (res.box.size() < 4) continue;
    int ancho = res.box[2] - res.box[0];
    if (ancho > r.ancho()) { r.hay = true; r.x1 = res.box[0]; r.y1 = res.box[1]; r.x2 = res.box[2]; r.y2 = res.box[3]; }
  }
  return r;
}

// Recorta el rostro con margen (cuadrado, dentro del cuadro) y lo devuelve en JPEG base64.
// El servidor vuelve a buscar el rostro y sus puntos en el recorte, por eso va con margen.
String recorte_base64(camera_fb_t *fb, const Rostro &r) {
  int w = fb->width, h = fb->height;
  int cx = (r.x1 + r.x2) / 2, cy = (r.y1 + r.y2) / 2;
  int lado = (int)(max(r.x2 - r.x1, r.y2 - r.y1) * (1.0f + 2 * MARGEN_RECORTE));
  lado = min(lado, min(w, h));
  int x0 = constrain(cx - lado / 2, 0, w - lado), y0 = constrain(cy - lado / 2, 0, h - lado);
  size_t bytes = (size_t)lado * lado * 2;
  uint8_t *recorte = (uint8_t *)ps_malloc(bytes);
  if (!recorte) return "";
  for (int y = 0; y < lado; y++)
    memcpy(recorte + (size_t)y * lado * 2, fb->buf + ((size_t)(y0 + y) * w + x0) * 2, (size_t)lado * 2);
  uint8_t *jpg = NULL; size_t n = 0;
  bool ok = fmt2jpg(recorte, bytes, lado, lado, PIXFORMAT_RGB565, CALIDAD_JPEG, &jpg, &n);
  free(recorte);
  if (!ok) return "";
  size_t largo = 0;
  mbedtls_base64_encode(NULL, 0, &largo, jpg, n);
  uint8_t *b64 = (uint8_t *)ps_malloc(largo + 1);
  String texto;
  if (b64 && mbedtls_base64_encode(b64, largo + 1, &largo, jpg, n) == 0) {
    b64[largo] = 0;
    texto = (const char *)b64;
  }
  free(b64);
  free(jpg);
  return texto;
}

// Junta hasta 'cuantos' rostros de cuadros distintos en 'ms' milisegundos y los manda.
// Devuelve cuantos mando.
int mandar_rostros(Stream &canal, int cuantos, uint32_t ms) {
  int mandados = 0;
  uint32_t inicio = millis(), ultimo = 0;
  int cuadros = 0;
  while (mandados < cuantos && millis() - inicio < ms) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { enviar(canal, "ERROR;MOTIVO:la camara dejo de enviar imagenes"); return -1; }
    cuadros++;
    if (millis() - ultimo >= MS_ENTRE_ROSTROS) {
      Rostro r = detectar(fb);
      if (r.hay && r.ancho() >= ANCHO_MIN_ROSTRO) {
        String b64 = recorte_base64(fb, r);
        if (b64.length()) {
          mandados++;
          ultimo = millis();
          enviar(canal, "ROSTRO;N:" + String(mandados) + ";DE:" + String(cuantos) + ";ANCHO:" + String(r.ancho()) +
                        ";JPG:" + b64);
        }
      }
    }
    esp_camera_fb_return(fb);
    delay(1);
  }
  Serial.printf("  (%d rostros en %.1f s, %d cuadros)\n", mandados, (millis() - inicio) / 1000.0, cuadros);
  return mandados;
}

// ------------------------------------------------------------------
//  Comandos
// ------------------------------------------------------------------
void identificar(Stream &canal) {
  enviar(canal, "IDENTIFICANDO");
  if (!camara_ok) { enviar(canal, "ERROR;MOTIVO:la camara no inicio"); return; }
  int n = mandar_rostros(canal, ROSTROS_POR_IDENTIFICACION, SEGUNDOS_IDENTIFICAR * 1000UL);
  if (n < 0) return;
  if (n == 0) { enviar(canal, "NO_RECONOCIDO;SIM:0.00;MOTIVO:no se vio ningun rostro de frente y cerca"); return; }
  enviar(canal, "FIN_ROSTROS;N:" + String(n));
}

void capturar(Stream &canal, const String &linea) {
  int n = campo(linea, "N").toInt();
  if (n <= 0 || n > 20) n = 8;
  if (!camara_ok) { enviar(canal, "ERROR;MOTIVO:la camara no inicio"); return; }
  enviar(canal, "CAPTURANDO;N:" + String(n) + ";MSG:mira a la camara y mueve un poco la cabeza");
  int hechos = mandar_rostros(canal, n, 60000UL);
  if (hechos >= 0) enviar(canal, "FIN_ROSTROS;N:" + String(hechos));
}

void probar(Stream &canal) {
  if (!camara_ok) { enviar(canal, "ERROR;MOTIVO:la camara no inicio"); return; }
  uint32_t inicio = millis();
  int cuadros = 0, con_rostro = 0;
  while (millis() - inicio < 10000UL) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) break;
    cuadros++;
    uint32_t t0 = millis();
    Rostro r = detectar(fb);
    String txt = "PRUEBA;CUADRO:" + String(cuadros);
    if (r.hay) {
      con_rostro++;
      txt += ";ANCHO:" + String(r.ancho()) + ";CAJA:" + String(r.x1) + "," + String(r.y1) + "," + String(r.x2) + "," +
             String(r.y2) + (r.ancho() >= ANCHO_MIN_ROSTRO ? ";SIRVE:SI" : ";SIRVE:NO_lejos");
    } else {
      txt += ";SIN_ROSTRO";
    }
    txt += ";MS:" + String(millis() - t0);
    esp_camera_fb_return(fb);
    enviar(canal, txt);
  }
  enviar(canal, "FIN_PRUEBA;CUADROS:" + String(cuadros) + ";CON_ROSTRO:" + String(con_rostro));
}

void foto(Stream &canal) {                  // solo por el cable
  camera_fb_t *fb = camara_ok ? esp_camera_fb_get() : NULL;
  if (!fb) { enviar(canal, "ERROR;MOTIVO:sin foto"); return; }
  uint8_t *jpg = NULL; size_t n = 0;
  bool ok = frame2jpg(fb, 80, &jpg, &n);
  esp_camera_fb_return(fb);
  if (!ok) { enviar(canal, "ERROR;MOTIVO:no se pudo convertir a JPEG"); return; }
  canal.printf("FOTO;BYTES:%u\n<<<HEX\n", n);
  for (size_t k = 0; k < n; k++) { canal.printf("%02x", jpg[k]); if (k % 64 == 63) canal.print("\n"); }
  canal.print("\nHEX>>>\n");
  free(jpg);
}

// Algunos celulares (HyperOS de Xiaomi/Poco) no muestran equipos Bluetooth que no dicen
// qué son. Se declara como "cámara" (clase Imaging) y se fuerza el modo visible y conectable.
void hacer_visible() {
  esp_bt_cod_t clase = {};
  clase.major = ESP_BT_COD_MAJOR_DEV_IMAGING;
  clase.minor = 0x08;                         // Imaging > cámara
  clase.service = ESP_BT_COD_SRVC_CAPTURING;  // servicio de captura de imágenes
  esp_bt_gap_set_cod(clase, ESP_BT_SET_COD_ALL);
  esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
}

String direccion_bt() {
  const uint8_t *d = esp_bt_dev_get_address();
  if (!d) return "?";
  char t[18];
  snprintf(t, sizeof t, "%02X:%02X:%02X:%02X:%02X:%02X", d[0], d[1], d[2], d[3], d[4], d[5]);
  return t;
}

void estado(Stream &canal) {
  char t[200];
  snprintf(t, sizeof t, "ESTADO;CAMARA:%s;SENSOR:0x%x;RAM_LIBRE:%u;PSRAM_LIBRE:%u;BT:%s;MAC:%s;VER:2",
           camara_ok ? "OK" : "ERROR", camara_ok ? esp_camera_sensor_get()->id.PID : 0,
           ESP.getFreeHeap(), ESP.getFreePsram(), NOMBRE_BT, direccion_bt().c_str());
  enviar(canal, t);
}

// Devuelve true si la orden tomo tiempo: lo que llego mientras tanto se descarta.
bool atender(Stream &canal, String linea) {
  linea.trim();
  if (linea.length() == 0) return false;
  String comando = linea;
  int pc = comando.indexOf(';');
  if (pc >= 0) comando = comando.substring(0, pc);
  comando.trim();
  comando.toUpperCase();
  if (&canal != &Serial) Serial.println("<- " + linea);

  if (comando == "PING") { enviar(canal, "PONG"); return false; }
  if (comando == "IDENTIFICAR") { identificar(canal); return true; }
  if (comando == "ESTADO") { estado(canal); return false; }
  if (&canal == &Serial) {                    // solo por el cable USB
    if (comando == "CAPTURAR") { capturar(canal, linea); return true; }
    if (comando == "PROBAR") { probar(canal); return true; }
    if (comando == "FOTO") { foto(canal); return false; }
  }
  enviar(canal, "ERROR;MOTIVO:comando desconocido");
  return false;
}

String pendiente_usb, pendiente_bt;

void leer_canal(Stream &canal, String &pendiente) {
  while (canal.available()) {
    char ch = canal.read();
    if (ch == '\n') {
      String linea = pendiente;
      pendiente = "";
      if (atender(canal, linea)) {            // lo que llego mientras tanto ya tuvo respuesta
        while (canal.available()) canal.read();
        return;
      }
    } else if (ch != '\r' && pendiente.length() < 200) {
      pendiente += ch;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_FLASH, OUTPUT);
  digitalWrite(PIN_FLASH, LOW);
  Serial.println("\nOrbita - equipo de reconocimiento de la cabina (ESP32-CAM, P3-S3)");
  camara_ok = iniciar_camara();
  if (!SerialBT.begin(NOMBRE_BT)) Serial.println("ERROR: no inicio el Bluetooth");
  hacer_visible();
  Serial.printf("Bluetooth listo: \"%s\". RAM libre %u, PSRAM libre %u\n", NOMBRE_BT, ESP.getFreeHeap(), ESP.getFreePsram());
  Serial.println("Comandos: PING, IDENTIFICAR, ESTADO; por cable: CAPTURAR;N:8, PROBAR, FOTO");
}

void loop() {
  leer_canal(Serial, pendiente_usb);
  leer_canal(SerialBT, pendiente_bt);
  delay(5);
}
