# BotiBank — Multi-Agent Banking Assistant

![Architecture](https://img.shields.io/badge/architecture-LangGraph%20%2B%20FastAPI-00A3E0?style=flat-square)
![LLM](https://img.shields.io/badge/LLM-OpenAI--compatible%20%7C%20Mistral-FF6F00?style=flat-square)
![State](https://img.shields.io/badge/state-in--memory-4CAF50?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)

A conversational banking demo built with **LangGraph** (reasoning + tool calling) and **FastAPI**
(HTTP surface). It is made of **two independent agents** that talk to each other over HTTP:

| Agent | File | Port | Owns |
| :--- | :--- | :--- | :--- |
| **`boti-bank`** | `main.py` | `8000` | Customers, bank accounts, transactions, utility bills, mortgages |
| **`credito`** | `credito.py` | `8001` | Credit cards, instalment purchases, card payments, risk scoring |

When a user asks `boti-bank` anything about credit cards, `boti-bank` does **not** answer by itself:
it calls the `credito` agent and relays its verdict. That agent-to-agent (**A2A**) hop is the point
of this project.

Both agents keep all their data in a Python dictionary in RAM — no database, no disk. Restarting a
process resets its world.

> 📐 The full design rationale, trade-offs and test findings live in **[`DESIGN-credito.md`](DESIGN-credito.md)**.

---

## Table of contents

- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Tool reference](#tool-reference)
- [How the two agents talk (A2A)](#how-the-two-agents-talk-a2a)
- [Credit policy (deterministic scoring)](#credit-policy-deterministic-scoring)
- [HTTP API reference](#http-api-reference)
- [Seed data](#seed-data)
- [Testing](#testing)
- [Deploying to WSO2 Agent Manager](#deploying-to-wso2-agent-manager)
- [Known limitations](#known-limitations)

---

## Architecture

```mermaid
flowchart TB
    subgraph client["Client layer"]
        C["cURL · Swagger UI · testing-*.py · Web/Mobile app"]
    end

    subgraph boti["boti-bank — main.py · port 8000"]
        BAPI["FastAPI  POST /chat"]
        BGRAPH["LangGraph<br/>agent node ⇄ tool node<br/>MemorySaver checkpointer"]
        BDB[("IN_MEMORY_DB<br/>clientes · cuentas<br/>movimientos · servicios · hipotecas")]
        BAPI --> BGRAPH --> BDB
    end

    subgraph credito["credito — credito.py · port 8001"]
        CAPI["FastAPI<br/>POST /chat  ·  /internal/*"]
        CGRAPH["LangGraph<br/>agent node ⇄ tool node"]
        COPS["_op_* business functions<br/>(shared by tools and REST)"]
        CDB[("CREDITO_DB<br/>tarjetas · transacciones · pagos")]
        CAPI --> CGRAPH --> COPS --> CDB
        CAPI -.-> COPS
    end

    LLM["LLM endpoint<br/>(local llama-server / OpenAI / Mistral gateway)"]

    C -->|"POST /chat<br/>message · session_id · context?"| BAPI
    BGRAPH -->|"A2A: POST /chat<br/>REST: POST /internal/*"| CAPI
    BGRAPH -.->|inference| LLM
    CGRAPH -.->|inference| LLM
```

**The LangGraph loop** (identical in both agents):

```
                  ┌─────────────────────────────┐
                  │                             │
                  ▼                             │
  START ──▶ ┌───────────┐   tool_calls?  ┌──────────────┐
            │   agent   │ ──── yes ────▶ │  tool node   │
            │ (LLM call)│                │ (runs tools) │
            └───────────┘                └──────────────┘
                  │
                  └──── no ────▶ END   (reply goes back to the caller)
```

**Ownership rule:** account money is *always* moved by `boti-bank`; credit (limit, instalments,
card balance) is *always* moved by `credito`. Neither agent writes into the other's store.
Communication is one-directional — `credito` never calls back into `boti-bank`, so there are no A2A
cycles.

---

## Repository layout

| Path | What it is |
| :--- | :--- |
| `main.py` | **`boti-bank` agent.** In-memory bank DB, 7 banking tools + 4 `credito_*` bridge tools, LangGraph graph, `POST /chat`. |
| `credito.py` | **`credito` agent.** In-memory credit DB, deterministic risk policy, 5 tools, `POST /chat` **and** `/internal/*` REST endpoints. |
| `main-mistral.py` | Variant of `main.py` that talks to a **Mistral** endpoint (`ChatMistralAI`) behind a gateway that authenticates with an `API-Key` header instead of `Authorization: Bearer`. Same tools, same graph. |
| `botibank.yaml` | OpenAPI 3.0 description of the **banking domain** (customers, accounts, services, mortgages). Reference/contract document — note that `main.py` itself only serves `POST /chat`. |
| `credito.yaml` | OpenAPI 3.0 spec of the `credito` component: `/chat` plus the `/internal/*` endpoints. This is what gets registered in Agent Manager. |
| `DESIGN-credito.md` | Design document: architecture decision, data model, tool contracts, sequence flows, risks, and what changed once it was tested against a real LLM. |
| `testing-boti.py` | Interactive test harness against `boti-bank` — two question batteries (banking, and credit via A2A). |
| `testing-credito.py` | Interactive test harness against the `credito` agent's `/chat`, sending hard data in `context`. |
| `manual-test.md` | Copy-paste `curl` snippets for manual smoke testing. |
| `.env` / `.env-mistral` | Environment configuration (see [Configuration](#configuration)). ⚠️ Both are **tracked in git** and `.gitignore` does not exclude them — see the warning under [Configure](#3-configure). |
| `requirements.txt` | Pinned dependencies. |

---

## Quick start

### 1. Requirements

- Python **3.10+** (this repo was developed on 3.14)
- An **OpenAI-compatible** chat endpoint that supports **tool / function calling**. Any of:
  - a local `llama-server` running e.g. `hermes-2-pro-llama3-8b`
  - the OpenAI API
  - a Mistral endpoint (use `main-mistral.py`)

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> This repo ships with a virtualenv already created **at the repository root**
> (`bin/`, `lib/`, `pyvenv.cfg` — all git-ignored). If you are using that one instead, drop the
> `venv` step and prefix commands with `./bin/python`, as `manual-test.md` does.

### 3. Configure

Create a `.env` in the repo root (see [Configuration](#configuration) for every variable):

```dotenv
# LLM
MODEL_BASE_URL=http://127.0.0.1:8081/v1
MODEL_API_KEY=not-needed
MODEL_NAME=hermes-2-pro-llama3-8b

# Agent-to-agent
CREDITO_AGENT_URL=http://127.0.0.1:8001/chat
CREDITO_AGENT_API_KEY=
CREDITO_MODE=a2a
CREDITO_TIMEOUT=45

# Servers
API_HOST=127.0.0.1
API_PORT=8000
CREDITO_API_PORT=8001
```

> ⚠️ **Before you put a real key in here.** `.env` and `.env-mistral` are currently **tracked in
> git**, and `.gitignore` does not exclude them. They hold no real secret today — the committed
> `MODEL_API_KEY` is the same placeholder as the in-code default — but the moment someone pastes a
> real WSO2 or OpenAI key and commits, it is in the history. Before sharing this repo, consider
> `git rm --cached .env .env-mistral`, adding `.env*` to `.gitignore`, and committing a
> `.env.example` with placeholder values instead.

### 4. Run both agents

Start `credito` **first** — `boti-bank` needs it to be up before any credit question:

```bash
# Terminal 1 — credit agent
python -m uvicorn credito:app --host 127.0.0.1 --port 8001

# Terminal 2 — main banking agent
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Swagger UI is available at `http://127.0.0.1:8000/docs` and `http://127.0.0.1:8001/docs`.

### 5. Talk to it

```bash
# Plain banking question — resolved entirely by boti-bank
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "List all the bank customers.",
        "session_id": "demo-01"
      }'

# Credit question — boti-bank delegates to the credito agent over A2A
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Can customer Ana, ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d, get a credit card?",
        "session_id": "demo-cards-01"
      }'
```

> The agents' system prompts and seed data are in **Spanish**, so the models answer in Spanish.
> They understand questions in either language.

---

## Configuration

All variables are read with `os.getenv` at import time and loaded from `.env` via `python-dotenv`.

| Variable | Used by | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `MODEL_BASE_URL` | both | `http://127.0.0.1:8081/v1` | OpenAI-compatible chat completions base URL. |
| `MODEL_API_KEY` | both | `sk-mi-clave-secreta-123` | API key for the LLM endpoint (`not-needed` for most local servers). |
| `MODEL_NAME` | both | `hermes-2-pro-llama3-8b` | Model id requested from that endpoint. |
| `MODEL_TIMEOUT` | `main-mistral.py` | `120` | HTTP timeout (seconds) for the Mistral client. |
| `API_HOST` | both | `0.0.0.0` | Bind address when run as `python main.py` / `python credito.py`. |
| `API_PORT` | `main.py` | `8000` | `boti-bank` port. |
| `CREDITO_API_PORT` | `credito.py` | `8001` | `credito` port. |
| `CREDITO_AGENT_URL` | `main.py` | `http://127.0.0.1:8001/chat` | The `credito` agent's `/chat` URL. |
| `CREDITO_BASE_URL` | `main.py` | derived from `CREDITO_AGENT_URL` | Base URL used for `/internal/*` calls. Set it only if it can't be derived by stripping `/chat`. |
| `CREDITO_AGENT_API_KEY` | `main.py` | *(empty)* | Sent as `x-api-key` when calling `credito`. Leave empty locally. |
| `CREDITO_MODE` | `main.py` | `a2a` | `a2a` = call `credito`'s `/chat`; `rest` = call its `/internal/*` endpoints. |
| `CREDITO_TIMEOUT` | `main.py` | `45` | Seconds to wait for the credit agent. A2A means two chained inferences, so keep it generous. |
| `AGENT_URL` | `testing-boti.py` | *(WSO2 URL)* | Where the test harness posts. |
| `AGENT_API_KEY` | `testing-boti.py` | *(empty)* | `x-api-key` for that endpoint. |

---

## Tool reference

### `boti-bank` — banking tools (`main.py`)

| Tool | Signature | What it does |
| :--- | :--- | :--- |
| `listar_clientes` | `()` | Returns every customer in the bank. |
| `consultar_cuentas` | `(cliente_id)` | Lists the customer's accounts with their balances. |
| `ingresar_dinero` | `(cuenta_id, monto)` | Deposits money into an account and records an `INGRESO` movement. |
| `transferir_dinero` | `(cuenta_origen, cuenta_destino, monto, concepto)` | Transfers between accounts after checking the source balance; records both legs. |
| `listar_servicios` | `()` | Lists outstanding utility bills (electricity, water, internet). |
| `pagar_servicio` | `(cuenta_origen, codigo_servicio)` | Pays a bill, debits the account and removes the bill from the list. |
| `pagar_hipoteca` | `(cuenta_origen, id_hipoteca, monto)` | Makes a partial mortgage payment and recomputes the outstanding balance. |

### `boti-bank` — bridge tools into the credit agent

| Tool | Signature | What it does |
| :--- | :--- | :--- |
| `credito_solicitar_tarjeta` | `(cliente_id)` | Builds the customer's **financial profile locally** and asks `credito` to score and issue a card. Returns approval, card id, limit and available credit. |
| `credito_comprar` | `(tarjeta_id, objeto, cuotas, monto_cuota)` | Registers an instalment purchase. Returns the transaction id and remaining credit. |
| `credito_abonar` | `(transaccion_id, monto, cuenta_origen)` | **The only cross-store operation.** Debits a real bank account, then asks `credito` to apply the payment — and reverses the debit if that fails. |
| `credito_estado` | `(cliente_id)` | Returns the customer's cards: limit, available credit, debt and live instalment purchases with their transaction ids. |

### `credito` — credit tools (`credito.py`)

| Tool | Signature | What it does |
| :--- | :--- | :--- |
| `solicitar_tarjeta_credito` | `(cliente_id, titular, ingreso_mensual, saldo_total, cuota_deuda_mensual)` | Applies the deterministic risk policy and issues the card if it qualifies. **Idempotent**: a customer with an active card gets that card back instead of a new one. |
| `comprar_con_tarjeta` | `(tarjeta_id, objeto, cuotas, monto_cuota)` | Records an instalment purchase, generates `TRX-xxxxxx` and reduces available credit. |
| `abonar_tarjeta` | `(transaccion_id, monto)` | Applies a payment to a transaction, recomputes instalments paid, frees up credit, and closes the transaction (`CANCELADA`) when fully paid. |
| `consultar_tarjetas` | `(cliente_id)` | Lists the customer's cards with limit and available credit. |
| `estado_cuenta_tarjeta` | `(tarjeta_id)` | Statement: total debt, next instalment total, and every live purchase with its transaction id. |

Every credit tool is a thin wrapper over an `_op_*` function. The `/internal/*` REST endpoints call
**the exact same** `_op_*` functions, which is why both integration modes behave identically.

**Core invariant, enforced by `_recalcular_disponible()` after every purchase and payment:**

```
available = limit − Σ(outstanding balance of that card's VIGENTE transactions)
```

---

## How the two agents talk (A2A)

`CREDITO_MODE` selects the transport:

### `a2a` (default) — agent-to-agent

`boti-bank` posts to `credito`'s `/chat`. This is the demo-worthy path: a real conversation between
two LLM agents. Three deliberate design choices make it reliable enough to trust:

**1. Hard data travels in `context`, never in the prose.**
`boti-bank` sends `{"message": "...", "session_id": "...", "context": {...}}`. The `credito` agent
appends the context to the prompt as an explicit `DATOS` block with the instruction to use those
values verbatim — so a small model never has to extract numbers out of a Spanish sentence.

**2. One fresh thread per call.**
Each A2A call uses `botibank-<session>-<random>` as its `session_id` on the child agent. This is an
RPC, not a conversation. Reusing a thread makes history pile up until the small model stops calling
its tools altogether — in one measured run it even replayed the *previous* operation's JSON as the
new answer. Measured: shared thread → 1 of 3 operations actually executed; fresh thread → 3 of 3.

**3. Execution is verified, not trusted.**
`credito`'s `/chat` appends the **real** result of every tool it executed under a
`[RESULTADO_TOOLS]` marker. That block is written by code, not by the model. `boti-bank`'s
`_procesar_a2a()` refuses any response without it.

> This is not paranoia. On the first real run, the 8B model replied *"I registered the purchase"*
> and *"I applied the payment"* **without ever calling the tools** — while `boti-bank` had already
> debited $400 from a real account. Keyword detection didn't catch it, because the reply contained
> no error text. See §10 of `DESIGN-credito.md`.

### `rest` — deterministic

`boti-bank` calls `credito`'s `/internal/*` endpoints directly. No intermediate LLM, same return
contract, zero risk of the child model going off-script. Use it as the fallback when the demo has
to just work.

### The delicate case: paying a card

`credito_abonar` is the only operation that touches both stores, and there is no distributed
transaction. The order is deliberate — the local, reversible, no-network step first; the remote step
last; **compensate** if the remote step fails.

```mermaid
sequenceDiagram
    participant U as User
    participant B as boti-bank (8000)
    participant C as credito (8001)

    U->>B: "Pay 150 of TRX-000101 from CTA-122"
    B->>B: Validate account exists, amount > 0, sufficient balance
    B->>B: Debit CTA-122 · record PAGO_TARJETA
    B->>C: POST /chat (or /internal/pagos) {transaccion_id, monto}
    alt Payment applied
        C-->>B: {saldoPendiente, cuotasPagadas, disponibleTarjeta} + [RESULTADO_TOOLS]
        B-->>U: "Payment applied. New account balance: …"
    else Error, timeout, or missing [RESULTADO_TOOLS]
        C-->>B: ERROR / no tool result
        B->>B: Refund CTA-122 · record AJUSTE_REVERSA
        B-->>U: "Could not apply the payment; the money was refunded to CTA-122."
    end
```

Both modes return a string starting with `ERROR` when the payment did not go through, so the
compensation check is a single exact `startswith("ERROR")`.

---

## Credit policy (deterministic scoring)

**The LLM never decides who gets a card or how big the limit is.** It only calls the tool and
reports the verdict. The policy lives in plain Python at the top of `credito.py`:

| Constant | Value | Meaning |
| :--- | :--- | :--- |
| `MIN_INGRESO_MENSUAL` | `400.0` | Below this monthly income, the application is rejected. |
| `DTI_MAXIMO` | `0.50` | Max debt-to-income ratio (existing monthly debt instalments ÷ monthly income). |
| `LIMITE_MINIMO` | `500.0` | Floor for an approved limit. |
| `LIMITE_MAXIMO` | `15000.0` | Ceiling for an approved limit. |
| `MAX_CUOTAS` | `24` | Max instalments per purchase. |

**Decision flow**

1. Already has an `ACTIVA` card? → return it unchanged (idempotent, no re-scoring).
2. `ingreso_mensual < 400` → **rejected**, with the reason.
3. `cuota_deuda_mensual / ingreso_mensual > 0.50` → **rejected**, with the DTI in the reason.
4. Otherwise **approved**, with:

```
capacity = (3 × monthly_income) + (0.20 × total_account_balance) − (2 × monthly_debt_instalments)
limit    = clamp(capacity, 500, 15000)
```

**Where the profile comes from.** `credito` has no access to bank accounts, so `boti-bank` computes
the profile itself in `_perfil_financiero()` and sends it along:

- `ingreso_mensual` — sum of all `INGRESO` + `TRANSFERENCIA_RECIBIDA` movements across the
  customer's accounts, divided by the number of distinct months those movements span.
- `saldo_total` — sum of the balances of all the customer's accounts.
- `cuota_deuda_mensual` — sum of the monthly instalments of the customer's mortgages.

---

## HTTP API reference

### `boti-bank` — `http://127.0.0.1:8000`

**`POST /chat`**

```jsonc
// Request
{
  "message": "List all the bank customers.",   // required
  "session_id": "demo-01",                     // required — LangGraph thread_id, keeps history
  "context": { "any": "json" }                 // optional
}

// Response
{ "response": "..." }
```

`session_id` maps directly onto the LangGraph `thread_id`, so reusing it continues the same
conversation via the in-memory `MemorySaver` checkpointer.

### `credito` — `http://127.0.0.1:8001`

**`POST /chat`** — same schema. When `context` is present it is appended to the prompt as a `DATOS`
block. The response body has the `[RESULTADO_TOOLS] [...]` block appended when tools ran.

**Deterministic endpoints** (no LLM in the path):

| Method | Path | Body / Params | Returns |
| :--- | :--- | :--- | :--- |
| `POST` | `/internal/tarjetas` | `{cliente_id, titular, ingreso_mensual, saldo_total, cuota_deuda_mensual}` | `{aprobada, tarjetaId, limite, disponible, motivo}` |
| `GET` | `/internal/tarjetas/{clienteId}` | — | `{clienteId, cantidad, tarjetas[]}` |
| `GET` | `/internal/tarjetas/{tarjetaId}/estado` | — | Full statement for the card |
| `POST` | `/internal/compras` | `{tarjeta_id, objeto, cuotas, monto_cuota}` | `{transaccionId, montoTotal, disponibleRestante, …}` |
| `POST` | `/internal/pagos` | `{transaccion_id, monto}` | `{pagoId, saldoPendiente, cuotasPagadas, estado, disponibleTarjeta}` |

Business-rule failures return **HTTP 400** with `{"detail": "<reason>"}`.

---

## Seed data

Both agents boot with the same fixed dataset every time, so demos are reproducible.

**Customers & accounts** (`main.py` → `IN_MEMORY_DB`)

| Customer | ID | Accounts (balance) | Mortgage |
| :--- | :--- | :--- | :--- |
| Ana García | `71992c72-cc1c-4c5a-8b50-9ee4fb6c214d` | `CTA-122` Corriente (1 800.50) · `CTA-123` Ahorro (100.00) · `CTA-999` Inversión (0.00) | `HIP-001` — 145 000 left, 800/mo |
| Pablo Saga | `88888888-cc1c-4c5a-8b50-9ee4fb6c214d` | `CTA-444` Sueldo (5 000.00) | `HIP-002` — 50 000 left, 1 200/mo |
| Carlos Pérez | `33333333-cc1c-4c5a-8b50-9ee4fb6c214d` | `CTA-555` Ahorro (250.00) | — |

**Utility bills:** `LZ1` Luz (50.00) · `AG2` Agua (25.50) · `IN3` Internet (40.00)

**Credit cards** (`credito.py` → `CREDITO_DB`)

| Card | Holder | Limit | Available | Live purchases |
| :--- | :--- | :--- | :--- | :--- |
| `TC-4821` | Ana García | 2 755.00 | 2 035.00 | `TRX-000101` notebook, 6×150, 600 left · `TRX-000102` sneakers, 3×40, 120 left |
| `TC-7315` | Pablo Saga | 15 000.00 | 10 600.00 | `TRX-000103` flights, 12×400, 4 400 left *(`TRX-000104` fully paid → `CANCELADA`)* |

Carlos Pérez deliberately has **no card** — he is the fixture for testing the scoring path
(his real profile is rejected for insufficient income).

---

## Testing

### Interactive harnesses

```bash
python testing-boti.py       # menu: 1 = banking battery, 2 = credit-via-A2A battery
python testing-credito.py    # talks straight to the credito agent, hard data in `context`
```

Both print the full request (with the API key masked), the raw JSON response and the agent's reply,
which makes them useful for debugging the A2A hop.

### Manual `curl`

Start both agents, then:

```bash
# Ask boti-bank a credit question — it delegates over A2A
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"¿Puede la clienta Ana, con ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d, sacar una tarjeta de crédito?","session_id":"demo-tarjetas-01"}'

# Talk to the credit agent directly (hard data goes in context, not in the prose)
curl -X POST http://127.0.0.1:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Mostrame el resumen de cuenta de esta tarjeta.","session_id":"credito-1","context":{"tarjeta_id":"TC-4821"}}'

# Deterministic path — what CREDITO_MODE=rest uses
curl -s http://127.0.0.1:8001/internal/tarjetas/71992c72-cc1c-4c5a-8b50-9ee4fb6c214d

curl -X POST http://127.0.0.1:8001/internal/compras \
  -H "Content-Type: application/json" \
  -d '{"tarjeta_id":"TC-4821","objeto":"Microondas","cuotas":4,"monto_cuota":75.0}'

curl -X POST http://127.0.0.1:8001/internal/pagos \
  -H "Content-Type: application/json" \
  -d '{"transaccion_id":"TRX-000101","monto":150.0}'
```

More snippets in [`manual-test.md`](manual-test.md).

### What has been verified

Covered by the batteries and documented in §10 of `DESIGN-credito.md`:

- The `available = limit − Σ outstanding` invariant, on seed data and after operating.
- Scoring: approval, rejection by income, rejection by DTI, limit ceiling, idempotency.
- Purchases: success, insufficient credit, instalments out of range, unknown card, zero instalment.
- Payments: success, auto-cancellation on full payment, overpayment, already-cancelled transaction.
- Cross-agent HTTP in **both** modes, including the compensating refund
  (`PAGO_TARJETA` followed by `AJUSTE_REVERSA` in the movement log).
- The end-to-end natural-language flow user → `boti-bank` → `credito` against a real LLM.

---

## Deploying to WSO2 Agent Manager

Each agent is deployed as its **own component**, with its own endpoint and its own API key:

| Component | Source | Spec | Port |
| :--- | :--- | :--- | :--- |
| `botibank` | `main.py` | `botibank.yaml` | 8000 |
| `credito-agent` | `credito.py` | `credito.yaml` | 8001 |

Then point `boti-bank` at the deployed credit agent:

```dotenv
CREDITO_AGENT_URL=https://<credito-endpoint>/chat
CREDITO_AGENT_API_KEY=<its api key>
```

> ⚠️ **The `/chat` schema must match the registered OpenAPI exactly** — `message` and `session_id`
> required, `context` optional. If the field names or their requiredness drift, the gateway rejects
> the request with **422 before it ever reaches the graph**. There is a `NOTE` comment guarding this
> in both `main.py` and `credito.py`; keep it in sync with the YAML.

---

## Known limitations

These are intentional trade-offs for a demo, not open bugs — all of them are discussed in
`DESIGN-credito.md` §9.

1. **State is in RAM.** Restarting `credito` wipes the cards while `boti-bank`'s accounts keep the
   debits that were already applied. Fine for demos; needs a persistence layer otherwise.
2. **Two hops of a small model compound routing error** (`boti-bank` picks a tool → `credito` picks a
   tool). Mitigated by structured `context`, a fresh thread per call, `[RESULTADO_TOOLS]`
   verification, few tools per agent, scoring in code, and `CREDITO_MODE=rest` as plan B.
3. **Latency.** A2A means two inferences in series; a purchase flow can take 10–20 s.
4. **Idempotency is scoped to one HTTP request.** If the model calls the same tool twice inside one
   request, the second call returns the first result (`_una_sola_vez`, via `contextvars`). A repeat
   from the *user* in a separate request still creates a second transaction — same as a real bank.
5. **One active card per customer.** A second application returns the existing card.
6. **Long histories degrade instruction following.** With enough accumulated history, `boti-bank`'s
   model starts asking for confirmation before running a tool, against rule 1 of its own prompt.
   Pre-existing, also happens with plain tools like `consultar_cuentas`, and does not occur in a
   fresh session. Workarounds: truncate the history sent to the model, or use a larger model.
