#!/usr/bin/env python3
"""Prueba el agente BotiBank por su /chat, y de paso imprime el curl de cada caso.

Solo lectura: listar clientes y consultar cuentas. No transfiere, no paga y no
pide tarjetas, asi que se puede correr contra el agente de verdad sin mover un
peso.

    python3 test-curl.py                  # corre todos los casos
    python3 test-curl.py --curl           # solo imprime los curl, no llama
    python3 test-curl.py --msg "hola"     # manda un mensaje suelto
    python3 test-curl.py --url http://127.0.0.1:8000/chat

Lo que vigila, ademas de que conteste:

  - Que NO devuelva el JSON crudo de la herramienta. Ese es el fallo que
    aparecio: el modelo copia el resultado de la tool en vez de redactarlo, o
    el endpoint devuelve el ToolMessage tal cual.
  - Que repetir la pregunta en la MISMA sesion siga dando prosa. Con el
    historial entero el modelo acaba repitiendo su propia salida anterior, y
    si una salida fue mala la repite para siempre.
  - Que un pedido distinto llame a la herramienta que le toca en vez de
    reciclar la respuesta anterior.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

VERDE, ROJO, GRIS, FIN = "\033[32m", "\033[31m", "\033[90m", "\033[0m"


def curl_de(url, payload):
    cuerpo = json.dumps(payload, ensure_ascii=False)
    return ("curl -sS %s \\\n     -H 'Content-Type: application/json' \\\n"
            "     -d '%s'" % (url, cuerpo.replace("'", "'\\''")))


def preguntar(url, mensaje, sesion, timeout, mostrar_curl=True):
    payload = {"message": mensaje, "session_id": sesion}
    if mostrar_curl:
        print(GRIS + curl_de(url, payload) + FIN)
    datos = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=datos, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cuerpo = json.loads(r.read().decode("utf-8"))
        return cuerpo.get("response", ""), time.time() - t0, None
    except urllib.error.HTTPError as e:
        return "", time.time() - t0, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:                                  # red, timeout, json
        return "", time.time() - t0, "%s: %s" % (type(e).__name__, e)


def es_json_crudo(texto):
    """El fallo tiene forma propia: la respuesta ES el payload de la tool."""
    t = (texto or "").strip()
    if not (t.startswith("[") or t.startswith("{")):
        return False
    try:
        json.loads(t)
        return True
    except ValueError:
        return False


class Informe:
    def __init__(self):
        self.mal = 0

    def check(self, nombre, ok, detalle=""):
        print(("  %s✔%s " % (VERDE, FIN)) if ok else ("  %s✘%s " % (ROJO, FIN)), end="")
        print(nombre + (("  → " + detalle) if detalle else ""))
        if not ok:
            self.mal += 1

    def respuesta(self, texto, segundos, error):
        if error:
            print("    %s[error] %s%s" % (ROJO, error, FIN))
            if "Connection error" in error:
                print("    %sEl agente esta vivo pero no alcanza al modelo. Mira MODEL_BASE_URL"
                      " en el .env y comprueba que ese host conteste:%s" % (GRIS, FIN))
                print("    %scurl -sS <MODEL_BASE_URL>/models%s" % (GRIS, FIN))
            elif "timed out" in error:
                print("    %sSin respuesta a tiempo. Un 8B en CPU tarda, y si hay varias"
                      " peticiones a la vez hacen cola: subi --timeout o probalo solo.%s"
                      % (GRIS, FIN))
            return
        corto = texto if len(texto) <= 220 else texto[:220] + "…"
        print("    " + corto.replace("\n", "\n    "))
        print("    %s(%.1fs)%s" % (GRIS, segundos, FIN))


def caso_una_pregunta(url, inf, timeout):
    print("\n1. Una pregunta suelta: debe contestar en texto, no en JSON")
    sesion = "test-" + uuid.uuid4().hex[:8]
    texto, seg, err = preguntar(url, "Clientes del banco?", sesion, timeout)
    inf.respuesta(texto, seg, err)
    if err:
        inf.check("el agente responde", False, err)
        return None, sesion
    inf.check("el agente responde", bool(texto.strip()))
    inf.check("NO devuelve el JSON crudo de la herramienta", not es_json_crudo(texto),
              "la respuesta empieza por '[{' y parsea como JSON" if es_json_crudo(texto) else "")
    inf.check("nombra a algun cliente", any(n in texto for n in ("Ana", "Pablo", "Carlos")),
              "" if any(n in texto for n in ("Ana", "Pablo", "Carlos")) else "no aparece ningun nombre")
    return texto, sesion


def caso_repetir(url, inf, sesion, timeout):
    print("\n2. La misma pregunta 3 veces en la MISMA sesion")
    print("   (con el historial entero, el modelo acaba repitiendo su salida anterior)")
    for vuelta in (2, 3, 4):
        texto, seg, err = preguntar(url, "clientes del banco?", sesion, timeout, mostrar_curl=False)
        inf.respuesta(texto, seg, err)
        if err:
            inf.check("vuelta %d responde" % vuelta, False, err)
            continue
        inf.check("vuelta %d sigue siendo texto, no JSON" % vuelta, not es_json_crudo(texto))


def caso_encadenado(url, inf, timeout):
    print("\n3. Un pedido distinto despues de otro: tiene que usar OTRA herramienta")
    sesion = "test-" + uuid.uuid4().hex[:8]
    primero, seg, err = preguntar(url, "Clientes del banco?", sesion, timeout)
    inf.respuesta(primero, seg, err)
    if err:
        inf.check("la primera pregunta responde", False, err)
        return

    # El id del cliente sale de lo que acaba de contestar: nada inventado.
    import re
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", primero)
    if not uuids:
        inf.check("la respuesta trae algun id de cliente para encadenar", False,
                  "sin UUID no se puede pedir sus cuentas")
        return
    cliente = uuids[0]
    inf.check("la respuesta trae el id completo del cliente", True, cliente)

    segundo, seg2, err2 = preguntar(url, "quiero las cuentas del cliente " + cliente,
                                    sesion, timeout)
    inf.respuesta(segundo, seg2, err2)
    if err2:
        inf.check("la segunda pregunta responde", False, err2)
        return
    inf.check("la segunda no es JSON crudo", not es_json_crudo(segundo))
    inf.check("la segunda NO repite la lista de clientes",
              segundo.strip() != primero.strip(),
              "devolvio exactamente lo mismo que la pregunta anterior"
              if segundo.strip() == primero.strip() else "")


def caso_sesion_nueva(url, inf, timeout):
    print("\n4. Sesion nueva: tiene que venir limpia")
    sesion = "test-" + uuid.uuid4().hex[:8]
    texto, seg, err = preguntar(url, "Clientes del banco?", sesion, timeout)
    inf.respuesta(texto, seg, err)
    if err:
        inf.check("responde en una sesion nueva", False, err)
        return
    inf.check("responde en una sesion nueva", bool(texto.strip()))
    inf.check("y tampoco en JSON", not es_json_crudo(texto))


def main():
    p = argparse.ArgumentParser(description="Prueba el /chat del agente BotiBank.")
    p.add_argument("--url", default="http://127.0.0.1:8000/chat")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="un modelo de 8B en CPU tarda; por defecto 300s")
    p.add_argument("--msg", help="manda un solo mensaje y sale")
    p.add_argument("--session", help="session_id a usar con --msg")
    p.add_argument("--curl", action="store_true", help="solo imprime los curl")
    args = p.parse_args()

    if args.curl:
        for m in ("Clientes del banco?", "clientes del banco?",
                  "quiero las cuentas del cliente <ID>"):
            print(curl_de(args.url, {"message": m, "session_id": "mi-sesion"}))
            print()
        return 0

    if args.msg:
        sesion = args.session or ("test-" + uuid.uuid4().hex[:8])
        texto, seg, err = preguntar(args.url, args.msg, sesion, args.timeout)
        if err:
            Informe().respuesta("", seg, err)
            return 1
        print("\n" + texto)
        print("%s(%.1fs, session_id=%s)%s" % (GRIS, seg, sesion, FIN))
        return 1 if es_json_crudo(texto) else 0

    print("Agente: " + args.url)
    inf = Informe()
    _, sesion = caso_una_pregunta(args.url, inf, args.timeout)
    if sesion:
        caso_repetir(args.url, inf, sesion, args.timeout)
    caso_encadenado(args.url, inf, args.timeout)
    caso_sesion_nueva(args.url, inf, args.timeout)

    print("\n" + ("%s%d comprobaciones mal%s" % (ROJO, inf.mal, FIN) if inf.mal
                  else "%sTodo bien%s" % (VERDE, FIN)))
    return 1 if inf.mal else 0


if __name__ == "__main__":
    sys.exit(main())
