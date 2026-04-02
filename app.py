from flask import Flask, request, jsonify
import gspread
import os
import re
import json
from datetime import datetime
from google.oauth2.service_account import Credentials
import requests
import threading
import re
from zoneinfo import ZoneInfo

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
NUMERO_MANU = os.getenv("NUMERO_MANU")
NUMERO_CAMI = os.getenv("NUMERO_CAMI")

# TimeZone
CHILE_TZ = ZoneInfo("America/Santiago")

# Conversaciones temporales
conversaciones = {}

# Sheet URL
SHEET_URL = "https://docs.google.com/spreadsheets/d/1E0eBDiwr6AmnuX04K-Q_Zw7PIvlSJPnBi4Vn9ggaPlc/edit?gid=559988184"

# Categorias
CATEGORIAS_FIJAS = ["Hogar", "Alimentos", "Compras", "Deporte", "Otros"]

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


def encontrar_ultima_fila_categoria(categoria):
    """Determina la próxima fila disponible para una categoría dada.
    Mantiene el bloque de cada categoría separado por cabeceras y evita
    sobrescribir cabeceras de otras categorías.
    """
    try:
        sheet = get_sheet()

        # 1) Localizar la cabecera de la categoría en la columna B
        cell = sheet.find(categoria, in_column=2)
        if not cell:
            # Si no se encuentra la cabecera, insertamos al final de la hoja
            return len(sheet.col_values(1)) + 1

        cabecera_fila = cell.row  # fila donde está la cabecera de la categoría

        # 2) Encontrar la próxima cabecera de cualquier categoría (si existe)
        col_b = sheet.col_values(2)
        proxima_cabecera_fila = None
        for r in range(cabecera_fila + 1, len(col_b) + 1):
            val_b = col_b[r - 1].strip() if r - 1 < len(col_b) else ""
            if val_b in CATEGORIAS_FIJAS:
                proxima_cabecera_fila = r
                break

        # Si no hay próxima cabecera, trabajamos con el final de la hoja
        if not proxima_cabecera_fila:
            proxima_cabecera_fila = len(sheet.col_values(3)) + 1

        # 3) Buscar la última fila con datos en la columna C (tienda) dentro este bloque
        col_c = sheet.col_values(3)
        ultima_fila_con_datos = cabecera_fila  # al menos la cabecera existe
        for r in range(cabecera_fila + 1, proxima_cabecera_fila):
            valor_c = col_c[r - 1].strip() if r - 1 < len(col_c) else ""
            if valor_c:
                ultima_fila_con_datos = r

        # 4) La próxima fila libre dentro del bloque de esta categoría
        return ultima_fila_con_datos + 1

    except Exception as e:
        print(f"❌ ERROR al determinar fila de categoría: {e}")
        import traceback
        traceback.print_exc()
        # Fallback conservador (en caso de fallo)
        return 31

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


def _norm_num(n):
    return re.sub(r'\D', '', str(n))


def notificar_pareja(from_number, datos):
    """Notifica a la pareja cuando alguien registra un gasto"""
    # Verificar que tengamos los números de Manu y Cami
    if not NUMERO_MANU or not NUMERO_CAMI:
        print("⚠️ Notificación no enviada: NUMERO_MANU o NUMERO_CAMI no definidos.")
        return

    # Normalizar y determinar destinatario
    f = _norm_num(from_number)
    m = _norm_num(NUMERO_MANU)
    c = _norm_num(NUMERO_CAMI)

    if f == m:
        notificar_a = NUMERO_CAMI
        quien_registro = "Manu"
    elif f == c:
        notificar_a = NUMERO_MANU
        quien_registro = "Cami"
    else:
        print("⚠️ Remitente no coincide con Manu ni con Cami.")
        return

    pagador = datos['pagador']
    tipo = datos['tipo']
    monto = datos['monto']

    if tipo == "100%":
        monto_deuda = 0 if pagador == quien_registro else monto
    elif tipo == "50/50":
        monto_deuda = monto / 2
    else:  # %
        if pagador == "Manu":
            monto_deuda = round(monto * 0.43, 2)
        else:
            monto_deuda = round(monto * 0.57, 2)

    mensaje = (
        f"🔔 *Nuevo Gasto Registrado*\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📝 {datos['tienda']}\n"
        f"💵 ${monto:,}\n"
        f"📂 {datos['categoria']}\n"
        f"💳 Pagó: {pagador}\n"
        f"📊 División: {tipo}\n\n"
        f"💰 Tú debes: *${monto_deuda:,.2f}*\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"📄 Se guardará en *{SHEET_NAME}*\n"
        f"🔗 Ver hoja: {SHEET_URL}"
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

            categorias = CATEGORIAS_FIJAS
            categorias_texto = "\n".join([f"{i+1}️⃣ {cat}" for i, cat in enumerate(categorias)])

            message = (
                f"💰 *{tienda}*\n"
                f"💵 ${monto:,}\n\n"
                f"📂 ¿En qué categoría?\n\n"
                f"{categorias_texto}\n\n"
                f"Responde con el *número* o *nombre*"
            )

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

        if respuesta.isdigit():
            idx = int(respuesta) - 1
            if 0 <= idx < len(categorias):
                categoria = categorias[idx]
            else:
                send_meta_message(from_number, "❌ Número inválido. Intenta de nuevo.")
                return
        else:
            respuesta_lower = respuesta.lower().strip()
            categoria = None
            for cat in categorias:
                if cat.lower() == respuesta_lower:
                    categoria = cat
                    break

            if not categoria:
                send_meta_message(from_number, "❌ Categoría no válida. Intenta de nuevo.")
                return

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


def copiar_formato_fila(sheet, fila_origen, fila_destino, col_end=8):
    """Copia el formato de una fila y quita el borde superior de la fila destino (evita línea intermedia)."""
    sheet_id = sheet._properties["sheetId"]

    sheet.spreadsheet.batch_update({
        "requests": [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": sheet_id,
                        "startRowIndex": fila_origen - 1,
                        "endRowIndex": fila_origen,
                        "startColumnIndex": 0,
                        "endColumnIndex": col_end,
                    },
                    "destination": {
                        "sheetId": sheet_id,
                        "startRowIndex": fila_destino - 1,
                        "endRowIndex": fila_destino,
                        "startColumnIndex": 0,
                        "endColumnIndex": col_end,
                    },
                    "pasteType": "PASTE_FORMAT",
                }
            },
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": fila_destino - 1,
                        "endRowIndex": fila_destino,
                        "startColumnIndex": 0,
                        "endColumnIndex": col_end,
                    },
                    "top": {"style": "NONE"},
                }
            },
        ]
    })


def asegurar_fila_vacia_debajo(sheet, fila, force=False):
    """
    Inserta una fila vacía en fila+1:
    - si fila+1 no está vacía, o
    - si force=True (útil para última sección, ej: Otros)
    """
    fila_abajo = fila + 1

    b = sheet.acell(f"B{fila_abajo}").value or ""
    c = sheet.acell(f"C{fila_abajo}").value or ""

    if not force and b.strip() == "" and c.strip() == "":
        return False  # ya hay espacio (pero puede estar sin formato)

    sheet.insert_row([], fila_abajo)

    # Copia formato a la fila insertada (para bordes laterales + barra negra)
    try:
        copiar_formato_fila(sheet, fila_origen=fila, fila_destino=fila_abajo, col_end=8)
    except Exception as e:
        print("⚠️ No se pudo copiar formato:", e)

    return True


def _guardar_transaccion_en_sheets(from_number, datos, tipo, fecha):
    try:
        sheet = get_sheet()
        fila = encontrar_ultima_fila_categoria(datos['categoria'])

        nueva_fila = [
            "",               # A
            "",               # B
            datos['tienda'],  # C
            datos['monto'],   # D
            fecha,            # E
            datos['pagador'], # F
            tipo              # G
        ]

        sheet.update(f"A{fila}:G{fila}", [nueva_fila], value_input_option="USER_ENTERED")
        asegurar_fila_vacia_debajo(sheet, fila, force=(datos["categoria"] == "Otros"))

        # notificar pareja sigue funcionando (en background)
        datos['tipo'] = tipo
        notificar_pareja(from_number, datos)

        # ✅ No enviar nada si salió OK

    except Exception as e:
        print(f"❌ ERROR guardando en Sheets: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error al guardar en Google Sheets: {str(e)}")


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

        ahora = datetime.now(CHILE_TZ)

        try:
            fecha = ahora.strftime("%-d/%-m/%Y")
        except:
            fecha = ahora.strftime("%d/%m/%Y").lstrip("0").replace("/0", "/")

        # ✅ 1) Responder INMEDIATO al usuario (antes de Sheets)
        message = (
            f"✅ *Creada transacción*\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"📝 {datos['tienda']}\n"
            f"💵 ${datos['monto']:,}\n"
            f"📂 {datos['categoria']}\n"
            f"💳 Pagó: {datos['pagador']}\n"
            f"📊 División: {tipo}\n"
            f"📅 Fecha: {fecha}\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"📄 Se guardará en *{SHEET_NAME}*\n"
            f"🔗 Ver hoja: {SHEET_URL}"
        )
        send_meta_message(from_number, message)

        # ✅ 2) Guardar en background (y solo avisar si falla)
        datos_copia = dict(datos)  # evita problemas si borramos la conversación
        t = threading.Thread(
            target=_guardar_transaccion_en_sheets,
            args=(from_number, datos_copia, tipo, fecha),
            daemon=True
        )
        t.start()

        # ✅ 3) Limpiar estado inmediatamente (no esperar a Sheets)
        del conversaciones[from_number]

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error: {str(e)}")


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
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)