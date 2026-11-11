#pragma once
#include "Arduino.h"
struct Preferences { static std::map<std::string,float> m;
 void begin(const char*,bool){} void end(){} void clear(){m.clear();}
 float getFloat(const char*k,float d){return m.count(k)?m[k]:d;} void putFloat(const char*k,float v){m[k]=v;}
 int8_t getChar(const char*k,int8_t d){return m.count(k)?(int8_t)m[k]:d;} void putChar(const char*k,int8_t v){m[k]=v;}
};
