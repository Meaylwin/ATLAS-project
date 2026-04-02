from flask import Flask, request, jsonify
import gspread
import os
import re
import json
from google.oauth2.service_account import Credentials
import requests
import threading
from zoneinfo import ZoneInfo
from gspread.exceptions import WorksheetNotFound
from datetime import datetime

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

# Porcentajes para división %
try:
    PCT_MANU = float(os.getenv("PCT_MANU", "0.57"))
    PCT_CAMI = float(os.getenv("PCT_CAMI", "0.43"))
except Exception:
    PCT_MANU = 0.57
    PCT_CAMI = 0.43

# TimeZone
CHILE_TZ = ZoneInfo("America/Santiago")

# Conversaciones temporales
conversaciones = {}

# Sheet URL
SHEET_URL = "https://docs.google.com/spreadsheets/d/1E0eBDiwr6AmnuX04K-Q_Zw7PIvlSJPnBi4Vn9ggaPlc/edit?gid=559988184"

# Categorías
CATEGORIAS_FIJAS = ["Hogar", "Alimentos", "Compras", "Deporte", "Otros"]

# Nombre de hoja: siempre usar el mes actual
MESES_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
]
SHEET_NAME = f"F. {MESES_ES[datetime.now().month - 1]}"


def _norm_num(n):
    return re.sub(r"\D", "", str(n or ""))


def nombre_por_numero(numero):
    n = _norm_num(numero)
    if n == _norm_num(NUMERO_MANU):
        return "Manu"
    if n == _norm_num(NUMERO_CAMI):
        return "Cami"
    return None


def to_int(n, default=0):
    """Convierte varias representaciones numéricas a int, sin decimales."""
    try:
        if isinstance(n, (int, float)):
            return int(round(n))

        s = str(n).strip()
        s = s.replace(" ", "")
        s = s.replace(".", "")
        s = s.replace(",", "")
        return int(float(s))
    except Exception:
        return default


def format_number_dot(n):
    """Formatea con separador de miles '.' y sin decimales."""
    try:
        n_int = to_int(n, default=None)
        if n_int is None:
            return str(n)
        return f"{n_int:,}".replace(",", ".")
    except Exception:
        return str(n)


def calcular_monto_deuda(monto_num, pagador, tipo):
    """Calcula cuánto debe la otra persona al pagador."""
    monto_num = to_int(monto_num, default=0)

    if tipo == "100%":
        return monto_num

    if tipo == "50/50":
        return int(round(monto_num / 2.0))

    if tipo == "%":
        if pagador == "Manu":
            return int(round(monto_num * PCT_CAMI))
        if pagador == "Cami":
            return int(round(monto_num * PCT_MANU))
        return 0

    return 0


def preparar_datos_transaccion(datos, tipo):
    """Calcula una sola vez los datos necesarios para templates/notificaciones."""
    datos_preparados = dict(datos)
    datos_preparados["monto"] = to_int(datos_preparados.get("monto", 0), default=0)
    datos_preparados["tipo"] = tipo
    datos_preparados["monto_deuda"] = calcular_monto_deuda(
        monto_num=datos_preparados["monto"],
        pagador=datos_preparados.get("pagador"),
        tipo=tipo,
    )
    return datos_preparados


def texto_deuda_para_destinatario(to_number, datos):
    """Devuelve 'Te deben' o 'Tú debes' según quién recibe el mensaje."""
    destinatario = nombre_por_numero(to_number)
    pagador = datos.get("pagador")
    monto_deuda = to_int(datos.get("monto_deuda", 0), default=0)

    if destinatario == pagador:
        return f"Te deben ${format_number_dot(monto_deuda)}"

    if destinatario in ("Manu", "Cami"):
        return f"Tú debes ${format_number_dot(monto_deuda)}"

    return f"Saldo ${format_number_dot(monto_deuda)}"


def get_sheet():
    """Conecta con Google Sheets y asegura que exista la hoja del mes actual.
    Si no existe, la crea duplicando la hoja de plantilla 'F. Template'."""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not spreadsheet_id or not creds_json:
        raise ValueError("Faltan variables de entorno de Google Sheets")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPE)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        sheet = spreadsheet.worksheet(SHEET_NAME)
        return sheet
    except WorksheetNotFound:
        try:
            template_sheet = spreadsheet.worksheet("F. Template")
            source_sheet_id = template_sheet.id

            body = {
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": source_sheet_id,
                            "newSheetName": SHEET_NAME
                        }
                    }
                ]
            }

            spreadsheet.batch_update(body)
            sheet = spreadsheet.worksheet(SHEET_NAME)
            print(f"✅ Hoja '{SHEET_NAME}' creada a partir de 'F. Template'.")
            return sheet
        except Exception as e:
            print(f"❌ Error creando hoja desde template: {e}")
            import traceback
            traceback.print_exc()
            raise
    except Exception as e:
        print(f"❌ Error al obtener la hoja: {e}")
        import traceback
        traceback.print_exc()
        raise


def encontrar_ultima_fila_categoria(categoria):
    """Determina la próxima fila disponible para una categoría dada."""
    try:
        sheet = get_sheet()

        cell = sheet.find(categoria, in_column=2)
        if not cell:
            return len(sheet.col_values(1)) + 1

        cabecera_fila = cell.row

        col_b = sheet.col_values(2)
        proxima_cabecera_fila = None
        for r in range(cabecera_fila + 1, len(col_b) + 1):
            val_b = col_b[r - 1].strip() if r - 1 < len(col_b) else ""
            if val_b in CATEGORIAS_FIJAS:
                proxima_cabecera_fila = r
                break

        if not proxima_cabecera_fila:
            proxima_cabecera_fila = len(sheet.col_values(3)) + 1

        col_c = sheet.col_values(3)
        ultima_fila_con_datos = cabecera_fila
        for r in range(cabecera_fila + 1, proxima_cabecera_fila):
            valor_c = col_c[r - 1].strip() if r - 1 < len(col_c) else ""
            if valor_c:
                ultima_fila_con_datos = r

        return ultima_fila_con_datos + 1

    except Exception as e:
        print(f"❌ ERROR al determinar fila de categoría: {e}")
        import traceback
        traceback.print_exc()
        return 31


def send_meta_message(to_number, message):
    """Envía mensaje de texto con Meta Cloud API."""
    try:
        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"

        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        clean_number = _norm_num(to_number)

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


def enviar_template_pareja(to_number, datos, template_name="expense_notification_v1"):
    """Envía plantilla de WhatsApp con datos del gasto al destinatario indicado."""
    url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    texto_deuda = texto_deuda_para_destinatario(to_number, datos)

    components = [
        {"type": "body", "parameters": [
            {"type": "text", "text": datos.get("tienda", "")},
            {"type": "text", "text": format_number_dot(datos.get("monto", 0))},
            {"type": "text", "text": datos.get("categoria", "")},
            {"type": "text", "text": datos.get("pagador", "")},
            {"type": "text", "text": datos.get("tipo", "")},
            {"type": "text", "text": texto_deuda},
            {"type": "text", "text": SHEET_NAME}
        ]}
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": _norm_num(to_number),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es_CL"},
            "components": components
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        data = resp.json()
        print("DEBUG: Plantilla enviada. Respuesta API:", data)
        return data
    except Exception as e:
        print(f"❌ ERROR enviando plantilla: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def notificar_pareja(from_number, datos):
    """Notifica a la pareja cuando alguien registra un gasto."""
    if not NUMERO_MANU or not NUMERO_CAMI:
        print("⚠️ Notificación no enviada: NUMERO_MANU o NUMERO_CAMI no definidos.")
        return

    f = _norm_num(from_number)
    m = _norm_num(NUMERO_MANU)
    c = _norm_num(NUMERO_CAMI)

    if f == m:
        notificar_a = NUMERO_CAMI
    elif f == c:
        notificar_a = NUMERO_MANU
    else:
        print("⚠️ Remitente no coincide con Manu ni con Cami.")
        return

    # Fallback de seguridad: si por alguna razón no viene preparado
    if "tipo" not in datos or "monto_deuda" not in datos:
        datos = preparar_datos_transaccion(datos, datos.get("tipo", ""))

    enviar_template_pareja(notificar_a, datos, template_name="expense_notification_v1")

    # OJO: no enviamos confirmación al emisor para que el último mensaje siga siendo el template.


def webhook():
    """Endpoint para Meta Cloud API"""
    pass


@app.route("/webhook", methods=["POST"])
def webhook_route():
    data = request.json

    if data.get("object") == "whatsapp_business_account":
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                for status in value.get("statuses", []):
                    status_id = status.get("id")
                    status_text = status.get("status")
                    recipient = status.get("recipient_id")
                    timestamp = status.get("timestamp")
                    print(f"STATUS: id={status_id} to={recipient} status={status_text} ts={timestamp}")

                if "messages" in value:
                    for message in value.get("messages", []):
                        if message.get("type") != "text":
                            continue
                        from_number = message.get("from")
                        message_text = message.get("text", {}).get("body", "")
                        procesar_mensaje(from_number, message_text)

    return jsonify({"status": "ok"}), 200


def procesar_mensaje(from_number, mensaje):
    """Procesa mensajes."""
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
    """Procesa nuevo gasto."""
    try:
        pattern = r'^(.+?),\s*([\d\.,]+)$'
        match = re.match(pattern, mensaje.strip())

        if match:
            tienda = match.group(1).strip()
            monto_str = match.group(2).strip()
            monto = to_int(monto_str, default=-1)

            if monto < 0:
                send_meta_message(
                    from_number,
                    "❌ Monto inválido\n\n"
                    "💡 Escribe: *Tienda, Monto*\n\n"
                    "Ejemplos:\n• Jumbo, 18990\n• Jumbo, 18.990"
                )
                return

            conversaciones[from_number] = {
                "tienda": tienda,
                "monto": monto,
                "estado": "esperando_categoria",
            }

            categorias = CATEGORIAS_FIJAS
            categorias_texto = "\n".join([f"{i+1}️⃣ {cat}" for i, cat in enumerate(categorias)])

            message = (
                f"💰 *{tienda}*\n"
                f"💵 ${format_number_dot(monto)}\n\n"
                f"📂 ¿En qué categoría?\n\n"
                f"{categorias_texto}\n\n"
                f"Responde con el *número* o *nombre*"
            )

            conversaciones[from_number]["categorias"] = categorias
            send_meta_message(from_number, message)

        else:
            send_meta_message(
                from_number,
                "❌ Formato incorrecto\n\n"
                "💡 Escribe: *Tienda, Monto*\n\n"
                "Ejemplos:\n• Jumbo, 18990\n• Jumbo, 18.990"
            )

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def manejar_tipo_division(from_number, respuesta):
    """Maneja tipo de división, envía template al emisor y guarda en Sheets."""
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
        except Exception:
            fecha = ahora.strftime("%d/%m/%Y").lstrip("0").replace("/0", "/")

        # Preparar datos una sola vez
        datos_preparados = preparar_datos_transaccion(datos, tipo)

        # Último mensaje al emisor = template WABA
        enviar_template_pareja(from_number, datos_preparados, template_name="expense_notification_v1")

        # Guardar en background
        datos_copia = dict(datos_preparados)
        t = threading.Thread(
            target=_guardar_transaccion_en_sheets,
            args=(from_number, datos_copia, fecha),
            daemon=True
        )
        t.start()

        # Limpiar estado inmediatamente
        del conversaciones[from_number]

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error: {str(e)}")


def manejar_categoria(from_number, respuesta):
    """Maneja selección de categoría con botones interactivos."""
    try:
        datos = conversaciones[from_number]
        categorias = datos["categorias"]

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

        conversaciones[from_number]["categoria"] = categoria
        conversaciones[from_number]["estado"] = "esperando_pagador"

        # --- send_meta_message con botón interactivo ---
        interactive_payload = {
            "messaging_product": "whatsapp",
            "to": _norm_num(from_number),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": f"✅ Categoría: {categoria}\n💳 ¿Quién pagó?"
                },
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "manu_paid", "title": "1️⃣ Manu"}},
                        {"type": "reply", "reply": {"id": "cami_paid", "title": "2️⃣ Cami"}}
                    ]
                }
            }
        }

        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        requests.post(url, headers=headers, json=interactive_payload, timeout=30)

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()


def manejar_pagador(from_number, respuesta):
    """Maneja quién pagó."""
    try:
        if respuesta.lower() in ["1", "manu", "manuel"]:
            pagador = "Manu"
        elif respuesta.lower() in ["2", "cami", "camila"]:
            pagador = "Cami"
        else:
            send_meta_message(from_number, "❌ Opción no válida. Responde 1 o 2")
            return

        conversaciones[from_number]["pagador"] = pagador
        conversaciones[from_number]["estado"] = "esperando_tipo"

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
    """Copia el formato de una fila y quita el borde superior de la fila destino."""
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
    - si force=True
    """
    fila_abajo = fila + 1

    b = sheet.acell(f"B{fila_abajo}").value or ""
    c = sheet.acell(f"C{fila_abajo}").value or ""

    if not force and b.strip() == "" and c.strip() == "":
        return False

    sheet.insert_row([], fila_abajo)

    try:
        copiar_formato_fila(sheet, fila_origen=fila, fila_destino=fila_abajo, col_end=8)
    except Exception as e:
        print("⚠️ No se pudo copiar formato:", e)

    return True


def _guardar_transaccion_en_sheets(from_number, datos, fecha):
    try:
        sheet = get_sheet()
        fila = encontrar_ultima_fila_categoria(datos["categoria"])

        nueva_fila = [
            "",                    # A
            "",                    # B
            datos["tienda"],       # C
            datos["monto"],        # D
            fecha,                 # E
            datos["pagador"],      # F
            datos["tipo"]          # G
        ]

        sheet.update(f"A{fila}:G{fila}", [nueva_fila], value_input_option="USER_ENTERED")
        asegurar_fila_vacia_debajo(sheet, fila, force=(datos["categoria"] == "Otros"))

        # Notificar a la pareja solo si Sheets guardó OK
        notificar_pareja(from_number, datos)

    except Exception as e:
        print(f"❌ ERROR guardando en Sheets: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error al guardar en Google Sheets: {str(e)}")


@app.route("/")
def home():
    return f"""
    <h1>🤖 ATLAS Bot - Finanzas C&M</h1>
    <p>✅ Bot activo con Meta Cloud API</p>
    <p>📊 Hoja actual: {SHEET_NAME}</p>
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