#!/usr/bin/env python3
import time
from flask import Flask, render_template, jsonify
import Adafruit_BBIO.GPIO as GPIO

# --- Configuración de pines (ajusta si necesitas) ---
TRIG = "P9_12"
ECHO = "P9_15"

# --- Inicialización GPIO ---
GPIO.setup(TRIG, GPIO.OUT)
GPIO.setup(ECHO, GPIO.IN)
GPIO.output(TRIG, GPIO.LOW)
time.sleep(0.1)

app = Flask(__name__)

def medir_distancia(timeout_s=0.05, muestras=3):
    """
    Mide la distancia en cm usando HC-SR04.
    Devuelve float con cm o None si falla.
    """
    lecturas = []
    for _ in range(muestras):
        # Asegurar TRIG bajo
        GPIO.output(TRIG, GPIO.LOW)
        time.sleep(0.00005)

        # Pulso TRIG ~10us
        GPIO.output(TRIG, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(TRIG, GPIO.LOW)

        pulse_start = None
        pulse_end = None
        start_time = time.time()

        # Espera inicio de eco
        while time.time() - start_time < timeout_s:
            if GPIO.input(ECHO):
                pulse_start = time.time()
                break
        if pulse_start is None:
            lecturas.append(None)
            time.sleep(0.02)
            continue

        # Espera fin de eco
        while time.time() - pulse_start < timeout_s:
            if not GPIO.input(ECHO):
                pulse_end = time.time()
                break
        if pulse_end is None:
            lecturas.append(None)
            time.sleep(0.02)
            continue

        pulse_duration = pulse_end - pulse_start
        distancia_cm = (pulse_duration * 34300.0) / 2.0
        lecturas.append(round(distancia_cm, 2))
        time.sleep(0.02)

    validas = [l for l in lecturas if l is not None]
    if not validas:
        return None
    return round(sum(validas) / len(validas), 2)

@app.route('/')
def index():
    dist = medir_distancia()
    status = "Aparcado" if (dist is not None and dist < 20) else "Libre"
    if dist is None:
        dist = 0
        status = "Error"
    # Tu HTML original usa {{ status }} y {{ distance }}
    return render_template('index.html', status=status, distance=dist)

@app.route('/distance')
def distance_json():
    dist = medir_distancia()
    if dist is None:
        return jsonify({"distance": None, "status": "Error"})
    status = "Aparcado" if dist < 20 else "Libre"
    return jsonify({"distance": dist, "status": status})

if __name__ == '__main__':
    # Ejecuta en localhost de la BBB. Cambia a '0.0.0.0' si quieres acceso directo en LAN.
    app.run(host='127.0.0.1', port=5000, debug=False)