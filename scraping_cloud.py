# -*- coding: utf-8 -*-
"""
Version NUBE del scraping - corre en GitHub Actions.
Usa Playwright para datos completos (nombre edificio, direccion, cronograma).
Detecta cambios, inmuebles nuevos/eliminados y envia alerta por email.
"""

import urllib.request, urllib.parse, json, re, os, smtplib, time
from datetime import datetime, timedelta, timezone

COL_TZ = timezone(timedelta(hours=-5))
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright

API_BASE   = "https://dev.activosporcolombia.com/net/api"
SITE_BASE  = "https://activosporcolombia.com"
CITY_ID_MEDELLIN = 5001
CITY_ID_BELLO    = 5088
CITY_ID_PINTADA  = 5390
DIAS_ROJO  = 2

EMAIL_REMITENTE    = os.environ.get("EMAIL_REMITENTE", "")
EMAIL_CONTRASENA   = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO", "")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATOS_FILE   = os.path.join(SCRIPT_DIR, "datos_anteriores.json")
CAMBIOS_FILE = os.path.join(SCRIPT_DIR, "registro_cambios.json")

# Re-scrape completo cada N horas (para detectar cambios de cronograma)
RESCRAPE_HORAS = 16
RESCRAPE_FILE = os.path.join(SCRIPT_DIR, "ultimo_rescrape.txt")

TIPOS = {
    1:"Apartaestudio",2:"Apartamento",3:"Bodega",4:"Casa",5:"Casa Lote",
    6:"Casa Recreo",7:"Centro Comercial",11:"Consultorio",13:"Hotel/Motel",
    14:"Edificio",16:"Finca",17:"Garaje",21:"Local Comercial",22:"Lote",
    24:"Lote con Construccion",29:"Oficina",30:"Parqueadero",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. API
# ═══════════════════════════════════════════════════════════════════════════════

def llamar_api(endpoint, params=None, reintentos=3):
    url = f"{API_BASE}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for intento in range(reintentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if intento < reintentos - 1: time.sleep(3)
            else: raise


def obtener_propiedades(filtro):
    todos, pagina = [], 1
    vistos = set()
    while True:
        params = {"query":"","page":pagina,"limit":50,"sort_by":"date_desc"}
        params.update(filtro)
        resp = llamar_api("/v1/properties/search", params)
        data = resp["data"]
        props = data.get("properties", [])
        if not props: break
        for p in props:
            pid = p["id"]
            if pid not in vistos:
                vistos.add(pid)
                todos.append(p)
        if pagina >= data.get("total_pages", 1): break
        pagina += 1
    return todos


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PLAYWRIGHT — detalle completo de cada propiedad
# ═══════════════════════════════════════════════════════════════════════════════

def construir_url(p):
    itype = (p.get("item_type") or "").upper()
    pid = p["id"]
    ref = (p.get("reference") or "").lower()
    slug = re.sub(r"[^a-z0-9\-]", "", ref.replace(" - ","-").replace(" ","-"))
    if itype == "UNIDAD_INMOBILIARIA":
        return f"{SITE_BASE}/es/unidad-inmobiliaria/{pid}/{slug}"
    return f"{SITE_BASE}/es/inmueble/{pid}/{slug}"


def necesita_rescrape_completo():
    """Retorna True si toca re-scrapear todo (cada RESCRAPE_HORAS)."""
    try:
        with open(RESCRAPE_FILE, "r") as f:
            ultimo = datetime.fromisoformat(f.read().strip())
        return (datetime.now(COL_TZ) - ultimo).total_seconds() > RESCRAPE_HORAS * 3600
    except Exception:
        return True

def marcar_rescrape():
    with open(RESCRAPE_FILE, "w") as f:
        f.write(datetime.now(COL_TZ).isoformat())


def scrape_detalles(propiedades, etiqueta="", tab="", forzar_todo=False):
    """Scrapea solo propiedades nuevas o con cambios en API. Reutiliza cache para el resto."""
    anteriores = cargar_json(DATOS_FILE)
    resultados = {}
    por_scrapear = []

    if forzar_todo:
        por_scrapear = list(propiedades)
        print(f"  {etiqueta}Re-scrape completo: {len(por_scrapear)} propiedades")
    else:
        for prop in propiedades:
            pid = str(prop["id"])
            base = f"{tab}:{pid}" if tab else pid
            prev = anteriores.get(base, {})

            # Detectar si algo cambio en la API (precio, estado)
            api_valor = formatear_precio(prop.get("base_sale_price") or prop.get("commercial_appraisal"))
            prev_valor = prev.get("valor", "")

            cambio_api = api_valor != prev_valor

            es_nuevo = not prev or prev.get("_eliminado") == "true"
            sin_nombre = not prev.get("nombre") or prev.get("nombre") == "Sin nombre"

            if es_nuevo or sin_nombre or cambio_api:
                por_scrapear.append(prop)
            else:
                # Reusar datos del cache
                resultados[pid] = {
                    "nombre": prev.get("nombre",""),
                    "direccion": prev.get("direccion",""),
                    "barrio": "",
                    "estado_crono": prev.get("estado_crono",""),
                    "etapa_actual": prev.get("etapa_actual",""),
                    "plazo": prev.get("plazo","X"),
                }

    print(f"  {etiqueta}Cache: {len(resultados)} | Por scrapear: {len(por_scrapear)}")

    if not por_scrapear:
        return resultados

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ).new_page()

        total = len(por_scrapear)
        for i, prop in enumerate(por_scrapear, 1):
            pid = str(prop["id"])
            url = construir_url(prop)
            ref = (prop.get("reference") or "")[:50]
            print(f"  {etiqueta}[{i}/{total}] {ref}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(3000)
                lineas = [l.strip() for l in page.inner_text("body").split("\n") if l.strip()]
            except Exception:
                lineas = []

            direccion, barrio = "", ""
            for l in lineas:
                if l.startswith("Direcci"):
                    direccion = l.split(":", 1)[-1].strip()
                if l.startswith("Barrio:"):
                    barrio = l.split(":", 1)[-1].strip()

            nombre = extraer_nombre_edificio(lineas, barrio)
            estado_crono, etapa_actual, plazo = extraer_cronograma(lineas)

            resultados[pid] = {
                "nombre": nombre, "direccion": direccion, "barrio": barrio,
                "estado_crono": estado_crono, "etapa_actual": etapa_actual, "plazo": plazo,
            }

        browser.close()
    return resultados


def extraer_nombre_edificio(lineas, barrio_fallback):
    texto = " ".join(lineas)
    patrones = [
        r"(?:Edificio|Ed\.)\s+([A-Z\u00C0-\u00DC][A-Za-z\u00C0-\u00FC\u00F1\u00D1\s]+?)(?:\s*,|\s*\.|ubicad|situad)",
        r"(?:Conjunto Residencial|Conj\.)\s+([A-Z\u00C0-\u00DC][A-Za-z\u00C0-\u00FC\u00F1\u00D1\s]+?)(?:\s*,|\s*\.|ubicad|situad)",
        r"(?:Centro Comercial|CC)\s+([A-Z\u00C0-\u00DC][A-Za-z\u00C0-\u00FC\u00F1\u00D1\s]+?)(?:\s*,|\s*\.|ubicad|situad)",
        r"(?:Parque Empresarial|Torre|Local)\s+([A-Z\u00C0-\u00DC][A-Za-z\u00C0-\u00FC\u00F1\u00D1\s]+?)(?:\s*,|\s*\.|ubicad|situad)",
    ]
    for pat in patrones:
        m = re.search(pat, texto)
        if m:
            nombre = m.group(1).strip()
            if 3 < len(nombre) < 50:
                return nombre
    return barrio_fallback or "Sin nombre"


def extraer_cronograma(lineas):
    """
    Parsea el cronograma de la pagina. Estructura de cada bloque:
      NOMBRE ETAPA / dia_inicio / DIA_SEMANA / [hasta / dia_fin / DIA_SEMANA] /
      mes. año / N dias / FINALIZADO|ACTIVO|PROXIMO
    Retorna: (estado_crono, etapa_actual, plazo)
    """
    MESES_CORTOS = {"ene":"01","feb":"02","mar":"03","abr":"04","may":"05",
                    "jun":"06","jul":"07","ago":"08","sep":"09","oct":"10",
                    "nov":"11","dic":"12"}
    MESES_LARGOS = {"enero":"01","febrero":"02","marzo":"03","abril":"04","mayo":"05",
                    "junio":"06","julio":"07","agosto":"08","septiembre":"09",
                    "octubre":"10","noviembre":"11","diciembre":"12"}
    ESTADOS = {"FINALIZADO", "ACTIVO", "PRÓXIMO", "PROXIMO"}

    # Primero verificar si hay cronograma — si lo hay, parsearlo siempre
    tiene_cronograma = any("ronograma" in l for l in lineas)

    if not tiene_cronograma:
        # Solo sin cronograma: buscar "manifestacion abierta" como estado
        for l in lineas:
            if "manifestaci" in l.lower() and "abierta" in l.lower():
                return "Manifestacion Abierta", "", "X"
        return "Manifestacion Abierta", "", "X"

    # Parsear bloques de etapas desde "Fechas del Proceso"
    inicio = None
    for i, l in enumerate(lineas):
        if "Fechas del Proceso" in l:
            inicio = i + 1
            break
    if inicio is None:
        for i, l in enumerate(lineas):
            if "ronograma" in l:
                inicio = i + 1
                break
    if inicio is None:
        return "Con cronograma", "", "X"

    # Recorrer lineas y armar bloques de etapas
    bloques = []
    bloque_actual = None
    ETAPAS_KEYWORDS = ["PUBLICACI","REGISTRO","DILIGENCIA","FINANCIERO",
                       "CUPONES","SERIEDAD","VALIDACI","SUBASTA","EXPEDICI"]

    for i in range(inicio, len(lineas)):
        l = lineas[i].strip()
        if not l:
            continue
        # Terminar al salir de la zona de cronograma
        if any(x in l for x in ["Aplican t", "Descripci", "Ubicaci", "Galeria"]):
            break

        lu = l.upper()
        # Detectar inicio de nueva etapa
        es_etapa = any(e in lu for e in ETAPAS_KEYWORDS) and len(l) < 120
        if es_etapa:
            if bloque_actual:
                bloques.append(bloque_actual)
            bloque_actual = {"nombre": l, "lineas_raw": [], "estado": "",
                             "dia_fin": "", "mes_num": ""}
            continue

        if bloque_actual is None:
            continue

        bloque_actual["lineas_raw"].append(l)

        # Detectar estado
        if lu in ESTADOS or lu == "PRÓXIMO":
            bloque_actual["estado"] = "ACTIVO" if lu == "ACTIVO" else (
                "FINALIZADO" if lu == "FINALIZADO" else "PROXIMO")

        # Detectar mes/año (ej: "abr. 2026" o "mar. a abr. 2026")
        m_mes = re.search(r"(\w{3})\.\s*(?:a\s+\w{3}\.\s*)?(\d{4})", l)
        if m_mes:
            # Tomar el ultimo mes mencionado (ej: "mar. a abr. 2026" -> abr)
            todos_meses = re.findall(r"(\w{3})\.", l)
            ultimo_mes = todos_meses[-1] if todos_meses else m_mes.group(1)
            bloque_actual["mes_num"] = MESES_CORTOS.get(ultimo_mes.lower(), "")

    if bloque_actual:
        bloques.append(bloque_actual)

    # Para cada bloque, extraer dia_fin (ultimo numero antes del estado/mes)
    for bloque in bloques:
        numeros = []
        for rl in bloque["lineas_raw"]:
            if re.fullmatch(r"\d{1,2}", rl.strip()):
                numeros.append(rl.strip())
        # dia_fin = ultimo numero encontrado (si hay "hasta X", X es el fin)
        if numeros:
            bloque["dia_fin"] = numeros[-1]
        elif len(numeros) == 0:
            # Etapa de un solo dia, buscar el unico numero
            for rl in bloque["lineas_raw"]:
                m = re.search(r"^(\d{1,2})$", rl.strip())
                if m:
                    bloque["dia_fin"] = m.group(1)
                    break

    # Buscar etapa ACTIVO; si no hay, tomar la primera PROXIMO
    etapa_activa = None
    for b in bloques:
        if b["estado"] == "ACTIVO":
            etapa_activa = b
    if etapa_activa is None:
        for b in bloques:
            if b["estado"] == "PROXIMO":
                etapa_activa = b
                break

    if etapa_activa is None:
        # Todas las etapas son FINALIZADO
        if bloques and all(b["estado"] == "FINALIZADO" for b in bloques):
            return "Con cronograma", "FINALIZADO", "X"
        return "Con cronograma", "", "X"

    # Armar plazo con dia_fin/mes de la etapa activa
    plazo = "X"
    if etapa_activa["dia_fin"] and etapa_activa["mes_num"]:
        plazo = f"{etapa_activa['dia_fin'].zfill(2)}/{etapa_activa['mes_num']}"
    elif etapa_activa["dia_fin"]:
        plazo = etapa_activa["dia_fin"]

    return "Con cronograma", etapa_activa["nombre"], plazo


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PROCESAR — combinar API + Playwright
# ═══════════════════════════════════════════════════════════════════════════════

def formatear_precio(v):
    if not v: return "X"
    try: return "$ {:,.0f}".format(float(v)).replace(",",".")
    except: return str(v)


def procesar(props_api, detalles):
    resultado = []
    for p in props_api:
        pid = str(p["id"])
        det = detalles.get(pid, {})
        estado = p.get("state") or {}
        estado_cod = estado.get("code","") if isinstance(estado, dict) else ""

        etapa = det.get("etapa_actual","")
        if estado_cod.upper() == "VENDIDO":
            etapa = "VENDIDO"

        resultado.append({
            "_id": pid,
            "nombre":       det.get("nombre") or det.get("barrio") or p.get("reference",""),
            "direccion":    det.get("direccion",""),
            "tipo":         TIPOS.get(p.get("property_type_id"), ""),
            "matricula":    p.get("matricula_number", ""),
            "area_m2":      p.get("built_area") or p.get("lot_area") or "",
            "valor":        formatear_precio(p.get("base_sale_price") or p.get("commercial_appraisal")),
            "estado_crono": det.get("estado_crono",""),
            "etapa_actual": etapa,
            "plazo":        det.get("plazo","X"),
            "estado_api":   estado_cod,
            "link":         construir_url(p),
            "fmi":          p.get("matricula_number", ""),
        })
    return resultado


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DETECCION DE CAMBIOS + NUEVOS + ELIMINADOS
# ═══════════════════════════════════════════════════════════════════════════════

CAMPOS = ["valor","estado_crono","etapa_actual","plazo"]
CAMPOS_GUARDAR = ["nombre","direccion","tipo","matricula","area_m2",
                  "valor","estado_crono","etapa_actual","plazo","link","fmi"]

def cargar_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def guardar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detectar_cambios(inmuebles, tab):
    anteriores = cargar_json(DATOS_FILE)
    registro = cargar_json(CAMBIOS_FILE)
    ahora = datetime.now(COL_TZ).isoformat()
    limite = (datetime.now(COL_TZ) - timedelta(days=DIAS_ROJO)).isoformat()
    resumen = []
    cambios_ahora = {}
    ids_nuevos = set()

    ids_actuales = set()

    for item in inmuebles:
        pid = item["_id"]
        base = f"{tab}:{pid}"
        ids_actuales.add(base)
        prev = anteriores.get(base, {})

        # Inmueble NUEVO (o que vuelve despues de ser eliminado)
        es_nuevo = not prev or prev.get("_eliminado") == "true"
        if es_nuevo:
            ids_nuevos.add(pid)
            resumen.append({
                "tipo": "NUEVO", "tab": tab.upper(),
                "nombre": item.get("nombre","?"),
                "tipo_inmueble": item.get("tipo",""),
                "valor": item.get("valor",""),
                "link": item.get("link",""),
                "fmi": item.get("fmi",""),
            })
            for campo in CAMPOS:
                registro[f"{base}:{campo}"] = ahora
                cambios_ahora[f"{base}:{campo}"] = True
            prev = {}

        # Cambios en campos
        for campo in CAMPOS:
            nuevo = str(item.get(campo,""))
            viejo = str(prev.get(campo,""))
            if prev and nuevo != viejo:
                registro[f"{base}:{campo}"] = ahora
                cambios_ahora[f"{base}:{campo}"] = True
                resumen.append({
                    "tipo": "CAMBIO", "tab": tab.upper(),
                    "nombre": item.get("nombre","?"),
                    "campo": campo, "antes": viejo, "ahora": nuevo,
                    "link": item.get("link",""),
                    "fmi": item.get("fmi",""),
                })

        anteriores[base] = {c: str(item.get(c,"")) for c in CAMPOS_GUARDAR}

    # Inmuebles ELIMINADOS (marcar, no borrar)
    for clave in list(anteriores.keys()):
        if clave.startswith(f"{tab}:") and clave not in ids_actuales:
            datos_viejos = anteriores[clave]
            if datos_viejos.get("_eliminado") != "true":
                resumen.append({
                    "tipo": "ELIMINADO", "tab": tab.upper(),
                    "nombre": datos_viejos.get("nombre", "?"),
                    "tipo_inmueble": datos_viejos.get("tipo", ""),
                    "direccion": datos_viejos.get("direccion", ""),
                    "matricula": datos_viejos.get("matricula", ""),
                    "area_m2": datos_viejos.get("area_m2", ""),
                    "valor": datos_viejos.get("valor", ""),
                    "estado_crono": datos_viejos.get("estado_crono", ""),
                    "etapa_actual": datos_viejos.get("etapa_actual", ""),
                    "plazo": datos_viejos.get("plazo", ""),
                    "link": datos_viejos.get("link", ""),
                    "fmi": datos_viejos.get("fmi", ""),
                })
                datos_viejos["_eliminado"] = "true"
                datos_viejos["_fecha_eliminado"] = ahora
                anteriores[clave] = datos_viejos

    # Limpiar cambios viejos
    for k in list(registro.keys()):
        if registro[k] < limite:
            del registro[k]

    guardar_json(DATOS_FILE, anteriores)
    guardar_json(CAMBIOS_FILE, registro)
    return cambios_ahora, resumen, ids_nuevos


# ═══════════════════════════════════════════════════════════════════════════════
# 5. EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

NOMBRES_CAMPO = {
    "nombre": "Nombre", "direccion": "Direccion", "tipo": "Tipo",
    "valor": "Valor", "estado_crono": "Estado", "etapa_actual": "Etapa",
    "plazo": "Plazo",
}

def enviar_email(resumen):
    if not EMAIL_REMITENTE or not EMAIL_CONTRASENA or not resumen:
        return
    try:
        nuevos = [c for c in resumen if c["tipo"] == "NUEVO"]
        eliminados = [c for c in resumen if c["tipo"] == "ELIMINADO"]
        cambios = [c for c in resumen if c["tipo"] == "CAMBIO"]

        tabs_orden = []
        for c in resumen:
            if c["tab"] not in tabs_orden:
                tabs_orden.append(c["tab"])

        fecha = datetime.now(COL_TZ).strftime("%d/%m/%Y %H:%M")

        partes = []
        if nuevos: partes.append(f"{len(nuevos)} nuevos")
        if eliminados: partes.append(f"{len(eliminados)} eliminados")
        if cambios: partes.append(f"{len(cambios)} cambios")
        asunto = f"Activos Colombia - {', '.join(partes)}"

        ESTILO_TABLA = (
            "border-collapse:collapse;width:100%;font-family:Arial,sans-serif;"
            "font-size:13px;margin-bottom:20px;"
        )
        ESTILO_TH = (
            "background-color:#1F3864;color:white;padding:8px 12px;"
            "text-align:left;border:1px solid #ccc;"
        )
        ESTILO_TD = "padding:8px 12px;border:1px solid #ddd;"
        ESTILO_TD_ALT = ESTILO_TD + "background-color:#f8f8f8;"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;">
        <h2 style="color:#1F3864;margin-bottom:5px;">Alerta - Activos por Colombia</h2>
        <p style="color:#666;margin-top:0;">{fecha}</p>
        """

        if nuevos:
            for tab in tabs_orden:
                tab_nuevos = [c for c in nuevos if c["tab"] == tab]
                if not tab_nuevos: continue
                tab_nombre = {"MED":"Medellin","ANT":"Antioquia","BEL":"Bello","PIN":"La Pintada"}.get(tab, tab)
                html += f'<h3 style="color:#2E7D32;">Nuevos - {tab_nombre} ({len(tab_nuevos)})</h3>'
                html += f'<table style="{ESTILO_TABLA}">'
                html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
                html += f'<th style="{ESTILO_TH}">Tipo</th>'
                html += f'<th style="{ESTILO_TH}">Valor</th>'
                html += f'<th style="{ESTILO_TH}">FMI</th>'
                html += f'<th style="{ESTILO_TH}">Link</th></tr>'
                for i, c in enumerate(tab_nuevos):
                    td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                    link = c.get("link","")
                    link_html = f'<a href="{link}" style="color:#1565C0;">Ver</a>' if link else ""
                    html += f'<tr><td style="{td}">{c["nombre"]}</td>'
                    html += f'<td style="{td}">{c["tipo_inmueble"]}</td>'
                    html += f'<td style="{td}">{c["valor"]}</td>'
                    html += f'<td style="{td}">{c.get("fmi","")}</td>'
                    html += f'<td style="{td}">{link_html}</td></tr>'
                html += '</table>'

        if cambios:
            for tab in tabs_orden:
                tab_cambios = [c for c in cambios if c["tab"] == tab]
                if not tab_cambios: continue
                tab_nombre = {"MED":"Medellin","ANT":"Antioquia","BEL":"Bello","PIN":"La Pintada"}.get(tab, tab)
                html += f'<h3 style="color:#1565C0;">Cambios - {tab_nombre} ({len(tab_cambios)})</h3>'
                html += f'<table style="{ESTILO_TABLA}">'
                html += f'<tr><th style="{ESTILO_TH}">Inmueble</th>'
                html += f'<th style="{ESTILO_TH}">Campo</th>'
                html += f'<th style="{ESTILO_TH}">Antes</th>'
                html += f'<th style="{ESTILO_TH}">Ahora</th>'
                html += f'<th style="{ESTILO_TH}">FMI</th>'
                html += f'<th style="{ESTILO_TH}">Link</th></tr>'
                for i, c in enumerate(tab_cambios):
                    td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                    campo_nombre = NOMBRES_CAMPO.get(c["campo"], c["campo"])
                    link = c.get("link","")
                    link_html = f'<a href="{link}" style="color:#1565C0;">Ver</a>' if link else ""
                    html += f'<tr><td style="{td}">{c["nombre"]}</td>'
                    html += f'<td style="{td}">{campo_nombre}</td>'
                    html += f'<td style="{td}">{c["antes"] or "-"}</td>'
                    html += f'<td style="{td}">{c["ahora"] or "-"}</td>'
                    html += f'<td style="{td}">{c.get("fmi","")}</td>'
                    html += f'<td style="{td}">{link_html}</td></tr>'
                html += '</table>'

        if eliminados:
            for tab in tabs_orden:
                tab_elim = [c for c in eliminados if c["tab"] == tab]
                if not tab_elim: continue
                tab_nombre = {"MED":"Medellin","ANT":"Antioquia","BEL":"Bello","PIN":"La Pintada"}.get(tab, tab)
                html += f'<h3 style="color:#C62828;">Eliminados - {tab_nombre} ({len(tab_elim)})</h3>'
                for c in tab_elim:
                    link = c.get("link","")
                    nombre_display = f'<a href="{link}">{c["nombre"]}</a>' if link else c["nombre"]
                    html += f'<table style="{ESTILO_TABLA}">'
                    html += f'<tr><th style="{ESTILO_TH}" colspan="2">{nombre_display}</th></tr>'
                    campos_elim = [
                        ("Tipo", c.get("tipo_inmueble","")),
                        ("Direccion", c.get("direccion","")),
                        ("Folio Matricula", c.get("matricula","")),
                        ("FMI", c.get("fmi","")),
                        ("Area m2", c.get("area_m2","")),
                        ("Valor", c.get("valor","")),
                        ("Estado Cronograma", c.get("estado_crono","")),
                        ("Etapa Actual", c.get("etapa_actual","")),
                        ("Plazo", c.get("plazo","")),
                    ]
                    for i, (label, val) in enumerate(campos_elim):
                        if val:
                            td = ESTILO_TD_ALT if i % 2 else ESTILO_TD
                            html += f'<tr><td style="{td}font-weight:bold;width:160px;">{label}</td>'
                            html += f'<td style="{td}">{val}</td></tr>'
                    if link:
                        html += f'<tr><td style="{ESTILO_TD}font-weight:bold;">Link</td>'
                        html += f'<td style="{ESTILO_TD}"><a href="{link}">{link}</a></td></tr>'
                    html += '</table><br>'

        html += """
        <p style="color:#999;font-size:12px;margin-top:20px;">
        Tabla actualizada en Google Sheets<br>
        Fuente: activosporcolombia.com
        </p></div>
        """

        msg = MIMEMultipart()
        msg["From"] = EMAIL_REMITENTE
        msg["To"] = EMAIL_DESTINATARIO
        msg["Subject"] = asunto
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_REMITENTE, EMAIL_CONTRASENA)
            s.send_message(msg)
        print(f"Email enviado a {EMAIL_DESTINATARIO}")
    except Exception as e:
        print(f"Error email: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("="*55)
    print("  Scraping NUBE - activosporcolombia.com")
    print("  Medellin + Antioquia (datos completos)")
    print("="*55)

    # Descargar listas
    print("\n[1/5] Descargando Medellin...")
    props_med = obtener_propiedades({"city_ids": CITY_ID_MEDELLIN})
    print(f"  {len(props_med)} propiedades")

    print("\n[2/5] Descargando Antioquia (sin Medellin)...")
    todas = obtener_propiedades({})
    props_ant = [p for p in todas if p.get("city_id") and 5000<=p["city_id"]<=5999 and p["city_id"]!=CITY_ID_MEDELLIN]
    print(f"  {len(props_ant)} propiedades")

    # Scrape detalles con Playwright (optimizado: solo nuevos/cambiados, rescrape completo cada 16h)
    forzar = necesita_rescrape_completo()
    if forzar:
        print("\n  >> Re-scrape completo (cada 16h)")
    else:
        print("\n  >> Scrape incremental (solo nuevos/cambiados)")

    print("\n[3/5] Visitando paginas de Medellin...")
    det_med = scrape_detalles(props_med, "MED ", tab="med", forzar_todo=forzar)

    print("\n[4/5] Visitando paginas de Antioquia...")
    det_ant = scrape_detalles(props_ant, "ANT ", tab="ant", forzar_todo=forzar)

    if forzar:
        marcar_rescrape()

    # Procesar
    med = procesar(props_med, det_med)
    ant = procesar(props_ant, det_ant)

    # Detectar cambios
    print("\n[5/5] Detectando cambios y actualizando Google Sheets...")
    cm, rm, nm = detectar_cambios(med, "med")
    ca, ra, na = detectar_cambios(ant, "ant")
    todos_cambios = rm + ra

    # Ordenar: nuevos arriba
    med.sort(key=lambda x: (0 if str(x["_id"]) in nm else 1))
    ant.sort(key=lambda x: (0 if str(x["_id"]) in na else 1))

    if todos_cambios:
        print(f"\n  *** {len(todos_cambios)} ALERTAS ***")
        for c in todos_cambios[:15]:
            if c["tipo"] == "CAMBIO":
                print(f"    [{c['tab']}] {c['nombre']}: {c['campo']} {c['antes']} -> {c['ahora']}")
            else:
                print(f"    {c['tipo']} [{c['tab']}] {c['nombre']}")
        if len(todos_cambios) > 15: print(f"    ... y {len(todos_cambios)-15} mas")
    else:
        print("  Sin cambios")

    # Recoger eliminados del historial
    anteriores = cargar_json(DATOS_FILE)
    elim_med = [v for k, v in anteriores.items() if k.startswith("med:") and v.get("_eliminado") == "true"]
    elim_ant = [v for k, v in anteriores.items() if k.startswith("ant:") and v.get("_eliminado") == "true"]

    # Google Sheets
    from sheets_sync import sync_to_sheets
    sync_to_sheets(med, ant, cm, ca, eliminados_med=elim_med, eliminados_ant=elim_ant)

    # Email
    if todos_cambios:
        enviar_email(todos_cambios)

    print(f"\nListo: {len(med)} Medellin + {len(ant)} Antioquia")
