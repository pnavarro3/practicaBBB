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

LED_PINS = {"led1": "P8_10", "led2": "P8_12"}  # led1 = luz garaje, led2 = calefacción
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
    "manual_override_heating": False,   # True si calefacción fue activada manualmente y debe permanecer hasta apagado manual
    "light_timer_until": 0.0            # timestamp hasta el que la luz debe permanecer encendida por pulsos
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
    # Si calefacción fue activada manualmente y está en modo manual persistente, no cambiar
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

    # Si hay coche y está oscuro -> luz ON permanente mientras haya coche
    if car and light_sensor == 1:
        set_output("led1", 1, manual=False)
        return

    # Si hay timer activo -> mantener luz encendida
    if timer_until and timer_until > now:
        set_output("led1", 1, manual=False)
        return

    # Si está claro -> apagar (salvo timer)
    if light_sensor == 0:
        set_output("led1", 0, manual=False)
        return

    # Si está oscuro y no hay coche -> apagar (salvo timer)
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
            # apply automatic logic
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
    # mostrar temperatura solo si luz o calefacción están encendidas
    show_temp = bool(outputs.get("led1", 0) or outputs.get("led2", 0))
    temp = None
    if show_temp:
        temp = latest.get("temp_c")
    return jsonify({
        "outputs": outputs,
        "sensors": {
            "temp_c": temp,
            "light": latest["light"],
            "ts": latest["ts"],
            "saturated": latest["saturated"],
            "car_parked": latest["car_parked"],
            "button_pressed": latest["button_pressed"]
        },
<<<<<<< HEAD
        "manual_override_until": latest["manual_override_until"],
        "mode": latest["mode"]
=======
        "manual_override_heating": latest["manual_override_heating"],
        "light_timer_until": latest["light_timer_until"]
>>>>>>> 10c89b6a046a7f3918315ca0b27443c700979e4a
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
    show_temp = bool(outputs.get("led1", 0) or outputs.get("led2", 0))
    temp = t if show_temp else None
    return jsonify({
        "temp_c": temp,
        "light": l,
        "ts": latest["ts"],
        "saturated": latest["saturated"],
        "car_parked": latest["car_parked"],
<<<<<<< HEAD
        "button_pressed": latest["button_pressed"],
        "outputs": outputs,
        "mode": latest["mode"],
        "manual_override_until": latest["manual_override_until"]
    })
=======
        "button_pressed
>>>>>>> 10c89b6a046a7f3918315ca0b27443c700979e4a
