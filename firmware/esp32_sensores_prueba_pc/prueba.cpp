// prueba.cpp — prueba en el PC la lógica del firmware esp32_sensores.ino con un sensor simulado.
// No necesita hardware. Uso (Linux, macOS o Git Bash en Windows con g++):  sh prueba.sh
#include "Arduino.h"
#include "Wire.h"
#include "Preferences.h"
#include "BluetoothSerial.h"
uint32_t T_MS = 0; int LED = 0; int LUZ_SIM = 40; float ACC[3] = {0, 0, 1}; bool MPU_VIVO = true; bool BT_CLIENTE = false;
SerialSim Serial; WireSim Wire; std::map<std::string, float> Preferences::m;
int indiceVertical(); void calcularEjes(); void empezarCalibracion(); void info(const char*, ...);
#include "sketch.cpp"

int fallas = 0;
std::string BT, SER;
void tomar() { BT = bt.out; SER = Serial.out; bt.out.clear(); Serial.out.clear(); }
int cuenta(const std::string& s, const std::string& x) { int n = 0; size_t p = 0; while ((p = s.find(x, p)) != std::string::npos) { n++; p++; } return n; }
void revisar(const char* nombre, bool ok) {
  printf("%s  %s\n", ok ? "OK   " : "FALLA", nombre);
  if (!ok) { fallas++; printf("      BT:\n%s      SERIE:\n%s\n", BT.c_str(), SER.c_str()); }
}
void run(uint32_t ms) { uint32_t e = T_MS + ms; while (T_MS < e) loop(); }
void ruido() { for (int i = 0; i < 3; i++) ACC[i] += ((rand() % 100) - 50) / 5000.0f; }  // ±0,01 g
void pulso(float ax, float ay, float az, uint32_t ms) { uint32_t e = T_MS + ms; while (T_MS < e) { ACC[0] = ax; ACC[1] = ay; ACC[2] = az; ruido(); loop(); } }
void quieto(uint32_t ms) { pulso(0, 0, 1, ms); }

int main() {
  srand(1);
  setup(); quieto(2600); tomar();
  revisar("arranque: calibra la gravedad (Z vertical)", SER.find("Eje vertical: Z+") != std::string::npos);
  revisar("arranque: nombre ORBITA-SENSORES", SER.find("'ORBITA-SENSORES'") != std::string::npos);

  BT_CLIENTE = true; run(5); tomar();
  revisar("al conectar manda HOLA", BT == "HOLA;DISP:SENSORES;VER:1\n");
  bt.feed("PING\nINICIO;ENVIO:7\n"); run(5); tomar();
  revisar("PING -> PONG; INICIO -> HOLA + LUZ con SEQ:1",
          BT == "PONG\nHOLA;DISP:SENSORES;VER:1\nLUZ;ESTADO:CERRADA;NIVEL:40;SEQ:1\n");

  pulso(-0.6, 0, 1, 300); quieto(500); tomar();
  revisar("frenada -0,6 g x 300 ms -> 1 FRENADA G:0.60 SEQ:2", BT == "EVENTO;TIPO:FRENADA;G:0.60;SEQ:2\n");
  quieto(2100); pulso(-0.6, 0, 1, 100); quieto(500); tomar();
  revisar("frenada de 100 ms -> nada", cuenta(BT, "EVENTO") == 0);
  quieto(2100); pulso(-0.7, 0, 1, 300); quieto(300); pulso(-0.7, 0, 1, 300); quieto(500); tomar();
  revisar("dos frenadas en < 2 s -> solo 1 (antirrebote)", cuenta(BT, "TIPO:FRENADA") == 1);
  quieto(2100); pulso(0.6, 0, 1, 300); quieto(300); tomar();
  revisar("acelerar +0,6 g -> nada", cuenta(BT, "EVENTO") == 0);
  quieto(2100); pulso(0, 0, 2.2, 30); quieto(300); tomar();
  revisar("golpe vertical +1,2 g x 30 ms -> HUECO", cuenta(BT, "TIPO:HUECO;G:1.2") == 1);
  quieto(2100); pulso(0, 0, 1.9, 400); quieto(300); tomar();
  revisar("vertical 0,9 g x 400 ms (loma) -> nada", cuenta(BT, "EVENTO") == 0);
  quieto(2100); pulso(0, 0.5, 1, 250); quieto(300); tomar();
  revisar("lateral 0,5 g x 250 ms -> CURVA", cuenta(BT, "TIPO:CURVA;G:0.50") == 1);

  LUZ_SIM = 900; quieto(500); tomar();
  revisar("destapar -> LUZ ABIERTA (una vez)", cuenta(BT, "LUZ;ESTADO:ABIERTA;NIVEL:900") == 1);
  LUZ_SIM = 320; quieto(500); tomar();
  revisar("nivel entre umbrales -> nada (histeresis)", cuenta(BT, "LUZ;") == 0);
  LUZ_SIM = 30; quieto(100); LUZ_SIM = 900; quieto(100); LUZ_SIM = 30; quieto(500); tomar();
  revisar("tapar (con un destello corto) -> 1 LUZ CERRADA", cuenta(BT, "LUZ;ESTADO:CERRADA") == 1 && cuenta(BT, "ABIERTA;NIVEL") == 0);
  quieto(10000); tomar();
  revisar("LATIDO cada 10 s", cuenta(BT, "LATIDO;LUZ:30;ESTADO:CERRADA") == 1);

  bt.feed("FIN\n"); run(5); pulso(-0.6, 0, 1, 300); quieto(300); LUZ_SIM = 900; quieto(500); tomar();
  revisar("tras FIN no salen EVENTO ni LUZ", cuenta(BT, "EVENTO") == 0 && cuenta(BT, "LUZ;") == 0);

  BT_CLIENTE = false; run(10); BT_CLIENTE = true; run(10); tomar();
  revisar("reconexion -> HOLA otra vez", BT == "HOLA;DISP:SENSORES;VER:1\n");
  BT_CLIENTE = false; quieto(10500); tomar();
  revisar("sin cliente no se manda LATIDO por BT", BT.empty() && cuenta(SER, "LATIDO") == 1);
  BT_CLIENTE = true; run(10); tomar();

  MPU_VIVO = false; quieto(500); LUZ_SIM = 30; bt.feed("INICIO;ENVIO:8\n"); quieto(600); tomar();
  revisar("GY-521 caido: avisa y la luz sigue", SER.find("se perdio el GY-521") != std::string::npos &&
          cuenta(BT, "LUZ;ESTADO:CERRADA") == 1);
  MPU_VIVO = true; quieto(6000); tomar();
  revisar("GY-521 vuelve: recalibra solo", SER.find("Gravedad:") != std::string::npos);

  Serial.feed("PING\ninicio;envio:3\nUMBRAL FRENADA 0,3\nUMBRAL LUZ_CIERRA 500\nEJE Y+\nPRUEBA HUECO\n"); run(5); tomar();
  revisar("modo prueba por el monitor serie", SER.find("PONG\n") != std::string::npos && SER.find("ENV-3") != std::string::npos);
  revisar("UMBRAL acepta coma y guarda", SER.find("FRENADA = 0.30 g (guardado)") != std::string::npos && Preferences::m["frenada_g"] > 0.29f);
  revisar("UMBRAL rechaza LUZ_CIERRA >= LUZ_ABRE", SER.find("LUZ_CIERRA debe ser MENOR") != std::string::npos);
  revisar("PRUEBA HUECO manda el evento", cuenta(BT, "TIPO:HUECO;G:1.10") == 1);
  quieto(2100); pulso(0, -0.5, 1, 300); quieto(300); tomar();
  revisar("EJE Y+: -0,5 g en Y -> FRENADA con umbral 0,30", cuenta(BT, "TIPO:FRENADA;G:0.50") == 1);

  Serial.feed("FABRICA\nRECAL\n"); run(5);
  pulso(1, 0, 0, 2600); tomar();
  revisar("modulo de canto: avisa y usa otro eje", SER.find("Por ahora uso Y+") != std::string::npos);

  printf("\n%s: %d falla(s)\n", fallas ? "HAY FALLAS" : "TODO BIEN", fallas);
  return fallas ? 1 : 0;
}
