from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse, Message
import gspread
import os
import re
import json
from datetime import datetime
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Configuración
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Almacenamiento temporal de conversaciones
conversaciones = {}


def get_sheet():
    """Conecta con Google Sheets"""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    worksheet_name = os.getenv("WORKSHEET_NAME", "Compras")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not spreadsheet_id or not creds_json:
        raise ValueError("Faltan variables de entorno")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(worksheet_name)


@app.route("/whatsapp", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal de WhatsApp"""
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")
    button_payload = request.values.get("ButtonPayload", "")

    resp = MessagingResponse()
    msg = resp.message()

    try:
        # Si viene de un botón, usar el payload
        if button_payload:
            procesar_boton(from_number, button_payload, msg)
        elif from_number in conversaciones:
            # Conversación en progreso
            estado = conversaciones[from_number].get("estado", "nuevo")
            
            if estado == "esperando_tipo":
                manejar_tipo_division(from_number, incoming_msg, msg)
            elif estado == "esperando_pagador":
                manejar_pagador(from_number, incoming_msg, msg)
        else:
            # Nueva compra
            procesar_nueva_compra(from_number, incoming_msg, msg)

    except Exception as e:
        msg.body(
            f"❌ *Error*\n\n"
            f"{str(e)}\n\n"
            "💡 Formato correcto:\n"
            "*Tienda, Monto*\n\n"
            "Ejemplo: _Jumbo, 18990_"
        )
        if from_number in conversaciones:
            del conversaciones[from_number]

    return str(resp)


def procesar_nueva_compra(from_number, mensaje, msg):
    """Procesa mensaje inicial con botones interactivos"""
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

        # Mensaje con formato mejorado
        msg.body(
            f"💰 *{tienda}*\n"
            f"${monto:,}\n\n"
            f"¿Cómo dividimos el gasto?"
        )
        
        # Agregar botones (Quick Reply)
        msg.action().add_reply(title="🔀 50/50", id="tipo_5050")
        msg.action().add_reply(title="📊 Por %", id="tipo_porcentaje")
        
    else:
        msg.body(
            "❌ *No entendí el formato*\n\n"
            "📝 Escribe así:\n"
            "*Tienda, Monto*\n\n"
            "✨ Ejemplos:\n"
            "• Jumbo, 18990\n"
            "• Lider 25000\n"
            "• Uber 8500"
        )


def procesar_boton(from_number, payload, msg):
    """Procesa clicks en botones"""
    if from_number not in conversaciones:
        msg.body("❌ Sesión expirada. Envía un nuevo gasto.")
        return

    datos = conversaciones[from_number]
    estado = datos.get("estado")

    if estado == "esperando_tipo":
        if payload == "tipo_5050":
            manejar_tipo_division(from_number, "1", msg)
        elif payload == "tipo_porcentaje":
            manejar_tipo_division(from_number, "2", msg)
            
    elif estado == "esperando_pagador":
        if payload == "pago_manu":
            manejar_pagador(from_number, "1", msg)
        elif payload == "pago_cami":
            manejar_pagador(from_number, "2", msg)


def manejar_tipo_division(from_number, respuesta, msg):
    """Maneja selección de tipo de división con botones"""
    datos = conversaciones[from_number]
    tienda = datos["tienda"]
    monto = datos["monto"]

    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "50/50", "50", "mitad", "tipo_5050"]:
        tipo = "50/50"
        manu_debe = monto / 2
        cami_debe = monto / 2
        emoji = "🔀"
    elif respuesta_lower in ["2", "%", "porcentaje", "pct", "tipo_porcentaje"]:
        tipo = "%"
        manu_debe = round(monto * 0.57)
        cami_debe = round(monto * 0.43)
        emoji = "📊"
    else:
        msg.body("❌ Opción no válida")
        msg.action().add_reply(title="🔀 50/50", id="tipo_5050")
        msg.action().add_reply(title="📊 Por %", id="tipo_porcentaje")
        return

    conversaciones[from_number].update({
        "tipo": tipo,
        "manu_debe": manu_debe,
        "cami_debe": cami_debe,
        "estado": "esperando_pagador",
    })

    msg.body(
        f"✅ *División {emoji} {tipo}*\n\n"
        f"👤 Manu debe: *${manu_debe:,.0f}*\n"
        f"👤 Cami debe: *${cami_debe:,.0f}*\n\n"
        f"¿Quién pagó?"
    )
    
    # Botones para seleccionar pagador
    msg.action().add_reply(title="🦊 Manu", id="pago_manu")
    msg.action().add_reply(title="👸🏼 Cami", id="pago_cami")


def manejar_pagador(from_number, respuesta, msg):
    """Guarda en Sheets con confirmación mejorada"""
    datos = conversaciones[from_number]
    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "manu", "manuel", "pago_manu"]:
        pagador = "Manu"
        debe = "Cami"
        monto_debe = datos["cami_debe"]
        emoji_pago = "🦊"
        emoji_debe = "👸🏼"
    elif respuesta_lower in ["2", "cami", "camila", "pago_cami"]:
        pagador = "Cami"
        debe = "Manu"
        monto_debe = datos["manu_debe"]
        emoji_pago = "👸🏼"
        emoji_debe = "🦊"
    else:
        msg.body("❌ Opción no válida")
        msg.action().add_reply(title="👨 Manu", id="pago_manu")
        msg.action().add_reply(title="👩 Cami", id="pago_cami")
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

        # Mensaje de confirmación mejorado
        msg.body(
            f"✅ *¡Registrado exitosamente!*\n\n"
            f"🏪 *{datos['tienda']}*\n"
            f"💵 Total: ${datos['monto']:,}\n"
            f"📊 División: {datos['tipo']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{emoji_pago} *{pagador}* pagó\n"
            f"{emoji_debe} *{debe}* debe: *${monto_debe:,.0f}*\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"📝 Guardado en Google Sheets"
        )

    except Exception as e:
        msg.body(
            f"❌ *Error al guardar*\n\n"
            f"{str(e)}\n\n"
            f"🔧 Revisa la configuración"
        )

    # Limpiar conversación
    if from_number in conversaciones:
        del conversaciones[from_number]


@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>ATLAS Bot</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 600px;
                margin: 50px auto;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                text-align: center;
            }
            .card {
                background: rgba(255,255,255,0.1);
                backdrop-filter: blur(10px);
                border-radius: 20px;
                padding: 30px;
                box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
            }
            h1 { font-size: 2.5em; margin: 0; }
            .emoji { font-size: 4em; margin: 20px 0; }
            .status { 
                background: rgba(76, 175, 80, 0.3);
                padding: 10px 20px;
                border-radius: 50px;
                display: inline-block;
                margin: 20px 0;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="emoji">🤖</div>
            <h1>ATLAS Bot</h1>
            <p style="font-size: 1.2em;">Finance Assistant</p>
            <div class="status">✅ Online & Running</div>
            <p>📱 Envía un mensaje por WhatsApp para comenzar</p>
            <p style="opacity: 0.7; font-size: 0.9em;">Powered by Railway</p>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return {
        "status": "ok",
        "conversaciones_activas": len(conversaciones),
        "version": "2.0-interactive"
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)