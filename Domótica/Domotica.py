#!/usr/bin/env python3
# Sistema domótico para BeagleBone Black - Control de iluminación y calefacción
from flask import Flask, render_template, jsonify, request
import time, threading

# Intentar importar librerías de BeagleBone (permite ejecución en simulación)
try:
    import Adafruit_BBIO.GPIO as GPIO
    import Adafruit_BBIO.ADC as ADC
    HAS_BBIO = True
except Exception:
    HAS_BBIO = False

app = Flask(__name__)

# === CONFIGURACIÓN DE PINES ===
LED_PINS = {"led1": "P8_10", "led2": "P8_12"}  # led1: luz, led2: calefacción
LIGHT_PIN = "P8_14"          # Sensor de luz (1=oscuro, 0=claro)
LM35_ADC_PIN = "P9_39"       # Sensor temperatura LM35
BUTTON_PIN = "P8_16"         # Botón simulador presencia coche

# === CONFIGURACIÓN ADC Y CALIBRACIÓN ===
ADC_VREF = 1.8               # Voltaje referencia ADC BeagleBone
DIV_R1 = 1000.0              # Resistencia divisor de tensión R1
DIV_R2 = 1000.0              # Resistencia divisor de tensión R2
CAL_FACTOR = 0.25            # Factor calibración temperatura
CAL_OFFSET = 0.0             # Offset calibración temperatura

# === UMBRALES DE CONTROL ===
HEAT_ON_THRESHOLD = 20.0     # Temperatura para activar calefacción
HEAT_OFF_THRESHOLD = 21.0    # Temperatura para desactivar calefacción
BUTTON_PRESSED_MIN_S = 2.0   # Segundos botón presionado para detectar coche

io_lock = threading.Lock()   # Lock para acceso seguro a GPIO

# === ESTADO GLOBAL DEL SISTEMA ===
latest = {
    "temp_c": None,                    # Temperatura actual en °C
    "light": None,                     # Estado sensor luz: 1=oscuro, 0=claro
    "ts": None,                        # Timestamp última lectura
    "saturated": False,                # ADC saturado (voltaje máximo)
    "car_parked": False,               # Coche detectado (botón >2s)
    "button_pressed": False,           # Estado instantáneo del botón
    "outputs": {"led1": 0, "led2": 0}, # Estado LEDs (0=apagado, 1=encendido)
    "manual_override_heating": False,  # Modo manual calefacción activado
    "light_timer_until": 0.0           # Timestamp fin temporizador luz
}

# === INICIALIZACIÓN HARDWARE ===
def hw_setup():
    """Configura pines GPIO y ADC al iniciar el sistema"""
    if not HAS_BBIO:
        print("Simulación: Adafruit_BBIO no disponible")
        return
    try:
        ADC.setup()  # Inicializar conversor analógico-digital
    except Exception:
        pass
    # Configurar LEDs como salidas y apagarlos
    for p in LED_PINS.values():
        GPIO.setup(p, GPIO.OUT)
        GPIO.output(p, GPIO.LOW)
    # Configurar sensor de luz como entrada
    GPIO.setup(LIGHT_PIN, GPIO.IN)
    # Configurar botón como entrada
    try:
        GPIO.setup(BUTTON_PIN, GPIO.IN)
    except Exception:
        pass
    time.sleep(0.05)  # Estabilizar hardware

# === LECTURA SENSOR TEMPERATURA ===
def read_lm35_temperature(samples=5, delay_s=0.01):
    """Lee temperatura del LM35 con promedio de múltiples muestras
    
    Returns:
        (temperatura_celsius, saturated): Temperatura y flag de saturación ADC
    """
    if not HAS_BBIO:
        return 25.0, False  # Valor simulado
    
    # Calcular ratio del divisor de tensión
    divider_ratio = 1.0 if DIV_R1 <= 0 else (DIV_R2 / (DIV_R1 + DIV_R2))
    vals = []
    
    # Leer múltiples muestras para promediar (reduce ruido)
    with io_lock:
        for _ in range(samples):
            try:
                v_prop = ADC.read(LM35_ADC_PIN)  # Lectura normalizada 0-1
                if v_prop is None:
                    raise Exception("ADC.read returned None")
                measured_v = v_prop * ADC_VREF  # Convertir a voltaje
            except Exception:
                try:
                    raw = ADC.read_raw(LM35_ADC_PIN)  # Lectura raw 0-4095
                    measured_v = (raw / 4095.0) * ADC_VREF
                except Exception:
                    measured_v = 0.0
            vals.append(measured_v)
            time.sleep(delay_s)
    
    if not vals:
        return None, False
    
    # Calcular promedio y detectar saturación
    mean_measured_v = sum(vals) / len(vals)
    saturated = mean_measured_v >= (ADC_VREF * 0.98)  # ADC al 98% = saturado
    
    # Compensar divisor de tensión
    actual_v = mean_measured_v if divider_ratio <= 0 else mean_measured_v / divider_ratio
    
    # Convertir voltaje a temperatura (LM35: 10mV/°C)
    temp_raw = (actual_v * 1000.0) / 10.0
    temp_cal = (temp_raw * CAL_FACTOR) + CAL_OFFSET  # Aplicar calibración
    return round(temp_cal, 2), saturated

# === LECTURA SENSOR LUZ ===
def read_light():
    """Lee sensor de luz digital
    
    Returns:
        1 = oscuro (sensor activado)
        0 = claro (sensor desactivado)
    """
    if not HAS_BBIO:
        return 1  # Simulación: siempre oscuro
    with io_lock:
        try:
            val = GPIO.input(LIGHT_PIN)
            return 1 if val else 0
        except Exception:
            return None

# === LECTURA BOTÓN ===
def read_button_pressed(debounce_s=0.05):
    """Lee estado del botón con debounce
    
    Returns:
        True = botón presionado (conectado a GND)
        False = botón suelto
    """
    if not HAS_BBIO:
        return False
    try:
        v1 = GPIO.input(BUTTON_PIN)
        if v1 != 0:  # No presionado
            return False
        time.sleep(debounce_s)  # Esperar para evitar rebotes
        v2 = GPIO.input(BUTTON_PIN)
        return v2 == 0  # Confirmar que sigue presionado
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

# === LÓGICA AUTOMÁTICA CALEFACCIÓN ===
def apply_heating_logic(temp):
    """Control automático de calefacción por temperatura con histéresis
    
    - Enciende si temp < 20°C
    - Apaga si temp >= 21°C
    - No actúa si está en modo manual
    """
    if latest.get("manual_override_heating"):
        return  # Modo manual activado, no interferir
    if temp is None:
        return
    
    # Lógica con histéresis (evita oscilaciones)
    if temp < HEAT_ON_THRESHOLD and latest["outputs"].get("led2", 0) == 0:
        set_output("led2", 1, manual=False)  # Encender calefacción
    elif temp >= HEAT_OFF_THRESHOLD and latest["outputs"].get("led2", 0) == 1:
        set_output("led2", 0, manual=False)  # Apagar calefacción

# === LÓGICA AUTOMÁTICA ILUMINACIÓN ===
def apply_light_logic(now):
    """Control automático de luz según sensor, coche y temporizador
    
    Prioridades:
    1. Coche presente + oscuro = luz ON
    2. Temporizador activo = luz ON
    3. Sensor claro = luz OFF
    4. Oscuro sin coche = luz OFF
    """
    light_sensor = latest.get("light")  # 1=oscuro, 0=claro
    car = latest.get("car_parked", False)
    timer_until = latest.get("light_timer_until", 0.0)

    # Prioridad 1: Coche aparcado + oscuro → luz encendida
    if car and light_sensor == 1:
        set_output("led1", 1, manual=False)
        return

    # Prioridad 2: Temporizador activo → mantener luz
    if timer_until and timer_until > now:
        set_output("led1", 1, manual=False)
        return

    # Prioridad 3: Sensor detecta luz → apagar
    if light_sensor == 0:
        set_output("led1", 0, manual=False)
        return

    # Prioridad 4: Oscuro pero sin coche → apagar
    if light_sensor == 1 and not car:
        set_output("led1", 0, manual=False)
        return

# === HILO DE LECTURA CONTINUA ===
def background_reader(interval=0.2):
    """Lee sensores continuamente y aplica lógicas automáticas
    
    Se ejecuta en hilo separado cada 0.2s:
    - Lee temperatura, luz y botón
    - Detecta coche (botón >2s continuo)
    - Aplica control automático calefacción e iluminación
    """
    button_counter = 0
    # Calcular cuántas lecturas consecutivas = 2 segundos de pulsación
    required_button_count = int(BUTTON_PRESSED_MIN_S / interval) if interval > 0 else 1
    
    while True:
        try:
            # Leer todos los sensores
            t, sat = read_lm35_temperature()
            l = read_light()
            btn = read_button_pressed()
            ts = time.time()
            
            # Actualizar estado global
            latest["temp_c"] = t
            latest["light"] = l
            latest["ts"] = ts
            latest["saturated"] = bool(sat)
            latest["button_pressed"] = bool(btn)
            
            # Detectar coche: botón presionado >2s
            if btn:
                button_counter += 1
            else:
                button_counter = 0
            
            if button_counter >= required_button_count:
                latest["car_parked"] = True  # Marcar coche aparcado
            
            # Aplicar lógicas automáticas
            apply_heating_logic(t)
            apply_light_logic(ts)
        except Exception:
            pass
        time.sleep(interval)  # Esperar antes de siguiente lectura

# === RUTAS WEB API ===
@app.route("/")
def index():
    """Ruta principal - Sirve interfaz HTML"""
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    """Devuelve estado actual del sistema (sensores y salidas)"""
    outputs = get_outputs_state()
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
    """Fuerza lectura inmediata de sensores y devuelve valores"""
    t, sat = read_lm35_temperature()
    l = read_light()
    btn = read_button_pressed()
    
    # Actualizar estado con nuevas lecturas
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
    """Controla manualmente la calefacción (led2)
    
    Body JSON: {"name": "led2", "value": 0 o 1}
    Activa modo manual al encender calefacción
    """
    j = request.get_json(force=True, silent=True)
    if not j:
        return jsonify({"error": "missing json body"}), 400
    
    name = j.get("name")
    if name not in ("led2",):  # Solo calefacción controlable manualmente
        return jsonify({"error": "invalid name"}), 400
    
    try:
        value = 1 if int(j.get("value", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid value"}), 400
    
    ok, err = set_output(name, value, manual=True)
    if not ok:
        return jsonify({"error": err}), 500
    
    # Activar/desactivar modo manual según si se enciende/apaga
    if name == "led2":
        latest["manual_override_heating"] = True if value == 1 else False
    
    outputs = get_outputs_state()
    return jsonify({
        "state": value, 
        "outputs": outputs, 
        "manual_override_heating": latest.get("manual_override_heating", False)
    })

@app.route("/api/light_pulse", methods=["POST"])
def api_light_pulse():
    """Activa luz temporalmente según sensor
    
    - Sensor claro: enciende 10 segundos
    - Sensor oscuro sin coche: enciende 20 segundos
    - Coche aparcado + oscuro: ya está encendida (no hace nada)
    """
    now = time.time()
    light_sensor = latest.get("light")
    car = latest.get("car_parked", False)
    
    if light_sensor is None:
        return jsonify({"error": "light sensor unknown"}), 400
    
    # Lógica diferenciada según condiciones
    if light_sensor == 0:  # Claro → 10 segundos
        latest["light_timer_until"] = now + 10.0
        set_output("led1", 1, manual=False)
        return jsonify({"light_timer_until": latest["light_timer_until"], "duration": 10})
    else:  # Oscuro
        if not car:  # Sin coche → 20 segundos
            latest["light_timer_until"] = now + 20.0
            set_output("led1", 1, manual=False)
            return jsonify({"light_timer_until": latest["light_timer_until"], "duration": 20})
        else:  # Con coche → ya encendida automáticamente
            return jsonify({
                "light_timer_until": latest["light_timer_until"], 
                "duration": 0, 
                "note": "already on due to car and dark"
            })

@app.route("/api/car", methods=["POST"])
def api_car():
    """Controla manualmente estado del coche aparcado
    
    Body JSON: {"car": 0 o 1}
    Nota: lógica invertida (1=no hay coche, 0=hay coche)
    """
    j = request.get_json(force=True, silent=True)
    if not j or "car" not in j:
        return jsonify({"error": "missing car"}), 400
    
    try:
        val = 1 if int(j.get("car", 0)) else 0
    except Exception:
        return jsonify({"error": "invalid car value"}), 400
    
    # Lógica invertida: 1 -> no coche, 0 -> hay coche
    latest["car_parked"] = not bool(val)
    return jsonify({"car_parked": latest["car_parked"]})

# === PUNTO DE ENTRADA ===
if __name__ == "__main__":
    print("DEBUG: Iniciando sistema domótico", flush=True)
    hw_setup()  # Configurar hardware
    print("DEBUG: Hardware configurado", flush=True)
    
    # Arrancar hilo de lectura continua en background
    t = threading.Thread(target=background_reader, args=(0.2,), daemon=True)
    t.start()
    print("DEBUG: Hilo de lectura iniciado", flush=True)
    
    # Iniciar servidor web Flask
    app.run(host="0.0.0.0", port=5000)
