# -*- coding: utf-8 -*-
"""
Modulo para sincronizar datos con Google Sheets.
Usado por scraping_activos.py (PC) y scraping_cloud.py (nube).
"""

import json, os
from datetime import datetime, timedelta

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SPREADSHEET_ID = "1a1ar-47FUtIMlw7uBSCKREh_ckIljk515qXLRB8-5C0"

# Buscar credenciales: archivo local o variable de entorno (GitHub)
CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scraping-activos-3539aac681f8.json")

COLOR_HEADER  = {"red":0.12, "green":0.22, "blue":0.39}   # 1F3864
COLOR_HDR2    = {"red":0.18, "green":0.36, "blue":0.62}   # 2E5D9E
COLOR_CRONO   = {"red":0.84, "green":0.89, "blue":0.94}   # D6E4F0
COLOR_MANIF   = {"red":0.95, "green":0.95, "blue":0.95}   # F2F2F2
COLOR_SUBASTA = {"red":1.0,  "green":0.95, "blue":0.80}   # FFF2CC
COLOR_BLANCO  = {"red":1.0,  "green":1.0,  "blue":1.0}


def get_client():
    """Crea el cliente de gspread con las credenciales disponibles."""
    import gspread
    from google.oauth2.service_account import Credentials

    # Opcion 1: archivo JSON local
    if os.path.exists(CREDS_FILE):
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        return gspread.authorize(creds)

    # Opcion 2: variable de entorno (GitHub Actions)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)

    raise Exception("No se encontraron credenciales de Google Sheets")


def color_fila(estado_crono, estado_api):
    cod = (estado_api or "").upper()
    if estado_crono == "Manifestacion Abierta":
        return COLOR_MANIF
    if "SUBASTA" in cod or "PROXIMO" in cod:
        return COLOR_SUBASTA
    return COLOR_CRONO


def sync_to_sheets(inmuebles_med, inmuebles_ant, cambios_med, cambios_ant):
    """
    Escribe los datos en Google Sheets con formato y colores.
    """
    print("Conectando con Google Sheets...")
    client = get_client()
    sh = client.open_by_key(SPREADSHEET_ID)

    # Crear/obtener pestanas
    hojas_existentes = {w.title: w for w in sh.worksheets()}

    ws_med = _preparar_hoja(sh, hojas_existentes, "Medellin", len(inmuebles_med))
    ws_ant = _preparar_hoja(sh, hojas_existentes, "Antioquia", len(inmuebles_ant))
    ws_ley = _preparar_hoja(sh, hojas_existentes, "Leyenda", 6)
    ws_inf = _preparar_hoja(sh, hojas_existentes, "Info", 5)

    # Eliminar Hoja 1 por defecto si existe
    if "Hoja 1" in hojas_existentes:
        try: sh.del_worksheet(hojas_existentes["Hoja 1"])
        except: pass

    # Escribir datos
    print("  Escribiendo Medellin...")
    _escribir_pestaña(ws_med, "LISTADO DE VENTA MASIVA - MEDELLIN",
                      inmuebles_med, cambios_med, "med")

    print("  Escribiendo Antioquia...")
    _escribir_pestaña(ws_ant, "LISTADO DE VENTA MASIVA - ANTIOQUIA (sin Medellin)",
                      inmuebles_ant, cambios_ant, "ant")

    print("  Escribiendo Leyenda e Info...")
    _escribir_leyenda(ws_ley)
    _escribir_info(ws_inf, len(inmuebles_med), len(inmuebles_ant))

    print(f"  Google Sheets actualizado: {sh.url}")


def _preparar_hoja(sh, existentes, nombre, filas_min):
    if nombre in existentes:
        ws = existentes[nombre]
        ws.clear()
    else:
        ws = sh.add_worksheet(title=nombre, rows=max(filas_min + 5, 10), cols=12)
    return ws


def _escribir_pestaña(ws, titulo, inmuebles, cambios, tab):
    COLS = ["NOMBRE","DIRECCION","TIPO","AREA m2","VALOR",
            "ESTADO CRONOGRAMA","ETAPA ACTUAL","PLAZO","CLIENTE","LINK"]
    CAMPOS = ["nombre","direccion","tipo","area_m2","valor",
              "estado_crono","etapa_actual","plazo","_c","link"]
    ANCHOS = [220,280,130,80,150,160,250,80,100,400]

    # Preparar todas las filas
    filas = []
    filas.append([titulo] + [""] * (len(COLS) - 1))   # fila 1: titulo
    filas.append(COLS)                                  # fila 2: encabezados
    for item in inmuebles:
        fila = []
        for campo in CAMPOS:
            if campo == "_c":
                fila.append("Grupo NBC")
            else:
                val = item.get(campo, "")
                fila.append(str(val) if val is not None else "")
        filas.append(fila)

    # Escribir todo de una vez (rapido)
    if filas:
        ws.update(filas, value_input_option="RAW")

    # Ajustar tamano
    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS))
    except: pass

    # Formato por lotes
    requests = []

    ws_id = ws.id

    # Merge titulo
    requests.append({
        "mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, len(COLS)),
            "mergeType": "MERGE_ALL"
        }
    })

    # Formato titulo
    requests.append(_formato_celdas(ws_id, 0, 0, 1, len(COLS),
                                     COLOR_HEADER, COLOR_BLANCO, True, 14))

    # Formato encabezados
    requests.append(_formato_celdas(ws_id, 1, 0, 2, len(COLS),
                                     COLOR_HDR2, COLOR_BLANCO, True, 10))

    # Formato filas de datos
    for i, item in enumerate(inmuebles):
        fila_idx = i + 2  # 0-based (titulo=0, headers=1, datos=2+)
        pid = item.get("_id", "")
        bg = color_fila(item.get("estado_crono",""), item.get("estado_api",""))

        for j, campo in enumerate(CAMPOS):
            requests.append(_formato_celdas(ws_id, fila_idx, j, fila_idx+1, j+1,
                                             bg, None, False, 10))

    # Anchos de columna
    for j, ancho in enumerate(ANCHOS):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                          "startIndex": j, "endIndex": j+1},
                "properties": {"pixelSize": ancho},
                "fields": "pixelSize"
            }
        })

    # Congelar filas
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": ws_id,
                "gridProperties": {"frozenRowCount": 2}
            },
            "fields": "gridProperties.frozenRowCount"
        }
    })

    # Filtros en encabezados (limpiar existente primero)
    requests.append({"clearBasicFilter": {"sheetId": ws_id}})
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": _rango(ws_id, 1, 0, total_filas, len(COLS))
            }
        }
    })

    # Ejecutar todo
    if requests:
        # Google limita a 100 requests por batch, dividir si necesario
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start+100]
            ws.spreadsheet.batch_update({"requests": chunk})


def _escribir_leyenda(ws):
    filas = [
        ["Leyenda de colores"],
        ["Proximo Subasta"],
        ["Con cronograma - En proceso"],
        ["Manifestacion Abierta"],
    ]
    ws.update(filas)
    ws_id = ws.id
    reqs = [
        _formato_celdas(ws_id, 0, 0, 1, 1, None, None, True, 12),
        _formato_celdas(ws_id, 1, 0, 2, 1, COLOR_SUBASTA, None, False, 10),
        _formato_celdas(ws_id, 2, 0, 3, 1, COLOR_CRONO, None, False, 10),
        _formato_celdas(ws_id, 3, 0, 4, 1, COLOR_MANIF, None, False, 10),
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 350}, "fields": "pixelSize"
        }}
    ]
    ws.spreadsheet.batch_update({"requests": reqs})


def _escribir_info(ws, n_med, n_ant):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    filas = [
        ["Ultima actualizacion", ahora],
        ["Medellin", f"{n_med} inmuebles"],
        ["Antioquia (sin Med.)", f"{n_ant} inmuebles"],
        ["Fuente", "activosporcolombia.com"],
    ]
    ws.update(filas)
    ws_id = ws.id
    reqs = [
        _formato_celdas(ws_id, 0, 0, 4, 1, None, None, True, 10),
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 200}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 250}, "fields": "pixelSize"
        }}
    ]
    ws.spreadsheet.batch_update({"requests": reqs})


# ── Helpers de formato ──

def _rango(sheet_id, r1, c1, r2, c2):
    return {"sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def _formato_celdas(sheet_id, r1, c1, r2, c2, bg=None, fg=None, bold=False, size=10):
    fmt = {"fontSize": size}
    if bold: fmt["bold"] = True
    if fg: fmt["foregroundColorStyle"] = {"rgbColor": fg}
    cell_fmt = {"textFormat": fmt}
    if bg: cell_fmt["backgroundColor"] = bg

    return {
        "repeatCell": {
            "range": _rango(sheet_id, r1, c1, r2, c2),
            "cell": {"userEnteredFormat": cell_fmt},
            "fields": "userEnteredFormat"
        }
    }
