#!/usr/bin/env python3
from flask import Flask, render_template, jsonify, request
import time, threading

try:
    import Adafruit_BBIO.GPIO as GPIO
    import Adafruit_BBIO.ADC as ADC
    HAS_BBIO = True
except Exception:
    HAS_BBIO = False

app = Flask(__name__)

LED_PINS = {"led1": "P8_10", "led2": "P8_12"}
LIGHT_PIN = "P8_14"
LM35_ADC_PIN = "P9_39"
ADC_VREF = 1.8
io_lock = threading.Lock()

# almacenamiento de lecturas recientes
latest = {"temp_c": None, "light": None, "ts": None}

def hw_setup():
    if not HAS_BBIO: return
    try: ADC.setup()
    except: pass
    for p in LED_PINS.values():
        GPIO.setup(p, GPIO.OUT); GPIO.output(p, GPIO.LOW)
    GPIO.setup(LIGHT_PIN, GPIO.IN)

def read_lm35_temperature(samples=5, delay_s=0.01):
    if not HAS_BBIO: return 25.0
    vals = []
    with io_lock:
        for _ in range(samples):
            try:
                v_raw = ADC.read(LM35_ADC_PIN)
            except Exception:
                try:
                    v_raw = ADC.read_raw(LM35_ADC_PIN) / 4095.0
                except Exception:
                    v_raw = 0.0
            vals.append(v_raw); time.sleep(delay_s)
    if not vals: return None
    mean_raw = sum(vals)/len(vals)
    voltage = mean_raw * ADC_VREF
    temp_c = (voltage * 1000) / 10.0
    return round(temp_c, 2)

def read_light():
    if not HAS_BBIO: return 1
    with io_lock:
        try:
            val = GPIO.input(LIGHT_PIN)
            return 1 if val else 0
        except Exception:
            return None

def get_outputs_state():
    states = {}
    if not HAS_BBIO:
        for name in LED_PINS: states[name] = 0
        return states
    with io_lock:
        for name, pin in LED_PINS.items():
            try: states[name] = 1 if GPIO.input(pin) else 0
            except: states[name] = 0
    return states

def set_output(name, value):
    if name not in LED_PINS: return False, "unknown output"
    if not HAS_BBIO: return True, None
    pin = LED_PINS[name]
    with io_lock:
        try:
            GPIO.output(pin, GPIO.HIGH if int(value) else GPIO.LOW)
            return True, None
        except Exception as e:
            return False, str(e)

# hilo que actualiza latest cada 3 segundos
def background_reader(interval=3.0):
    while True:
        try:
            t = read_lm35_temperature()
            l = read_light()
            ts = time.time()
            latest["temp_c"] = t
            latest["light"] = l
            latest["ts"] = ts
        except Exception:
            pass
        time.sleep(interval)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    outputs = get_outputs_state()
    return jsonify({"outputs": outputs, "sensors": {"temp_c": latest["temp_c"], "light": latest["light"], "ts": latest["ts"]}})

@app.route("/api/measure", methods=["POST", "GET"])
def api_measure():
    # lectura a demanda además de la lectura periódica
    t = read_lm35_temperature()
    l = read_light()
    if t is None and l is None:
        return jsonify({"error": "read_error"}), 500
    # actualizar cache con la lectura inmediata
    latest["temp_c"] = t
    latest["light"] = l
    latest["ts"] = time.time()
    return jsonify({"temp_c": t, "light": l, "ts": latest["ts"]})

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    j = request.get_json(force=True, silent=True)
    if not j: return jsonify({"error": "missing json body"}), 400
    name = j.get("name")
    if name is None: return jsonify({"error": "missing name"}), 400
    try:
        value = 1 if int(j.get("value", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid value"}), 400
    ok, err = set_output(name, value)
    if not ok: return jsonify({"error": err}), 500
    outputs = get_outputs_state()
    return jsonify({"state": value, "outputs": outputs})

if __name__ == "__main__":
    hw_setup()
    t = threading.Thread(target=background_reader, args=(3.0,), daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000)
