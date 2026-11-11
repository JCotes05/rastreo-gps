#!/bin/sh
# Compila y corre la prueba de lógica del firmware en el PC (necesita g++).
cd "$(dirname "$0")"
cp ../esp32_sensores/esp32_sensores.ino sketch.cpp
g++ -std=c++17 -Iinc -w -o prueba prueba.cpp && ./prueba
r=$?; rm -f sketch.cpp prueba; exit $r
