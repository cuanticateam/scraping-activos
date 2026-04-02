# -*- coding: utf-8 -*-
"""
Version NUBE del scraping - corre en GitHub Actions.
Usa Playwright para datos completos (nombre edificio, direccion, cronograma).
Detecta cambios, inmuebles nuevos/eliminados y envia alerta por email.
"""

import urllib.request, urllib.parse, json, re, os, smtplib, time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright

API_BASE   = "https://dev.activosporcolombia.com/net/api"
SITE_BASE  = "https://activosporcolombia.com"
CITY_ID_MEDELLIN = 5001
DIAS_ROJO  = 2

EMAIL_REMITENTE    = os.environ.get("EMAIL_REMITENTE", "")
EMAIL_CONTRASENA   = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO", "")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATOS_FILE   = os.path.join(SCRIPT_DIR, "datos_anteriores.json")
CAMBIOS_FILE = os.path.join(SCRIPT_DIR, "registro_cambios.json")

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
    while True:
        params = {"query":"","page":pagina,"limit":50,"sort_by":"date_desc"}
        params.update(filtro)
        resp = llamar_api("/v1/properties/search", params)
        data = resp["data"]
        props = data.get("properties", [])
        if not props: break
        todos.extend(props)
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


def scrape_detalles(propiedades, etiqueta=""):
    resultados = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ).new_page()

        total = len(propiedades)
        for i, prop in enumerate(propiedades, 1):
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
    ETAPAS = ["PUBLICACI","REGISTRO","DILIGENCIA","FINANCIERO",
              "CUPONES","SERIEDAD","VALIDACI","SUBASTA"]

    for l in lineas:
        if "manifestaci" in l.lower() and "abierta" in l.lower():
            return "Manifestacion Abierta", "", "X"

    if not any("ronograma" in l for l in lineas):
        return "Manifestacion Abierta", "", "X"

    etapa_activa = ""
    for i, l in enumerate(lineas):
        lu = l.upper()
        if any(e in lu for e in ETAPAS):
            contexto = " ".join(lineas[max(0,i-2):i+6]).upper()
            if "ACTIVO" in contexto:
                etapa_activa = l.strip()
                break

    plazo = "X"
    for l in lineas:
        m = re.search(r"Fin:\s*\w+,\s*(\d+)\s+de\s+(\w+)\s+de\s+(\d{4})", l)
        if m:
            dia = m.group(1).zfill(2)
            meses = {"enero":"01","febrero":"02","marzo":"03","abril":"04","mayo":"05",
                     "junio":"06","julio":"07","agosto":"08","septiembre":"09",
                     "octubre":"10","noviembre":"11","diciembre":"12"}
            mes = meses.get(m.group(2).lower(), "??")
            plazo = f"{dia}/{mes}"
            break

    if etapa_activa or plazo != "X":
        return "Con cronograma", etapa_activa, plazo
    return "Manifestacion Abierta", "", "X"


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

        resultado.append({
            "_id": pid,
            "nombre":       det.get("nombre") or det.get("barrio") or p.get("reference",""),
            "direccion":    det.get("direccion",""),
            "tipo":         TIPOS.get(p.get("property_type_id"), ""),
            "area_m2":      p.get("built_area") or p.get("lot_area") or "",
            "valor":        formatear_precio(p.get("base_sale_price") or p.get("commercial_appraisal")),
            "estado_crono": det.get("estado_crono",""),
            "etapa_actual": det.get("etapa_actual",""),
            "plazo":        det.get("plazo","X"),
            "estado_api":   estado_cod,
            "link":         construir_url(p),
        })
    return resultado


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DETECCION DE CAMBIOS + NUEVOS + ELIMINADOS
# ═══════════════════════════════════════════════════════════════════════════════

CAMPOS = ["nombre","direccion","tipo","valor","estado_crono","etapa_actual","plazo"]

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
    ahora = datetime.now().isoformat()
    limite = (datetime.now() - timedelta(days=DIAS_ROJO)).isoformat()
    resumen = []

    ids_actuales = set()

    for item in inmuebles:
        pid = item["_id"]
        base = f"{tab}:{pid}"
        ids_actuales.add(base)
        prev = anteriores.get(base, {})

        # Inmueble NUEVO
        if not prev:
            resumen.append(f"NUEVO [{tab.upper()}] {item.get('nombre','?')} - {item.get('tipo','')} - {item.get('valor','')}")
            # Marcar todas las celdas como cambio para que salgan en rojo
            for campo in CAMPOS:
                registro[f"{base}:{campo}"] = ahora

        # Cambios en campos
        for campo in CAMPOS:
            nuevo = str(item.get(campo,""))
            viejo = str(prev.get(campo,""))
            if prev and nuevo != viejo:
                registro[f"{base}:{campo}"] = ahora
                resumen.append(
                    f"CAMBIO [{tab.upper()}] {item.get('nombre','?')}: "
                    f"{campo} cambio de '{viejo}' a '{nuevo}'"
                )

        anteriores[base] = {c: str(item.get(c,"")) for c in CAMPOS}

    # Inmuebles ELIMINADOS
    for clave in list(anteriores.keys()):
        if clave.startswith(f"{tab}:") and clave not in ids_actuales:
            nombre_viejo = anteriores[clave].get("nombre", "?")
            tipo_viejo = anteriores[clave].get("tipo", "")
            resumen.append(f"ELIMINADO [{tab.upper()}] {nombre_viejo} - {tipo_viejo}")
            del anteriores[clave]

    # Limpiar cambios viejos
    for k in list(registro.keys()):
        if registro[k] < limite:
            del registro[k]

    guardar_json(DATOS_FILE, anteriores)
    guardar_json(CAMBIOS_FILE, registro)
    return {k:True for k,v in registro.items() if v >= limite}, resumen


# ═══════════════════════════════════════════════════════════════════════════════
# 5. EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

def enviar_email(resumen):
    if not EMAIL_REMITENTE or not EMAIL_CONTRASENA or not resumen:
        return
    try:
        # Separar por tipo
        nuevos = [c for c in resumen if c.startswith("NUEVO")]
        eliminados = [c for c in resumen if c.startswith("ELIMINADO")]
        cambios = [c for c in resumen if c.startswith("CAMBIO")]

        cuerpo = f"ALERTA - Activos por Colombia\n"
        cuerpo += f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        cuerpo += f"{'='*50}\n\n"

        if nuevos:
            cuerpo += f"INMUEBLES NUEVOS ({len(nuevos)}):\n"
            for c in nuevos: cuerpo += f"  + {c}\n"
            cuerpo += "\n"

        if eliminados:
            cuerpo += f"INMUEBLES ELIMINADOS ({len(eliminados)}):\n"
            for c in eliminados: cuerpo += f"  - {c}\n"
            cuerpo += "\n"

        if cambios:
            cuerpo += f"DATOS QUE CAMBIARON ({len(cambios)}):\n"
            for c in cambios[:40]: cuerpo += f"  * {c}\n"
            if len(cambios) > 40: cuerpo += f"  ... y {len(cambios)-40} mas\n"
            cuerpo += "\n"

        cuerpo += f"{'='*50}\n"
        cuerpo += "La tabla se actualizo automaticamente.\n"

        # Asunto descriptivo
        partes = []
        if nuevos: partes.append(f"{len(nuevos)} nuevos")
        if eliminados: partes.append(f"{len(eliminados)} eliminados")
        if cambios: partes.append(f"{len(cambios)} cambios")
        asunto = f"Activos Colombia - {', '.join(partes)}"

        msg = MIMEMultipart()
        msg["From"] = EMAIL_REMITENTE
        msg["To"] = EMAIL_DESTINATARIO
        msg["Subject"] = asunto
        msg.attach(MIMEText(cuerpo, "plain"))

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

    # Scrape detalles con Playwright
    print("\n[3/5] Visitando paginas de Medellin...")
    det_med = scrape_detalles(props_med, "MED ")

    print("\n[4/5] Visitando paginas de Antioquia...")
    det_ant = scrape_detalles(props_ant, "ANT ")

    # Procesar
    med = procesar(props_med, det_med)
    ant = procesar(props_ant, det_ant)

    # Detectar cambios
    print("\n[5/5] Detectando cambios y actualizando Google Sheets...")
    cm, rm = detectar_cambios(med, "med")
    ca, ra = detectar_cambios(ant, "ant")
    todos_cambios = rm + ra

    if todos_cambios:
        print(f"\n  *** {len(todos_cambios)} ALERTAS ***")
        for c in todos_cambios[:15]: print(f"    {c}")
        if len(todos_cambios) > 15: print(f"    ... y {len(todos_cambios)-15} mas")
    else:
        print("  Sin cambios")

    # Google Sheets
    from sheets_sync import sync_to_sheets
    sync_to_sheets(med, ant, cm, ca)

    # Email
    if todos_cambios:
        enviar_email(todos_cambios)

    print(f"\nListo: {len(med)} Medellin + {len(ant)} Antioquia")
