curl -X POST http://default-default.gateway.localhost:19080/mistral-llm/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-8b-2512",
    "messages": [
      {
        "role": "user",
        "content": "Hola, ¿estás funcionando correctamente? Responde en una sola frase."
      }
    ],
    "max_tokens": 50
  }'