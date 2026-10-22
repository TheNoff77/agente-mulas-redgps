import os
import re
import math
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from playwright.sync_api import sync_playwright

# ==================== CONFIGURACION ====================
REDGPS_USER = os.getenv("REDGPS_USER", "")
REDGPS_PASSWORD = os.getenv("REDGPS_PASSWORD", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# URL del Webhook de Google Sheets (creado via Google Apps Script)
GOOGLE_SHEETS_WEBHOOK_URL = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "")

VEHICULOS = [
    {"placa": "XVP020", "imei": "862476052151957", "label": "XVP020 - 862476052151957"},
    {"placa": "XVP022", "imei": "862476053363858", "label": "XVP022 - 862476053363858"}
]

FACTOR_CORRECCION_CURVAS = 1.03
CARPETA_DESCARGAS = os.path.join(os.getcwd(), "descargas_kml")
os.makedirs(CARPETA_DESCARGAS, exist_ok=True)
# =======================================================

def distancia_haversine(lon1, lat1, lon2, lat2):
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlambda / 2) ** 2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def procesar_kml(ruta_kml, placa):
    archivo_xml = ET.parse(ruta_kml)
    raiz = archivo_xml.getroot()
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    paradas = []
    frenadas_bruscas = 0
    vel_maxima = 0
    puntos_ruta = []
    coord_totales = []

    for placemark in raiz.findall(".//kml:Placemark", ns):
        nombre_el = placemark.find("kml:name", ns)
        desc_el = placemark.find("kml:description", ns)
        estilo_el = placemark.find("kml:styleUrl", ns)
        linea_el = placemark.find(".//kml:LineString/kml:coordinates", ns)

        nombre = nombre_el.text.strip() if (nombre_el is not None and nombre_el.text) else ""
        desc = desc_el.text.strip() if (desc_el is not None and desc_el.text) else ""
        estilo = estilo_el.text.strip() if (estilo_el is not None and estilo_el.text) else ""

        if nombre == "Stop":
            paradas.append(desc)
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

        if linea_el is not None and linea_el.text:
            pares = linea_el.text.strip().split()
            for par in pares:
                partes_coord = par.split(",")
                if len(partes_coord) >= 2:
                    lon = float(partes_coord[0])
                    lat = float(partes_coord[1])
                    coord_totales.append((lon, lat))

    km_gps = 0.0
    for i in range(len(coord_totales) - 1):
        km_gps += distancia_haversine(
            coord_totales[i][0], coord_totales[i][1],
            coord_totales[i + 1][0], coord_totales[i + 1][1]
        )

    km_estimados = km_gps * FACTOR_CORRECCION_CURVAS

    puntos_ruta.reverse()
    inicio_dia = puntos_ruta[0] if puntos_ruta else "Sin movimientos hoy"
    ultimo_punto = puntos_ruta[-1] if puntos_ruta else "Sin movimientos hoy"

    paradas.reverse()
    resumen_paradas = []
    for p in paradas:
        m = re.search(r"desde \d{4}-\d{2}-\d{2} (\d{2}:\d{2}).*Hasta \d{4}-\d{2}-\d{2} (\d{2}:\d{2}).*detenido: (.*)", p)
        if m:
            h_ini, h_fin, duracion = m.groups()
            resumen_paradas.append(f"  • {h_ini} a {h_fin} ({duracion})")

    fecha_hoy = datetime.now().strftime("%d/%m/%Y")
    lineas_paradas = "\n".join(resumen_paradas) if resumen_paradas else "  • Sin paradas registradas"

    mensaje = (
        f"🚛 *REPORTE DIARIO | {placa}* ({fecha_hoy})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📏 *Distancia GPS:* {km_gps:.1f} km\n"
        f"🛣️ *Distancia Real (+3%):* {km_estimados:.1f} km\n"
        f"🚀 *Velocidad Maxima:* {vel_maxima} km/h\n"
        f"⚠️ *Frenadas Bruscas:* {frenadas_bruscas}\n\n"
        f"📍 *Punto de Salida:*\n`{inicio_dia}`\n\n"
        f"🏁 *Ubicacion de Cierre:*\n`{ultimo_punto}`\n\n"
        f"🛑 *Paradas ({len(resumen_paradas)}):*\n{lineas_paradas}"
    )
    datos_auditoria = {
        "fecha": fecha_hoy,
        "placa": placa,
        "km_gps": round(km_gps, 1),
        "km_estimados": round(km_estimados, 1),
        "inicio_dia": inicio_dia,
        "ultimo_punto": ultimo_punto,
        "vel_maxima": vel_maxima,
        "frenadas_bruscas": frenadas_bruscas,
        "total_paradas": len(resumen_paradas)
    }

    return mensaje, datos_auditoria

def enviar_telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("   📲 Enviado a Telegram correctamente.")
        else:
            print(f"   ⚠️ Error al enviar a Telegram: {r.text}")
    except Exception as e:
        print(f"   ⚠️ Error de conexion con Telegram: {e}")

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

            reporte, datos_auditoria = procesar_kml(ruta_destino, placa)
            print("\n" + reporte + "\n")
            enviar_telegram(reporte)
            registrar_en_google_sheets(datos_auditoria)
            time.sleep(2)

        print("\n✅ Proceso finalizado con exito para las dos mulas.")
        browser.close()

if __name__ == "__main__":
    descargar_recorridos_y_reportar()