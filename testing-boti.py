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
URL = os.getenv(
    "AGENT_URL", 
    "https://development-wc-019e6383-b65fad2b.agent-manager.us-east-2.cloud.wso2.com:443/botibank-xz-botibank-xz-endpoint/chat"
)

AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

HEADERS = {
    "Content-Type": "application/json",
    "x-api-key": AGENT_API_KEY
}

SESSION_ID = "sesion-automatica-004"

# ==========================================
# 1. BATERÍA DE PRUEBAS: BOTIBANK ESTÁNDAR
# ==========================================
PREGUNTAS_BOTIBANK = [
    "Por favor, enumera a todos los clientes del banco.",
    "¿Me puedes decir cuáles son las cuentas bancarias y los saldos de la clienta Ana, con ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d?",
    "¿Qué cuentas tiene registradas el cliente Pablo (ID: 88888888-cc1c-4c5a-8b50-9ee4fb6c214d)?",
    "Necesito hacer un depósito. Por favor, ingresa 500 en la cuenta CTA-999.",
    "Necesito hacer una transferencia de 200 desde la cuenta CTA-122 hacia la cuenta CTA-123 con el concepto 'Préstamo personal'.",
    "Intenta transferir 5000 desde la cuenta CTA-123 a la cuenta CTA-122 con el concepto 'Compra de auto'.", 
    "¿Qué servicios públicos tienen disponibles para pagar?",
    "Intenta pagar el servicio de luz (código LZ1) utilizando la cuenta CTA-999, por favor.", 
    "Por favor, paga el servicio de luz (código LZ1) utilizando mi cuenta CTA-122.",
    "Quiero pagar 300 de mi hipoteca con ID HIP-001 usando la cuenta CTA-122." 
]

# ==========================================
# 2. BATERÍA DE PRUEBAS: AGENTE CRÉDITO (A2A)
# ==========================================
PREGUNTAS_CREDITO = [
    # Solicitud de tarjeta (Aprobada)
    "¿Puede la clienta Ana, con ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d, sacar una tarjeta de crédito? Decime cuánto crédito tendría disponible.",
    
    # Solicitud de tarjeta (Rechazada / Cliente sin capacidad o no existe)
    "¿Y el cliente Carlos, con ID 33333333-cc1c-4c5a-8b50-9ee4fb6c214d, puede sacar una tarjeta de crédito?",
    
    # Estado de la cuenta (Consultar límite y vigentes)
    "¿Cómo viene la tarjeta de crédito de Ana (ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d)? Decime el límite, el disponible y las compras en cuotas que tiene.",
    
    # Compras (Éxito y Fallo por límite)
    "Comprá un microondas con la tarjeta TC-4821 en 4 cuotas de 75.",
    "Intentá comprar un auto usado con la tarjeta TC-4821 en 12 cuotas de 1000.",
    
    # Abono de cuotas
    "Aboná 150 de la transacción TRX-000101 usando la cuenta CTA-122."
]

def ejecutar_peticiones(preguntas: list):
    """Lógica principal para enviar las preguntas al servidor."""
    while True:
        try:
            cantidad_preguntas = int(input("\n¿Cuántas preguntas automáticas deseas enviar?: "))
            if cantidad_preguntas > 0:
                break
            else:
                print("Por favor, ingresa un número mayor a 0.")
        except ValueError:
            print("Entrada inválida. Debes ingresar un número entero.")

    print(f"\n🚀 Iniciando batería de {cantidad_preguntas} pregunta(s).")
    print("-" * 60)

    for i in range(cantidad_preguntas):
        pregunta_actual = preguntas[i % len(preguntas)]
        
        print(f"\n▶ PREGUNTA {i + 1} DE {cantidad_preguntas}")
        print(f"✍️  Enviando: {pregunta_actual}")
        
        payload = {
            "message": pregunta_actual,
            "session_id": SESSION_ID
        }

        # --- INICIO DEL TRACKING ---
        print("\n" + "." * 60)
        print("🔍 [TRACK] ENVIANDO PETICIÓN (REQUEST)")
        print(f"URL     : {URL}")
        
        safe_headers = HEADERS.copy()
        if "x-api-key" in safe_headers and len(safe_headers["x-api-key"]) > 10:
            safe_headers["x-api-key"] = safe_headers["x-api-key"][:10] + "...[OCULTO]"
            
        print(f"Headers : {json.dumps(safe_headers, indent=2)}")
        print(f"Body    : {json.dumps(payload, indent=2, ensure_ascii=False)}")
        print("." * 60)

        try:
            response = requests.post(URL, headers=HEADERS, json=payload, timeout=45) # A2A puede tardar más
            
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
            print(f"\n🤖 BotiBank: {respuesta_agente}\n")
            print("-" * 60)

        except requests.exceptions.RequestException as e:
            print(f"\n❌ ERROR EN LA COMUNICACIÓN: {e}\n")
            print("-" * 60)
            
        time.sleep(2)

    print("\n✅ Batería de pruebas finalizada.")

def menu_principal():
    print("=" * 60)
    print("🤖 AUTOMATIZADOR DE PRUEBAS - BOTIBANK Y CRÉDITO")
    print("=" * 60)
    print("1. Probar BotiBank (Consultas de cuentas, pagos, transferencias)")
    print("2. Probar Agente de Crédito A2A (Tarjetas, compras en cuotas)")
    print("3. Salir")
    
    while True:
        opcion = input("\nElige una opción (1-3): ")
        if opcion == "1":
            ejecutar_peticiones(PREGUNTAS_BOTIBANK)
            break
        elif opcion == "2":
            ejecutar_peticiones(PREGUNTAS_CREDITO)
            break
        elif opcion == "3":
            print("Saliendo del simulador...")
            break
        else:
            print("Opción inválida. Intenta nuevamente.")

if __name__ == "__main__":
    menu_principal()