import os
import re
import sys
import math
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from playwright.sync_api import sync_playwright

# Compatibilidad UTF-8 para consola de Windows
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==================== CONFIGURACION ====================
REDGPS_USER = os.getenv("REDGPS_USER", "")
REDGPS_PASSWORD = os.getenv("REDGPS_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# URL del Webhook de Google Sheets (creado via Google Apps Script)
GOOGLE_SHEETS_WEBHOOK_URL = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "")

# ==================== AUDITORIA Y COMBUSTIBLE ====================
# Parámetros comerciales para tractomulas de carga en Colombia
RENDIMIENTO_KM_POR_GALON = float(os.getenv("RENDIMIENTO_KM_POR_GALON", "6.5"))  # Promedio km por galón
PRECIO_GALON_ACPM = float(os.getenv("PRECIO_GALON_ACPM", "11500"))             # Precio en COP por galón de ACPM
FACTOR_CORRECCION_CURVAS = 1.03

# Verificación de credenciales al iniciar
print("--- Diagnóstico de Credenciales ---")
print(f"• REDGPS_USER: {'✅ Presente' if REDGPS_USER else '❌ VACÍO (Falta Secret)'}")
print(f"• REDGPS_PASSWORD: {'✅ Presente' if REDGPS_PASSWORD else '❌ VACÍO (Falta Secret)'}")
print(f"• TELEGRAM_TOKEN: {'✅ Presente' if TELEGRAM_TOKEN else '❌ VACÍO (Falta Secret)'}")
print(f"• TELEGRAM_CHAT_ID: {'✅ Presente' if TELEGRAM_CHAT_ID else '❌ VACÍO (Falta Secret)'}")
print(f"• GOOGLE_SHEETS_WEBHOOK_URL: {'✅ Presente' if GOOGLE_SHEETS_WEBHOOK_URL else 'ℹ️ No configurado (Opcional)'}")
print("-----------------------------------")

VEHICULOS = [
    {"placa": "XVP020", "imei": "862476052151957", "label": "XVP020 - 862476052151957"},
    {"placa": "XVP022", "imei": "862476053363858", "label": "XVP022 - 862476053363858"}
]

CARPETA_DESCARGAS = os.path.join(os.getcwd(), "descargas_kml")
os.makedirs(CARPETA_DESCARGAS, exist_ok=True)
# =======================================================

def formatear_cop(valor):
    """Formatea valores en pesos colombianos con punto de miles (ej: $176.500 COP)."""
    return f"${int(round(valor)):,}".replace(",", ".") + " COP"

def limpiar_direccion(texto_desc):
    """Limpia el texto de ubicacion del KML para mostrar municipio y departamento conciso."""
    if not texto_desc:
        return "Ubicación no identificada"
    t = re.sub(r'^\d{4}-\d{2}-\d{2}[- ]\d{2}:\d{2}:\d{2}\s*', '', texto_desc).strip()
    t = re.sub(r',?\s*Colombia\s*$', '', t, flags=re.I).strip()
    partes = [p.strip() for p in t.split(',') if p.strip()]
    if len(partes) >= 2:
        return f"{partes[-2]}, {partes[-1]}"
    return t if t else "Ubicación no identificada"

def limpiar_punto_ruta(texto):
    """Limpia puntos de salida y cierre de ruta."""
    if not texto or texto == "Sin movimientos hoy":
        return texto
    t = re.sub(r'^\d{4}-\d{2}-\d{2}[- ]\d{2}:\d{2}:\d{2}\s*', '', texto).strip()
    t = re.sub(r',?\s*Colombia\s*$', '', t, flags=re.I).strip()
    return t if t else texto

def distancia_haversine(lon1, lat1, lon2, lat2):
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlambda / 2) ** 2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def extraer_tiempos_motor_panel(page):
    """
    Lee del panel 'Detalle del recorrido' en el DOM de RedGPS:
    - Tiempo en movimiento
    - Tiempo ralentí
    - Horas uso de motor
    """
    tiempos = {
        "tiempo_movimiento": "No disponible",
        "tiempo_ralenti": "No disponible",
        "horas_motor": "No disponible"
    }
    
    try:
        time.sleep(2)  # Pausa breve para asegurar renderizado de datos en el panel
        datos = page.evaluate("""() => {
            const resultado = {
                tiempo_movimiento: "No disponible",
                tiempo_ralenti: "No disponible",
                horas_motor: "No disponible"
            };

            function buscarValor(palabrasClave) {
                // 1. Buscar en filas de tabla (tr), listas o divs de detalle
                const contenedores = Array.from(document.querySelectorAll('tr, li, div.row, div[class*="item"], div[class*="detalle"], div[class*="detail"]'));
                for (const el of contenedores) {
                    const texto = (el.innerText || "").trim();
                    for (const kw of palabrasClave) {
                        if (texto.toLowerCase().includes(kw.toLowerCase())) {
                            // Buscar en celdas o elementos internos
                            const celdas = Array.from(el.querySelectorAll('td, th, span, div, p')).filter(c => c.children.length === 0);
                            for (let i = 0; i < celdas.length; i++) {
                                const cTexto = (celdas[i].innerText || "").trim();
                                if (cTexto.toLowerCase().includes(kw.toLowerCase())) {
                                    for (let j = i + 1; j < celdas.length; j++) {
                                        const valorCandidato = (celdas[j].innerText || "").trim();
                                        if (valorCandidato && !valorCandidato.toLowerCase().includes(kw.toLowerCase())) {
                                            return valorCandidato;
                                        }
                                    }
                                }
                            }
                            // Formato con dos puntos o salto de linea
                            const partes = texto.split(/[:\\n\\t]+/);
                            for (let i = 0; i < partes.length - 1; i++) {
                                if (partes[i].toLowerCase().includes(kw.toLowerCase())) {
                                    const val = partes[i+1].trim();
                                    if (val && !val.toLowerCase().includes(kw.toLowerCase())) {
                                        return val;
                                    }
                                }
                            }
                        }
                    }
                }

                // 2. Búsqueda por regex en el cuerpo visible si no se halló en tabla
                const textoCompleto = document.body ? document.body.innerText : "";
                for (const kw of palabrasClave) {
                    const regex = new RegExp(kw + "[:\\\\s\\\\n\\\\t]+([0-9a-zA-Z\\\\s:hmsd]+)", "i");
                    const match = textoCompleto.match(regex);
                    if (match && match[1]) {
                        const limpia = match[1].split("\\n")[0].trim();
                        if (limpia && limpia.length < 30) {
                            return limpia;
                        }
                    }
                }
                return "No disponible";
            }

            resultado.tiempo_movimiento = buscarValor(["Tiempo en movimiento", "En movimiento"]);
            resultado.tiempo_ralenti = buscarValor(["Tiempo ralentí", "Tiempo ralenti", "Ralentí", "Ralenti"]);
            resultado.horas_motor = buscarValor(["Horas uso de motor", "Uso de motor", "Horas motor", "Horas de motor", "Uso motor"]);

            return resultado;
        }""")

        if isinstance(datos, dict):
            for k in tiempos:
                val = datos.get(k, "No disponible")
                if val and str(val).strip() and str(val).strip().lower() != "null":
                    tiempos[k] = str(val).strip()
    except Exception as e:
        print(f"   ⚠️ No se pudo extraer detalle de tiempos de motor del panel: {e}")

    return tiempos

def procesar_kml(ruta_kml, placa, tiempos_motor=None):
    archivo_xml = ET.parse(ruta_kml)
    raiz = archivo_xml.getroot()
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    paradas_raw = []
    frenadas_bruscas = 0
    vel_maxima = 0
    puntos_ruta = []
    puntos_geo = []
    coord_totales = []

    for placemark in raiz.findall(".//kml:Placemark", ns):
        nombre_el = placemark.find("kml:name", ns)
        desc_el = placemark.find("kml:description", ns)
        estilo_el = placemark.find("kml:styleUrl", ns)
        linea_el = placemark.find(".//kml:LineString/kml:coordinates", ns)
        pt_el = placemark.find(".//kml:Point/kml:coordinates", ns)

        nombre = nombre_el.text.strip() if (nombre_el is not None and nombre_el.text) else ""
        desc = desc_el.text.strip() if (desc_el is not None and desc_el.text) else ""
        estilo = estilo_el.text.strip() if (estilo_el is not None and estilo_el.text) else ""

        if nombre == "Stop":
            coords_stop = None
            if pt_el is not None and pt_el.text:
                partes_pt = pt_el.text.strip().split(",")
                if len(partes_pt) >= 2:
                    coords_stop = (float(partes_pt[0]), float(partes_pt[1]))
            paradas_raw.append((desc, coords_stop))
        elif nombre == "Frenada brusca":
            frenadas_bruscas += 1
        elif estilo.startswith("#velocity_"):
            partes = estilo.split("_")
            if len(partes) >= 2 and partes[1].isdigit():
                vel = int(partes[1])
                if vel > vel_maxima:
                    vel_maxima = vel
            if desc:
                puntos_ruta.append(desc)
            if pt_el is not None and pt_el.text and desc:
                partes_pt = pt_el.text.strip().split(",")
                if len(partes_pt) >= 2:
                    puntos_geo.append((float(partes_pt[0]), float(partes_pt[1]), desc))

        if linea_el is not None and linea_el.text:
            pares = linea_el.text.strip().split()
            for par in pares:
                partes_coord = par.split(",")
                if len(partes_coord) >= 2:
                    lon = float(partes_coord[0])
                    lat = float(partes_coord[1])
                    coord_totales.append((lon, lat))

    # Cálculo de distancias
    km_gps = 0.0
    for i in range(len(coord_totales) - 1):
        km_gps += distancia_haversine(
            coord_totales[i][0], coord_totales[i][1],
            coord_totales[i + 1][0], coord_totales[i + 1][1]
        )

    km_estimados = km_gps * FACTOR_CORRECCION_CURVAS

    # Cálculos comerciales de ACPM
    galones_estimados = km_estimados / RENDIMIENTO_KM_POR_GALON if RENDIMIENTO_KM_POR_GALON > 0 else 0.0
    costo_acpm = galones_estimados * PRECIO_GALON_ACPM

    # Indicador de estilo de conducción
    if frenadas_bruscas <= 2:
        indicador_conduccion = "🟢 Óptimo"
    elif frenadas_bruscas <= 5:
        indicador_conduccion = "🟡 Moderado (Revisar)"
    else:
        indicador_conduccion = "🔴 Riesgoso (Desgaste de frenos/llantas)"

    # Tiempos de motor extraídos del panel
    if not tiempos_motor:
        tiempos_motor = {}
    horas_motor = tiempos_motor.get("horas_motor", "No disponible")
    tiempo_movimiento = tiempos_motor.get("tiempo_movimiento", "No disponible")
    tiempo_ralenti = tiempos_motor.get("tiempo_ralenti", "No disponible")

    puntos_ruta.reverse()
    inicio_dia = puntos_ruta[0] if puntos_ruta else "Sin movimientos hoy"
    ultimo_punto = puntos_ruta[-1] if puntos_ruta else "Sin movimientos hoy"

    # Procesar paradas cruzando coordenadas con el punto de ruta más cercano
    paradas_raw.reverse()
    resumen_paradas = []
    for desc_stop, coords_stop in paradas_raw:
        m = re.search(r"desde \d{4}-\d{2}-\d{2} (\d{2}:\d{2}).*Hasta \d{4}-\d{2}-\d{2} (\d{2}:\d{2}).*detenido: (.*?)(?:\.|$)", desc_stop)
        if m:
            h_ini, h_fin, duracion = m.groups()
            dur_limpia = re.sub(r'\s*\d+s$', '', duracion).strip()

            ubicacion_parada = "Ubicación en ruta"
            if coords_stop and puntos_geo:
                lon_s, lat_s = coords_stop
                mejor_dist = float("inf")
                mejor_desc = ""
                for p_lon, p_lat, p_desc in puntos_geo:
                    d = distancia_haversine(lon_s, lat_s, p_lon, p_lat)
                    if d < mejor_dist:
                        mejor_dist = d
                        mejor_desc = p_desc
                if mejor_desc:
                    ubicacion_parada = limpiar_direccion(mejor_desc)

            resumen_paradas.append(f"  • {h_ini} a {h_fin} ({dur_limpia}) - {ubicacion_parada}")

    fecha_hoy = datetime.now().strftime("%d/%m/%Y")
    lineas_paradas = "\n".join(resumen_paradas) if resumen_paradas else "  • Sin paradas registradas"

    # Formato ejecutivo comercial para el mensaje individual
    mensaje = (
        f"🚛 *AUDITORÍA DIARIA | MULA {placa}* ({fecha_hoy})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛣️ *Recorrido Real (+3%):* {km_estimados:.1f} km _(GPS: {km_gps:.1f} km)_\n"
        f"⛽ *ACPM Estimado:* {galones_estimados:.1f} gal (~{formatear_cop(costo_acpm)})\n"
        f"⏱️ *Uso de Motor:* {horas_motor} _(En marcha: {tiempo_movimiento} | Ralentí: {tiempo_ralenti})_\n"
        f"🚀 *Vel. Máxima:* {vel_maxima} km/h\n"
        f"⚠️ *Conducción:* {frenadas_bruscas} frenadas bruscas ({indicador_conduccion})\n\n"
        f"📍 *Punto de Salida:*\n`{limpiar_punto_ruta(inicio_dia)}`\n\n"
        f"🏁 *Ubicación de Cierre:*\n`{limpiar_punto_ruta(ultimo_punto)}`\n\n"
        f"🛑 *Paradas ({len(resumen_paradas)}):*\n{lineas_paradas}"
    )

    datos_auditoria = {
        "fecha": fecha_hoy,
        "placa": placa,
        "km_gps": round(km_gps, 1),
        "km_estimados": round(km_estimados, 1),
        "galones_estimados": round(galones_estimados, 1),
        "costo_acpm": round(costo_acpm, 0),
        "horas_motor": horas_motor,
        "tiempo_movimiento": tiempo_movimiento,
        "tiempo_ralenti": tiempo_ralenti,
        "vel_maxima": vel_maxima,
        "frenadas_bruscas": frenadas_bruscas,
        "indicador_conduccion": indicador_conduccion,
        "total_paradas": len(resumen_paradas),
        "inicio_dia": limpiar_punto_ruta(inicio_dia),
        "ultimo_punto": limpiar_punto_ruta(ultimo_punto)
    }

    return mensaje, datos_auditoria

def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("   ❌ [Telegram] No se puede enviar porque TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no están configurados en GitHub Secrets.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("   📲 Enviado a Telegram correctamente.")
            return True
        else:
            print(f"   ❌ [Telegram] Error al enviar ({r.status_code}): {r.text}")
            if r.status_code == 401:
                print("   👉 CAUSA: El token fue revocado por Telegram (o es inválido). Debes generar uno nuevo en @BotFather y actualizar el secret TELEGRAM_TOKEN.")
            return False
    except Exception as e:
        print(f"   ❌ [Telegram] Error de conexion: {e}")
        return False

def registrar_en_google_sheets(datos):
    if not GOOGLE_SHEETS_WEBHOOK_URL:
        print("   ℹ️ [Google Sheets] No hay URL configurada en GOOGLE_SHEETS_WEBHOOK_URL. Omitiendo.")
        return
    try:
        r = requests.post(GOOGLE_SHEETS_WEBHOOK_URL, json=datos, timeout=15)
        if r.status_code == 200:
            print(f"   📊 [Google Sheets] Registro de {datos.get('placa')} guardado exitosamente.")
        else:
            print(f"   ⚠️ [Google Sheets] Error al registrar: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"   ⚠️ [Google Sheets] Error de conexion: {e}")

def descargar_recorridos_y_reportar():
    metricas_flota = []

    with sync_playwright() as p:
        # Ejecucion en modo headless para servidores Linux / GitHub Actions
        browser = p.chromium.launch(headless=True, slow_mo=300)
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()

        print("1. Conectando a RedGPS...")
        page.goto("https://plataforma.redgps.com/")
        time.sleep(3)

        if page.locator("input[type='password']").count() > 0:
            print("2. Iniciando sesion en RedGPS...")
            page.locator("input[type='text']:visible, input[type='email']:visible").first.fill(REDGPS_USER)
            page.locator("input[type='password']:visible").first.fill(REDGPS_PASSWORD)
            page.keyboard.press("Enter")
            time.sleep(8)

        print("3. Activando modulo de Recorridos directamente...")
        page.evaluate("show(2, undefined, undefined, 'Recorridos');")
        time.sleep(3)

        page.wait_for_selector("text=Ver Recorridos", state="visible", timeout=25000)

        for idx, vehiculo in enumerate(VEHICULOS):
            placa = vehiculo["placa"]
            print(f"\n4.{idx+1}. Procesando mula {placa}...")

            # Limpiar recorridos previos si ya habia uno cargado
            btn_quitar = page.locator("text=Quitar Recorridos").locator("visible=true")
            if btn_quitar.count() > 0 and idx > 0:
                btn_quitar.first.click()
                time.sleep(2)

            # Desmarcar todas las casillas de la tabla de Recorridos primero
            for chk in page.locator("tr:visible input[type='checkbox']").all():
                if chk.is_checked():
                    chk.uncheck(force=True)

            # Marcar unicamente la casilla de la mula actual
            fila = page.locator(f"tr:visible:has-text('{placa}')")
            if fila.count() > 0:
                fila.locator("input[type='checkbox']").first.check(force=True)

            time.sleep(1)
            page.locator("text=Ver Recorridos").locator("visible=true").first.click()
            print(f"   Consultando recorrido de {placa} en el servidor...")
            time.sleep(6)

            # 2. Extraer tiempos de motor del panel izquierdo "Detalle del recorrido" antes de exportar
            print(f"   Extrayendo tiempos de motor de {placa} desde el panel...")
            tiempos_motor = extraer_tiempos_motor_panel(page)
            print(f"   ⏱️ Motor: Horas: {tiempos_motor['horas_motor']} | Mov: {tiempos_motor['tiempo_movimiento']} | Ralentí: {tiempos_motor['tiempo_ralenti']}")

            print(f"   Descargando KML de {placa}...")
            # Abrir el menu Exportar solo si 'Recorrido KML' no esta visible aun
            opcion_kml = page.locator("text=Recorrido KML").locator("visible=true")
            if opcion_kml.count() == 0:
                page.locator("text=Exportar").locator("visible=true").first.click()
                time.sleep(1.5)

            # Si aun no aparece, reintentar clic en Exportar
            if page.locator("text=Recorrido KML").locator("visible=true").count() == 0:
                page.locator("button:has-text('Exportar'), a:has-text('Exportar'), text=Exportar").locator("visible=true").first.click(force=True)
                time.sleep(1.5)

            with page.expect_download(timeout=30000) as download_info:
                page.locator("text=Recorrido KML").first.click(force=True)

            download = download_info.value
            ruta_destino = os.path.join(CARPETA_DESCARGAS, f"{placa}_{datetime.now().strftime('%Y%m%d')}.kml")
            download.save_as(ruta_destino)
            print(f"   ✅ Archivo guardado: {ruta_destino}")

            reporte, datos_auditoria = procesar_kml(ruta_destino, placa, tiempos_motor)
            print("\n" + reporte + "\n")
            enviar_telegram(reporte)
            registrar_en_google_sheets(datos_auditoria)
            metricas_flota.append(datos_auditoria)
            time.sleep(2)

        # 4. Resumen Consolidado de Flota para Telegram
        if metricas_flota:
            total_km = sum(m["km_estimados"] for m in metricas_flota)
            total_galones = sum(m["galones_estimados"] for m in metricas_flota)
            total_costo = sum(m["costo_acpm"] for m in metricas_flota)
            total_frenadas = sum(m["frenadas_bruscas"] for m in metricas_flota)
            total_paradas = sum(m["total_paradas"] for m in metricas_flota)
            fecha_hoy = datetime.now().strftime("%d/%m/%Y")

            resumen_flota = (
                f"📊 *RESUMEN CONSOLIDADO DE FLOTA* ({fecha_hoy})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🚛 *Vehículos auditados:* {len(metricas_flota)} ({', '.join(m['placa'] for m in metricas_flota)})\n"
                f"🛣️ *Kilometraje Total:* {total_km:.1f} km\n"
                f"⛽ *Consumo Total ACPM:* {total_galones:.1f} gal (~{formatear_cop(total_costo)})\n"
                f"⚠️ *Total Frenadas Bruscas:* {total_frenadas}\n"
                f"🛑 *Total Paradas Registradas:* {total_paradas}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💼 *Auditoría Logística y Control de Combustible*"
            )
            print("\n" + resumen_flota + "\n")
            enviar_telegram(resumen_flota)

        print("\n✅ Proceso de auditoría finalizado con éxito para la flota.")
        browser.close()

if __name__ == "__main__":
    descargar_recorridos_y_reportar()
