// iram.cpp — libera IRAM para que el firmware quepa.
//
// Bluetooth clasico (30 KB de IRAM) + camara + rutinas de esp-dl que corren desde IRAM
// (dl_xtensa_s16_block, 7,8 KB) se pasan por ~1,2 KB de la IRAM del ESP32 (128 KB).
// Con la PSRAM activa, el nucleo 2.0.17 pone en IRAM las funciones de hora de la libc
// (CONFIG_SPIRAM_CACHE_LIBTIME_IN_IRAM), y la clase Print de Arduino arrastra strftime
// (3,2 KB) aunque este programa nunca imprime fechas.
// Esta version vacia queda en flash y el enlazador ya no trae la de la libc.
#include <stddef.h>
#include <time.h>

extern "C" size_t strftime(char *s, size_t max, const char *formato, const struct tm *t) {
  (void)formato;
  (void)t;
  if (max) s[0] = '\0';
  return 0;
}
