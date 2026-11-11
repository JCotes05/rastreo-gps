#pragma once
#include <string>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cmath>
#include <cstdarg>
#include <algorithm>
#include <deque>
#include <map>
using std::min; using std::max;
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define ADC_11db 3
extern uint32_t T_MS;
inline uint32_t millis(){return T_MS;}
inline void delay(uint32_t ms){T_MS+=ms;}
inline void pinMode(int,int){}
extern int LED;
inline void digitalWrite(int,int v){LED=v;}
extern int LUZ_SIM;
inline int analogRead(int){return LUZ_SIM;}
inline void analogReadResolution(int){}
inline void analogSetPinAttenuation(int,int){}
class String {
public: std::string s;
 String(){} String(const char*c):s(c?c:""){} String(const std::string&x):s(x){}
 String(char c):s(1,c){}
 String(int v){s=std::to_string(v);} String(unsigned v){s=std::to_string(v);} String(long v){s=std::to_string(v);} String(unsigned long v){s=std::to_string(v);}
 String(float v,int d){char b[32];snprintf(b,32,"%.*f",d,v);s=b;} String(double v,int d){char b[32];snprintf(b,32,"%.*f",d,v);s=b;}
 int length()const{return s.size();} const char*c_str()const{return s.c_str();}
 int indexOf(char c,int from=0)const{auto p=s.find(c,from);return p==std::string::npos?-1:p;}
 int indexOf(const String&x)const{auto p=s.find(x.s);return p==std::string::npos?-1:p;}
 String substring(int a,int b=-1)const{if(b<0)b=s.size();return String(s.substr(a,b-a));}
 void trim(){auto a=s.find_first_not_of(" \t\r\n");if(a==std::string::npos){s="";return;}auto b=s.find_last_not_of(" \t\r\n");s=s.substr(a,b-a+1);}
 void toUpperCase(){for(auto&c:s)c=toupper(c);}
 bool equalsIgnoreCase(const char*o)const{return strcasecmp(s.c_str(),o)==0;}
 float toFloat()const{return atof(s.c_str());}
 void replace(char a,char b){for(auto&c:s)if(c==a)c=b;}
 char operator[](int i)const{return s[i];}
 String& operator+=(char c){s+=c;return *this;}
 bool operator==(const char*o)const{return s==o;} bool operator==(const String&o)const{return s==o.s;}
 friend String operator+(const String&a,const String&b){return String(a.s+b.s);}
 friend String operator+(const String&a,const char*b){return String(a.s+b);}
};
struct SerialSim {
 std::deque<char> in; std::string out;
 void begin(int){}
 int available(){return in.size();} int read(){char c=in.front();in.pop_front();return c;}
 void print(const char*x){out+=x;} void print(const String&x){out+=x.s;} void print(char c){out+=c;}
 void println(){out+="\n";} void println(const char*x){out+=x;out+="\n";} void println(const String&x){out+=x.s+"\n";}
 void printf(const char*f,...){char b[400];va_list a;va_start(a,f);vsnprintf(b,400,f,a);va_end(a);out+=b;}
 void feed(const char*x){while(*x)in.push_back(*x++);}
};
extern SerialSim Serial;
