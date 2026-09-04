import os
import requests
import json
import time
from dotenv import load_dotenv

# ==========================================
# Cargar variables de entorno (.env)
# ==========================================
load_dotenv()

# ==========================================
# CONFIGURACIÓN
# ==========================================
URL = os.getenv("CREDITO_AGENT_URL", "http://127.0.0.1:8001/chat")

# Clave específica del agente de crédito (vacía si se prueba en local)
CREDITO_API_KEY = os.getenv("CREDITO_AGENT_API_KEY", "")

HEADERS = {"Content-Type": "application/json"}
if CREDITO_API_KEY:
    HEADERS["x-api-key"] = CREDITO_API_KEY

SESSION_ID = "sesion-credito-001"

# ==========================================
# PREGUNTAS AUTOMÁTICAS (Basadas en la data inicial de CREDITO_DB)
# ==========================================
# Cada tupla es (pregunta, context). El context viaja igual que cuando el agente
# boti-bank llama a este agente por A2A: los datos duros no van en la prosa.
PREGUNTAS_BASE = [
    # --- Consultas sobre la data precargada ---
    ("¿Qué tarjetas de crédito tiene este cliente, con su límite y su disponible?",
     {"cliente_id": "71992c72-cc1c-4c5a-8b50-9ee4fb6c214d"}),

    ("Mostrame el resumen de cuenta de esta tarjeta con el detalle de las compras vigentes.",
     {"tarjeta_id": "TC-4821"}),

    ("Mostrame el resumen de cuenta de esta tarjeta.",
     {"tarjeta_id": "TC-7315"}),

    # --- Solicitud aprobada (Carlos no tiene tarjeta; con este perfil califica) ---
    ("Solicitá una tarjeta de crédito para este cliente y decime el límite y el disponible.",
     {"cliente_id": "33333333-cc1c-4c5a-8b50-9ee4fb6c214d", "titular": "Carlos Pérez",
      "ingreso_mensual": 1200.0, "saldo_total": 250.0, "cuota_deuda_mensual": 0.0}),

    # --- Solicitud rechazada por ingresos insuficientes (el perfil real de Carlos) ---
    ("Solicitá una tarjeta de crédito para este cliente.",
     {"cliente_id": "99999999-aaaa-bbbb-cccc-dddddddddddd", "titular": "Cliente Sin Ingresos",
      "ingreso_mensual": 375.0, "saldo_total": 250.0, "cuota_deuda_mensual": 0.0}),

    # --- Solicitud rechazada por endeudamiento alto (DTI > 50%) ---
    ("Solicitá una tarjeta de crédito para este cliente.",
     {"cliente_id": "77777777-aaaa-bbbb-cccc-dddddddddddd", "titular": "Cliente Endeudado",
      "ingreso_mensual": 1000.0, "saldo_total": 500.0, "cuota_deuda_mensual": 800.0}),

    # --- Idempotencia: Ana ya tiene tarjeta activa, no se emite otra ---
    ("Solicitá una tarjeta de crédito para este cliente.",
     {"cliente_id": "71992c72-cc1c-4c5a-8b50-9ee4fb6c214d", "titular": "Ana García",
      "ingreso_mensual": 1325.25, "saldo_total": 1900.50, "cuota_deuda_mensual": 800.0}),

    # --- Compra dentro del disponible ---
    ("Registrá esta compra con tarjeta en cuotas y decime el id de transacción.",
     {"tarjeta_id": "TC-4821", "objeto": "Microondas Whirlpool", "cuotas": 4, "monto_cuota": 75.0}),

    # --- Compra rechazada por crédito insuficiente ---
    ("Registrá esta compra con tarjeta en cuotas.",
     {"tarjeta_id": "TC-4821", "objeto": "Auto usado", "cuotas": 12, "monto_cuota": 1000.0}),

    # --- Compra rechazada por cantidad de cuotas inválida ---
    ("Registrá esta compra con tarjeta en cuotas.",
     {"tarjeta_id": "TC-7315", "objeto": "Televisor OLED", "cuotas": 36, "monto_cuota": 100.0}),

    # --- Abono de una cuota sobre la compra precargada de Ana ---
    ("Aplicá este abono y decime el saldo pendiente y las cuotas que quedan.",
     {"transaccion_id": "TRX-000101", "monto": 150.0}),

    # --- Abono rechazado por superar el saldo pendiente ---
    ("Aplicá este abono.",
     {"transaccion_id": "TRX-000102", "monto": 5000.0}),

    # --- Abono rechazado: la transacción ya está cancelada ---
    ("Aplicá este abono.",
     {"transaccion_id": "TRX-000104", "monto": 120.0}),
]

def ejecutar_pruebas_automaticas():
    print("=" * 60)
    print("💳 AUTOMATIZADOR DE PRUEBAS - AGENTE DE CRÉDITO")
    print("=" * 60)

    while True:
        try:
            cantidad_preguntas = int(input(f"¿Cuántas preguntas automáticas deseas enviar al agente? (hay {len(PREGUNTAS_BASE)} distintas): "))
            if cantidad_preguntas > 0:
                break
            else:
                print("Por favor, ingresa un número mayor a 0.")
        except ValueError:
            print("Entrada inválida. Debes ingresar un número entero.")

    print(f"\n🚀 Iniciando batería de {cantidad_preguntas} pregunta(s) automáticas.")
    print("-" * 60)

    for i in range(cantidad_preguntas):
        pregunta_actual, contexto = PREGUNTAS_BASE[i % len(PREGUNTAS_BASE)]

        print(f"\n▶ PREGUNTA {i + 1} DE {cantidad_preguntas}")
        print(f"✍️  Enviando: {pregunta_actual}")

        payload = {
            "message": pregunta_actual,
            "session_id": SESSION_ID,
            "context": contexto
        }

        # --- INICIO DEL TRACKING ---
        print("\n" + "." * 60)
        print("🔍 [TRACK] ENVIANDO PETICIÓN (REQUEST)")
        print(f"URL     : {URL}")

        # Ocultamos la clave x-api-key en la consola
        safe_headers = HEADERS.copy()
        if "x-api-key" in safe_headers and len(safe_headers["x-api-key"]) > 10:
            safe_headers["x-api-key"] = safe_headers["x-api-key"][:10] + "...[OCULTO]"

        print(f"Headers : {json.dumps(safe_headers, indent=2)}")
        print(f"Body    : {json.dumps(payload, indent=2, ensure_ascii=False)}")
        print("." * 60)

        try:
            response = requests.post(URL, headers=HEADERS, json=payload, timeout=60)

            # --- SEGUIMIENTO DE LA RESPUESTA ---
            print("\n🔍 [TRACK] RESPUESTA RECIBIDA (RESPONSE)")
            print(f"Status Code : {response.status_code}")

            try:
                raw_json = response.json()
                print(f"Raw JSON    :\n{json.dumps(raw_json, indent=2, ensure_ascii=False)}")
            except json.JSONDecodeError:
                print(f"Raw Text    : {response.text}")
            print("." * 60)

            response.raise_for_status()

            respuesta_agente = raw_json.get("response", "[No se encontró el campo 'response']")
            print(f"\n💳 Crédito: {respuesta_agente}\n")
            print("-" * 60)

        except requests.exceptions.RequestException as e:
            print(f"\n❌ ERROR EN LA COMUNICACIÓN: {e}\n")
            print("-" * 60)

        time.sleep(2)

    print("\n✅ Batería de pruebas automáticas finalizada.")

if __name__ == "__main__":
    ejecutar_pruebas_automaticas()
