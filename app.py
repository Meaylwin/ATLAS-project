from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import gspread
import os
import re
import json
from datetime import datetime
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Configuración de Google Sheets
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Diccionario temporal para conversaciones (nota: se pierde si el dyno reinicia)
conversaciones = {}


def get_sheet():
    """Conecta con Google Sheets usando credenciales desde ENV (Heroku Config Vars)."""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    worksheet_name = os.getenv("WORKSHEET_NAME", "Compras")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not spreadsheet_id:
        raise ValueError("Falta SPREADSHEET_ID en variables de entorno.")
    if not creds_json:
        raise ValueError("Falta GOOGLE_SERVICE_ACCOUNT_JSON en variables de entorno.")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(worksheet_name)


@app.route("/whatsapp", methods=["POST"])
@app.route("/webhook", methods=["POST"])  # alias por compatibilidad con tu URL actual
def webhook():
    """Endpoint principal de WhatsApp (Twilio Webhook)."""
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")

    resp = MessagingResponse()
    msg = resp.message()

    try:
        if from_number in conversaciones:
            estado = conversaciones[from_number].get("estado", "nuevo")

            if estado == "esperando_tipo":
                manejar_tipo_division(from_number, incoming_msg, msg)
            elif estado == "esperando_pagador":
                manejar_pagador(from_number, incoming_msg, msg)
            else:
                # Estado desconocido: reseteamos conversación
                del conversaciones[from_number]
                procesar_nueva_compra(from_number, incoming_msg, msg)
        else:
            procesar_nueva_compra(from_number, incoming_msg, msg)

    except Exception as e:
        msg.body(
            f"❌ Error: {str(e)}\n\n"
            "📝 Formato correcto:\n*Tienda, Monto*\nEjemplo: Jumbo, 18990"
        )
        if from_number in conversaciones:
            del conversaciones[from_number]

    return str(resp)


def procesar_nueva_compra(from_number, mensaje, msg):
    """Parsea el mensaje inicial: 'Jumbo, 18990'"""
    pattern = r"([^,\d]+)[,\s]+(\d+)"
    match = re.search(pattern, mensaje)

    if match:
        tienda = match.group(1).strip()
        monto = int(match.group(2).replace(".", "").replace(",", ""))

        conversaciones[from_number] = {
            "tienda": tienda,
            "monto": monto,
            "estado": "esperando_tipo",
        }

        msg.body(
            f"💰 *{tienda}*: ${monto:,}\n\n"
            "¿Cómo dividimos?\n\n"
            "1️⃣ *50/50* (mitad cada uno)\n"
            "2️⃣ *%* (Manu 57% / Cami 43%)\n\n"
            "Responde *1* o *2*"
        )
    else:
        msg.body(
            "❌ No entendí el formato.\n\n"
            "📝 Escribe así:\n*Tienda, Monto*\n\n"
            "Ejemplos:\n• Jumbo, 18990\n• Lider 25000\n• Uber 8500"
        )


def manejar_tipo_division(from_number, respuesta, msg):
    """Maneja la respuesta de tipo de división"""
    datos = conversaciones[from_number]
    tienda = datos["tienda"]
    monto = datos["monto"]

    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "50/50", "50", "mitad"]:
        tipo = "50/50"
        manu_debe = monto / 2
        cami_debe = monto / 2
    elif respuesta_lower in ["2", "%", "porcentaje", "pct"]:
        tipo = "%"
        manu_debe = round(monto * 0.57)
        cami_debe = round(monto * 0.43)
    else:
        msg.body("❌ Respuesta no válida.\n\nResponde:\n*1* para 50/50\n*2* para %")
        return

    conversaciones[from_number].update(
        {
            "tipo": tipo,
            "manu_debe": manu_debe,
            "cami_debe": cami_debe,
            "estado": "esperando_pagador",
        }
    )

    msg.body(
        f"✅ División *{tipo}*\n\n"
        f"Manu debe: ${manu_debe:,.0f}\n"
        f"Cami debe: ${cami_debe:,.0f}\n\n"
        "¿Quién pagó?\n\n"
        "1️⃣ *Manu*\n"
        "2️⃣ *Cami*"
    )


def manejar_pagador(from_number, respuesta, msg):
    """Maneja quién pagó y guarda en Sheets"""
    datos = conversaciones[from_number]
    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "manu", "manuel"]:
        pagador = "Manu"
        debe = "Cami"
        monto_debe = datos["cami_debe"]
    elif respuesta_lower in ["2", "cami", "camila"]:
        pagador = "Cami"
        debe = "Manu"
        monto_debe = datos["manu_debe"]
    else:
        msg.body("❌ Respuesta no válida.\n\nResponde:\n*1* para Manu\n*2* para Cami")
        return

    try:
        sheet = get_sheet()
        fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

        fila = [
            fecha,
            datos["tienda"],
            datos["monto"],
            datos["tipo"],
            pagador,
            f"${datos['manu_debe']:,.0f}",
            f"${datos['cami_debe']:,.0f}",
            debe,
            f"${monto_debe:,.0f}",
        ]

        sheet.append_row(fila)

        msg.body(
            "✅ *¡Registrado!*\n\n"
            f"📝 {datos['tienda']}: ${datos['monto']:,}\n"
            f"💳 Pagó: {pagador}\n"
            f"💰 {debe} debe: ${monto_debe:,.0f}\n"
            f"📊 División: {datos['tipo']}"
        )

    except Exception as e:
        msg.body(f"❌ Error al guardar: {str(e)}\n\nRevisa la configuración de Google Sheets.")

    # Limpiar conversación
    if from_number in conversaciones:
        del conversaciones[from_number]


@app.route("/")
def home():
    return """
    <h1>🤖 ATLAS Bot - Finance Assistant</h1>
    <p>✅ Bot funcionando correctamente</p>
    <p>📱 Envía un mensaje por WhatsApp para comenzar</p>
    """


@app.route("/health")
def health():
    return {"status": "ok", "conversaciones_activas": len(conversaciones)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)