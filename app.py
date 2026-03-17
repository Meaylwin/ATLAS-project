from flask import Flask, request, jsonify
import gspread
import os
import re
import json
from datetime import datetime
from google.oauth2.service_account import Credentials
import requests

app = Flask(__name__)

# Configuración Google Sheets
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Meta Cloud API
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")

# Números
NUMERO_MANU = os.getenv("NUMERO_MANU", "56995438310")
NUMERO_CAMI = os.getenv("NUMERO_CAMI", "")

# Estado conversaciones
conversaciones = {}

# Nombre hoja dinámica
MESES_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", 
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
SHEET_NAME = f"F. {MESES_ES[datetime.now().month - 1]}"


# ---------------- GOOGLE SHEETS ----------------

def get_sheet():
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    return client.open_by_key(spreadsheet_id).worksheet(SHEET_NAME)


def get_categorias():
    try:
        sheet = get_sheet()
        categorias_raw = sheet.col_values(2)[6:]
        categorias = []

        for cat in categorias_raw:
            cat = cat.strip()
            if cat and cat not in categorias:
                if cat in ['Hogar', 'Compras', 'Otros']:
                    categorias.append(cat)

        return categorias if categorias else ['Hogar', 'Compras', 'Otros']
    except:
        return ['Hogar', 'Compras', 'Otros']


def encontrar_ultima_fila_categoria(categoria):
    sheet = get_sheet()
    cell = sheet.find(categoria, in_column=2)

    if not cell:
        return len(sheet.col_values(1)) + 1

    fila_inicio = cell.row + 1
    valores_c = sheet.col_values(3)
    valores_b = sheet.col_values(2)

    categorias = ['Hogar', 'Compras', 'Otros']

    for i in range(fila_inicio - 1, len(valores_c)):
        if i < len(valores_b):
            valor_b = valores_b[i].strip()
            if valor_b in categorias and (i + 1) != cell.row:
                return i + 1

        if i >= len(valores_c) or not valores_c[i].strip():
            return i + 1

    return len(valores_c) + 1


# ---------------- WHATSAPP ----------------

def send_meta_message(to_number, message):
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    clean_number = re.sub(r'\D', '', str(to_number))

    data = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "text",
        "text": {"body": message}
    }

    requests.post(url, headers=headers, json=data, timeout=30)


def notificar_pareja(from_number, datos):
    if not NUMERO_CAMI:
        return

    if from_number == NUMERO_MANU:
        notificar_a = NUMERO_CAMI
        quien = "Manu"
    else:
        notificar_a = NUMERO_MANU
        quien = "Cami"

    monto = datos['monto']
    tipo = datos['tipo']
    pagador = datos['pagador']

    if tipo == "100%":
        deuda = monto if pagador != quien else 0
    elif tipo == "50/50":
        deuda = monto / 2
    else:
        deuda = round(monto * (0.43 if pagador == "Manu" else 0.57), 0)

    mensaje = (
        f"🔔 Nuevo gasto\n\n"
        f"{datos['tienda']}\n"
        f"${monto:,}\n"
        f"{datos['categoria']}\n"
        f"Pagó: {pagador}\n"
        f"División: {tipo}\n\n"
        f"Debes: ${int(deuda):,}"
    )

    send_meta_message(notificar_a, mensaje)


# ---------------- LÓGICA BOT ----------------

def procesar_mensaje(from_number, mensaje):
    if from_number in conversaciones:
        estado = conversaciones[from_number]["estado"]

        if estado == "esperando_categoria":
            manejar_categoria(from_number, mensaje)
        elif estado == "esperando_pagador":
            manejar_pagador(from_number, mensaje)
        elif estado == "esperando_tipo":
            manejar_tipo_division(from_number, mensaje)
    else:
        procesar_nuevo_gasto(from_number, mensaje)


def procesar_nuevo_gasto(from_number, mensaje):
    match = re.match(r'^(.+?),\s*(\d+)$', mensaje.strip())

    if not match:
        send_meta_message(from_number, "Formato: Tienda, monto")
        return

    tienda = match.group(1).strip()
    monto = int(match.group(2))

    conversaciones[from_number] = {
        "tienda": tienda,
        "monto": monto,
        "estado": "esperando_categoria"
    }

    categorias = get_categorias()
    texto = "\n".join([f"{i+1}. {c}" for i, c in enumerate(categorias)])

    conversaciones[from_number]["categorias"] = categorias

    send_meta_message(from_number, f"{tienda} ${monto:,}\n\nCategoría:\n{texto}")


def manejar_categoria(from_number, respuesta):
    datos = conversaciones[from_number]
    categorias = datos["categorias"]

    if respuesta.isdigit():
        categoria = categorias[int(respuesta)-1]
    else:
        categoria = respuesta

    datos["categoria"] = categoria
    datos["estado"] = "esperando_pagador"

    send_meta_message(from_number, "Quién pagó?\n1 Manu\n2 Cami")


def manejar_pagador(from_number, respuesta):
    pagador = "Manu" if respuesta in ["1", "manu"] else "Cami"

    conversaciones[from_number]["pagador"] = pagador
    conversaciones[from_number]["estado"] = "esperando_tipo"

    send_meta_message(from_number, "División:\n1 100%\n2 50/50\n3 %")


# ---------------- 🔥 PARTE CLAVE ----------------

def manejar_tipo_division(from_number, respuesta):
    datos = conversaciones[from_number]

    if respuesta in ["1"]:
        tipo = "100%"
    elif respuesta in ["2"]:
        tipo = "50/50"
    else:
        tipo = "%"

    # Fecha formateada
    try:
        fecha = datetime.now().strftime("%-d/%-m/%Y")
    except:
        fecha = datetime.now().strftime("%d/%m/%Y").lstrip("0").replace("/0", "/")

    sheet = get_sheet()
    fila = encontrar_ultima_fila_categoria(datos['categoria'])

    nueva_fila = [
        "", "", 
        datos['tienda'], 
        datos['monto'], 
        fecha, 
        datos['pagador'], 
        tipo, 
        ""
    ]

    # 🔥 escritura optimizada
    sheet.update(f"A{fila}:H{fila}", [nueva_fila])

    datos["tipo"] = tipo
    notificar_pareja(from_number, datos)

    send_meta_message(from_number, f"✅ Guardado {datos['tienda']} ${datos['monto']:,}")

    del conversaciones[from_number]


# ---------------- WEBHOOK ----------------

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == META_VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Forbidden", 403

    data = request.json

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):
                if message.get("type") == "text":
                    procesar_mensaje(
                        message["from"],
                        message["text"]["body"]
                    )

    return jsonify({"status": "ok"})


@app.route("/")
def home():
    return f"Bot activo - {SHEET_NAME}"


@app.route("/health")
def health():
    return {"status": "ok", "sheet": SHEET_NAME}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)