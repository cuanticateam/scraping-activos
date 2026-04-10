# -*- coding: utf-8 -*-
"""
Monitor de correos electronicos.
Lee buzones via IMAP, extrae remitente/asunto/fecha y sincroniza a Google Sheets.
Corre en GitHub Actions 3 veces al dia (8am, 12pm, 6pm COL).
"""

import imaplib
import email
from email.header import decode_header
import json
import os
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

COL_TZ = timezone(timedelta(hours=-5))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_CHECK_FILE = os.path.join(SCRIPT_DIR, "email_last_check.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDS_FILE = os.path.join(SCRIPT_DIR, "scraping-activos-3539aac681f8.json")
SHARE_WITH = "cuanticateamsas@gmail.com"

# ID del Google Sheet para emails (creado manualmente y compartido con la service account)
SPREADSHEET_ID_EMAILS = os.environ.get(
    "SPREADSHEET_ID_EMAILS",
    ""  # Se configura como secreto en GitHub o aqui directamente
)

# Cuentas: JSON con lista de objetos {"email", "password", "imap_server"}
# Se lee de la variable de entorno EMAIL_ACCOUNTS_CONFIG
# Ejemplo:
# [
#   {"email":"cuanticateamsas@gmail.com","password":"xxxx xxxx xxxx xxxx","imap_server":"imap.gmail.com"},
#   {"email":"hello@oxigenog.com","password":"xxxx","imap_server":"imap.gmail.com"},
#   ...
# ]

IMAP_DEFAULTS = {
    "gmail.com": "imap.gmail.com",
    "oxigenog.com": "imap.gmail.com",   # ajustar si no es Google Workspace
    "bankdv.com": "imap.gmail.com",     # ajustar si no es Google Workspace
}


def get_accounts():
    """Lee las cuentas desde la variable de entorno."""
    raw = os.environ.get("EMAIL_ACCOUNTS_CONFIG", "")
    if not raw:
        raise Exception("Variable EMAIL_ACCOUNTS_CONFIG no configurada")
    accounts = json.loads(raw)
    # Asignar servidor IMAP por defecto si no viene
    for acc in accounts:
        if "imap_server" not in acc or not acc["imap_server"]:
            domain = acc["email"].split("@")[1]
            acc["imap_server"] = IMAP_DEFAULTS.get(domain, "imap.gmail.com")
    return accounts


# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS
# ═══════════════════════════════════════════════════════════════════════════════

def get_sheets_client():
    if os.path.exists(CREDS_FILE):
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        return gspread.authorize(creds)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)
    raise Exception("No se encontraron credenciales de Google Sheets")


def get_sheet(client):
    """Abre el Google Sheet para emails usando el ID configurado."""
    sheet_id = SPREADSHEET_ID_EMAILS
    if not sheet_id:
        raise Exception(
            "SPREADSHEET_ID_EMAILS no configurado. "
            "Crea un Google Sheet manualmente, compartelo con "
            "sheets-bot@scraping-activos.iam.gserviceaccount.com (Editor) "
            "y configura el ID como secreto en GitHub."
        )
    return client.open_by_key(sheet_id)


# ═══════════════════════════════════════════════════════════════════════════════
# IMAP - LECTURA DE CORREOS
# ═══════════════════════════════════════════════════════════════════════════════

def decode_mime_header(header_value):
    """Decodifica un header MIME (puede tener charset variados)."""
    if not header_value:
        return ""
    parts = decode_header(header_value)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return " ".join(decoded)


def parse_date(date_str):
    """Extrae fecha y hora de un header Date."""
    if not date_str:
        return "", ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        # Convertir a hora Colombia
        dt_col = dt.astimezone(COL_TZ)
        return dt_col.strftime("%Y-%m-%d"), dt_col.strftime("%H:%M")
    except Exception:
        return date_str[:10] if len(date_str) >= 10 else date_str, ""


def detect_gmail_category(mail, msg_id):
    """Detecta la bandeja de Gmail (Principal, Promociones, etc.) via X-GM-LABELS."""
    try:
        status, data = mail.fetch(msg_id, "(X-GM-LABELS)")
        if status == "OK" and data[0]:
            raw_labels = data[0][1] if isinstance(data[0], tuple) else data[0]
            if isinstance(raw_labels, bytes):
                raw_labels = raw_labels.decode("utf-8", errors="replace")
            labels = str(raw_labels).upper()
            if "CATEGORY_PROMOTIONS" in labels:
                return "Promociones"
            if "CATEGORY_SOCIAL" in labels:
                return "Social"
            if "CATEGORY_UPDATES" in labels:
                return "Notificaciones"
            if "CATEGORY_FORUMS" in labels:
                return "Foros"
        return "Principal"
    except Exception:
        return "Principal"


def is_forwarded_from_hotmail(msg):
    """Detecta si un correo fue reenviado desde bayrongil1@hotmail.com."""
    # Revisar headers de reenvio
    for header_name in ("X-Forwarded-To", "X-Forwarded-For",
                        "X-MS-Exchange-Organization-AutoForwarded-From",
                        "Resent-From"):
        val = msg.get(header_name, "")
        if "bayrongil" in val.lower():
            return True
    # Revisar cadena Received por mencion a bayrongil
    received_headers = msg.get_all("Received") or []
    for r in received_headers:
        if "bayrongil" in r.lower():
            return True
    return False


def fetch_emails(account, since_date):
    """
    Conecta via IMAP y obtiene correos desde since_date.
    Retorna lista de dicts: {buzon, remitente, asunto, fecha, hora, bandeja}
    """
    imap_server = account["imap_server"]
    email_addr = account["email"]
    password = account["password"]
    is_gmail = "gmail.com" in imap_server
    results = []

    try:
        mail = imaplib.IMAP4_SSL(imap_server, 993)
        mail.login(email_addr, password)
        mail.select("INBOX", readonly=True)

        # Buscar correos desde la fecha
        search_date = since_date.strftime("%d-%b-%Y")
        status, msg_ids = mail.search(None, f'(SINCE "{search_date}")')

        if status != "OK" or not msg_ids[0]:
            mail.logout()
            return results

        id_list = msg_ids[0].split()
        print(f"    {len(id_list)} correos desde {search_date}")

        for msg_id in id_list:
            try:
                # Fetch headers
                status, data = mail.fetch(msg_id, "(RFC822.HEADER)")
                if status != "OK":
                    continue

                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                remitente = decode_mime_header(msg.get("From", ""))
                asunto = decode_mime_header(msg.get("Subject", ""))
                fecha_str = msg.get("Date", "")
                fecha, hora = parse_date(fecha_str)

                remitente = remitente.replace('"', '').replace("'", "")

                # Detectar bandeja (solo Gmail)
                bandeja = detect_gmail_category(mail, msg_id) if is_gmail else "Principal"

                # Detectar reenvios de Hotmail
                buzon = email_addr
                if email_addr == "cuanticateamsas@gmail.com" and is_forwarded_from_hotmail(msg):
                    buzon = "bayrongil1@hotmail.com"

                results.append({
                    "buzon": buzon,
                    "remitente": remitente,
                    "asunto": asunto,
                    "fecha": fecha,
                    "hora": hora,
                    "bandeja": bandeja,
                })
            except Exception as e:
                print(f"    Error leyendo mensaje {msg_id}: {e}")

        mail.logout()
    except Exception as e:
        print(f"  ERROR conectando a {email_addr}: {e}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# LAST CHECK TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def get_last_check():
    """Retorna la fecha desde la cual buscar correos."""
    if os.path.exists(LAST_CHECK_FILE):
        with open(LAST_CHECK_FILE, "r") as f:
            data = json.load(f)
            last = data.get("last_check", "")
            if last:
                return datetime.fromisoformat(last)
    # Primera ejecucion: buscar correos de hoy
    return datetime.now(COL_TZ).replace(hour=0, minute=0, second=0)


def save_last_check():
    """Guarda el timestamp actual como ultimo chequeo."""
    now = datetime.now(COL_TZ).isoformat()
    with open(LAST_CHECK_FILE, "w") as f:
        json.dump({"last_check": now}, f)


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC A GOOGLE SHEETS
# ═══════════════════════════════════════════════════════════════════════════════

COLOR_HEADER = {"red": 0.12, "green": 0.22, "blue": 0.39}
COLOR_BLANCO = {"red": 1.0, "green": 1.0, "blue": 1.0}
COLOR_ALTERNO = {"red": 0.95, "green": 0.96, "blue": 0.98}

# Color por buzon para identificar visualmente
COLORES_BUZON = {
    "cuanticateamsas@gmail.com":    {"red": 0.85, "green": 0.92, "blue": 1.0},
    "hello@oxigenog.com":           {"red": 0.85, "green": 1.0, "blue": 0.85},
    "pedrormesas@gmail.com":        {"red": 1.0, "green": 0.95, "blue": 0.85},
    "gruponbc76@gmail.com":         {"red": 1.0, "green": 0.85, "blue": 0.85},
    "choppam30@gmail.com":          {"red": 0.95, "green": 0.85, "blue": 1.0},
    "sandramija984@gmail.com":      {"red": 1.0, "green": 1.0, "blue": 0.85},
    "tecnologia.oxigenog@gmail.com":{"red": 0.85, "green": 1.0, "blue": 0.95},
    "management@bankdv.com":        {"red": 0.95, "green": 0.90, "blue": 0.90},
    "bayrongil1@hotmail.com":        {"red": 0.90, "green": 0.95, "blue": 0.95},
}

COLS = ["BUZON", "BANDEJA", "REMITENTE", "ASUNTO", "FECHA", "HORA"]
ANCHOS = [250, 120, 300, 450, 110, 70]


def _rango(sheet_id, r1, c1, r2, c2):
    return {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def _formato_celdas(sheet_id, r1, c1, r2, c2, bg=None, fg=None, bold=False, size=10):
    fmt = {"fontSize": size}
    if bold:
        fmt["bold"] = True
    if fg:
        fmt["foregroundColorStyle"] = {"rgbColor": fg}
    cell_fmt = {"textFormat": fmt}
    if bg:
        cell_fmt["backgroundColor"] = bg
    return {
        "repeatCell": {
            "range": _rango(sheet_id, r1, c1, r2, c2),
            "cell": {"userEnteredFormat": cell_fmt},
            "fields": "userEnteredFormat"
        }
    }


def sync_emails_to_sheet(all_emails):
    """Escribe los correos en Google Sheets, organizados por fecha."""
    print("\nConectando con Google Sheets...")
    client = get_sheets_client()
    sh = get_sheet(client)

    # Agrupar por fecha (YYYY-MM-DD)
    hoy = datetime.now(COL_TZ).strftime("%Y-%m-%d")

    # Obtener o crear hoja del dia
    hoja_nombre = f"Correos {hoy}"
    hojas = {w.title: w for w in sh.worksheets()}

    if hoja_nombre in hojas:
        ws = hojas[hoja_nombre]
        # Leer existentes para no duplicar
        existing = ws.get_all_values()
        existing_keys = set()
        for row in existing[2:]:  # saltar titulo + encabezados
            if len(row) >= 6:
                existing_keys.add(f"{row[0]}|{row[2]}|{row[3]}|{row[4]}|{row[5]}")
        # Filtrar duplicados
        nuevos = []
        for e in all_emails:
            key = f"{e['buzon']}|{e['remitente']}|{e['asunto']}|{e['fecha']}|{e['hora']}"
            if key not in existing_keys:
                nuevos.append(e)
        if not nuevos:
            print("  Sin correos nuevos para agregar")
            return
        print(f"  {len(nuevos)} correos nuevos (de {len(all_emails)} totales)")
        all_emails = nuevos
        # Append al final
        filas = []
        for e in all_emails:
            filas.append([e["buzon"], e["bandeja"], e["remitente"], e["asunto"], e["fecha"], e["hora"]])
        start_row = len(existing) + 1
        ws.update(filas, f"A{start_row}", value_input_option="RAW")
        _aplicar_formato_filas(ws, start_row - 1, all_emails)
        print(f"  {len(filas)} correos agregados a {hoja_nombre}")
        return

    # Crear hoja nueva del dia
    ws = sh.add_worksheet(title=hoja_nombre, rows=max(len(all_emails) + 5, 50), cols=len(COLS))
    # Eliminar "Hoja 1" si existe
    if "Hoja 1" in hojas:
        try:
            sh.del_worksheet(hojas["Hoja 1"])
        except Exception:
            pass

    # Ordenar por hora (mas reciente primero)
    all_emails.sort(key=lambda x: (x["fecha"], x["hora"]), reverse=True)

    # Construir filas
    titulo = f"MONITOR DE CORREOS - {hoy}"
    filas = [[titulo] + [""] * (len(COLS) - 1), COLS]
    for e in all_emails:
        filas.append([e["buzon"], e["bandeja"], e["remitente"], e["asunto"], e["fecha"], e["hora"]])

    ws.update(filas, value_input_option="RAW")

    # Formato
    _aplicar_formato_completo(ws, filas, all_emails)
    print(f"  Hoja '{hoja_nombre}' creada con {len(all_emails)} correos")

    # Actualizar hoja resumen
    _actualizar_resumen(sh, hojas, all_emails)

    print(f"  Google Sheets: {sh.url}")


def _aplicar_formato_completo(ws, filas, emails):
    """Aplica formato a una hoja nueva completa."""
    ws_id = ws.id
    total = len(filas)
    requests = []

    # Merge titulo
    requests.append({
        "mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, len(COLS)),
            "mergeType": "MERGE_ALL"
        }
    })
    # Formato titulo
    requests.append(_formato_celdas(ws_id, 0, 0, 1, len(COLS),
                                     COLOR_HEADER, COLOR_BLANCO, True, 13))
    # Formato encabezados
    requests.append(_formato_celdas(ws_id, 1, 0, 2, len(COLS),
                                     COLOR_HEADER, COLOR_BLANCO, True, 10))

    # Formato filas con color por buzon
    for i, e in enumerate(emails):
        fila_idx = i + 2
        bg = COLORES_BUZON.get(e["buzon"], COLOR_ALTERNO)
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx + 1, len(COLS),
                                         bg, None, False, 10))

    # Anchos
    for j, ancho in enumerate(ANCHOS):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                          "startIndex": j, "endIndex": j + 1},
                "properties": {"pixelSize": ancho},
                "fields": "pixelSize"
            }
        })

    # Congelar filas
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": ws_id,
                           "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"
        }
    })

    # Filtros
    requests.append({"clearBasicFilter": {"sheetId": ws_id}})
    requests.append({
        "setBasicFilter": {
            "filter": {"range": _rango(ws_id, 1, 0, total, len(COLS))}
        }
    })

    if requests:
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start + 100]
            ws.spreadsheet.batch_update({"requests": chunk})


def _aplicar_formato_filas(ws, start_row, emails):
    """Aplica formato a filas nuevas agregadas."""
    ws_id = ws.id
    requests = []
    for i, e in enumerate(emails):
        fila_idx = start_row + i
        bg = COLORES_BUZON.get(e["buzon"], COLOR_ALTERNO)
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx + 1, len(COLS),
                                         bg, None, False, 10))
    if requests:
        ws.spreadsheet.batch_update({"requests": requests})


def _actualizar_resumen(sh, hojas, emails):
    """Crea/actualiza una hoja Resumen con conteo por buzon."""
    if "Resumen" in hojas:
        ws = hojas["Resumen"]
    else:
        ws = sh.add_worksheet(title="Resumen", rows=15, cols=3)
        # Mover al inicio
        try:
            sh.reorder_worksheets([ws] + [w for w in sh.worksheets() if w.id != ws.id])
        except Exception:
            pass

    ws.clear()
    ahora = datetime.now(COL_TZ).strftime("%Y-%m-%d %H:%M")

    # Contar por buzon
    conteo = {}
    for e in emails:
        conteo[e["buzon"]] = conteo.get(e["buzon"], 0) + 1

    filas = [
        ["RESUMEN - MONITOR DE CORREOS", "", ""],
        ["Ultima actualizacion", ahora, ""],
        ["", "", ""],
        ["BUZON", "CORREOS HOY", ""],
    ]
    for buzon, count in sorted(conteo.items()):
        filas.append([buzon, str(count), ""])
    filas.append(["", "", ""])
    filas.append(["TOTAL", str(sum(conteo.values())), ""])

    ws.update(filas, value_input_option="RAW")

    ws_id = ws.id
    requests = [
        {"mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, 3),
            "mergeType": "MERGE_ALL"
        }},
        _formato_celdas(ws_id, 0, 0, 1, 3, COLOR_HEADER, COLOR_BLANCO, True, 13),
        _formato_celdas(ws_id, 3, 0, 4, 2, COLOR_HEADER, COLOR_BLANCO, True, 10),
        _formato_celdas(ws_id, len(filas) - 1, 0, len(filas), 2, None, None, True, 10),
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 300}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 120}, "fields": "pixelSize"
        }},
    ]
    # Color por buzon en resumen
    for i, (buzon, _) in enumerate(sorted(conteo.items())):
        fila_idx = 4 + i
        bg = COLORES_BUZON.get(buzon, COLOR_ALTERNO)
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx + 1, 2, bg, None, False, 10))

    ws.spreadsheet.batch_update({"requests": requests})


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  Monitor de Correos Electronicos")
    print("  8 buzones -> Google Sheets")
    print("=" * 55)

    accounts = get_accounts()
    print(f"\n{len(accounts)} cuentas configuradas")

    since = get_last_check()
    print(f"Buscando correos desde: {since.strftime('%Y-%m-%d %H:%M')}")

    all_emails = []
    for i, acc in enumerate(accounts, 1):
        print(f"\n[{i}/{len(accounts)}] {acc['email']}")
        emails = fetch_emails(acc, since)
        all_emails.extend(emails)
        print(f"    {len(emails)} correos encontrados")

    print(f"\nTotal: {len(all_emails)} correos de {len(accounts)} buzones")

    if all_emails:
        sync_emails_to_sheet(all_emails)
    else:
        print("Sin correos nuevos")

    save_last_check()
    print("\nListo.")
