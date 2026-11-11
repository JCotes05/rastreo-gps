#pragma once
// Imitación mínima de esp_gap_bt_api.h para la prueba en el PC: hacerVisible() no hace nada.
#include <cstdint>
enum { ESP_BT_COD_MAJOR_DEV_PERIPHERAL = 5, ESP_BT_COD_SRVC_NONE = 0 };
enum esp_bt_cod_mode_t { ESP_BT_SET_COD_ALL = 4 };
enum esp_bt_connection_mode_t { ESP_BT_CONNECTABLE = 1 };
enum esp_bt_discovery_mode_t { ESP_BT_GENERAL_DISCOVERABLE = 2 };
struct esp_bt_cod_t { uint32_t reserved_2 : 2; uint32_t minor : 6; uint32_t major : 5; uint32_t service : 11; uint32_t reserved_8 : 8; };
inline int esp_bt_gap_set_cod(esp_bt_cod_t, esp_bt_cod_mode_t) { return 0; }
inline int esp_bt_gap_set_scan_mode(esp_bt_connection_mode_t, esp_bt_discovery_mode_t) { return 0; }
