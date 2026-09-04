import contextvars
import json
import os
import uuid
from datetime import datetime
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

# ---------------------------------------------------------
# Cargar variables de entorno desde el archivo .env
# ---------------------------------------------------------
load_dotenv()

# ==========================================
# 1. BASE DE DATOS EN MEMORIA
# ==========================================
# Todo el almacenamiento vivirá en este diccionario global en la RAM.
# Los clienteId son los mismos que usa el agente boti-bank (main.py), pero
# este agente NO conoce cuentas ni saldos bancarios: solo crédito.
#
# Invariante que sostiene todo el módulo:
#   disponible = limite - suma(saldoPendiente de las transacciones VIGENTES de esa tarjeta)
# Se recalcula con _recalcular_disponible() después de cada compra y cada abono.
CREDITO_DB = {
    "tarjetas": [
        # --- Ana García: tarjeta con dos consumos vigentes ---
        {
            "tarjetaId": "TC-4821",
            "clienteId": "71992c72-cc1c-4c5a-8b50-9ee4fb6c214d",
            "titular": "Ana García",
            "limite": 2755.00,
            "disponible": 2035.00,   # 2755 - (600 + 120)
            "estado": "ACTIVA",
            "fechaAlta": "2026-01-15T10:00:00Z"
        },
        # --- Pablo Saga: tarjeta de límite alto, un consumo vigente y uno cancelado ---
        {
            "tarjetaId": "TC-7315",
            "clienteId": "88888888-cc1c-4c5a-8b50-9ee4fb6c214d",
            "titular": "Pablo Saga",
            "limite": 15000.00,
            "disponible": 10600.00,  # 15000 - 4400
            "estado": "ACTIVA",
            "fechaAlta": "2026-02-01T09:30:00Z"
        }
        # --- Carlos Pérez NO tiene tarjeta: sirve para probar el rechazo por scoring ---
    ],
    "transacciones": [
        # --- Consumos de Ana: TC-4821 ---
        {
            "transaccionId": "TRX-000101",
            "tarjetaId": "TC-4821",
            "objeto": "Notebook Lenovo IdeaPad",
            "cuotas": 6,
            "montoCuota": 150.00,
            "montoTotal": 900.00,
            "saldoPendiente": 600.00,
            "cuotasPagadas": 2,
            "estado": "VIGENTE",
            "fecha": "2026-02-10T16:20:00Z"
        },
        {
            "transaccionId": "TRX-000102",
            "tarjetaId": "TC-4821",
            "objeto": "Zapatillas running Nike",
            "cuotas": 3,
            "montoCuota": 40.00,
            "montoTotal": 120.00,
            "saldoPendiente": 120.00,
            "cuotasPagadas": 0,
            "estado": "VIGENTE",
            "fecha": "2026-03-01T12:05:00Z"
        },
        # --- Consumos de Pablo: TC-7315 ---
        {
            "transaccionId": "TRX-000103",
            "tarjetaId": "TC-7315",
            "objeto": "Pasajes aéreos a Madrid",
            "cuotas": 12,
            "montoCuota": 400.00,
            "montoTotal": 4800.00,
            "saldoPendiente": 4400.00,
            "cuotasPagadas": 1,
            "estado": "VIGENTE",
            "fecha": "2026-02-20T19:20:00Z"
        },
        {
            "transaccionId": "TRX-000104",
            "tarjetaId": "TC-7315",
            "objeto": "Heladera Samsung No Frost",
            "cuotas": 10,
            "montoCuota": 120.00,
            "montoTotal": 1200.00,
            "saldoPendiente": 0.00,
            "cuotasPagadas": 10,
            "estado": "CANCELADA",
            "fecha": "2026-02-05T11:00:00Z"
        }
    ],
    "pagos": [
        # --- Abonos de Ana sobre TRX-000101 (2 de 6 cuotas) ---
        {"pagoId": "PG-000001", "transaccionId": "TRX-000101", "monto": 150.00, "fecha": "2026-03-10T10:00:00Z"},
        {"pagoId": "PG-000002", "transaccionId": "TRX-000101", "monto": 150.00, "fecha": "2026-04-10T10:15:00Z"},
        # --- Abono de Pablo sobre TRX-000103 (1 de 12 cuotas) ---
        {"pagoId": "PG-000003", "transaccionId": "TRX-000103", "monto": 400.00, "fecha": "2026-03-20T18:00:00Z"},
        # --- Pago total anticipado de Pablo sobre TRX-000104 ---
        {"pagoId": "PG-000004", "transaccionId": "TRX-000104", "monto": 1200.00, "fecha": "2026-03-05T09:45:00Z"}
    ]
}

# ==========================================
# 2. POLÍTICA DE RIESGO (determinística, NO la decide el LLM)
# ==========================================
MIN_INGRESO_MENSUAL = 400.0    # por debajo de esto se rechaza la solicitud
LIMITE_MINIMO = 500.0
LIMITE_MAXIMO = 15000.0
DTI_MAXIMO = 0.50              # cuota de deuda / ingreso mensual
MAX_CUOTAS = 24

def _ahora() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _buscar_tarjeta(tarjeta_id: str):
    return next((t for t in CREDITO_DB["tarjetas"] if t["tarjetaId"] == tarjeta_id), None)

def _tarjeta_activa_de_cliente(cliente_id: str):
    return next((t for t in CREDITO_DB["tarjetas"]
                 if t["clienteId"] == cliente_id and t["estado"] == "ACTIVA"), None)

def _buscar_transaccion(transaccion_id: str):
    return next((x for x in CREDITO_DB["transacciones"]
                 if x["transaccionId"] == transaccion_id), None)

def _recalcular_disponible(tarjeta_id: str) -> float:
    """Reescribe en memoria el disponible de la tarjeta a partir de sus consumos vigentes."""
    tarjeta = _buscar_tarjeta(tarjeta_id)
    if not tarjeta:
        return 0.0
    consumido = sum(x["saldoPendiente"] for x in CREDITO_DB["transacciones"]
                    if x["tarjetaId"] == tarjeta_id and x["estado"] == "VIGENTE")
    tarjeta["disponible"] = round(tarjeta["limite"] - consumido, 2)
    return tarjeta["disponible"]

def _nuevo_id(prefijo: str, coleccion: str, campo: str) -> str:
    """Genera el próximo ID secuencial legible (TRX-000105, PG-000005, ...)."""
    numeros = []
    for item in CREDITO_DB.get(coleccion, []):
        sufijo = item[campo].split("-")[-1]
        if sufijo.isdigit():
            numeros.append(int(sufijo))
    return f"{prefijo}-{(max(numeros) + 1 if numeros else 1):06d}"

def _nuevo_id_tarjeta() -> str:
    numeros = [int(t["tarjetaId"].split("-")[-1]) for t in CREDITO_DB["tarjetas"]
               if t["tarjetaId"].split("-")[-1].isdigit()]
    return f"TC-{(max(numeros) + 1 if numeros else 1001)}"

def _registrar_pago(transaccion_id: str, monto: float) -> str:
    """Auxiliar para guardar pagos directamente en memoria."""
    pago = {
        "pagoId": _nuevo_id("PG", "pagos", "pagoId"),
        "transaccionId": transaccion_id,
        "monto": round(monto, 2),
        "fecha": _ahora()
    }
    CREDITO_DB.setdefault("pagos", []).append(pago)
    return pago["pagoId"]

# ==========================================
# 3. OPERACIONES DE NEGOCIO
# ==========================================
# Cada _op_* devuelve un dict. Si algo falla, devuelve {"error": "..."}.
# Las tools del agente y los endpoints REST /internal/* consumen ESTAS MISMAS
# funciones, así el comportamiento es idéntico por los dos caminos.

def _op_solicitar_tarjeta(cliente_id: str, titular: str, ingreso_mensual: float,
                          saldo_total: float, cuota_deuda_mensual: float = 0.0) -> dict:
    if not cliente_id:
        return {"error": "falta cliente_id"}

    # Idempotente: si ya tiene una tarjeta activa, se devuelve esa sin volver a evaluar.
    existente = _tarjeta_activa_de_cliente(cliente_id)
    if existente:
        return {
            "aprobada": True,
            "tarjetaId": existente["tarjetaId"],
            "limite": existente["limite"],
            "disponible": _recalcular_disponible(existente["tarjetaId"]),
            "motivo": "El cliente ya tenia una tarjeta activa, no se emite una nueva"
        }

    ingreso_mensual = float(ingreso_mensual or 0)
    saldo_total = float(saldo_total or 0)
    cuota_deuda_mensual = float(cuota_deuda_mensual or 0)

    if ingreso_mensual < MIN_INGRESO_MENSUAL:
        return {
            "aprobada": False, "tarjetaId": None, "limite": 0.0, "disponible": 0.0,
            "motivo": (f"Ingreso mensual estimado ({ingreso_mensual:.2f}) inferior al minimo "
                       f"requerido ({MIN_INGRESO_MENSUAL:.2f})")
        }

    dti = cuota_deuda_mensual / ingreso_mensual
    if dti > DTI_MAXIMO:
        return {
            "aprobada": False, "tarjetaId": None, "limite": 0.0, "disponible": 0.0,
            "motivo": (f"Nivel de endeudamiento alto: las cuotas de deuda ({cuota_deuda_mensual:.2f}) "
                       f"representan el {dti * 100:.0f}% del ingreso mensual "
                       f"(maximo permitido {DTI_MAXIMO * 100:.0f}%)")
        }

    capacidad = (3 * ingreso_mensual) + (0.20 * saldo_total) - (2 * cuota_deuda_mensual)
    limite = round(min(max(capacidad, LIMITE_MINIMO), LIMITE_MAXIMO), 2)

    tarjeta = {
        "tarjetaId": _nuevo_id_tarjeta(),
        "clienteId": cliente_id,
        "titular": titular or "Sin nombre",
        "limite": limite,
        "disponible": limite,
        "estado": "ACTIVA",
        "fechaAlta": _ahora()
    }
    CREDITO_DB["tarjetas"].append(tarjeta)

    return {
        "aprobada": True,
        "tarjetaId": tarjeta["tarjetaId"],
        "limite": limite,
        "disponible": limite,
        "motivo": (f"Aprobada por capacidad de pago suficiente "
                   f"(ingreso {ingreso_mensual:.2f}, saldo {saldo_total:.2f}, "
                   f"cuotas de deuda {cuota_deuda_mensual:.2f})")
    }

def _op_comprar(tarjeta_id: str, objeto: str, cuotas: int, monto_cuota: float) -> dict:
    tarjeta = _buscar_tarjeta(tarjeta_id)
    if not tarjeta:
        return {"error": f"tarjeta {tarjeta_id} no encontrada"}
    if tarjeta["estado"] != "ACTIVA":
        return {"error": f"la tarjeta {tarjeta_id} esta {tarjeta['estado']}"}

    try:
        cuotas = int(cuotas)
        monto_cuota = float(monto_cuota)
    except (TypeError, ValueError):
        return {"error": "cuotas debe ser un entero y monto_cuota un numero"}

    if cuotas < 1 or cuotas > MAX_CUOTAS:
        return {"error": f"cantidad de cuotas invalida ({cuotas}); permitido entre 1 y {MAX_CUOTAS}"}
    if monto_cuota <= 0:
        return {"error": "el monto de cada cuota debe ser mayor a cero"}

    monto_total = round(cuotas * monto_cuota, 2)
    disponible = _recalcular_disponible(tarjeta_id)
    if monto_total > disponible:
        return {"error": (f"credito disponible insuficiente: la compra suma {monto_total:.2f} "
                          f"y la tarjeta {tarjeta_id} tiene {disponible:.2f} disponible")}

    transaccion = {
        "transaccionId": _nuevo_id("TRX", "transacciones", "transaccionId"),
        "tarjetaId": tarjeta_id,
        "objeto": objeto or "Compra sin descripcion",
        "cuotas": cuotas,
        "montoCuota": round(monto_cuota, 2),
        "montoTotal": monto_total,
        "saldoPendiente": monto_total,
        "cuotasPagadas": 0,
        "estado": "VIGENTE",
        "fecha": _ahora()
    }
    CREDITO_DB["transacciones"].append(transaccion)

    return {
        "transaccionId": transaccion["transaccionId"],
        "tarjetaId": tarjeta_id,
        "objeto": transaccion["objeto"],
        "cuotas": cuotas,
        "montoCuota": transaccion["montoCuota"],
        "montoTotal": monto_total,
        "disponibleRestante": _recalcular_disponible(tarjeta_id)
    }

def _op_abonar(transaccion_id: str, monto: float) -> dict:
    transaccion = _buscar_transaccion(transaccion_id)
    if not transaccion:
        return {"error": f"transaccion {transaccion_id} no encontrada"}
    if transaccion["estado"] != "VIGENTE":
        return {"error": (f"la transaccion {transaccion_id} ya esta {transaccion['estado']}, "
                          f"no admite mas abonos")}

    try:
        monto = float(monto)
    except (TypeError, ValueError):
        return {"error": "el monto a abonar debe ser un numero"}
    if monto <= 0:
        return {"error": "el monto a abonar debe ser mayor a cero"}
    if monto > transaccion["saldoPendiente"]:
        return {"error": (f"el abono ({monto:.2f}) supera el saldo pendiente de la transaccion "
                          f"({transaccion['saldoPendiente']:.2f}); aboná como maximo ese monto")}

    transaccion["saldoPendiente"] = round(transaccion["saldoPendiente"] - monto, 2)
    pagado = transaccion["montoTotal"] - transaccion["saldoPendiente"]
    transaccion["cuotasPagadas"] = round(pagado / transaccion["montoCuota"], 2)
    if transaccion["saldoPendiente"] <= 0:
        transaccion["saldoPendiente"] = 0.0
        transaccion["estado"] = "CANCELADA"
        transaccion["cuotasPagadas"] = transaccion["cuotas"]

    pago_id = _registrar_pago(transaccion_id, monto)

    return {
        "pagoId": pago_id,
        "transaccionId": transaccion_id,
        "objeto": transaccion["objeto"],
        "montoAbonado": round(monto, 2),
        "saldoPendiente": transaccion["saldoPendiente"],
        "cuotasPagadas": transaccion["cuotasPagadas"],
        "cuotasRestantes": round(transaccion["cuotas"] - transaccion["cuotasPagadas"], 2),
        "estado": transaccion["estado"],
        "disponibleTarjeta": _recalcular_disponible(transaccion["tarjetaId"])
    }

def _op_consultar_tarjetas(cliente_id: str) -> dict:
    tarjetas = []
    for t in CREDITO_DB["tarjetas"]:
        if t["clienteId"] != cliente_id:
            continue
        _recalcular_disponible(t["tarjetaId"])
        tarjetas.append(t)
    return {"clienteId": cliente_id, "cantidad": len(tarjetas), "tarjetas": tarjetas}

def _op_estado_cuenta(tarjeta_id: str) -> dict:
    tarjeta = _buscar_tarjeta(tarjeta_id)
    if not tarjeta:
        return {"error": f"tarjeta {tarjeta_id} no encontrada"}

    vigentes = [x for x in CREDITO_DB["transacciones"]
                if x["tarjetaId"] == tarjeta_id and x["estado"] == "VIGENTE"]
    return {
        "tarjetaId": tarjeta_id,
        "titular": tarjeta["titular"],
        "limite": tarjeta["limite"],
        "disponible": _recalcular_disponible(tarjeta_id),
        "deudaTotal": round(sum(x["saldoPendiente"] for x in vigentes), 2),
        "proximaCuotaTotal": round(sum(x["montoCuota"] for x in vigentes), 2),
        "transaccionesVigentes": [
            {
                "transaccionId": x["transaccionId"],
                "objeto": x["objeto"],
                "cuotas": x["cuotas"],
                "montoCuota": x["montoCuota"],
                "cuotasPagadas": x["cuotasPagadas"],
                "saldoPendiente": x["saldoPendiente"]
            } for x in vigentes
        ]
    }

# ==========================================
# 4. DEFINICIÓN DE TOOLS PARA EL AGENTE
# ==========================================
# Estado acotado a cada request HTTP. Cumple dos funciones, y ninguna depende de que
# el LLM se porte bien:
#   1) Idempotencia: si el modelo llama dos veces la misma tool en el mismo request,
#      la segunda devuelve el resultado de la primera en vez de aplicar la operación de nuevo.
#   2) Recolección del resultado REAL de las tools ejecutadas, que /chat devuelve
#      anexado a la respuesta. Sin esto, quien nos llama solo tiene la prosa del modelo,
#      y un modelo chico puede afirmar que hizo una operación sin haberla ejecutado.
_request_state = contextvars.ContextVar("credito_request_state", default=None)

MARCADOR_RESULTADOS = "[RESULTADO_TOOLS]"

def _abrir_request() -> dict:
    estado = {"ops": {}, "resultados": []}
    _request_state.set(estado)
    return estado

def _una_sola_vez(clave: tuple, operacion) -> dict:
    """Ejecuta la operación una sola vez por request HTTP."""
    estado = _request_state.get()
    if estado is None:
        return operacion()
    if clave in estado["ops"]:
        repetida = dict(estado["ops"][clave])
        repetida["repetida"] = True
        return repetida
    res = operacion()
    estado["ops"][clave] = res
    return res

def _resultado(res: dict) -> str:
    """Convierte el dict de la operación en la respuesta que ve el LLM, y deja registrado
    el resultado crudo para que /chat lo devuelva de forma estructurada."""
    estado = _request_state.get()
    if estado is not None:
        estado["resultados"].append(res)
    if "error" in res:
        return f"ERROR: {res['error']}"
    return json.dumps(res, ensure_ascii=False)

@tool
def solicitar_tarjeta_credito(cliente_id: str, titular: str, ingreso_mensual: float,
                              saldo_total: float, cuota_deuda_mensual: float = 0.0) -> str:
    """Solicita una tarjeta de crédito para un cliente y devuelve si fue aprobada y cuánto
    dinero de crédito tiene disponible para gastar. Requiere el perfil financiero del cliente:
    ingreso mensual, saldo total en cuentas y cuota mensual de deudas que ya tiene."""
    return _resultado(_una_sola_vez(
        ("solicitar", cliente_id),
        lambda: _op_solicitar_tarjeta(cliente_id, titular, ingreso_mensual,
                                      saldo_total, cuota_deuda_mensual)))

@tool
def comprar_con_tarjeta(tarjeta_id: str, objeto: str, cuotas: int, monto_cuota: float) -> str:
    """Registra una compra con tarjeta de crédito en cuotas. Recibe el id de la tarjeta, el
    objeto que se compra, la cantidad de cuotas y de cuánto dinero es cada cuota. Devuelve el
    id de transacción generado y el crédito disponible que queda."""
    return _resultado(_una_sola_vez(
        ("comprar", tarjeta_id, objeto, cuotas, monto_cuota),
        lambda: _op_comprar(tarjeta_id, objeto, cuotas, monto_cuota)))

@tool
def abonar_tarjeta(transaccion_id: str, monto: float) -> str:
    """Abona (paga) dinero sobre una compra hecha con tarjeta de crédito. Recibe el id de
    transacción y el dinero a abonar. Devuelve el saldo pendiente, las cuotas que quedan y el
    crédito disponible liberado en la tarjeta."""
    return _resultado(_una_sola_vez(
        ("abonar", transaccion_id, monto),
        lambda: _op_abonar(transaccion_id, monto)))

@tool
def consultar_tarjetas(cliente_id: str) -> str:
    """Lista las tarjetas de crédito de un cliente con su límite y su crédito disponible."""
    return _resultado(_op_consultar_tarjetas(cliente_id))

@tool
def estado_cuenta_tarjeta(tarjeta_id: str) -> str:
    """Muestra el resumen de una tarjeta: límite, disponible, deuda total y el detalle de las
    compras vigentes con su id de transacción y cuotas pendientes."""
    return _resultado(_op_estado_cuenta(tarjeta_id))

# Agrupamos las tools
tools = [
    solicitar_tarjeta_credito, comprar_con_tarjeta, abonar_tarjeta,
    consultar_tarjetas, estado_cuenta_tarjeta
]

# ==========================================
# 5. CONFIGURACIÓN DEL GRAFO Y AGENTE
# ==========================================

# Leer configuración del modelo desde el .env (compartida con boti-bank)
LLM_API_KEY = os.getenv("MODEL_API_KEY", "sk-mi-clave-secreta-123")
LLM_BASE_URL = os.getenv("MODEL_BASE_URL", "http://127.0.0.1:8081/v1")
LLM_MODEL_NAME = os.getenv("MODEL_NAME", "hermes-2-pro-llama3-8b")

llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    model=LLM_MODEL_NAME,
    temperature=0.0
)
llm_with_tools = llm.bind_tools(tools)

system_prompt = SystemMessage(content="""
Eres el agente de CRÉDITO de BotiBank. Gestionás tarjetas de crédito: solicitudes de tarjeta,
compras en cuotas y abonos sobre esas compras.

REGLAS CRÍTICAS DE OPERACIÓN:
1. ERES COMPLETAMENTE AUTÓNOMO: nunca pidas permiso ni confirmación al usuario para ejecutar una herramienta.
2. Si tenés los datos necesarios, DEBES EJECUTAR LA HERRAMIENTA INMEDIATAMENTE y responder recién después con su resultado.
3. Si el mensaje trae un bloque DATOS, usá esos valores TAL CUAL: no los modifiques, no los redondees y no inventes los que falten.
4. NO evalúes vos si un cliente merece la tarjeta ni cuánto límite darle: eso lo decide la herramienta
   solicitar_tarjeta_credito. Limitate a llamarla y a comunicar su veredicto junto con el motivo que devuelve.
5. NO tenés acceso a cuentas bancarias, saldos ni transferencias. Si te preguntan por eso, aclará que
   solo manejás el circuito de tarjetas de crédito.
6. Si una herramienta devuelve un texto que empieza con ERROR, explicale al usuario exactamente qué falló.
7. NUNCA afirmes que registraste una compra, emitiste una tarjeta o aplicaste un abono si no ejecutaste
   la herramienta correspondiente. Primero la herramienta, después la respuesta.
8. NUNCA pidas datos que ya vienen en el bloque DATOS: si están ahí, ejecutá la herramienta con ellos.
""")

def agent_node(state: MessagesState):
    messages = state["messages"]
    if not isinstance(messages[0], SystemMessage):
        messages = [system_prompt] + messages
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

def should_continue(state: MessagesState):
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return END

workflow = StateGraph(MessagesState)
workflow.add_node("agent", agent_node)
workflow.add_node("tools", ToolNode(tools))

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_continue, ["tools", END])
workflow.add_edge("tools", "agent")

checkpointer = MemorySaver()
credito_agent = workflow.compile(checkpointer=checkpointer)

# ==========================================
# 6. FASTAPI ENDPOINTS
# ==========================================

app = FastAPI(title="BotiBank Credito AI Agent", version="1.0.0")

# NOTE: field names/requiredness here must match the OpenAPI schema
# registered for this component's /chat endpoint in Agent Manager
# (message + session_id required, context optional) — otherwise the
# gateway's requests get rejected with 422 before they ever reach the graph.
class ChatRequest(BaseModel):
    message: str = Field(..., description="El mensaje o instrucción del usuario")
    session_id: str = Field(..., description="ID de sesión para mantener el historial")
    context: Optional[Dict[str, Any]] = Field(default=None, description="Contexto opcional en formato JSON")

class ChatResponse(BaseModel):
    response: str

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    run_config = {"configurable": {"thread_id": req.session_id}}
    estado_request = _abrir_request()

    # El agente boti-bank manda los datos duros en `context` en vez de en prosa:
    # así este agente no tiene que extraer números de una oración en español.
    contenido = req.message
    if req.context:
        contenido += ("\n\nDATOS (usá estos valores tal cual, no los inventes): "
                      + json.dumps(req.context, ensure_ascii=False))

    input_message = HumanMessage(content=contenido)

    try:
        final_state = await credito_agent.ainvoke(
            {"messages": [input_message]},
            config=run_config
        )

        last_message = final_state["messages"][-1].content

        # Anexamos el resultado REAL de las tools ejecutadas. Lo agrega este código,
        # no el modelo, así quien nos llama (el agente boti-bank) puede distinguir
        # "la operación se ejecutó" de "el modelo dice que se ejecutó".
        if estado_request["resultados"]:
            last_message += ("\n\n" + MARCADOR_RESULTADOS + " "
                             + json.dumps(estado_request["resultados"], ensure_ascii=False))

        return ChatResponse(response=last_message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ------------------------------------------------------------------
# Endpoints internos: mismo comportamiento que las tools, sin LLM.
# boti-bank los usa cuando CREDITO_MODE=rest (camino determinístico).
# ------------------------------------------------------------------
class SolicitudTarjetaRequest(BaseModel):
    cliente_id: str
    titular: str = ""
    ingreso_mensual: float = 0.0
    saldo_total: float = 0.0
    cuota_deuda_mensual: float = 0.0

class CompraRequest(BaseModel):
    tarjeta_id: str
    objeto: str
    cuotas: int
    monto_cuota: float

class AbonoRequest(BaseModel):
    transaccion_id: str
    monto: float

def _responder(res: dict) -> dict:
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

@app.post("/internal/tarjetas")
async def internal_solicitar_tarjeta(req: SolicitudTarjetaRequest):
    _abrir_request()
    return _responder(_op_solicitar_tarjeta(req.cliente_id, req.titular, req.ingreso_mensual,
                                            req.saldo_total, req.cuota_deuda_mensual))

@app.post("/internal/compras")
async def internal_comprar(req: CompraRequest):
    _abrir_request()
    return _responder(_op_comprar(req.tarjeta_id, req.objeto, req.cuotas, req.monto_cuota))

@app.post("/internal/pagos")
async def internal_abonar(req: AbonoRequest):
    _abrir_request()
    return _responder(_op_abonar(req.transaccion_id, req.monto))

@app.get("/internal/tarjetas/{cliente_id}")
async def internal_tarjetas_cliente(cliente_id: str):
    return _responder(_op_consultar_tarjetas(cliente_id))

@app.get("/internal/tarjetas/{tarjeta_id}/estado")
async def internal_estado_cuenta(tarjeta_id: str):
    return _responder(_op_estado_cuenta(tarjeta_id))

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("CREDITO_API_PORT", 8001))

    uvicorn.run("credito:app", host=host, port=port)
