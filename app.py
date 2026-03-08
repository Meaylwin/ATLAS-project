from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
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

# Almacenamiento temporal
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
            procesar_nueva_compra(from_number, incoming_msg, msg)

    except Exception as e:
        msg.body(
            f"❌ *Error*\n\n"
            f"{str(e)}\n\n"
            "💡 *Formato correcto:*\n"
            "_Tienda, Monto_\n\n"
            "✨ *Ejemplos:*\n"
            "• Jumbo, 18990\n"
            "• Santa Isabel, 25000\n"
            "• Mundo Lider, 15000"
        )
        if from_number in conversaciones:
            del conversaciones[from_number]

    return str(resp)


def procesar_nueva_compra(from_number, mensaje, msg):
    """Procesa mensaje inicial - ACEPTA ESPACIOS EN NOMBRE"""
    # Regex mejorado: captura TODO antes de la última coma + números
    # Permite: "Santa Isabel, 25000" o "Mundo Lider, 15000"
    pattern = r'^(.+?),\s*(\d+)$'
    match = re.match(pattern, mensaje.strip())

    if match:
        tienda = match.group(1).strip()  # Captura todo antes de la coma
        monto_str = match.group(2).strip()
        monto = int(monto_str.replace(".", "").replace(",", ""))

        conversaciones[from_number] = {
            "tienda": tienda,
            "monto": monto,
            "estado": "esperando_tipo",
        }

        msg.body(
            f"💰 *{tienda}*\n"
            f"💵 ${monto:,}\n\n"
            f"¿Cómo dividimos el gasto?\n\n"
            f"🔀 *1* → 50/50 (mitad cada uno)\n"
            f"📊 *2* → Por % (Manu 57% / Cami 43%)\n\n"
            f"Responde *1* o *2*"
        )
    else:
        msg.body(
            "❌ *No entendí el formato*\n\n"
            "💡 *Formato correcto:*\n"
            "_Tienda, Monto_\n\n"
            "✨ *Ejemplos válidos:*\n"
            "• Jumbo, 18990\n"
            "• Santa Isabel, 25000\n"
            "• Mundo Lider, 15000\n"
            "• Uber, 8500\n\n"
            "⚠️ *Importante:* Separa con *coma* (,)"
        )


def manejar_tipo_division(from_number, respuesta, msg):
    """Maneja selección de tipo de división"""
    datos = conversaciones[from_number]
    tienda = datos["tienda"]
    monto = datos["monto"]

    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "50/50", "50", "mitad"]:
        tipo = "50/50"
        manu_debe = monto / 2
        cami_debe = monto / 2
        emoji = "🔀"
    elif respuesta_lower in ["2", "%", "porcentaje", "pct"]:
        tipo = "%"
        manu_debe = round(monto * 0.57)
        cami_debe = round(monto * 0.43)
        emoji = "📊"
    else:
        msg.body(
            "❌ *Opción no válida*\n\n"
            "Responde:\n"
            "🔀 *1* para 50/50\n"
            "📊 *2* para por %"
        )
        return

    conversaciones[from_number].update({
        "tipo": tipo,
        "manu_debe": manu_debe,
        "cami_debe": cami_debe,
        "estado": "esperando_pagador",
    })

    msg.body(
        f"✅ *División {emoji} {tipo}*\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👨 Manu debe: *${manu_debe:,.0f}*\n"
        f"👩 Cami debe: *${cami_debe:,.0f}*\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"¿Quién pagó?\n\n"
        f"👨 *1* → Manu\n"
        f"👩 *2* → Cami\n\n"
        f"Responde *1* o *2*"
    )


def manejar_pagador(from_number, respuesta, msg):
    """Guarda en Sheets con confirmación mejorada"""
    datos = conversaciones[from_number]
    respuesta_lower = respuesta.lower().strip()

    if respuesta_lower in ["1", "manu", "manuel"]:
        pagador = "Manu"
        debe = "Cami"
        monto_debe = datos["cami_debe"]
        emoji_pago = "👨"
        emoji_debe = "👩"
    elif respuesta_lower in ["2", "cami", "camila"]:
        pagador = "Cami"
        debe = "Manu"
        monto_debe = datos["manu_debe"]
        emoji_pago = "👩"
        emoji_debe = "👨"
    else:
        msg.body(
            "❌ *Opción no válida*\n\n"
            "Responde:\n"
            "👨 *1* para Manu\n"
            "👩 *2* para Cami"
        )
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
            f"✅ *¡Registrado exitosamente!*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏪 *{datos['tienda']}*\n"
            f"💵 Total: *${datos['monto']:,}*\n"
            f"📊 División: *{datos['tipo']}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{emoji_pago} *{pagador}* pagó la cuenta\n"
            f"{emoji_debe} *{debe}* debe: *${monto_debe:,.0f}*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Guardado en Google Sheets ✅"
        )

    except Exception as e:
        msg.body(
            f"❌ *Error al guardar en Sheets*\n\n"
            f"Detalle: {str(e)}\n\n"
            f"🔧 Revisa la configuración de variables"
        )

    if from_number in conversaciones:
        del conversaciones[from_number]


@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ATLAS Bot - Finance Assistant</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                padding: 20px;
            }
            
            .container {
                max-width: 600px;
                width: 100%;
            }
            
            .card {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 24px;
                padding: 48px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
                text-align: center;
                color: #2d3748;
            }
            
            .emoji {
                font-size: 80px;
                margin-bottom: 24px;
                animation: bounce 2s ease-in-out infinite;
            }
            
            @keyframes bounce {
                0%, 100% { transform: translateY(0); }
                50% { transform: translateY(-20px); }
            }
            
            h1 {
                font-size: 48px;
                font-weight: 800;
                margin-bottom: 12px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            
            .subtitle {
                font-size: 20px;
                color: #718096;
                margin-bottom: 32px;
                font-weight: 500;
            }
            
            .status {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
                color: white;
                padding: 12px 28px;
                border-radius: 50px;
                font-weight: 600;
                margin-bottom: 32px;
                box-shadow: 0 4px 12px rgba(72, 187, 120, 0.4);
            }
            
            .status::before {
                content: '';
                width: 8px;
                height: 8px;
                background: white;
                border-radius: 50%;
                animation: pulse 2s ease-in-out infinite;
            }
            
            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }
            
            .info {
                background: #f7fafc;
                border-radius: 16px;
                padding: 24px;
                margin-top: 32px;
            }
            
            .info p {
                margin: 12px 0;
                font-size: 16px;
                color: #4a5568;
            }
            
            .footer {
                margin-top: 32px;
                font-size: 14px;
                color: #a0aec0;
            }
            
            .feature {
                display: inline-block;
                margin: 8px;
                padding: 8px 16px;
                background: #edf2f7;
                border-radius: 8px;
                font-size: 14px;
                color: #4a5568;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <div class="emoji">🤖</div>
                <h1>ATLAS Bot</h1>
                <p class="subtitle">Asistente de Finanzas Compartidas</p>
                <div class="status">Online & Running</div>
                
                <div class="info">
                    <p><strong>📱 Envía un mensaje por WhatsApp</strong></p>
                    <p style="font-size: 14px; color: #718096; margin-top: 16px;">
                        Formato: <code>Tienda, Monto</code>
                    </p>
                    <div style="margin-top: 16px;">
                        <span class="feature">💰 División automática</span>
                        <span class="feature">📊 Tracking en tiempo real</span>
                        <span class="feature">☁️ Hosted en Railway</span>
                    </div>
                </div>
                
                <div class="footer">
                    <p>💜 Powered by Railway · Made with ❤️</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return {
        "status": "ok",
        "conversaciones_activas": len(conversaciones),
        "version": "2.1-improved-regex"
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)