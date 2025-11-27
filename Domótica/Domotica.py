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

# ADC referencia BBB
ADC_VREF = 1.8

# Divisor físico: R1 entre Vout(LM35) y ADC, R2 entre ADC y GND.
# Si usas dos resistencias de 1k: DIV_R1 = 1000, DIV_R2 = 1000
DIV_R1 = 1000.0
DIV_R2 = 1000.0

# Factor de calibración para ajustar la lectura final.
# Con DIV_R1=1000 y DIV_R2=1000, si P9_39 mide 0.4 V:
# actual_v = 0.4 / 0.5 = 0.8 V -> temp_raw = 80 °C.
# Para que eso devuelva 20 °C usamos CAL_FACTOR = 20 / 80 = 0.25
CAL_FACTOR = 0.25
CAL_OFFSET = 0.0  # si necesitas un desplazamiento lineal, ajústalo aquí

io_lock = threading.Lock()
latest = {"temp_c": None, "light": None, "ts": None, "saturated": False}

def hw_setup():
    if not HAS_BBIO:
        print("Modo simulación: Adafruit_BBIO no disponible")
        return
    try:
        ADC.setup()
    except Exception:
        pass
    for p in LED_PINS.values():
        GPIO.setup(p, GPIO.OUT)
        GPIO.output(p, GPIO.LOW)
    GPIO.setup(LIGHT_PIN, GPIO.IN)

def read_lm35_temperature(samples=5, delay_s=0.01):
    """
    Lee LM35 aplicando divisor y calibración.
    Devuelve (temp_c, saturated_flag).
    """
    if not HAS_BBIO:
        return 25.0, False

    if DIV_R1 <= 0:
        divider_ratio = 1.0
    else:
        divider_ratio = DIV_R2 / (DIV_R1 + DIV_R2)

    vals = []
    with io_lock:
        for _ in range(samples):
            try:
                v_prop = ADC.read(LM35_ADC_PIN)  # 0..1
                if v_prop is None:
                    raise Exception("ADC.read returned None")
                measured_v = v_prop * ADC_VREF
            except Exception:
                try:
                    raw = ADC.read_raw(LM35_ADC_PIN)  # 0..4095
                    measured_v = (raw / 4095.0) * ADC_VREF
                except Exception:
                    measured_v = 0.0
            vals.append(measured_v)
            time.sleep(delay_s)

    if not vals:
        return None, False

    mean_measured_v = sum(vals) / len(vals)
    saturated = mean_measured_v >= (ADC_VREF * 0.98)

    # recuperar voltaje real en la salida del LM35
    if divider_ratio <= 0:
        actual_v = mean_measured_v
    else:
        actual_v = mean_measured_v / divider_ratio

    # cálculo estándar: LM35 -> 10 mV/°C
    temp_raw = (actual_v * 1000.0) / 10.0

    # aplicar calibración lineal
    temp_cal = (temp_raw * CAL_FACTOR) + CAL_OFFSET

    return round(temp_cal, 2), saturated

def read_light():
    if not HAS_BBIO:
        return 1
    with io_lock:
        try:
            val = GPIO.input(LIGHT_PIN)
            return 1 if val else 0
        except Exception:
            return None

def get_outputs_state():
    states = {}
    if not HAS_BBIO:
        for name in LED_PINS:
            states[name] = 0
        return states
    with io_lock:
        for name, pin in LED_PINS.items():
            try:
                states[name] = 1 if GPIO.input(pin) else 0
            except Exception:
                states[name] = 0
    return states

def set_output(name, value):
    if name not in LED_PINS:
        return False, "unknown output"
    if not HAS_BBIO:
        return True, None
    pin = LED_PINS[name]
    with io_lock:
        try:
            GPIO.output(pin, GPIO.HIGH if int(value) else GPIO.LOW)
            return True, None
        except Exception as e:
            return False, str(e)

def background_reader(interval=3.0):
    while True:
        try:
            t, sat = read_lm35_temperature()
            l = read_light()
            ts = time.time()
            latest["temp_c"] = t
            latest["light"] = l
            latest["ts"] = ts
            latest["saturated"] = bool(sat)
        except Exception:
            pass
        time.sleep(interval)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    outputs = get_outputs_state()
    return jsonify({
        "outputs": outputs,
        "sensors": {
            "temp_c": latest["temp_c"],
            "light": latest["light"],
            "ts": latest["ts"],
            "saturated": latest["saturated"]
        }
    })

@app.route("/api/measure", methods=["POST", "GET"])
def api_measure():
    t, sat = read_lm35_temperature()
    l = read_light()
    if t is None and l is None:
        return jsonify({"error": "read_error"}), 500
    latest["temp_c"] = t
    latest["light"] = l
    latest["ts"] = time.time()
    latest["saturated"] = bool(sat)
    return jsonify({"temp_c": t, "light": l, "ts": latest["ts"], "saturated": latest["saturated"]})

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    j = request.get_json(force=True, silent=True)
    if not j:
        return jsonify({"error": "missing json body"}), 400
    name = j.get("name")
    if name is None:
        return jsonify({"error": "missing name"}), 400
    try:
        value = 1 if int(j.get("value", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid value"}), 400
    ok, err = set_output(name, value)
    if not ok:
        return jsonify({"error": err}), 500
    outputs = get_outputs_state()
    return jsonify({"state": value, "outputs": outputs})

if __name__ == "__main__":
    hw_setup()
    t = threading.Thread(target=background_reader, args=(3.0,), daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000)
