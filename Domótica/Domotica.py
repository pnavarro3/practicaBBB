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

# Pines
LED_PINS = {"led1": "P8_10", "led2": "P8_12"}  # led1 = luz garaje, led2 = calefacción
LIGHT_PIN = "P8_14"       # sensor de luz digital
LM35_ADC_PIN = "P9_39"    # LM35
BUTTON_PIN = "P8_16"      # botón simulador

# ADC / LM35
ADC_VREF = 1.8
DIV_R1 = 1000.0
DIV_R2 = 1000.0
CAL_FACTOR = 0.25
CAL_OFFSET = 0.0

# Lógica auto
HEAT_ON_THRESHOLD = 20.0
HEAT_OFF_THRESHOLD = 21.0
BUTTON_PRESSED_MIN_S = 2.0
MANUAL_OVERRIDE_SEC = 60

io_lock = threading.Lock()

latest = {
    "temp_c": None,
    "light": None,  # 1 = oscuro, 0 = claro
    "ts": None,
    "saturated": False,
    "car_parked": False,
    "button_pressed": False,
    "outputs": {"led1": 0, "led2": 0},
    "manual_override_until": {"led1": 0, "led2": 0},
    "mode": "auto"  # "auto" | "manual"
}

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
    try:
        GPIO.setup(BUTTON_PIN, GPIO.IN)
    except Exception:
        pass
    time.sleep(0.05)

def read_lm35_temperature(samples=5, delay_s=0.01):
    if not HAS_BBIO:
        return 25.0, False
    divider_ratio = 1.0 if DIV_R1 <= 0 else (DIV_R2 / (DIV_R1 + DIV_R2))
    vals = []
    with io_lock:
        for _ in range(samples):
            try:
                v_prop = ADC.read(LM35_ADC_PIN)
                if v_prop is None:
                    raise Exception("ADC.read returned None")
                measured_v = v_prop * ADC_VREF
            except Exception:
                try:
                    raw = ADC.read_raw(LM35_ADC_PIN)
                    measured_v = (raw / 4095.0) * ADC_VREF
                except Exception:
                    measured_v = 0.0
            vals.append(measured_v)
            time.sleep(delay_s)
    if not vals:
        return None, False
    mean_measured_v = sum(vals) / len(vals)
    saturated = mean_measured_v >= (ADC_VREF * 0.98)
    actual_v = mean_measured_v if divider_ratio <= 0 else mean_measured_v / divider_ratio
    temp_raw = (actual_v * 1000.0) / 10.0
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

def read_button_pressed(debounce_s=0.05):
    if not HAS_BBIO:
        return False
    try:
        v1 = GPIO.input(BUTTON_PIN)
        if v1 != 0:
            return False
        time.sleep(debounce_s)
        v2 = GPIO.input(BUTTON_PIN)
        return v2 == 0
    except Exception:
        return False

def get_outputs_state():
    states = {}
    if not HAS_BBIO:
        return latest["outputs"].copy()
    with io_lock:
        for name, pin in LED_PINS.items():
            try:
                states[name] = 1 if GPIO.input(pin) else 0
            except Exception:
                states[name] = latest["outputs"].get(name, 0)
    return states

def set_output(name, value, manual=True):
    if name not in LED_PINS:
        return False, "unknown output"
    if not HAS_BBIO:
        latest["outputs"][name] = 1 if int(value) else 0
        if manual:
            latest["manual_override_until"][name] = time.time() + MANUAL_OVERRIDE_SEC
        return True, None
    pin = LED_PINS[name]
    with io_lock:
        try:
            GPIO.output(pin, GPIO.HIGH if int(value) else GPIO.LOW)
            latest["outputs"][name] = 1 if int(value) else 0
            if manual:
                latest["manual_override_until"][name] = time.time() + MANUAL_OVERRIDE_SEC
            return True, None
        except Exception as e:
            return False, str(e)

def apply_auto_logic():
    if latest.get("mode") != "auto":
        return
    now = time.time()
    temp = latest.get("temp_c")
    # calefacción (led2)
    override_until = latest["manual_override_until"].get("led2", 0)
    if temp is not None and now >= override_until:
        if temp < HEAT_ON_THRESHOLD and latest["outputs"].get("led2", 0) == 0:
            set_output("led2", 1, manual=False)
        elif temp >= HEAT_OFF_THRESHOLD and latest["outputs"].get("led2", 0) == 1:
            set_output("led2", 0, manual=False)
    # luz garaje (led1): coche y oscuro -> ON; claro -> OFF
    override_until = latest["manual_override_until"].get("led1", 0)
    light = latest.get("light")
    car = latest.get("car_parked", False)
    if now >= override_until and light is not None:
        if car and light == 1 and latest["outputs"].get("led1", 0) == 0:
            set_output("led1", 1, manual=False)
        if light == 0 and latest["outputs"].get("led1", 0) == 1:
            set_output("led1", 0, manual=False)

def background_reader(interval=0.2):
    button_counter = 0
    required_button_count = int(BUTTON_PRESSED_MIN_S / interval) if interval > 0 else 1
    while True:
        try:
            t, sat = read_lm35_temperature()
            l = read_light()
            btn = read_button_pressed()
            ts = time.time()
            latest["temp_c"] = t
            latest["light"] = l
            latest["ts"] = ts
            latest["saturated"] = bool(sat)
            latest["button_pressed"] = bool(btn)
            if btn:
                button_counter += 1
            else:
                button_counter = 0
            latest["car_parked"] = True if button_counter >= required_button_count else False
            apply_auto_logic()
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
            "saturated": latest["saturated"],
            "car_parked": latest["car_parked"],
            "button_pressed": latest["button_pressed"]
        },
        "manual_override_until": latest["manual_override_until"],
        "mode": latest["mode"]
    })

@app.route("/api/measure", methods=["POST", "GET"])
def api_measure():
    t, sat = read_lm35_temperature()
    l = read_light()
    btn = read_button_pressed()
    if t is None and l is None and not btn:
        return jsonify({"error": "read_error"}), 500
    latest["temp_c"] = t
    latest["light"] = l
    latest["ts"] = time.time()
    latest["saturated"] = bool(sat)
    latest["button_pressed"] = bool(btn)
    return jsonify({
        "temp_c": t,
        "light": l,
        "ts": latest["ts"],
        "saturated": latest["saturated"],
        "car_parked": latest["car_parked"],
        "button_pressed": latest["button_pressed"],
        "outputs": latest["outputs"],
        "mode": latest["mode"],
        "manual_override_until": latest["manual_override_until"]
    })

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
    ok, err = set_output(name, value, manual=True)
    if not ok:
        return jsonify({"error": err}), 500
    outputs = get_outputs_state()
    return jsonify({"state": value, "outputs": outputs, "manual_override_until": latest["manual_override_until"], "mode": latest["mode"]})

@app.route("/api/mode", methods=["POST", "GET"])
def api_mode():
    if request.method == "GET":
        return jsonify({"mode": latest["mode"]})
    j = request.get_json(force=True, silent=True)
    if not j:
        return jsonify({"error": "missing json body"}), 400
    mode = j.get("mode")
    if mode not in ("auto", "manual"):
        return jsonify({"error": "invalid mode"}), 400
    latest["mode"] = mode
    return jsonify({"mode": latest["mode"]})

@app.route("/api/car", methods=["POST"])
def api_car():
    j = request.get_json(force=True, silent=True)
    if not j or "car" not in j:
        return jsonify({"error": "missing car"}), 400
    val = 1 if int(j.get("car", 0)) else 0
    latest["car_parked"] = bool(val)
    return jsonify({"car_parked": latest["car_parked"]})

if __name__ == "__main__":
    hw_setup()
    t = threading.Thread(target=background_reader, args=(0.2,), daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000)
