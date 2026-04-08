# -*- coding: utf-8 -*-
"""
Modulo para sincronizar datos con Google Sheets.
Usado por scraping_activos.py (PC) y scraping_cloud.py (nube).

Columnas auto-actualizables: ESTADO CRONOGRAMA, ETAPA ACTUAL, PLAZO, VALOR
Columnas manuales (preserva ediciones): NOMBRE, DIRECCION, TIPO, CLIENTE, LINK, AREA m2
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

COLS = ["NOMBRE","DIRECCION","TIPO",
        "ESTADO CRONOGRAMA","ETAPA ACTUAL","PLAZO","CLIENTE","LINK",
        "AREA m2","VALOR"]
CAMPOS = ["nombre","direccion","tipo",
          "estado_crono","etapa_actual","plazo","_c","link",
          "area_m2","valor"]
ANCHOS = [220,280,130,160,250,80,100,400,80,150]

# Columnas que el script actualiza automaticamente (indice en CAMPOS)
AUTO_CAMPOS = {"estado_crono", "etapa_actual", "plazo", "valor"}
IDX_LINK = CAMPOS.index("link")


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
    Preserva ediciones manuales en columnas no-automaticas.
    """
    print("Conectando con Google Sheets...")
    client = get_client()
    sh = client.open_by_key(SPREADSHEET_ID)

    hojas_existentes = {w.title: w for w in sh.worksheets()}

    # Obtener hojas (sin borrar)
    ws_med = _obtener_hoja(sh, hojas_existentes, "Medellin", len(inmuebles_med))
    ws_ant = _obtener_hoja(sh, hojas_existentes, "Antioquia", len(inmuebles_ant))

    # Leyenda e Info se pueden reescribir siempre
    ws_ley = _obtener_hoja(sh, hojas_existentes, "Leyenda", 6)
    ws_inf = _obtener_hoja(sh, hojas_existentes, "Info", 5)

    if "Hoja 1" in hojas_existentes:
        try: sh.del_worksheet(hojas_existentes["Hoja 1"])
        except: pass

    print("  Escribiendo Medellin...")
    _escribir_pestaña(ws_med, "LISTADO DE VENTA MASIVA - MEDELLIN",
                      inmuebles_med, cambios_med, "med")

    print("  Escribiendo Antioquia...")
    _escribir_pestaña(ws_ant, "LISTADO DE VENTA MASIVA - ANTIOQUIA (sin Medellin)",
                      inmuebles_ant, cambios_ant, "ant")

    print("  Escribiendo Leyenda e Info...")
    ws_ley.clear()
    _escribir_leyenda(ws_ley)
    ws_inf.clear()
    _escribir_info(ws_inf, len(inmuebles_med), len(inmuebles_ant))

    print(f"  Google Sheets actualizado: {sh.url}")


def _obtener_hoja(sh, existentes, nombre, filas_min):
    if nombre in existentes:
        return existentes[nombre]
    return sh.add_worksheet(title=nombre, rows=max(filas_min + 5, 10), cols=len(COLS))


def _escribir_pestaña(ws, titulo, inmuebles, cambios, tab):
    # ── Leer datos existentes para preservar ediciones manuales ──
    existing = ws.get_all_values()
    manual_por_link = {}  # link -> fila completa
    if len(existing) > 2:
        for row in existing[2:]:  # saltar titulo y encabezados
            link = row[IDX_LINK] if IDX_LINK < len(row) else ""
            if link:
                manual_por_link[link] = row

    # ── Construir filas nuevas preservando columnas manuales ──
    filas = []
    filas.append([titulo] + [""] * (len(COLS) - 1))
    filas.append(COLS)

    for item in inmuebles:
        link = str(item.get("link", ""))
        prev = manual_por_link.get(link)

        fila = []
        for col_idx, campo in enumerate(CAMPOS):
            if campo in AUTO_CAMPOS:
                # Siempre usar valor del scraping
                val = item.get(campo, "")
                fila.append(str(val) if val is not None else "")
            elif prev and col_idx < len(prev) and prev[col_idx]:
                # Preservar valor manual existente
                fila.append(prev[col_idx])
            else:
                # Propiedad nueva: usar valor del scraping
                if campo == "_c":
                    fila.append("Grupo NBC")
                else:
                    val = item.get(campo, "")
                    fila.append(str(val) if val is not None else "")
        filas.append(fila)

    # ── Escribir (borrar y reescribir para manejar filas eliminadas) ──
    ws.clear()
    if filas:
        ws.update(filas, value_input_option="RAW")

    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS))
    except: pass

    # ── Formato ──
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
        fila_idx = i + 2
        bg = color_fila(item.get("estado_crono",""), item.get("estado_api",""))
        for j in range(len(COLS)):
            requests.append(_formato_celdas(ws_id, fila_idx, j, fila_idx+1, j+1,
                                             bg, None, False, 10))

    # Wrap text en columna LINK
    requests.append({
        "repeatCell": {
            "range": _rango(ws_id, 2, IDX_LINK, total_filas, IDX_LINK + 1),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy"
        }
    })

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

    # Filtros
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
