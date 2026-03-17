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

# Conversaciones temporales
conversaciones = {}


def get_sheet():
    """Conecta con Google Sheets"""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    worksheet_name = os.getenv("WORKSHEET_NAME", "Compras")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not spreadsheet_id or not creds_json:
        raise ValueError("Faltan variables de entorno de Google Sheets")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(worksheet_name)


def send_meta_message(to_number, message):
    """Envía mensaje con Meta Cloud API"""
    try:
        print(f"🔵 send_meta_message called")
        print(f"🔵 to_number: {to_number}")
        print(f"🔵 message preview: {message[:100]}...")
        print(f"🔵 META_PHONE_NUMBER_ID: {META_PHONE_NUMBER_ID}")
        print(f"🔵 META_ACCESS_TOKEN exists: {bool(META_ACCESS_TOKEN)}")
        
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
        
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Limpiar número (solo dígitos)
        clean_number = re.sub(r'\D', '', str(to_number))
        print(f"🔵 clean_number: {clean_number}")
        
        data = {
            "messaging_product": "whatsapp",
            "to": clean_number,
            "type": "text",
            "text": {
                "body": message
            }
        }
        
        print(f"🔵 Sending POST to: {url}")
        response = requests.post(url, headers=headers, json=data, timeout=30)
        print(f"🔵 Response status: {response.status_code}")
        print(f"🔵 Response body: {response.text}")
        
        return response.json()
        
    except Exception as e:
        print(f"❌ ERROR in send_meta_message: {e}")
        import traceback
        traceback.print_exc()
        return None


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Endpoint para Meta Cloud API"""
    
    # Verificación de webhook (GET request de Meta)
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        print(f"Webhook verification: mode={mode}, token={token}")
        
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            print("✅ Webhook verified successfully!")
            return challenge, 200
        else:
            print("❌ Webhook verification failed!")
            return "Forbidden", 403
    
    # Webhook de mensajes (POST)
    if request.method == "POST":
        data = request.json
        print(f"📨 Received webhook: {json.dumps(data, indent=2)}")
        
        try:
            if data.get("object") == "whatsapp_business_account":
                for entry in data.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})
                        
                        # Ignorar status updates
                        if "messages" not in value:
                            print("⏭️ Skipping - no messages in webhook")
                            continue
                        
                        for message in value.get("messages", []):
                            # Ignorar mensajes no de texto
                            if message.get("type") != "text":
                                print(f"⏭️ Skipping - message type: {message.get('type')}")
                                continue
                            
                            from_number = message.get("from")
                            message_text = message.get("text", {}).get("body", "")
                            
                            print(f"📍 Processing message from {from_number}: {message_text}")
                            procesar_mensaje_meta(from_number, message_text)
            
            return jsonify({"status": "ok"}), 200
        
        except Exception as e:
            print(f"❌ ERROR processing webhook: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500


def procesar_mensaje_meta(from_number, mensaje):
    """Procesa mensajes de Meta"""
    try:
        print(f"📍 Entered procesar_mensaje_meta for {from_number}")
        
        if from_number in conversaciones:
            print(f"📍 Found existing conversation")
            estado = conversaciones[from_number].get("estado")
            
            if estado == "esperando_tipo":
                print(f"📍 Calling manejar_tipo_division")
                manejar_tipo_division(from_number, mensaje)
            elif estado == "esperando_pagador":
                print(f"📍 Calling manejar_pagador")
                manejar_pagador(from_number, mensaje)
        else:
            print(f"📍 Starting new purchase flow")
            procesar_nueva_compra(from_number, mensaje)
            
        print(f"✅ Completed processing for {from_number}")
        
    except Exception as e:
        print(f"❌ ERROR in procesar_mensaje_meta: {e}")
        import traceback
        traceback.print_exc()


def procesar_nueva_compra(from_number, mensaje):
    """Procesa nueva compra"""
    try:
        print(f"📍 Parsing message: {mensaje}")
        pattern = r'^(.+?),\s*(\d+)$'
        match = re.match(pattern, mensaje.strip())
        
        if match:
            tienda = match.group(1).strip()
            monto = int(match.group(2).replace(".", "").replace(",", ""))
            
            print(f"📍 Parsed: tienda={tienda}, monto={monto}")
            
            conversaciones[from_number] = {
                "tienda": tienda,
                "monto": monto,
                "estado": "esperando_tipo",
            }
            
            print(f"📍 Stored conversation for {from_number}")
            
            message = (
                f"💰 *{tienda}*\n"
                f"💵 ${monto:,}\n\n"
                f"¿Cómo dividimos el gasto?\n\n"
                f"🔀 *1* → 50/50 (mitad cada uno)\n"
                f"📊 *2* → Por % (Manu 57% / Cami 43%)\n\n"
                f"Responde *1* o *2*"
            )
            
            print(f"📍 About to send message to {from_number}")
            result = send_meta_message(from_number, message)
            print(f"📍 Send result: {result}")
            
        else:
            print(f"📍 Message didn't match pattern")
            send_meta_message(
                from_number,
                "❌ Formato incorrecto\n\n"
                "💡 Escribe: Tienda, Monto\n\n"
                "Ejemplos:\n• Jumbo, 18990\n• Santa Isabel, 25000"
            )
            
    except Exception as e:
        print(f"❌ ERROR in procesar_nueva_compra: {e}")
        import traceback
        traceback.print_exc()


def manejar_tipo_division(from_number, respuesta):
    """Maneja tipo de división"""
    try:
        print(f"📍 manejar_tipo_division: {respuesta}")
        datos = conversaciones[from_number]
        monto = datos["monto"]
        
        if respuesta.lower() in ["1", "50/50", "50", "mitad"]:
            tipo = "50/50"
            manu_debe = monto / 2
            cami_debe = monto / 2
        elif respuesta.lower() in ["2", "%", "porcentaje", "pct"]:
            tipo = "%"
            manu_debe = round(monto * 0.57, 2)
            cami_debe = round(monto * 0.43, 2)
        else:
            send_meta_message(from_number, "❌ Opción no válida. Responde 1 o 2")
            return
        
        conversaciones[from_number].update({
            "tipo": tipo,
            "manu_debe": manu_debe,
            "cami_debe": cami_debe,
            "estado": "esperando_pagador",
        })
        
        message = (
            f"✅ División {tipo}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👨 Manu: ${manu_debe:,.2f}\n"
            f"👩 Cami: ${cami_debe:,.2f}\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"¿Quién pagó?\n\n"
            f"👨 *1* → Manu\n"
            f"👩 *2* → Cami\n\n"
            f"Responde *1* o *2*"
        )
        
        send_meta_message(from_number, message)
        
    except Exception as e:
        print(f"❌ ERROR in manejar_tipo_division: {e}")
        import traceback
        traceback.print_exc()


def manejar_pagador(from_number, respuesta):
    """Guarda en Sheets"""
    try:
        print(f"📍 manejar_pagador: {respuesta}")
        datos = conversaciones[from_number]
        
        if respuesta.lower() in ["1", "manu", "manuel"]:
            pagador = "Manu"
            debe = "Cami"
            monto_debe = datos["cami_debe"]
        elif respuesta.lower() in ["2", "cami", "camila"]:
            pagador = "Cami"
            debe = "Manu"
            monto_debe = datos["manu_debe"]
        else:
            send_meta_message(from_number, "❌ Opción no válida")
            return
        
        print(f"📍 Saving to Google Sheets...")
        sheet = get_sheet()
        fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        fila = [
            fecha,
            datos["tienda"],
            datos["monto"],
            datos["tipo"],
            pagador,
            f"${datos['manu_debe']:,.2f}",
            f"${datos['cami_debe']:,.2f}",
            debe,
            f"${monto_debe:,.2f}",
        ]
        
        sheet.append_row(fila)
        print(f"✅ Saved to Google Sheets")
        
        message = (
            f"✅ *¡Registrado!*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏪 *{datos['tienda']}*\n"
            f"💵 Total: *${datos['monto']:,}*\n"
            f"📊 División: *{datos['tipo']}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💳 *{pagador}* pagó\n"
            f"💰 *{debe}* debe: *${monto_debe:,.2f}*\n\n"
            f"📝 Guardado en Sheets ✅"
        )
        
        send_meta_message(from_number, message)
        del conversaciones[from_number]
        
    except Exception as e:
        print(f"❌ ERROR in manejar_pagador: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error al guardar: {str(e)}")


@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>ATLAS Bot - Meta Cloud API</title>
        <style>
            body {
                font-family: -apple-system, sans-serif;
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
            }
            h1 { font-size: 2.5em; margin: 0; }
            .emoji { font-size: 4em; margin: 20px 0; }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="emoji">🤖</div>
            <h1>ATLAS Bot</h1>
            <p>Finance Assistant con Meta Cloud API</p>
            <div style="background: rgba(76, 175, 80, 0.3); padding: 10px 20px; border-radius: 50px; display: inline-block; margin: 20px 0;">
                ✅ Online & Running
            </div>
            <p>📱 Envía un mensaje por WhatsApp</p>
        </div>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "provider": "meta-cloud-api",
        "conversaciones": len(conversaciones)
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)