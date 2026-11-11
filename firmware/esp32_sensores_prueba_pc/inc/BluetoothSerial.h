#pragma once
#include "Arduino.h"
#define CONFIG_BT_ENABLED 1
#define CONFIG_BLUEDROID_ENABLED 1
#define CONFIG_BT_SPP_ENABLED 1
extern bool BT_CLIENTE;
struct BluetoothSerial { std::deque<char> in; std::string out;
 bool begin(const char*){return true;} bool hasClient(){return BT_CLIENTE;}
 int available(){return in.size();} int read(){char c=in.front();in.pop_front();return c;}
 void print(const String&x){out+=x.s;} void print(char c){out+=c;}
 void feed(const char*x){while(*x)in.push_back(*x++);}
};
