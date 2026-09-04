## Agente de Crédito (local, puerto 8001)

Levantar los dos agentes:

    ./bin/python -m uvicorn credito:app --host 127.0.0.1 --port 8001    # agente credito
    ./bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000   # boti-bank

Preguntarle a boti-bank (él consulta al agente de crédito por A2A):

curl -X POST "http://127.0.0.1:8000/chat" \
     -H "Content-Type: application/json" \
     -d '{
           "message": "¿Puede la clienta Ana, con ID 71992c72-cc1c-4c5a-8b50-9ee4fb6c214d, sacar una tarjeta de crédito?",
           "session_id": "demo-tarjetas-01"
         }'

Hablarle directo al agente de crédito (los datos duros van en context, no en la prosa):

curl -X POST "http://127.0.0.1:8001/chat" \
     -H "Content-Type: application/json" \
     -d '{
           "message": "Mostrame el resumen de cuenta de esta tarjeta.",
           "session_id": "credito-1",
           "context": {"tarjeta_id": "TC-4821"}
         }'

Camino determinístico, sin LLM intermedio (lo que usa boti-bank con CREDITO_MODE=rest):

curl -s http://127.0.0.1:8001/internal/tarjetas/71992c72-cc1c-4c5a-8b50-9ee4fb6c214d

curl -X POST http://127.0.0.1:8001/internal/compras \
     -H "Content-Type: application/json" \
     -d '{"tarjeta_id":"TC-4821","objeto":"Microondas","cuotas":4,"monto_cuota":75.0}'

curl -X POST http://127.0.0.1:8001/internal/pagos \
     -H "Content-Type: application/json" \
     -d '{"transaccion_id":"TRX-000101","monto":150.0}'
