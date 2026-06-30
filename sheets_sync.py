# -*- coding: utf-8 -*-
"""
Modulo para sincronizar datos con Google Sheets.
Usado por scraping_activos.py (PC) y scraping_cloud.py (nube).

Columnas auto-actualizables: ESTADO CRONOGRAMA, ETAPA ACTUAL, PLAZO, VALOR
Columnas manuales (preserva ediciones): NOMBRE, DIRECCION, TIPO, CLIENTE, LINK, AREA m2
"""

import json, os, time
from datetime import datetime, timedelta, timezone

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
COLOR_CAMBIO  = {"red":1.0,  "green":0.85, "blue":0.85}  # rojo claro para cambios
COLOR_VENDIDO = {"red":0.6,  "green":0.0,  "blue":0.4}   # magenta oscuro
COLOR_ELIMINADO = {"red":0.85, "green":0.85, "blue":0.85}  # gris claro
COLOR_ELIM_HDR  = {"red":0.55, "green":0.55, "blue":0.55}  # gris oscuro

COLS = ["NOMBRE","DIRECCION","TIPO",
        "ESTADO CRONOGRAMA","ETAPA ACTUAL","PLAZO","LINK",
        "AREA m2","VALOR","FMI","ANOTACIONES"]
CAMPOS = ["nombre","direccion","tipo",
          "estado_crono","etapa_actual","plazo","link",
          "area_m2","valor","fmi","_anotaciones"]
ANCHOS = [220,280,130,160,250,80,80,80,150,140,300]

# Columnas que el script actualiza automaticamente (indice en CAMPOS)
AUTO_CAMPOS = {"estado_crono", "etapa_actual", "plazo", "valor", "area_m2"}
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
    if "VENDIDO" in cod:
        return COLOR_VENDIDO
    if estado_crono == "Manifestacion Abierta":
        return COLOR_MANIF
    if "SUBASTA" in cod or "PROXIMO" in cod:
        return COLOR_SUBASTA
    return COLOR_CRONO


def sync_to_sheets(inmuebles_med, inmuebles_ant, cambios_med, cambios_ant,
                   inmuebles_bello=None, inmuebles_pintada=None,
                   cambios_bello=None, cambios_pintada=None,
                   eliminados_med=None, eliminados_ant=None):
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

    # Bello y La Pintada
    if inmuebles_bello is not None:
        ws_bel = _obtener_hoja(sh, hojas_existentes, "Bello", len(inmuebles_bello))
        print("  Escribiendo Bello...")
        _escribir_pestaña(ws_bel, "LISTADO DE VENTA MASIVA - BELLO",
                          inmuebles_bello, cambios_bello or {}, "bel")

    if inmuebles_pintada is not None:
        ws_pin = _obtener_hoja(sh, hojas_existentes, "La Pintada", len(inmuebles_pintada))
        print("  Escribiendo La Pintada...")
        _escribir_pestaña(ws_pin, "LISTADO DE VENTA MASIVA - LA PINTADA",
                          inmuebles_pintada, cambios_pintada or {}, "pin")

    # Pestaña Eliminados (separada)
    todos_elim = []
    for e in (eliminados_med or []):
        todos_elim.append(("MED", e))
    for e in (eliminados_ant or []):
        todos_elim.append(("ANT", e))
    if todos_elim:
        n_elim = len(todos_elim)
        ws_elim = _obtener_hoja(sh, hojas_existentes, "Eliminados", n_elim + 5)
        print(f"  Escribiendo Eliminados ({n_elim})...")
        _escribir_eliminados(ws_elim, todos_elim)

    # Pestaña Con Precio (Medellin + Antioquia)
    con_precio = [i for i in (inmuebles_med + inmuebles_ant) if i.get("valor","X") != "X"]
    if con_precio:
        ws_precio = _obtener_hoja(sh, hojas_existentes, "Con Precio", len(con_precio) + 5)
        print(f"  Escribiendo Con Precio ({len(con_precio)})...")
        _escribir_con_precio(ws_precio, con_precio)

    print("  Escribiendo Leyenda e Info...")
    ws_ley.clear()
    _escribir_leyenda(ws_ley)
    n_bel = len(inmuebles_bello) if inmuebles_bello else 0
    n_pin = len(inmuebles_pintada) if inmuebles_pintada else 0
    ws_inf.clear()
    _escribir_info(ws_inf, len(inmuebles_med), len(inmuebles_ant), n_bel, n_pin)

    print(f"  Google Sheets actualizado: {sh.url}")


def _obtener_hoja(sh, existentes, nombre, filas_min):
    if nombre in existentes:
        return existentes[nombre]
    return sh.add_worksheet(title=nombre, rows=max(filas_min + 5, 10), cols=len(COLS))


def _escribir_pestaña(ws, titulo, inmuebles, cambios, tab):
    # ── Leer datos existentes para preservar ediciones manuales ──
    existing = ws.get_all_values()
    # Leer formulas para extraer URLs de HYPERLINK
    try:
        existing_formulas = ws.get(value_render_option="FORMULA")
    except Exception:
        existing_formulas = existing
    manual_por_link = {}  # link -> fila completa (values)
    if len(existing) > 2:
        for ri, row in enumerate(existing[2:], start=2):
            link_val = ""
            # Extraer URL de la formula HYPERLINK
            if ri < len(existing_formulas):
                frow = existing_formulas[ri]
                if IDX_LINK < len(frow):
                    cell_f = str(frow[IDX_LINK])
                    if "HYPERLINK" in cell_f:
                        import re
                        m = re.search(r'HYPERLINK\("([^"]+)"', cell_f)
                        if m:
                            link_val = m.group(1)
            # Fallback: buscar celda con http
            if not link_val:
                for ci, cell in enumerate(row):
                    if cell and str(cell).startswith("http"):
                        link_val = cell
                        break
            if link_val:
                manual_por_link[link_val] = row

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
            elif campo == "link":
                if link:
                    fila.append(f'=HYPERLINK("{link}";"Ver")')
                else:
                    fila.append("")
            elif campo == "nombre":
                # Permitir sobreescribir "Sin nombre"
                if prev and col_idx < len(prev) and prev[col_idx] and prev[col_idx] != "Sin nombre":
                    fila.append(prev[col_idx])
                else:
                    val = item.get(campo, "")
                    fila.append(str(val) if val is not None else "")
            elif prev and col_idx < len(prev) and prev[col_idx]:
                # Preservar valor manual existente
                fila.append(prev[col_idx])
            else:
                # Propiedad nueva: usar valor del scraping
                val = item.get(campo, "")
                fila.append(str(val) if val is not None else "")
        filas.append(fila)

    # ── Escribir (borrar y reescribir para manejar filas eliminadas) ──
    ws.clear()
    if filas:
        ws.update(filas, value_input_option="USER_ENTERED")

    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS))
    except: pass

    # ── Formato ──
    requests = []
    ws_id = ws.id

    # Desmerge todas las celdas combinadas existentes
    try:
        sheet_meta = ws.spreadsheet.fetch_sheet_metadata()
        for s in sheet_meta.get("sheets", []):
            if s["properties"]["sheetId"] == ws_id:
                for m in s.get("merges", []):
                    requests.append({"unmergeCells": {"range": m}})
                break
    except Exception:
        pass  # si no puede leer merges, continua

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

    # Formato filas de datos — por FILA (no por celda) para reducir API calls
    for i, item in enumerate(inmuebles):
        fila_idx = i + 2
        pid = item.get("_id", "")
        estado_api = (item.get("estado_api","") or "").upper()
        es_vendido = "VENDIDO" in estado_api
        bg = color_fila(item.get("estado_crono",""), item.get("estado_api",""))
        fg = COLOR_BLANCO if es_vendido else None

        # Aplicar color base a toda la fila
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx+1, len(COLS),
                                         bg, fg, False, 10))

        # Sobrescribir con rojo solo las celdas con cambios
        for j, campo in enumerate(CAMPOS):
            clave_cambio = f"{tab}:{pid}:{campo}"
            if campo in AUTO_CAMPOS and clave_cambio in cambios:
                requests.append(_formato_celdas(ws_id, fila_idx, j, fila_idx+1, j+1,
                                                 COLOR_CAMBIO, fg, False, 10))

    # Columna LINK: no wrap (es solo "Ver" ahora)


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

    # Altura de filas de datos (delgadas)
    if total_filas > 2:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "ROWS",
                          "startIndex": 2, "endIndex": total_filas},
                "properties": {"pixelSize": 21},
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

    # Ejecutar todo (con pausa entre chunks para no exceder quota)
    if requests:
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start+100]
            for intento in range(3):
                try:
                    ws.spreadsheet.batch_update({"requests": chunk})
                    break
                except Exception as e:
                    if "429" in str(e) and intento < 2:
                        print(f"    Rate limit, esperando {30*(intento+1)}s...")
                        time.sleep(30 * (intento + 1))
                    else:
                        raise
            time.sleep(5)


COLS_PRECIO = ["NOMBRE","DIRECCION","TIPO","FMI",
               "ESTADO CRONOGRAMA","ETAPA ACTUAL","PLAZO",
               "AREA m2","VALOR","LINK"]
CAMPOS_PRECIO = ["nombre","direccion","tipo","fmi",
                 "estado_crono","etapa_actual","plazo",
                 "area_m2","valor","link"]
ANCHOS_PRECIO = [220,280,130,140,160,250,80,80,150,80]


def _escribir_con_precio(ws, inmuebles):
    """Escribe la pestaña Con Precio con inmuebles que tienen valor."""
    filas = []
    filas.append(["INMUEBLES CON PRECIO - MEDELLIN Y ANTIOQUIA"] + [""] * (len(COLS_PRECIO) - 1))
    filas.append(COLS_PRECIO)

    for item in inmuebles:
        fila = []
        for campo in CAMPOS_PRECIO:
            if campo == "link":
                link = str(item.get("link", ""))
                fila.append(f'=HYPERLINK("{link}";"Ver")' if link else "")
            else:
                val = item.get(campo, "")
                fila.append(str(val) if val is not None else "")
        filas.append(fila)

    ws.clear()
    if filas:
        ws.update(filas, value_input_option="USER_ENTERED")

    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS_PRECIO))
    except: pass

    requests = []
    ws_id = ws.id

    # Desmerge
    try:
        sheet_meta = ws.spreadsheet.fetch_sheet_metadata()
        for s in sheet_meta.get("sheets", []):
            if s["properties"]["sheetId"] == ws_id:
                for m in s.get("merges", []):
                    requests.append({"unmergeCells": {"range": m}})
                break
    except: pass

    # Merge titulo
    requests.append({
        "mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, len(COLS_PRECIO)),
            "mergeType": "MERGE_ALL"
        }
    })
    requests.append(_formato_celdas(ws_id, 0, 0, 1, len(COLS_PRECIO),
                                     COLOR_HEADER, COLOR_BLANCO, True, 14))
    requests.append(_formato_celdas(ws_id, 1, 0, 2, len(COLS_PRECIO),
                                     COLOR_HDR2, COLOR_BLANCO, True, 10))

    # Filas de datos
    for i, item in enumerate(inmuebles):
        fila_idx = i + 2
        bg = color_fila(item.get("estado_crono",""), item.get("estado_api",""))
        estado_api = (item.get("estado_api","") or "").upper()
        fg = COLOR_BLANCO if "VENDIDO" in estado_api else None
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx+1, len(COLS_PRECIO),
                                         bg, fg, False, 10))

    # Anchos
    for j, ancho in enumerate(ANCHOS_PRECIO):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                          "startIndex": j, "endIndex": j+1},
                "properties": {"pixelSize": ancho},
                "fields": "pixelSize"
            }
        })

    # Altura filas
    if total_filas > 2:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "ROWS",
                          "startIndex": 2, "endIndex": total_filas},
                "properties": {"pixelSize": 21},
                "fields": "pixelSize"
            }
        })

    # Congelar + filtros
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": ws_id, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"
        }
    })
    requests.append({"clearBasicFilter": {"sheetId": ws_id}})
    requests.append({
        "setBasicFilter": {
            "filter": {"range": _rango(ws_id, 1, 0, total_filas, len(COLS_PRECIO))}
        }
    })

    if requests:
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start+100]
            for intento in range(3):
                try:
                    ws.spreadsheet.batch_update({"requests": chunk})
                    break
                except Exception as e:
                    if "429" in str(e) and intento < 2:
                        print(f"    Rate limit (precio), esperando {30*(intento+1)}s...")
                        time.sleep(30 * (intento + 1))
                    else:
                        raise
            time.sleep(5)


COLS_ELIM = ["ORIGEN","NOMBRE","DIRECCION","TIPO","FOLIO MATRICULA",
             "VALOR","FECHA ELIMINADO","LINK","ANOTACIONES"]
CAMPOS_ELIM = ["_origen","nombre","direccion","tipo","matricula",
               "valor","_fecha_eliminado","link","_anotaciones"]
ANCHOS_ELIM = [90,220,280,130,140,150,120,80,300]


def _escribir_eliminados(ws, todos_elim):
    """Escribe la pestaña Eliminados con todas las propiedades eliminadas."""
    # Leer datos existentes para preservar anotaciones manuales
    existing = ws.get_all_values()
    manual_por_link = {}
    if len(existing) > 2:
        idx_link_elim = CAMPOS_ELIM.index("link")
        for row in existing[2:]:
            link = row[idx_link_elim] if idx_link_elim < len(row) else ""
            if link:
                manual_por_link[link] = row

    filas = []
    filas.append(["PROPIEDADES ELIMINADAS DE LA PAGINA"] + [""] * (len(COLS_ELIM) - 1))
    filas.append(COLS_ELIM)

    for origen, elim in todos_elim:
        link = str(elim.get("link", ""))
        prev = manual_por_link.get(link)
        fila = []
        for col_idx, campo in enumerate(CAMPOS_ELIM):
            if campo == "_origen":
                fila.append(origen)
            elif campo == "_fecha_eliminado":
                fila.append(elim.get("_fecha_eliminado", "")[:10])
            elif campo == "_anotaciones":
                # Preservar anotacion manual
                if prev and col_idx < len(prev) and prev[col_idx]:
                    fila.append(prev[col_idx])
                else:
                    fila.append("")
            elif campo == "link":
                if link:
                    fila.append(f'=HYPERLINK("{link}";"Ver")')
                else:
                    fila.append("")
            else:
                fila.append(str(elim.get(campo, "")))
        filas.append(fila)

    ws.clear()
    if filas:
        ws.update(filas, value_input_option="USER_ENTERED")

    total_filas = len(filas)
    try:
        if ws.row_count < total_filas:
            ws.resize(rows=total_filas + 2, cols=len(COLS_ELIM))
    except: pass

    # Formato
    requests = []
    ws_id = ws.id

    # Merge titulo
    requests.append({
        "mergeCells": {
            "range": _rango(ws_id, 0, 0, 1, len(COLS_ELIM)),
            "mergeType": "MERGE_ALL"
        }
    })
    requests.append(_formato_celdas(ws_id, 0, 0, 1, len(COLS_ELIM),
                                     COLOR_ELIM_HDR, COLOR_BLANCO, True, 13))

    # Encabezados
    requests.append(_formato_celdas(ws_id, 1, 0, 2, len(COLS_ELIM),
                                     COLOR_HEADER, COLOR_BLANCO, True, 10))

    # Filas de datos
    for i in range(len(todos_elim)):
        fila_idx = i + 2
        requests.append(_formato_celdas(ws_id, fila_idx, 0, fila_idx + 1, len(COLS_ELIM),
                                         COLOR_ELIMINADO, None, False, 10))

    # Wrap text en columna LINK
    idx_link_elim = CAMPOS_ELIM.index("link")
    requests.append({
        "repeatCell": {
            "range": _rango(ws_id, 2, idx_link_elim, total_filas, idx_link_elim + 1),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy"
        }
    })

    # Anchos
    for j, ancho in enumerate(ANCHOS_ELIM):
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
                "range": _rango(ws_id, 1, 0, total_filas, len(COLS_ELIM))
            }
        }
    })

    if requests:
        for chunk_start in range(0, len(requests), 100):
            chunk = requests[chunk_start:chunk_start+100]
            for intento in range(3):
                try:
                    ws.spreadsheet.batch_update({"requests": chunk})
                    break
                except Exception as e:
                    if "429" in str(e) and intento < 2:
                        print(f"    Rate limit (eliminados), esperando {30*(intento+1)}s...")
                        time.sleep(30 * (intento + 1))
                    else:
                        raise
            time.sleep(5)


def _escribir_leyenda(ws):
    filas = [
        ["Leyenda de colores"],
        ["Proximo Subasta"],
        ["Con cronograma - En proceso"],
        ["Manifestacion Abierta"],
        ["Vendido"],
    ]
    ws.update(filas)
    ws_id = ws.id
    reqs = [
        _formato_celdas(ws_id, 0, 0, 1, 1, None, None, True, 12),
        _formato_celdas(ws_id, 1, 0, 2, 1, COLOR_SUBASTA, None, False, 10),
        _formato_celdas(ws_id, 2, 0, 3, 1, COLOR_CRONO, None, False, 10),
        _formato_celdas(ws_id, 3, 0, 4, 1, COLOR_MANIF, None, False, 10),
        _formato_celdas(ws_id, 4, 0, 5, 1, COLOR_VENDIDO, COLOR_BLANCO, False, 10),
        {"updateDimensionProperties": {
            "range": {"sheetId": ws_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 350}, "fields": "pixelSize"
        }}
    ]
    for intento in range(3):
        try:
            ws.spreadsheet.batch_update({"requests": reqs})
            break
        except Exception as e:
            if "429" in str(e) and intento < 2:
                print(f"    Rate limit (leyenda), esperando {30*(intento+1)}s...")
                time.sleep(30 * (intento + 1))
            else:
                raise
    time.sleep(5)


def _escribir_info(ws, n_med, n_ant, n_bel=0, n_pin=0):
    COL = timezone(timedelta(hours=-5))
    ahora = datetime.now(COL).strftime("%Y-%m-%d %H:%M")
    filas = [
        ["Ultima actualizacion", ahora],
        ["Medellin", f"{n_med} inmuebles"],
        ["Antioquia (sin Med.)", f"{n_ant} inmuebles"],
        ["Bello", f"{n_bel} inmuebles"],
        ["La Pintada", f"{n_pin} inmuebles"],
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
    for intento in range(3):
        try:
            ws.spreadsheet.batch_update({"requests": reqs})
            break
        except Exception as e:
            if "429" in str(e) and intento < 2:
                print(f"    Rate limit (info), esperando {30*(intento+1)}s...")
                time.sleep(30 * (intento + 1))
            else:
                raise


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
