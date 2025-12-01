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
BUTTON_PIN = "P8_16"

ADC_VREF = 1.8
DIV_R1 = 1000.0
DIV_R2 = 1000.0
CAL_FACTOR = 0.25
CAL_OFFSET = 0.0

HEAT_ON_THRESHOLD = 20.0
HEAT_OFF_THRESHOLD = 21.0
BUTTON_PRESSED_MIN_S = 2.0

io_lock = threading.Lock()

latest = {
    "temp_c": None,
    "light": None,               # 1 = oscuro, 0 = claro
    "ts": None,
    "saturated": False,
    "car_parked": False,
    "button_pressed": False,
    "outputs": {"led1": 0, "led2": 0},
    "manual_override_heating": False,
    "light_timer_until": 0.0
}

def hw_setup():
    if not HAS_BBIO:
        print("Simulación: Adafruit_BBIO no disponible")
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
    if not HAS_BBIO:
        return latest["outputs"].copy()
    states = {}
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
        return True, None
    pin = LED_PINS[name]
    with io_lock:
        try:
            GPIO.output(pin, GPIO.HIGH if int(value) else GPIO.LOW)
            latest["outputs"][name] = 1 if int(value) else 0
            return True, None
        except Exception as e:
            return False, str(e)

def apply_heating_logic(temp):
    if latest.get("manual_override_heating"):
        return
    if temp is None:
        return
    if temp < HEAT_ON_THRESHOLD and latest["outputs"].get("led2", 0) == 0:
        set_output("led2", 1, manual=False)
    elif temp >= HEAT_OFF_THRESHOLD and latest["outputs"].get("led2", 0) == 1:
        set_output("led2", 0, manual=False)

def apply_light_logic(now):
    light_sensor = latest.get("light")
    car = latest.get("car_parked", False)
    timer_until = latest.get("light_timer_until", 0.0)

    if car and light_sensor == 1:
        set_output("led1", 1, manual=False)
        return

    if timer_until and timer_until > now:
        set_output("led1", 1, manual=False)
        return

    if light_sensor == 0:
        set_output("led1", 0, manual=False)
        return

    if light_sensor == 1 and not car:
        set_output("led1", 0, manual=False)
        return

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
            if button_counter >= required_button_count:
                latest["car_parked"] = True
            apply_heating_logic(t)
            apply_light_logic(ts)
        except Exception:
            pass
        time.sleep(interval)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    outputs = get_outputs_state()
    # ahora siempre devolvemos temperatura (si está disponible)
    temp = latest.get("temp_c")
    return jsonify({
        "outputs": outputs,
        "sensors": {
            "temp_c": temp,
            "light": latest.get("light"),
            "ts": latest.get("ts"),
            "saturated": latest.get("saturated"),
            "car_parked": latest.get("car_parked"),
            "button_pressed": bool(latest.get("button_pressed", False))
        },
        "manual_override_heating": latest.get("manual_override_heating", False),
        "light_timer_until": latest.get("light_timer_until", 0.0)
    })

@app.route("/api/measure", methods=["POST", "GET"])
def api_measure():
    t, sat = read_lm35_temperature()
    l = read_light()
    btn = read_button_pressed()
    latest["temp_c"] = t
    latest["light"] = l
    latest["ts"] = time.time()
    latest["saturated"] = bool(sat)
    latest["button_pressed"] = bool(btn)
    outputs = get_outputs_state()
    return jsonify({
        "temp_c": latest.get("temp_c"),
        "light": l,
        "ts": latest["ts"],
        "saturated": latest["saturated"],
        "car_parked": latest["car_parked"],
        "button_pressed": latest["button_pressed"],
        "outputs": outputs,
        "manual_override_heating": latest.get("manual_override_heating", False),
        "light_timer_until": latest.get("light_timer_until", 0.0)
    })

@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    j = request.get_json(force=True, silent=True)
    if not j:
        return jsonify({"error": "missing json body"}), 400
    name = j.get("name")
    if name not in ("led2",):
        return jsonify({"error": "invalid name"}), 400
    try:
        value = 1 if int(j.get("value", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid value"}), 400
    ok, err = set_output(name, value, manual=True)
    if not ok:
        return jsonify({"error": err}), 500
    if name == "led2":
        latest["manual_override_heating"] = True if value == 1 else False
    outputs = get_outputs_state()
    return jsonify({"state": value, "outputs": outputs, "manual_override_heating": latest.get("manual_override_heating", False)})

@app.route("/api/light_pulse", methods=["POST"])
def api_light_pulse():
    now = time.time()
    light_sensor = latest.get("light")
    car = latest.get("car_parked", False)
    if light_sensor is None:
        return jsonify({"error": "light sensor unknown"}), 400
    if light_sensor == 0:
        latest["light_timer_until"] = now + 10.0
        set_output("led1", 1, manual=False)
        return jsonify({"light_timer_until": latest["light_timer_until"], "duration": 10})
    else:
        if not car:
            latest["light_timer_until"] = now + 20.0
            set_output("led1", 1, manual=False)
            return jsonify({"light_timer_until": latest["light_timer_until"], "duration": 20})
        else:
            return jsonify({"light_timer_until": latest["light_timer_until"], "duration": 0, "note": "already on due to car and dark"})

@app.route("/api/car", methods=["POST"])
def api_car():
    j = request.get_json(force=True, silent=True)
    if not j or "car" not in j:
        return jsonify({"error": "missing car"}), 400
    try:
        val = 1 if int(j.get("car", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid car value"}), 400
    # lógica invertida: 1 -> no coche, 0 -> hay coche
    latest["car_parked"] = not bool(val)
    return jsonify({"car_parked": latest["car_parked"]})

if __name__ == "__main__":
    print("DEBUG: entrando en __main__", flush=True)
    hw_setup()
    print("DEBUG: hw_setup completado", flush=True)
    t = threading.Thread(target=background_reader, args=(0.2,), daemon=True)
    t.start()
    print("DEBUG: background_reader arrancado", flush=True)
    app.run(host="0.0.0.0", port=5000)
