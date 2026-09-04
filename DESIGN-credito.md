# Diseño: Agente `credito` + integración A2A con `boti-bank`

Estado: propuesta de diseño (sin implementar). Base actual: `main.py` (LangGraph + FastAPI, DB en RAM),
desplegado como componente en WSO2 Agent Manager y expuesto por `POST /chat`.

---

## 1. Decisión de arquitectura

Dos agentes **independientes**, cada uno con su proceso, su LLM, su grafo y **su propia base de datos**:

- `boti-bank` (main.py, puerto 8000) — dueño de: clientes, cuentas, movimientos, servicios, hipotecas.
  Actúa como **supervisor / orquestador** frente al usuario.
- `credito` (credito.py, puerto 8001) — dueño de: tarjetas, transacciones (consumos en cuotas), pagos.
  Actúa como **especialista**. No conoce cuentas ni saldos bancarios.

Por qué separados y no un solo agente con más tools:
- Es el caso de uso A2A que se quiere demostrar en Agent Manager (dos componentes, dos endpoints, dos API keys).
- Aísla la política de riesgo (scoring) del core transaccional.
- Cada agente mantiene un set de tools chico → crítico con un modelo de 8B (`hermes-2-pro-llama3-8b`),
  que se degrada bastante pasadas ~8-10 tools.

```
                      ┌──────────────────────────────────────┐
   Usuario  ─────────► │  AGENTE boti-bank  (supervisor)      │
   POST /chat          │  puerto 8000                         │
                       │  tools: clientes, cuentas, ingresar, │
                       │   transferir, servicios, hipotecas   │
                       │   + credito_solicitar_tarjeta        │
                       │   + credito_comprar                  │
                       │   + credito_abonar                   │
                       │   + credito_estado                   │
                       └───────────────┬──────────────────────┘
                                       │ A2A: POST /chat
                                       │ x-api-key + {message, session_id, context}
                                       ▼
                       ┌──────────────────────────────────────┐
                       │  AGENTE credito  (especialista)      │
                       │  puerto 8001                         │
                       │  tools: solicitar_tarjeta_credito,   │
                       │   comprar_con_tarjeta,               │
                       │   abonar_tarjeta,                    │
                       │   consultar_tarjetas, estado_cuenta  │
                       └───────────────┬──────────────────────┘
                                       ▼
                       ┌──────────────────────────────────────┐
                       │ CREDITO_DB (RAM)                     │
                       │ { tarjetas, transacciones, pagos }   │
                       └──────────────────────────────────────┘
```

**Regla de oro del diseño:** el dinero de las cuentas lo mueve SIEMPRE `boti-bank`;
el crédito (límite, cuotas, saldo de tarjeta) lo mueve SIEMPRE `credito`.
Nunca hay escritura cruzada, y por eso `credito` no necesita llamar de vuelta a `boti-bank`
(la comunicación es unidireccional → no hay ciclos A2A).

---

## 2. Modelo de datos del agente `credito`

```python
CREDITO_DB = {
  "tarjetas": [
    {
      "tarjetaId": "TC-4821",
      "clienteId": "71992c72-...",
      "titular": "Ana García",
      "limite": 2755.00,        # asignado en la solicitud
      "disponible": 2755.00,    # limite - saldo consumido no abonado
      "estado": "ACTIVA",       # ACTIVA | BLOQUEADA
      "fechaAlta": "2026-08-25T...Z"
    }
  ],
  "transacciones": [
    {
      "transaccionId": "TRX-000123",
      "tarjetaId": "TC-4821",
      "objeto": "Notebook Lenovo",
      "cuotas": 6,
      "montoCuota": 150.00,
      "montoTotal": 900.00,      # cuotas * montoCuota
      "saldoPendiente": 900.00,
      "cuotasPagadas": 0,
      "estado": "VIGENTE",       # VIGENTE | CANCELADA
      "fecha": "..."
    }
  ],
  "pagos": [
    {"pagoId": "PG-...", "transaccionId": "TRX-000123", "monto": 150.00, "fecha": "..."}
  ]
}
```

Invariante: `disponible = limite - Σ saldoPendiente(transacciones VIGENTES de esa tarjeta)`.
Se recalcula tras cada compra y cada abono (una función `_recalcular_disponible(tarjeta_id)`),
así nunca hay drift entre los dos campos.

---

## 3. Tools del agente `credito`

### 3.1 `solicitar_tarjeta_credito`

```python
solicitar_tarjeta_credito(
    cliente_id: str,
    titular: str,
    ingreso_mensual: float,        # lo provee boti-bank (promedio de INGRESOS)
    saldo_total: float,            # lo provee boti-bank (suma de saldos)
    cuota_deuda_mensual: float = 0 # lo provee boti-bank (cuotas de hipoteca)
) -> str  # JSON
```

Punto clave: **`credito` no adivina la situación financiera**. `boti-bank` la calcula desde su
propia DB (movimientos/cuentas/hipotecas) y la envía. Así el especialista aplica *política*,
no *datos*.

Scoring **determinístico en código Python** (no lo decide el LLM):

```python
MIN_INGRESO   = 400.0
LIMITE_MIN    = 500.0
LIMITE_MAX    = 15000.0
DTI_MAX       = 0.50   # deuda/ingreso

if ingreso_mensual < MIN_INGRESO:                  -> RECHAZADA ("ingresos insuficientes")
if cuota_deuda_mensual / ingreso_mensual > DTI_MAX: -> RECHAZADA ("endeudamiento alto")
if ya tiene tarjeta ACTIVA:                        -> devuelve la existente (idempotente)

capacidad = 3*ingreso_mensual + 0.20*saldo_total - 2*cuota_deuda_mensual
limite    = round(clamp(capacidad, LIMITE_MIN, LIMITE_MAX), 2)
```

Respuesta:
```json
{"aprobada": true, "tarjetaId": "TC-4821", "limite": 2755.0,
 "disponible": 2755.0, "motivo": "Aprobada por capacidad de pago suficiente"}
```
Rechazo:
```json
{"aprobada": false, "tarjetaId": null, "limite": 0,
 "motivo": "Ingreso mensual estimado (375.00) inferior al minimo requerido (400.00)"}
```

Con la data actual del repo el demo da tres caminos distintos (bueno para mostrar):

| Cliente | ingreso/mes | saldo | cuota hipoteca | Resultado |
|---|---|---|---|---|
| Ana    | 1233.50 | 1900.50 | 800  | Aprobada, límite 2755 |
| Pablo  | 6000    | 5000    | 1200 | Aprobada, límite topeado en 15000 |
| Carlos | 375     | 250     | 0    | **Rechazada** (ingresos insuficientes) |

### 3.2 `comprar_con_tarjeta`

```python
comprar_con_tarjeta(tarjeta_id: str, objeto: str, cuotas: int, monto_cuota: float) -> str
```
Validaciones (en orden, cada una devuelve `ERROR: ...` claro para que el LLM lo explique):
1. tarjeta existe y está `ACTIVA`
2. `1 <= cuotas <= 24`, `monto_cuota > 0`
3. `montoTotal = cuotas * monto_cuota <= disponible`

Efecto: crea `TRX-xxxxxx`, `saldoPendiente = montoTotal`, resta del `disponible`.

```json
{"transaccionId":"TRX-000123","objeto":"Notebook Lenovo","cuotas":6,"montoCuota":150.0,
 "montoTotal":900.0,"disponibleRestante":1855.0}
```

### 3.3 `abonar_tarjeta`

```python
abonar_tarjeta(transaccion_id: str, monto: float) -> str
```
Validaciones: transacción existe, está `VIGENTE`, `0 < monto <= saldoPendiente`
(un abono mayor al pendiente se rechaza con el monto exacto sugerido — más simple que manejar saldo a favor).

Efecto: `saldoPendiente -= monto`; `cuotasPagadas = round((montoTotal - saldoPendiente)/montoCuota, 2)`;
si `saldoPendiente == 0` → `estado = "CANCELADA"`; recalcula `disponible` de la tarjeta.

```json
{"transaccionId":"TRX-000123","montoAbonado":150.0,"saldoPendiente":750.0,
 "cuotasPagadas":1,"cuotasRestantes":5,"estado":"VIGENTE","disponibleTarjeta":2005.0}
```

### 3.4 Tools de lectura (necesarias para que el flujo conversacional cierre)

```python
consultar_tarjetas(cliente_id: str) -> str          # tarjetas + limite/disponible
estado_cuenta_tarjeta(tarjeta_id: str) -> str       # transacciones VIGENTES, total a pagar
```
Sin estas, el usuario no puede decir "aboná mi cuota de la notebook" sin recordar el `TRX-`.

---

## 4. Tools nuevas en `boti-bank` (el lado A2A)

Cuatro tools, cada una envuelve una llamada HTTP al agente `credito`:

```python
credito_solicitar_tarjeta(cliente_id: str)                           -> str
credito_comprar(tarjeta_id: str, objeto: str, cuotas: int, monto_cuota: float) -> str
credito_abonar(transaccion_id: str, monto: float, cuenta_origen: str) -> str
credito_estado(cliente_id: str)                                      -> str
```

### 4.1 El perfil financiero se calcula localmente

`credito_solicitar_tarjeta` no le pide nada al usuario más que el `cliente_id`; arma el perfil sola:

```python
def _perfil_financiero(cliente_id: str) -> dict:
    cuentas  = [c for c in IN_MEMORY_DB["cuentas"] if c["clienteId"] == cliente_id]
    ids      = {c["cuentaId"] for c in cuentas}
    movs     = [m for m in IN_MEMORY_DB["movimientos"] if m["cuentaId"] in ids]
    ingresos = [m for m in movs if m["tipo"] in ("INGRESO", "TRANSFERENCIA_RECIBIDA")]
    meses    = len({m["fecha"][:7] for m in ingresos}) or 1
    return {
      "titular": nombre + apellido,
      "ingreso_mensual": round(sum(m["monto"] for m in ingresos) / meses, 2),
      "saldo_total": round(sum(c["saldo"] for c in cuentas), 2),
      "cuota_deuda_mensual": sum(h["cuotaMensual"] for h in IN_MEMORY_DB["hipotecas"]
                                 if h["clienteId"] == cliente_id),
    }
```

### 4.2 Transporte A2A

```python
def _call_credito(instruccion: str, datos: dict, session_id: str) -> str:
    payload = {"message": instruccion,
               "session_id": f"botibank-{session_id}",   # aísla el hilo del agente hijo
               "context": datos}                          # <- datos estructurados, NO en prosa
    r = requests.post(CREDITO_AGENT_URL, headers={"x-api-key": CREDITO_AGENT_API_KEY,
                                                  "Content-Type": "application/json"},
                      json=payload, timeout=45)
    r.raise_for_status()
    return r.json()["response"]
```

Dos detalles importantes:

1. **`context` en lugar de prosa.** El `ChatRequest` ya tiene `context: Optional[Dict[str, Any]]`.
   `credito.py` lo inyecta al grafo como texto anexo al mensaje:
   `HumanMessage(f"{message}\n\nDATOS (usá estos valores tal cual, no los inventes): {json.dumps(context)}")`.
   Con un modelo de 8B, esto es la diferencia entre que funcione y que no: el agente hijo
   no tiene que extraer números de una oración en español.

2. **Un thread nuevo por llamada en el agente hijo** (corregido tras probarlo, ver §10).
   La llamada A2A es un RPC de un solo turno, no una conversación: `session_id` viaja como
   `botibank-{session_id}-{uuid corto}`. El `session_id` del usuario se obtiene inyectando la
   config de LangGraph en la firma de la tool:
   ```python
   from langchain_core.runnables import RunnableConfig
   @tool
   def credito_estado(cliente_id: str, config: RunnableConfig) -> str:
       sid = config["configurable"]["thread_id"]
   ```
   (el parámetro `config` no se le expone al modelo como argumento).

3. **Verificación estructurada del resultado** (agregado tras probarlo, ver §10).
   El agente `credito` anexa a su respuesta el resultado REAL de las tools que ejecutó, bajo
   el marcador `[RESULTADO_TOOLS]`. Ese bloque lo escribe su **código**, no su modelo, así
   `boti-bank` distingue "la operación se ejecutó" de "el modelo dice que se ejecutó".
   Sin marcador → `boti-bank` trata la operación como fallida.

### 4.3 Interruptor A2A / REST

`credito.py` expone, además de `/chat`, endpoints internos 1:1 con las tools
(`POST /internal/tarjetas`, `/internal/compras`, `/internal/pagos`, `GET /internal/tarjetas/{cliente_id}`),
que llaman a **las mismas funciones Python** que usan las tools.

`boti-bank` elige por env: `CREDITO_MODE=a2a` (default, es la demo A2A) o `CREDITO_MODE=rest`
(determinístico, cero riesgo de que el LLM del hijo alucine). Mismo contrato de retorno en
ambos casos, así el resto del código no cambia. Es la vía de escape si el modelo local
resulta inestable en la demo.

---

## 5. El caso delicado: `abonar_tarjeta` mueve dinero real

El abono toca **las dos** bases de datos: debita una cuenta en `boti-bank` y baja el
`saldoPendiente` en `credito`. No hay transacción distribuida, así que el orden importa.

Orden propuesto en `credito_abonar(transaccion_id, monto, cuenta_origen)`:

```
1. Validar saldo de cuenta_origen localmente        (falla barata, sin efectos)
2. Debitar cuenta_origen + registrar movimiento     (tipo: PAGO_TARJETA)
3. A2A -> abonar_tarjeta(transaccion_id, monto)
4. Si el paso 3 falla o devuelve ERROR:
      -> revertir el débito (compensación) + movimiento AJUSTE_REVERSA
      -> devolver "ERROR: no se pudo aplicar el abono, se reintegro el dinero"
```

La decisión del paso 4 es **exacta en los dos modos**: se dispara cuando la respuesta empieza
con `ERROR`, y eso incluye el caso "el agente de crédito contestó sin haber ejecutado la tool"
(sin marcador `[RESULTADO_TOOLS]`). No se adivina nada leyendo la prosa del modelo.

Se debita primero porque es el paso reversible localmente y sin red; el paso irreversible-remoto
va último. El movimiento nuevo `PAGO_TARJETA` hay que agregarlo al enum de `Movimiento` en
`botibank.yaml`.

También hace falta pedir la `cuenta_origen`: es el único dato que el usuario debe aportar
y que ningún agente puede inferir (Ana tiene 3 cuentas). Si el usuario no la dice, la tool
devuelve `ERROR: falta cuenta_origen. Cuentas disponibles: CTA-122 (1800.50), ...`
y el LLM pregunta — el único punto del flujo donde se rompe la autonomía total, y está bien
que se rompa ahí.

---

## 6. Flujos (secuencia)

### 6.1 "¿Puede Ana sacar una tarjeta de crédito?"

```
Usuario -> boti-bank : "¿Ana (71992c72-...) puede sacar una tarjeta?"
boti-bank            : _perfil_financiero() sobre su DB local
                       {ingreso_mensual: 1233.50, saldo_total: 1900.50, cuota_deuda_mensual: 800}
boti-bank -> credito : POST /chat  message="Solicitá una tarjeta para este cliente"
                                   context={cliente_id, titular, ingreso_mensual, ...}
credito              : tool solicitar_tarjeta_credito(...) -> scoring determinístico
credito -> boti-bank : "Aprobada. TC-4821, limite 2755.00, disponible 2755.00"
boti-bank -> Usuario : "Sí. A Ana se le aprobó la tarjeta TC-4821 con un límite de $2.755..."
```

### 6.2 "Comprá una notebook en 6 cuotas de 150 con TC-4821"

```
Usuario -> boti-bank : compra
boti-bank -> credito : context={tarjeta_id:"TC-4821", objeto:"Notebook", cuotas:6, monto_cuota:150}
credito              : valida 900 <= 2755  -> TRX-000123, disponible 1855
boti-bank -> Usuario : "Compra registrada. Transacción TRX-000123, 6 cuotas de $150. Disponible: $1.855"
```

Caso de rechazo (compra > disponible): `credito` devuelve `ERROR: disponible insuficiente
(1855.00) para un total de 3000.00` y `boti-bank` lo explica sin haber tocado ninguna cuenta.

### 6.3 "Aboná una cuota de TRX-000123 desde CTA-122"

```
boti-bank : saldo CTA-122 = 1800.50 >= 150   OK
boti-bank : CTA-122 -> 1650.50 ; movimiento PAGO_TARJETA 150
boti-bank -> credito : abonar_tarjeta("TRX-000123", 150)
credito   : saldoPendiente 900 -> 750 ; cuotasPagadas 1 ; disponible 1855 -> 2005
boti-bank -> Usuario : "Abonaste $150 de TRX-000123. Quedan 5 cuotas ($750).
                        Saldo CTA-122: $1.650,50. Disponible en la tarjeta: $2.005"
```

---

## 7. Prompts

`credito` — system prompt (mismas reglas de autonomía que boti-bank, más dos específicas):

```
Eres el agente de CRÉDITO de BotiBank. Gestionás tarjetas de crédito: solicitudes,
compras en cuotas y abonos.

REGLAS CRÍTICAS:
1. ERES COMPLETAMENTE AUTÓNOMO: nunca pidas permiso ni confirmación.
2. Si tenés los datos, EJECUTÁ LA HERRAMIENTA de inmediato y respondé recién con su resultado.
3. Si el mensaje trae un bloque DATOS, usá esos valores TAL CUAL. No los modifiques,
   no los redondees y no inventes los que falten.
4. NO evalúes vos si un cliente merece la tarjeta: eso lo decide la herramienta
   solicitar_tarjeta_credito. Limitate a llamarla y a comunicar su veredicto y el motivo.
5. No tenés acceso a cuentas bancarias ni saldos: si te preguntan por eso, aclaralo.
```

`boti-bank` — agregado al system prompt existente:

```
Para TODO lo relacionado con tarjetas de crédito (solicitar, comprar en cuotas, abonar,
consultar límite o disponible) usá las herramientas credito_*. Ese circuito lo maneja
el agente de crédito: no inventes límites ni apruebes tarjetas por tu cuenta.
Para abonar una tarjeta necesitás la cuenta de origen; si el usuario no la indicó, preguntala.
```

---

## 8. Archivos y despliegue

| Archivo | Acción |
|---|---|
| `credito.py` | **nuevo** — agente completo (DB, 5 tools, grafo, `/chat`, `/internal/*`), `API_PORT=8001` |
| `credito.yaml` | **nuevo** — OpenAPI del componente para Agent Manager, `/chat` con `{message, session_id, context}` |
| `main.py` | 4 tools `credito_*` + `_perfil_financiero` + `_call_credito` + prompt + `import requests` |
| `botibank.yaml` | agregar `PAGO_TARJETA` y `AJUSTE_REVERSA` al enum de `Movimiento` |
| `.env` | `CREDITO_AGENT_URL`, `CREDITO_AGENT_API_KEY`, `CREDITO_MODE=a2a` |
| `testing-credito.py` | **nuevo** — batería sobre el `/chat` de credito (clon de `testing-boti.py`) |
| `testing-boti.py` | agregar preguntas de tarjeta al final de `PREGUNTAS_BASE` |
| `requirements.txt` | `requests` ya está (2.34.2) — sin cambios |

En Agent Manager: segundo componente `credito-agent` con su propio endpoint y su propia API key.
**Atención al schema del `/chat`** — el comentario ya presente en `main.py` avisa que si los campos
o su obligatoriedad no coinciden con el OpenAPI registrado, el gateway rechaza con 422 antes de
llegar al grafo. `credito.yaml` debe declarar `message` y `session_id` requeridos y `context` opcional.

---

## 9. Riesgos y puntos abiertos

**Riesgos**
1. *Modelo de 8B en dos saltos*: el error de routing se compone (boti-bank elige tool → credito elige tool).
   Mitigaciones incluidas: `context` estructurado, un thread nuevo por llamada, verificación por
   `[RESULTADO_TOOLS]`, pocas tools por agente, scoring en código, y `CREDITO_MODE=rest` como plan B.
   Con todo eso el circuito completo pasó en modo a2a con el LLM real (§10).
2. *Latencia*: /chat → /chat son dos inferencias en serie. Con timeout 45s y el LLM remoto en
   el gateway WSO2, un flujo de compra puede tardar 10-20s. Aceptable para demo.
3. *Estado en RAM*: cada reinicio de `credito` borra las tarjetas mientras las cuentas de
   `boti-bank` siguen con los débitos hechos. Para demos está bien; si molesta, `dbcase/main-db.py`
   ya tiene el patrón de persistencia en archivo para copiar.
4. *Idempotencia acotada al request*: si el modelo del agente de crédito llama dos veces la misma
   tool dentro de un mismo request HTTP, la segunda devuelve el resultado de la primera en vez de
   aplicar la operación de nuevo (`_una_sola_vez`, con estado por request vía `contextvars`).
   Lo que sigue sin cubrir es un reintento del **usuario**: dos pedidos de la misma compra en dos
   requests distintos generan dos transacciones, igual que en un banco real.

**Dos decisiones tomadas al implementar** (se pueden revertir sin tocar la arquitectura):
- **A. El abono debita una cuenta real**, con la compensación de la §5. La alternativa más simple
  era que `abonar_tarjeta` solo registrara el pago en `credito` sin tocar ninguna cuenta: menos
  realista, pero elimina el problema de consistencia entre las dos DBs.
- **B. Una tarjeta activa por cliente**: la segunda solicitud devuelve la existente (idempotente).
  Permitir varias obliga a que todas las tools pidan `tarjeta_id` explícito y complica el flujo
  conversacional.

---

## 10. Implementación: qué cambió respecto de este diseño

Dos correcciones salieron de probar el circuito contra el LLM real, no del papel:

**1. La prosa del modelo no sirve como confirmación.** En la primera corrida en modo `a2a`, el
modelo de 8B respondió *"Realicé la compra"* y *"He aplicado el abono"* **sin haber llamado las
herramientas**: la DB de crédito quedó intacta y `boti-bank` ya había debitado $400 de CTA-444.
La detección por palabras clave en el texto no lo vio (la respuesta no decía "error").
Arreglado con el marcador `[RESULTADO_TOOLS]`, que agrega el código de `credito` con el resultado
crudo de cada tool ejecutada; sin marcador, `boti-bank` considera la operación fallida y compensa.

**2. El agente hijo no debe acumular historial.** Reutilizar un thread para las sucesivas llamadas
A2A degradaba al modelo hasta que dejaba de llamar sus tools (*"no tengo acceso a esas funciones"*),
y en un caso repitió el JSON de la operación anterior como si fuera la respuesta nueva. Medido:
con thread compartido, 1 de 3 operaciones ejecutó; con un thread nuevo por llamada, 3 de 3.

### Verificado

- Invariante `disponible = limite - Σ saldoPendiente` sobre la data inicial y después de operar.
- Scoring: aprobación, rechazo por ingresos, rechazo por DTI, tope máximo, idempotencia.
- Compras: OK, crédito insuficiente, cuotas fuera de rango, tarjeta inexistente, cuota en cero.
- Abonos: OK, cancelación al pagar todo, abono mayor al pendiente, transacción ya cancelada.
- Cruce de agentes por HTTP en los dos modos (`rest` y `a2a`), incluida la compensación del débito
  con la traza `PAGO_TARJETA` + `AJUSTE_REVERSA`.
- Circuito completo usuario → `boti-bank` → `credito` en lenguaje natural, con el LLM real.

### Limitación conocida (preexistente, no la introdujo este cambio)

Con historial largo, el modelo de `boti-bank` empieza a pedir confirmación antes de ejecutar una
tool, contra la regla 1 de su propio prompt. Pasa igual con tools originales como
`consultar_cuentas`; en sesión nueva no ocurre. Si molesta en la demo, las salidas son acortar el
historial que se le manda al modelo (últimos N mensajes) o un modelo más grande.
