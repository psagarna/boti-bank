

curl -X POST "https://development-wc-019e6383-b65fad2b.agent-manager.us-east-2.cloud.wso2.com:443/botibank-xz-botibank-xz-endpoint/chat" \
     -H "Content-Type: application/json" \
     -d '{
           "message": "Please, list me the client of the bank",
           "session_id": "test-session-123"
         }'


curl -X POST https://apigateway-wc-019e6383-b65fad2b.us-east-2.cloud.wso2.com/local-llm/chat/completions \
  -H "Authorization: Bearer a10e802672e9be1ae059cc8eff011bb6563b082b8c404215d5b25e88f3a9836f" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-2-pro-llama3-8b",
    "messages": [
      {"role": "system", "content": "Eres un asistente experto en programación de sistemas."},
      {"role": "user", "content": "Explícame en una frase corta por qué Go es tan rápido."}
    ],
    "temperature": 0.7,
    "max_tokens": 150
  }'

  MODEL_API_KEY=a10e802672e9be1ae059cc8eff011bb6563b082b8c404215d5b25e88f3a9836f
MODEL_BASE_URL=https://apigateway-wc-019e6383-b65fad2b.us-east-2.cloud.wso2.com/local-llm

Boti bank-ext

curl -X POST "https://development-wc-019e6383-b65fad2b.agent-manager.us-east-2.cloud.wso2.com:443/boti-bank-localllm-boti-bank-localllm-endpoint/chat" \
     -H "x-api-key: e8a89b4d69aafb7ebc118faedea58f6de930753fa0b52fceb3d14238f08e2103" \
     -H "Content-Type: application/json" \
     -d '{
           "message": "Please, list me the client of the bank",
           "session_id": "test-session-123"
         }'


curl -X POST "http://localhost:8000/chat" \
     -H "x-api-key: e8a89b4d69aafb7ebc118faedea58f6de930753fa0b52fceb3d14238f08e2103" \
     -H "Content-Type: application/json" \
     -d '{
           "message": "Please, list me the client of the bank",
           "session_id": "test-session-123"
         }'


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
