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
PCT_MANU = 0.61
PCT_CAMI = 0.39
PCTS_CARGADOS = False

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
        s = s.replace("$", "")
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
        return monto_num // 2

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

    # Actualizar porcentajes desde la hoja actual si es necesario
    try:
        if not PCTS_CARGADOS:
            actualizar_pcts_desde_sheets()
    except Exception:
        pass

    datos_preparados["monto_deuda"] = calcular_monto_deuda(
        monto_num=datos_preparados["monto"],
        pagador=datos_preparados.get("pagador"),
        tipo=tipo,
    )
    return datos_preparados

def texto_deuda_para_destinatario(to_number, datos):
    """Devuelve el texto de deuda para destinatario en una sola línea."""
    destinatario = nombre_por_numero(to_number)
    pagador = datos.get("pagador")
    monto_deuda = to_int(datos.get("monto_deuda", 0), default=0)
    monto = to_int(datos.get("monto", 0), default=0)

    # Lo que realmente termina pagando quien pagó, después de lo que le deben
    monto_neto_pagador = max(0, monto - monto_deuda)

    # Si el destinatario es quien pagó
    if destinatario == pagador:
        return f"Tu gasto final ${format_number_dot(monto_neto_pagador)} \n Te deben ${format_number_dot(monto_deuda)}"

    # Si el destinatario es la otra persona (Manu o Cami)
    if destinatario in ("Manu", "Cami"):
        return f"Tú debes ${format_number_dot(monto_deuda)}"

    # Caso genérico
    return f"Saldo ${format_number_dot(monto_deuda)}"

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

def enviar_lista_categorias(from_number, tienda, monto):
    """Envía una lista interactiva de categorías."""
    try:
        rows = [
            {"id": "cat_Hogar", "title": "Hogar"},
            {"id": "cat_Alimentos", "title": "Alimentos"},
            {"id": "cat_Compras", "title": "Compras"},
            {"id": "cat_Deporte", "title": "Deporte"},
            {"id": "cat_Otros", "title": "Otros"},
        ]

        interactive_payload = {
            "messaging_product": "whatsapp",
            "to": _norm_num(from_number),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {
                    "text": (
                        f"💰 {tienda}\n"
                        f"💵 ${format_number_dot(monto)}\n\n"
                        f"📂 ¿En qué categoría?\n\n*⚠️Escribe Cancelar para detener el proceso⚠️*"
                    )
                },
                "action": {
                    "button": "Elegir categoría",
                    "sections": [
                        {
                            "title": "Categorías",
                            "rows": rows
                        }
                    ]
                }
            }
        }

        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        resp = requests.post(url, headers=headers, json=interactive_payload, timeout=30)
        print("DEBUG lista categorías:", resp.json())

    except Exception as e:
        print(f"❌ ERROR enviando lista de categorías: {e}")
        import traceback
        traceback.print_exc()

def enviar_botones_pagador(from_number, categoria):
    """Envía botones interactivos para elegir quién pagó."""
    try:
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
                        {"type": "reply", "reply": {"id": "manu_paid", "title": "Manu"}},
                        {"type": "reply", "reply": {"id": "cami_paid", "title": "Cami"}}
                    ]
                }
            }
        }

        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        resp = requests.post(url, headers=headers, json=interactive_payload, timeout=30)
        print("DEBUG botones pagador:", resp.json())

    except Exception as e:
        print(f"❌ ERROR enviando botones de pagador: {e}")
        import traceback
        traceback.print_exc()

def enviar_tipo_division(from_number):
    """Envía botones interactivos para elegir tipo de división (dinámico)."""
    try:
        label_pct = _pct_label_actual()

        interactive_payload = {
            "messaging_product": "whatsapp",
            "to": _norm_num(from_number),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": "📊 ¿Cómo se divide el gasto?"
                },
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "tipo_pct", "title": label_pct}},
                        {"type": "reply", "reply": {"id": "tipo_50", "title": "50/50"}},
                        {"type": "reply", "reply": {"id": "tipo_100", "title": "100%"}}
                    ]
                }
            }
        }

        url = f"https://graph.facebook.com/v18.0/{META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {META_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        resp = requests.post(url, headers=headers, json=interactive_payload, timeout=30)
        print("DEBUG tipo división (dinámico):", resp.json())

    except Exception as e:
        print(f"❌ ERROR enviando tipo de división: {e}")
        import traceback
        traceback.print_exc()

def enviar_template(to_number, datos, template_name="expense_notification_v1"):
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

        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}

        print(f"DEBUG TEMPLATE status={resp.status_code} response={data}")

        if resp.status_code >= 400 or "error" in data:
            print("❌ ERROR enviando template:", data)
            return {
                "ok": False,
                "status_code": resp.status_code,
                "data": data
            }

        return {
            "ok": True,
            "status_code": resp.status_code,
            "data": data
        }

    except Exception as e:
        print(f"❌ ERROR enviando plantilla: {e}")
        import traceback
        traceback.print_exc()
        return {
            "ok": False,
            "error": str(e)
        }

def _parse_pct_cell(cell_value, default=None):
    """Parsea un valor de celda que puede ser '0.61' o '61%'. Devuelve float o None."""
    if cell_value is None:
        return None
    s = str(cell_value).strip()
    try:
        if s.endswith("%"):
            s = s.rstrip("%")
            return float(s) / 100.0
        return float(s)
    except Exception:
        return None

def leer_pcts_desde_hoja(sheet):
    """Lee D3 (Manu) y D4 (Cami) y actualiza PCT_MANU/PCT_CAMI si son válidos."""
    global PCT_MANU, PCT_CAMI
    try:
        manu_val = sheet.acell("D3").value
        cami_val = sheet.acell("D4").value

        manu_parsed = _parse_pct_cell(manu_val, default=None)
        cami_parsed = _parse_pct_cell(cami_val, default=None)

        if manu_parsed is not None:
            PCT_MANU = float(manu_parsed)
        if cami_parsed is not None:
            PCT_CAMI = float(cami_parsed)
    except Exception as e:
        print(f"⚠️ No se pudieron leer PCT desde hoja: {e}")

def _pct_label_actual():
    """Devuelve la etiqueta actual de división usando los PCT ya cargados."""
    try:
        if not PCTS_CARGADOS:
            actualizar_pcts_desde_sheets()
    except Exception:
        pass

    manu_pct = int(round(PCT_MANU * 100))
    cami_pct = int(round(PCT_CAMI * 100))
    return f"{manu_pct}%/{cami_pct}%"

def actualizar_pcts_desde_sheets():
    """Consulta Google Sheets y actualiza PCT_MANU / PCT_CAMI."""
    global PCTS_CARGADOS
    try:
        sheet = get_sheet()
        leer_pcts_desde_hoja(sheet)
        PCTS_CARGADOS = True
        print(f"✅ PCT actualizados desde Sheets: Manu={PCT_MANU}, Cami={PCT_CAMI}")
    except Exception as e:
        print(f"⚠️ No se pudieron actualizar PCT desde Sheets: {e}")

def precargar_pcts_en_background():
    """Precarga D3 y D4 en segundo plano para reducir latencia percibida."""
    try:
        t = threading.Thread(target=actualizar_pcts_desde_sheets, daemon=True)
        t.start()
    except Exception as e:
        print(f"⚠️ No se pudo iniciar precarga de PCT: {e}")

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

    if "tipo" not in datos or "monto_deuda" not in datos:
        datos = preparar_datos_transaccion(datos, datos.get("tipo", ""))

    resultado = enviar_template(notificar_a, datos, template_name="expense_notification_v1")
    if not resultado.get("ok"):
        print("❌ Falló template a la pareja:", resultado)
def is_cancelar(texto):
    """Detecta si el usuario escribió 'Cancelar' (case-insensitive)."""
    if texto is None:
        return False
    return str(texto).strip().lower() == "cancelar"

def cancelar_proceso(from_number):
    """Cancela el proceso en curso para un usuario y notifica la cancelación."""
    if from_number in conversaciones:
        del conversaciones[from_number]
    # Confirmación de cancelación
    send_meta_message(
        from_number,
        "*Cancelación realizada.* Se detuvo el proceso de registro. Si quieres iniciar otro gasto, escribe: Tienda, Monto"
    )

def procesar_mensaje(from_number, mensaje):
    """Procesa mensajes."""
    try:
        # Detección de cancelación en cualquier punto
        if is_cancelar(mensaje):
            cancelar_proceso(from_number)
            return

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
        pattern = r'^(.+?),\s*(\$?\s*[\d\.,]+)$'
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
                    "Ejemplos:\n• Lider, 18990\n• Lider, 18.990\n• Lider, $18.990"
                )
                return

            conversaciones[from_number] = {
                "tienda": tienda,
                "monto": monto,
                "estado": "esperando_categoria",
                "categorias": CATEGORIAS_FIJAS,
            }

            # Precargar porcentajes mientras el usuario elige categoría
            precargar_pcts_en_background()

            enviar_lista_categorias(from_number, tienda, monto)

        else:
            send_meta_message(
                from_number,
                "❌ Formato incorrecto\n\n"
                "💡 Escribe: *Tienda, Monto*\n\n"
                "Ejemplos:\n• Lider, 18990\n• Lider, 18.990\n• Lider, $18.990"
            )

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

def manejar_categoria(from_number, respuesta):
    """Maneja selección de categoría desde lista interactiva."""
    try:
        # Detección de Cancelar en cualquier punto de la categoría
        if is_cancelar(respuesta):
            cancelar_proceso(from_number)
            return

        datos = conversaciones[from_number]
        categorias = datos["categorias"]

        categoria = respuesta.strip()
        if categoria not in categorias:
            send_meta_message(
                from_number,
                "❌ Categoría no válida. Intenta de nuevo usando la lista\n*⚠️Escribe Cancelar para detener el proceso⚠️*"
            )
            return

        conversaciones[from_number]["categoria"] = categoria
        conversaciones[from_number]["estado"] = "esperando_pagador"

        enviar_botones_pagador(from_number, categoria)

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
            send_meta_message(
                from_number,
                "❌ Opción no válida. Responde 1 o 2\n*⚠️Escribe Cancelar para detener el proceso⚠️*"
            )
            return

        conversaciones[from_number]["pagador"] = pagador
        conversaciones[from_number]["estado"] = "esperando_tipo"

        enviar_tipo_division(from_number)

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
            send_meta_message(
                from_number,
                "❌ Opción no válida. Responde 1, 2 o 3\n*⚠️Escribe Cancelar para detener el proceso⚠️*"
            )
            return

        # Enviar plantilla al emisor tan pronto como tengamos los datos
        datos_preparados = preparar_datos_transaccion(datos, tipo)
        try:
            resultado_template = enviar_template(
                from_number,
                datos_preparados,
                template_name="expense_notification_v1"
            )

            if not resultado_template.get("ok"):
                print("❌ Falló template al emisor:", resultado_template)
                send_meta_message(
                    from_number,
                    "⚠️ Se registró el gasto, pero no pude enviar la notificación final por template."
                )

        except Exception as e:
            print(f"❌ ERROR enviando plantilla: {e}")
            import traceback
            traceback.print_exc()

        # Luego, guardar en background (fecha para la hoja)
        ahora = datetime.now(CHILE_TZ)
        try:
            fecha = ahora.strftime("%-d/%-m/%Y")
        except Exception:
            fecha = ahora.strftime("%d/%m/%Y").lstrip("0").replace("/0", "/")

        datos_copia = dict(datos_preparados)
        t = threading.Thread(
            target=_guardar_transaccion_en_sheets,
            args=(from_number, datos_copia, fecha),
            daemon=True
        )
        t.start()

        del conversaciones[from_number]

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_meta_message(from_number, f"❌ Error: {str(e)}")

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
            "",               # A
            "",               # B
            datos["tienda"],  # C
            datos["monto"],   # D
            fecha,            # E
            datos["pagador"], # F
            datos["tipo"]     # G
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

# Webhook combinado (GET para verificación, POST para mensajes)
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 🔹 VERIFICACIÓN (GET)
    verify_token = os.getenv("META_VERIFY_TOKEN")

    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        else:
            return "Forbidden", 403

    # 🔹 MENSAJES (POST)
    if request.method == "POST":
        data = request.json

        if not data:
            return jsonify({"status": "no_data"}), 200

        if data.get("object") == "whatsapp_business_account":
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    # Status updates
                    for status in value.get("statuses", []):
                        status_id = status.get("id")
                        status_text = status.get("status")
                        recipient = status.get("recipient_id")
                        timestamp = status.get("timestamp")
                        print(f"STATUS: id={status_id} to={recipient} status={status_text} ts={timestamp}")

                    # Mensajes entrantes
                    for message in value.get("messages", []):
                        from_number = message.get("from")

                        if message.get("type") == "text":
                            message_text = message.get("text", {}).get("body", "")
                            procesar_mensaje(from_number, message_text)

                        elif message.get("type") == "interactive":
                            interactive = message.get("interactive", {})

                            # Lista de categorías
                            if interactive.get("type") == "list_reply":
                                list_id = interactive.get("list_reply", {}).get("id")

                                mapping_categorias = {
                                    "cat_Hogar": "Hogar",
                                    "cat_Alimentos": "Alimentos",
                                    "cat_Compras": "Compras",
                                    "cat_Deporte": "Deporte",
                                    "cat_Otros": "Otros",
                                }

                                if list_id in mapping_categorias:
                                    procesar_mensaje(from_number, mapping_categorias[list_id])

                            # Botones
                            elif interactive.get("type") == "button_reply":
                                button_id = interactive.get("button_reply", {}).get("id")

                                if button_id == "manu_paid":
                                    procesar_mensaje(from_number, "Manu")

                                elif button_id == "cami_paid":
                                    procesar_mensaje(from_number, "Cami")

                                elif button_id in ["tipo_100", "tipo_50", "tipo_pct"]:
                                    mapping_tipos = {
                                        "tipo_100": "1",
                                        "tipo_50": "2",
                                        "tipo_pct": "3",
                                    }
                                    procesar_mensaje(from_number, mapping_tipos[button_id])

        return jsonify({"status": "ok"}), 200

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