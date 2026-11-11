#pragma once
#include "Arduino.h"
extern float ACC[3]; extern bool MPU_VIVO;
struct WireSim {
 uint8_t reg=0; int n=0; std::map<int,int> regs; std::deque<uint8_t> rx; bool first=true;
 bool begin(int,int,uint32_t){regs[0x75]=0x68;return true;} void setTimeOut(int){}
 void beginTransmission(int){n=0;}
 void write(uint8_t b){if(n==0)reg=b;else regs[reg]=b;n++;}
 uint8_t endTransmission(bool=true){return MPU_VIVO?0:2;}
 uint8_t requestFrom(uint8_t,uint8_t k){if(!MPU_VIVO)return 0;rx.clear();
   if(reg==0x3B){for(int i=0;i<3;i++){int16_t v=(int16_t)lrintf(ACC[i]*4096);rx.push_back((v>>8)&0xFF);rx.push_back(v&0xFF);}}
   else rx.push_back(regs[reg]); return k;}
 int read(){int v=rx.front();rx.pop_front();return v;}
};
extern WireSim Wire;
