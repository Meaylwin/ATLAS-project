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

# Números de usuarios
NUMERO_MANU = os.getenv("NUMERO_MANU", "56995438310")
NUMERO_CAMI = os.getenv("NUMERO_CAMI", "")

# Conversaciones temporales
conversaciones = {}

# Nombre de hoja: siempre usar el mes actual
MESES_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", 
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
SHEET_NAME = f"F. {MESES_ES[datetime.now().month - 1]}"


def get_sheet():
    """Conecta con Google Sheets"""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not spreadsheet_id or not creds_json:
        raise ValueError("Faltan variables de entorno de Google Sheets")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(SHEET_NAME)


def get_categorias():
    """Obtiene las categorías desde B7 en adelante"""
    try:
        sheet = get_sheet()
        # Leer columna B desde fila 7 hasta encontrar vacío
        categorias_raw = sheet.col_values(2)[6:]  # B7 en adelante (índice 6)
        
        # Filtrar solo categorías únicas y no vacías
        categorias = []
        for cat in categorias_raw:
            cat = cat.strip()
            if cat and cat not in categorias:
                # Buscar títulos (Hogar, Compras, Otros)
                if cat in ['Hogar', 'Compras', 'Otros']:
                    categorias.append(cat)
        
        return categorias if categorias else ['Hogar', 'Compras', 'Otros']
    except Exception as e:
        print(f"Error obteniendo categorías: {e}")
        return ['Hogar', 'Compras', 'Otros']


def encontrar_fila_categoria(categoria):
    """Encuentra la fila donde insertar un nuevo gasto en la categoría"""
    try:
        sheet = get_sheet()
        # Buscar la fila de la categoría
        cell = sheet.find(categoria, in_column=2)  # Buscar en columna B
        
        if cell:
            fila_categoria = cell.row
            # Buscar la siguiente fila vacía en esa sección
            valores_columna_b = sheet.col_values(2)
            
            # Desde la fila de la categoría, buscar hasta encontrar vacío o siguiente categoría
            for i in range(fila_categoria, len(valores_columna_b)):
                if not valores_columna_b[i].strip():
                    return i + 1  # +1 porque las listas empiezan en 0
                # Si encontramos otra categoría, insertar antes
                if valores_columna_b[i].strip() in ['Hogar', 'Compras', 'Otros'] and i != fila_categoria:
                    return i
            
            # Si llegamos aquí, agregar al final
            return len(valores_columna_b) + 1
        else:
            # Si no encuentra la categoría, agregar al final
            return len(sheet.col_values(2)) + 1
            
    except Exception as e:
        print(f"Error encontrando fila: {e}")
        return 30  # Fila por defecto


def send_meta_message(to_number, message):
    """Envía mensaje con Meta Cloud API"""
    try:
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
            "text": {
                "body": message
            }
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        return response.json()
        
    except Exception as e:
        print(f"❌ ERROR sending message: {e}")
        import traceback
        traceback.print_exc()
        return None


def notificar_pareja(from_number, datos):
    """Notifica a la pareja cuando alguien registra un gasto"""
    if not NUMERO_CAMI:
        return  # No notificar si no está configurado
    
    # Determinar quién registró
    if from_number == NUMERO_MANU:
        notificar_a = NUMERO_CAMI
        quien_registro = "Manu"
    else:
        notificar_a = NUMERO_MANU
        quien_registro = "Cami"
    
    # Calcular cuánto debe el otro
    pagador = datos['pagador']
    tipo = datos['tipo']
    monto = datos['monto']
    
    if tipo == "100%":
        if pagador == quien_registro:
            monto_deuda = 0
        else:
            monto_deuda = monto
    elif tipo == "50/50":
        monto_deuda = monto / 2
    else:  # %
        if pagador == "Manu":
            monto_deuda = round(monto * 0.43, 2)
        else:
            monto_deuda = round(monto * 0.57, 2)
    
    mensaje = (
        f"🔔 *Nuevo Gasto Registrado*\n\n"
        f"📝 {datos['tienda']}\n"
        f"💵 ${monto:,}\n"
        f"📂 {datos['categoria']}\n"
        f"💳 Pagó: {pagador}\n"
        f"📊 División: {tipo}\n\n"
        f"💰 Tú debes: *${monto_deuda:,.2f}*"
    )
    
    send_meta_message(notificar_a, mensaje)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Endpoint para Meta Cloud API"""
    
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return challenge, 200
        else:
            return "Forbidden", 403
    
    if request.method == "POST":
        data = request.json
        
        try:
            if data.get("object") == "whatsapp_business_account":
                for entry in data.get("entry", []):
                    for change in entry.get("changes", []):
                        value = change.get("value", {})
                        
                        if "messages" not in value:
                            continue
                        
                        for message in value.get("messages", []):
                            if message.get("type") != "text":
                                continue
                            
                            from_number = message.get("from")
                            message_text = message.get("text", {}).get("body", "")
                            
                            procesar_mensaje(from_number, message_text)
            
            return jsonify({"status": "ok"}), 200
        
        except Exception as e:
            print(f"❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({"status": "error"}), 500


def procesar_mensaje(from_number, mensaje):
    """Procesa mensajes"""
    try:
        if from_number in conversaciones:
            estado = conversaciones[from_number].get("estado")
            
            if estado == "esperando_categoria":
                manejar_categoria(from_number, mensaje)
            elif estado == "esperando_pagador":
                manejar_pagador(from_number, mensaje)
            elif estado == "esperando_tipo":
                manejar_tipo_division(from_number, mensaje)
        else:
            procesar_nuevo_gasto(from_number, mensaje)
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def procesar_nuevo_gasto(from_number, mensaje):
    """Procesa nuevo gasto"""
    try:
        pattern = r'^(.+?),\s*(\d+)$'
        match = re.match(pattern, mensaje.strip())
        
        if match:
            tienda = match.group(1).strip()
            monto = int(match.group(2).replace(".", "").replace(",", ""))
            
            conversaciones[from_number] = {
                "tienda": tienda,
                "monto": monto,
                "estado": "esperando_categoria",
            }
            
            # Obtener categorías
            categorias = get_categorias()
            categorias_texto = "\n".join([f"{i+1}️⃣ {cat}" for i, cat in enumerate(categorias)])
            
            message = (
                f"💰 *{tienda}*\n"
                f"💵 ${monto:,}\n\n"
                f"📂 ¿En qué categoría?\n\n"
                f"{categorias_texto}\n\n"
                f"Responde con el *número* o *nombre*"
            )
            
            # Guardar categorías para referencia
            conversaciones[from_number]['categorias'] = categorias
            
            send_meta_message(from_number, message)
            
        else:
            send_meta_message(
                from_number,
                "❌ Formato incorrecto\n\n"
                "💡 Escribe: *Tienda, Monto*\n\n"
                "Ejemplos:\n• Jumbo, 18990\n• Santa Isabel, 25000"
            )
            
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def manejar_categoria(from_number, respuesta):
    """Maneja selección de categoría"""
    try:
        datos = conversaciones[from_number]
        categorias = datos['categorias']
        
        # Intentar por número
        if respuesta.isdigit():
            idx = int(respuesta) - 1
            if 0 <= idx < len(categorias):
                categoria = categorias[idx]
            else:
                send_meta_message(from_number, "❌ Número inválido. Intenta de nuevo.")
                return
        else:
            # Intentar por nombre
            respuesta_lower = respuesta.lower().strip()
            categoria = None
            for cat in categorias:
                if cat.lower() == respuesta_lower:
                    categoria = cat
                    break
            
            if not categoria:
                send_meta_message(from_number, "❌ Categoría no válida. Intenta de nuevo.")
                return
        
        # Guardar categoría
        conversaciones[from_number]['categoria'] = categoria
        conversaciones[from_number]['estado'] = 'esperando_pagador'
        
        message = (
            f"✅ Categoría: *{categoria}*\n\n"
            f"💳 ¿Quién pagó?\n\n"
            f"1️⃣ Manu\n"
            f"2️⃣ Cami\n\n"
            f"Responde *1* o *2*"
        )
        
        send_meta_message(from_number, message)
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def manejar_pagador(from_number, respuesta):
    """Maneja quién pagó"""
    try:
        datos = conversaciones[from_number]
        
        if respuesta.lower() in ["1", "manu", "manuel"]:
            pagador = "Manu"
        elif respuesta.lower() in ["2", "cami", "camila"]:
            pagador = "Cami"
        else:
            send_meta_message(from_number, "❌ Opción no válida. Responde 1 o 2")
            return
        
        conversaciones[from_number]['pagador'] = pagador
        conversaciones[from_number]['estado'] = 'esperando_tipo'
        
        message = (
            f"✅ Pagó: *{pagador}*\n\n"
            f"📊 ¿Cómo se divide?\n\n"
            f"1️⃣ 100% (debe pagar el otro)\n"
            f"2️⃣ 50/50\n"
            f"3️⃣ % (Manu 57% / Cami 43%)\n\n"
            f"Responde *1*, *2* o *3*"
        )
        
        send_meta_message(from_number, message)
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def manejar_tipo_division(from_number, respuesta):
    """Maneja tipo de división y guarda en Sheets"""
    try:
        datos = conversaciones[from_number]
        
        if respuesta.lower() in ["1", "100", "100%"]:
            tipo = "100%"
        elif respuesta.lower() in ["2", "50/50", "50", "mitad"]:
            tipo = "50/50"
        elif respuesta.lower() in ["3", "%", "porcentaje", "pct"]:
            tipo = "%"
        else:
            send_meta_message(from_number, "❌ Opción no válida. Responde 1, 2 o 3")
            return
        
        # Guardar en Google Sheets
        sheet = get_sheet()
        
        # Encontrar fila correcta en la categoría
        fila = encontrar_fila_categoria(datos['categoria'])
        
        # Preparar datos
        # Columnas: A=(vacío), B=(vacío), C=Tienda, D=Monto, E=(vacío), F=Quién pagó, G=Tipo, H=(calculado)
        nueva_fila = [
            "",  # A (vacío)
            "",  # B (vacío)
            datos['tienda'],  # C - NOMBRE DE TRANSACCIÓN
            datos['monto'],  # D
            "",  # E (vacío)
            datos['pagador'],  # F
            tipo,  # G
            ""  # H (lo calcula la fórmula)
        ]
        
        # Insertar fila
        sheet.insert_row(nueva_fila, fila)
        
        # Notificar a la pareja
        datos['tipo'] = tipo
        notificar_pareja(from_number, datos)
        
        # Mensaje de confirmación
        message = (
            f"✅ *¡Registrado!*\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📝 {datos['tienda']}\n"
            f"💵 ${datos['monto']:,}\n"
            f"📂 {datos['categoria']}\n"
            f"💳 Pagó: {datos['pagador']}\n"
            f"📊 División: {tipo}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"📄 Guardado en *{SHEET_NAME}* ✅"
        )
        
        send_meta_message(from_number, message)
        
        # Limpiar conversación
        del conversaciones[from_number]
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error al guardar: {str(e)}")


@app.route("/")
def home():
    return """
    <h1>🤖 ATLAS Bot - Finanzas C&M</h1>
    <p>✅ Bot activo con Meta Cloud API</p>
    <p>📊 Hoja actual: """ + SHEET_NAME + """</p>
    """


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "sheet": SHEET_NAME,
        "conversaciones": len(conversaciones)
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)