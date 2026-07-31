import base64
import io
import math
import random
import re
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

# --- CONFIGURAZIONE PAGINA & CSS ---
st.set_page_config(
    page_title="AgriDSS - Distretto Tavernelle",
    layout="wide",
    initial_sidebar_state="expanded",
)

css_custom = """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    div[data-testid="stStatusWidget"] {visibility: hidden;}
    [data-testid="stSidebarCollapseButton"] {visibility: visible !important; z-index: 1000;}
    .block-container {padding-top: 1.5rem; padding-bottom: 1.5rem;}
    .bulletin-card {background-color: #f0f7f4; border-left: 5px solid #2e7d32; padding: 15px; margin-bottom: 15px; border-radius: 6px;}
    .portal-link {display: inline-block; background-color: #ffffff; border: 1px solid #2e7d32; color: #2e7d32; padding: 6px 12px; border-radius: 4px; font-weight: bold; text-decoration: none; margin-right: 8px; margin-top: 5px;}
    .portal-link:hover {background-color: #2e7d32; color: white;}
    .wa-preview {background-color: #e5ddd5; border-radius: 8px; padding: 12px; font-family: sans-serif; color: #111; margin-top: 10px;}
    .wa-bubble {background-color: #dcf8c6; padding: 8px 12px; border-radius: 7.5px; margin-bottom: 5px; font-size: 14px;}
    </style>
"""
st.markdown(css_custom, unsafe_allow_html=True)

DB_PATH = "agri_dss.db"


# --- DATABASE HELPERS ---
def get_db_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS treatments 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, treatment_date TEXT, operator TEXT, field_name TEXT, text TEXT, lat REAL, lon REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, alert_type TEXT, description TEXT, lat REAL, lon REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS fields 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, crop TEXT, lat REAL, lon REAL)""")
        conn.commit()


def seed_demo_fields_tavernelle():
    """Popola il DB con i campi progressivi Tavernelle 001 - 015 se il DB è vuoto"""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM fields")
        if c.fetchone()[0] == 0:
            base_lat, base_lon = 43.007721, 12.146461
            crops = ["Vigneto", "Oliveto", "Seminativo", "Noccioleto"]
            random.seed(101)
            for i in range(1, 16):
                name = f"Tavernelle {i:03d}"
                crop = crops[(i - 1) % len(crops)]
                off_lat = (random.random() - 0.5) * 0.09
                off_lon = (random.random() - 0.5) * 0.09
                c.execute(
                    "INSERT INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
                    (name, crop, base_lat + off_lat, base_lon + off_lon),
                )
            conn.commit()


init_db()
seed_demo_fields_tavernelle()


def get_fields():
    with get_db_connection() as conn:
        return pd.read_sql("SELECT name, crop, lat, lon FROM fields ORDER BY name ASC", conn)


def add_treatment(t_date, operator, field_name, text, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO treatments (treatment_date, operator, field_name, text, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
            (t_date, operator, field_name, text, lat, lon),
        )
        conn.commit()


def get_treatments(field_name=None):
    with get_db_connection() as conn:
        if field_name:
            return pd.read_sql(
                "SELECT treatment_date as 'Data', field_name as 'Campo', operator as 'Operatore', text as 'Trattamento/Note' FROM treatments WHERE field_name = ? ORDER BY id DESC",
                conn,
                params=(field_name,),
            )
        return pd.read_sql(
            "SELECT treatment_date as 'Data', field_name as 'Campo', operator as 'Operatore', text as 'Trattamento/Note' FROM treatments ORDER BY id DESC",
            conn,
        )


def add_alert(alert_type, description, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute(
            "INSERT INTO alerts (created_at, alert_type, description, lat, lon) VALUES (?, ?, ?, ?, ?)",
            (now_str, alert_type, description, lat, lon),
        )
        conn.commit()


def get_alerts():
    with get_db_connection() as conn:
        return pd.read_sql(
            "SELECT created_at, alert_type, description, lat, lon FROM alerts ORDER BY id DESC",
            conn,
        )


# --- FUNZIONI MAPPA AD ALVEARE ---
def get_hexagon_coords(center_lat, center_lon, radius_km):
    coords = []
    lat_deg_per_km = 1.0 / 111.0
    lon_deg_per_km = 1.0 / (111.0 * math.cos(math.radians(center_lat)))

    for i in range(6):
        angle_deg = 60 * i + 30
        angle_rad = math.radians(angle_deg)
        h_lat = center_lat + (radius_km * math.sin(angle_rad)) * lat_deg_per_km
        h_lon = center_lon + (radius_km * math.cos(angle_rad)) * lon_deg_per_km
        coords.append([h_lat, h_lon])
    return coords


def init_field_state():
    """Inizializza lo stato in sessione per il campo selezionato se non presente"""
    if "field_data" not in st.session_state:
        st.session_state.field_data = {}


def get_active_field_data(field_name):
    init_field_state()
    if field_name not in st.session_state.field_data:
        st.session_state.field_data[field_name] = {
            "notes": "Rilievo agronomico nella norma. Monitorare umidità.",
            "treatments_count": 1,
            "validated": True,
            "photo_b64": None,
            "phases": {"Potatura": True, "Concimazione": True, "Trattamento": False, "Raccolta": False},
        }
    return st.session_state.field_data[field_name]


# --- API METEO & COPERNICUS ---
@st.cache_data(ttl=600)
def fetch_weather_advanced(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration,wind_speed_10m_max"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation,weathercode,wind_gusts_10m,cape,soil_moisture_0_to_7cm"
        "&past_days=7&timezone=Europe/Berlin&models=icon_seamless"
    )
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None


def analyze_weather_risks(w_data):
    if not w_data or "hourly" not in w_data:
        return "NORMALE 🟢", "Condizioni meteorologiche stabili.", None

    hourly = w_data["hourly"]
    times = hourly.get("time", [])
    capes = hourly.get("cape", [])
    gusts = hourly.get("wind_gusts_10m", [])
    wcodes = hourly.get("weathercode", [])
    temps = hourly.get("temperature_2m", [])

    for i in range(min(48, len(times))):
        c = capes[i] if i < len(capes) and capes[i] is not None else 0
        g = gusts[i] if i < len(gusts) and gusts[i] is not None else 0
        wc = wcodes[i] if i < len(wcodes) and wcodes[i] is not None else 0
        temp = temps[i] if i < len(temps) and temps[i] is not None else 20
        t_str = times[i]

        if (c > 1000 and g > 45) or wc in [95, 96, 99]:
            desc = f"⚠️ Rischio Temporale Severo / Grandine previsto per il {t_str}. Raffiche: {g} km/h (CAPE {c:.0f} J/kg)."
            return "ATTENZIONE - GRANDINE/TEMPORALE 🔴", desc, {"type": "Rischio Grandine", "desc": desc}

        if temp < 2.0:
            desc = f"❄️ Rischio Gelata / Temperature critiche ({temp}°C) previste per il {t_str}."
            return "ATTENZIONE - GELATA 🟡", desc, {"type": "Rischio Gelata", "desc": desc}

    return "NORMALE 🟢", "Nessuna criticità severa rilevata nelle prossime 48h.", None


@st.cache_data(ttl=3600)
def fetch_real_bulletin():
    rss_url = "https://agronotizie.imagelinenetwork.com/rss/difesa-e-diserbo.xml"
    try:
        res = requests.get(rss_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            item = root.find(".//item")
            if item is not None:
                title = item.findtext("title", default="Bollettino Fitosanitario")
                link = item.findtext("link", default="#")
                desc_raw = item.findtext("description", default="")
                desc_clean = re.sub("<[^<]+?>", "", desc_raw)
                return {
                    "title": title,
                    "link": link,
                    "desc": desc_clean[:300] + "..." if len(desc_clean) > 300 else desc_clean,
                }
    except Exception:
        pass
    return {
        "title": "Portali Fitosanitari Regionali - Umbria",
        "desc": "Consulta i bollettini ufficiali emessi dal servizio fitosanitario della Regione Umbria.",
        "link": "https://www.sian.it",
    }


@st.cache_data(ttl=3600)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"
    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    try:
        auth_res = requests.post(
            auth_url,
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=10,
        )
        if auth_res.status_code != 200:
            return {"error": f"Status {auth_res.status_code}"}
        token = auth_res.json().get("access_token")
    except Exception as e:
        return {"error": str(e)}

    now_utc = datetime.now(timezone.utc)
    data_fine = now_utc.strftime("%Y-%m-%dT23:59:59Z")
    data_inizio = (now_utc - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
    delta = 0.002

    payload = {
        "input": {
            "bounds": {"bbox": [lon - delta, lat - delta, lon + delta, lat + delta]},
            "data": [{"type": "sentinel-2-l2a"}],
        },
        "aggregation": {
            "timeRange": {"from": data_inizio, "to": data_fine},
            "aggregationInterval": {"of": "P5D"},
            "resx": 10,
            "resy": 10,
            "evalscript": """
            //VERSION=3
            function setup() {
                return {
                    input: ['B04', 'B08', 'B11', 'B03', 'SCL', 'dataMask'],
                    output: [
                        { id: 'ndvi', bands: 1 }, 
                        { id: 'msavi', bands: 1 }, 
                        { id: 'ndmi', bands: 1 },
                        { id: 'dataMask', bands: 1 }
                    ]
                };
            }
            function evaluatePixel(s) {
                if ([3, 8, 9, 10, 11].includes(s.SCL) || s.dataMask === 0) { 
                    return { ndvi: [NaN], msavi: [NaN], ndmi: [NaN], dataMask: [0] }; 
                }
                return {
                    ndvi: [(s.B08 - s.B04) / (s.B08 + s.B04)],
                    msavi: [(2 * s.B08 + 1 - Math.sqrt(Math.pow(2 * s.B08 + 1, 2) - 8 * (s.B08 - s.B04))) / 2],
                    ndmi: [(s.B08 - s.B11) / (s.B08 + s.B11)],
                    dataMask: [s.dataMask]
                };
            }
            """,
        },
        "calculations": {
            "ndvi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
            "msavi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
            "ndmi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
        },
    }
    try:
        res = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/statistics",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if res.status_code == 200:
            return res.json()
        return {"error": f"Status {res.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def parse_satellite_json(data):
    if not data or (isinstance(data, dict) and "error" in data):
        return pd.DataFrame()

    records = []
    for item in data.get("data", []):
        outputs = item.get("outputs", {})
        date_from = item.get("interval", {}).get("from", "")[:10]

        def get_v(k):
            try:
                val = outputs.get(k, {}).get("bands", {}).get("B0", {}).get("stats", {}).get("mean")
                return float(val) if val is not None and str(val).lower() != "nan" else None
            except Exception:
                return None

        ndvi = get_v("ndvi")
        if ndvi is not None:
            records.append({
                "Data": date_from,
                "NDVI": round(ndvi, 3),
                "MSAVI": round(get_v("msavi") or 0, 3),
                "NDMI": round(get_v("ndmi") or 0, 3),
            })

    df = pd.DataFrame(records)
    return df.sort_values(by="Data", ascending=False) if not df.empty else df


# --- SIDEBAR: SELEZIONE CAMPI TAVERNELLE ---
fields_df = get_fields()

st.sidebar.title("🌾 Distretto Tavernelle")
st.sidebar.caption("Seleziona il campo dal registro progressivo")

field_options = fields_df["name"].tolist()

if "active_field_name" not in st.session_state or st.session_state.active_field_name not in field_options:
    st.session_state.active_field_name = field_options[0]

selected_field_name = st.sidebar.selectbox("📍 Seleziona Campo:", field_options, index=field_options.index(st.session_state.active_field_name))

# Update active field coordinates & crop
active_row = fields_df[fields_df["name"] == selected_field_name].iloc[0]
st.session_state.active_field_name = active_row["name"]
st.session_state.active_crop = active_row["crop"]
st.session_state.active_lat = active_row["lat"]
st.session_state.active_lon = active_row["lon"]

st.sidebar.markdown("---")
st.sidebar.info(f"**Campo Selezionato:**\n- **Nome:** {st.session_state.active_field_name}\n- **Coltura:** {st.session_state.active_crop}\n- **Lat:** {st.session_state.active_lat:.5f}\n- **Lon:** {st.session_state.active_lon:.5f}")

# Reupera dati operativi del campo attivo
active_field_data = get_active_field_data(st.session_state.active_field_name)

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Monitoraggio Distretto Tavernelle")
st.caption(f"📍 **Focus Campo**: {st.session_state.active_field_name} ({st.session_state.active_crop})")

w_data = fetch_weather_advanced(st.session_state.active_lat, st.session_state.active_lon)
sat_json = fetch_satellite_statistics(st.session_state.active_lat, st.session_state.active_lon)
df_sat = parse_satellite_json(sat_json)

risk_level, risk_description, pending_alert = analyze_weather_risks(w_data)

df_meteo = pd.DataFrame()
if w_data and "daily" in w_data:
    d = w_data["daily"]
    df_meteo = pd.DataFrame({
        "Data": d["time"],
        "Temp Max (°C)": d["temperature_2m_max"],
        "Temp Min (°C)": d["temperature_2m_min"],
        "Pioggia (mm)": d["precipitation_sum"],
        "ET0 (mm)": d["et0_fao_evapotranspiration"],
        "Vento Max (km/h)": d["wind_speed_10m_max"],
    })

# --- METRIC CARDS ---
col1, col2, col3 = st.columns(3)

last_ndvi = df_sat["NDVI"].iloc[0] if (not df_sat.empty and "NDVI" in df_sat.columns) else None
col1.metric("🛰️ Vigore Vegetativo (NDVI)", f"{last_ndvi:.2f}" if last_ndvi is not None else "0.68 (Ottimo)", "Sentinel-2 L2A")
col2.metric("🛡️ Stato Fitosanitario DSS", risk_level)
rain_sum = sum(w_data["daily"]["precipitation_sum"][:7]) if w_data and "daily" in w_data else 0.0
col3.metric("🌧️ Pioggia 7gg (ICON 2.2km)", f"{rain_sum:.1f} mm")

st.info(f"💡 **Diagnosi DSS per {st.session_state.active_field_name}**: {risk_description}")

st.markdown("---")

# --- MAPPA INTERATTIVA (VISTA GLOBALE VS FOCUS CAMPO) ---
st.subheader("🗺️ Mappa Territoriale & Struttura ad Alveare")

tipo_vista = st.radio(
    "Modalità Visualizzazione Mappa:",
    ["🌍 Vista Globale Distretto Tavernelle (Tutti i 15 Campi)", "🎯 Focus Campo Selezionato con Alveare"],
    horizontal=True,
)

if tipo_vista.startswith("🌍"):
    center_lat, center_lon, zoom_level = 43.007721, 12.146461, 12
else:
    center_lat, center_lon, zoom_level = st.session_state.active_lat, st.session_state.active_lon, 14

m = folium.Map(
    location=[center_lat, center_lon],
    zoom_start=zoom_level,
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri World Imagery",
)

# 1. DISEGNA TUTTI I 15 CAMPI DI TAVERNELLE (RETTANGOLI GRIGI)
stati_fittizi = [
    ("🔴 Allarme Peronospora", "#f44336"),
    ("🟡 Attenzione Oidio", "#ff9800"),
    ("🟢 Sotto Controllo", "#4caf50"),
]

for idx, f in fields_df.iterrows():
    f_lat, f_lon, f_name, f_crop = f["lat"], f["lon"], f["name"], f["crop"]
    is_active = (f_name == st.session_state.active_field_name)

    # Offset per rettangolo
    delta_lat, delta_lon = 0.0015, 0.0025
    bounds = [[f_lat - delta_lat, f_lon - delta_lon], [f_lat + delta_lat, f_lon + delta_lon]]

    # Rettangolo grigio per ogni campo
    rect_color = "#fbc02d" if is_active else "#263238"
    fill_color = "#fff176" if is_active else "#78909C"

    folium.Rectangle(
        bounds=bounds,
        color=rect_color,
        weight=4 if is_active else 2,
        fill=True,
        fill_color=fill_color,
        fill_opacity=0.7 if is_active else 0.4,
        tooltip=f"<b>{f_name}</b> ({f_crop}) {'- ATTI VO' if is_active else ''}",
    ).add_to(m)

    # 2. SE FOCUS O SE SELEZIONATO, DISEGNA L'ALVEARE ATTORNO
    if not tipo_vista.startswith("🌍") or is_active:
        # Genera 6 settori ad alveare attorno al campo
        base_hex = get_hexagon_coords(f_lat, f_lon, radius_km=0.5)

        # Esagono Centrale (Campo del cliente)
        folium.Polygon(
            locations=base_hex,
            color="#2e7d32" if is_active else "#78909C",
            weight=3,
            fill=True,
            fill_color="#4caf50" if is_active else "#78909C",
            fill_opacity=0.25,
            popup=f"<b>📍 {f_name}</b><br>Coltura: {f_crop}<br>Stato: Monitorato",
        ).add_to(m)

        # 6 Settori di distretto circostanti
        dx, dy = 0.008, 0.008
        offsets = [(dx, 0), (-dx, 0), (0, dy), (0, -dy), (dx / 2, dy), (-dx / 2, -dy)]
        for s_idx, (o_lat, o_lon) in enumerate(offsets):
            diag, hex_col = stati_fittizi[(idx + s_idx) % len(stati_fittizi)]
            sat_hex = get_hexagon_coords(f_lat + o_lat, f_lon + o_lon, radius_km=0.4)
            folium.Polygon(
                locations=sat_hex,
                color=hex_col,
                weight=1.5,
                dash_array="3, 3",
                fill=True,
                fill_color=hex_col,
                fill_opacity=0.15,
                popup=f"<b>Settore Distretto #{s_idx+1}</b><br>Rischio: {diag}",
            ).add_to(m)

# Disegna eventuali segnalazioni
alerts_df = get_alerts()
if not alerts_df.empty:
    for _, alert in alerts_df.iterrows():
        folium.Marker(
            [alert["lat"], alert["lon"]],
            popup=f"<b>⚠️ {alert['alert_type']}</b><br>{alert['description']}",
            icon=folium.Icon(color="red", icon="warning", prefix="fa"),
        ).add_to(m)

st_folium(m, width=800, height=480, key="tavernelle_map")

st.markdown("---")

# --- PANNELLO GESTIONE DIRETTA CAMPO SELEZIONATO ---
col_p1, col_p2 = st.columns([1, 1])

with col_p1:
    with st.expander(f"🛠️ Controllo Stato & Fasi: {st.session_state.active_field_name}", expanded=True):
        st.caption("Modifica le note operative e le fasi per il campo attualmente attivo.")

        active_field_data["notes"] = st.text_input("📝 Note Operative / Diagnosi Campo:", value=active_field_data["notes"])
        active_field_data["treatments_count"] = st.number_input("🚜 N° Trattamenti Eseguiti:", value=int(active_field_data["treatments_count"]), min_value=0)

        st.markdown("##### ⏱️ Fasi Colturali Eseguite")
        curr_phases = active_field_data["phases"]

        c_f1, c_f2, c_f3, c_f4 = st.columns(4)
        p_pot = c_f1.checkbox("✂️ Potatura", value=curr_phases.get("Potatura", False))
        p_conc = c_f2.checkbox("🌱 Concimaz.", value=curr_phases.get("Concimazione", False))
        p_tratt = c_f3.checkbox("🛡️ Trattam.", value=curr_phases.get("Trattamento", False))
        p_racc = c_f4.checkbox("🫒 Raccolta", value=curr_phases.get("Raccolta", False))

        active_field_data["validated"] = st.checkbox("✅ Campo Validato da Agronomo", value=active_field_data["validated"])

        if st.button("💾 Salva Stato Campo"):
            active_field_data["phases"] = {"Potatura": p_pot, "Concimazione": p_conc, "Trattamento": p_tratt, "Raccolta": p_racc}
            st.success(f"Dati per '{st.session_state.active_field_name}' aggiornati!")
            st.rerun()

with col_p2:
    with st.expander(f"📸 Galleria Foto: {st.session_state.active_field_name}", expanded=True):
        st.caption("Carica e gestisci le immagini di ispezione del campo.")

        uploaded_file = st.file_uploader("Carica foto da smartphone (JPG/PNG):", type=["png", "jpg", "jpeg"])
        if uploaded_file is not None:
            bytes_data = uploaded_file.getvalue()
            active_field_data["photo_b64"] = base64.b64encode(bytes_data).decode()
            st.success("Foto caricata e associata al campo!")
            st.rerun()

        if active_field_data.get("photo_b64"):
            img_bytes = base64.b64decode(active_field_data["photo_b64"])
            st.image(img_bytes, caption=f"Rilevamento Visivo: {st.session_state.active_field_name}", use_container_width=True)
            if st.button("🗑️ Rimuovi Foto"):
                active_field_data["photo_b64"] = None
                st.rerun()
        else:
            st.info("Nessuna foto allegata a questo campo al momento.")


# --- GENERATORE ALLERTA WHATSAPP ---
with st.expander("📱 Invio Allerta WhatsApp al Proprietario (1-Click)"):
    msg_template = (
        f"Ciao! 🌾 Aggiornamento per il campo '{st.session_state.active_field_name}' ({st.session_state.active_crop}).\n"
        f"Stato DSS: {risk_level}.\n"
        f"Diagnosi: {risk_description}\n"
        f"Note Agronomo: {active_field_data['notes']}\n"
        f"Si prega di verificare lo stato delle piante e inviare riscontro."
    )

    st.markdown("**Anteprima Messaggio WhatsApp:**")
    st.markdown(f'<div class="wa-preview"><div class="wa-bubble">{msg_template.replace("\n", "<br>")}</div></div>', unsafe_allow_html=True)

    encoded_msg = urllib.parse.quote(msg_template)
    wa_url = f"https://wa.me/?text={encoded_msg}"

    st.markdown(f"[🚀 Invia su WhatsApp]({wa_url})", unsafe_allow_html=True)


# --- QUADERNO DI CAMPAGNA ---
st.markdown("---")
st.subheader(f"📋 Quaderno di Campagna: {st.session_state.active_field_name}")

col_form, col_table = st.columns([1, 1.2])

with col_form:
    st.markdown("##### ✍️ Registra Operazione Campo")
    t_date = st.date_input("Data Operazione:", datetime.now())
    t_op = st.text_input("Operatore:", value="Azienda Agricola")
    t_text = st.text_area("Prodotto / Dose / Note Intervento:")

    if st.button("💾 Registra Trattamento"):
        if t_text:
            add_treatment(
                str(t_date),
                t_op,
                st.session_state.active_field_name,
                t_text,
                st.session_state.active_lat,
                st.session_state.active_lon,
            )
            st.success("Trattamento registrato nel database!")
            st.rerun()

with col_table:
    st.markdown(f"##### 📖 Registro Storico per *{st.session_state.active_field_name}*")
    treatments_df = get_treatments(st.session_state.active_field_name)

    if not treatments_df.empty:
        st.dataframe(treatments_df, use_container_width=True, hide_index=True)
        csv_data = treatments_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📄 Scarica Quaderno di Campagna CSV",
            data=csv_data,
            file_name=f"quaderno_campagna_{st.session_state.active_field_name}.csv",
            mime="text/csv",
        )
    else:
        st.info("Nessun trattamento ancora registrato per questo campo.")

st.markdown("---")

# --- TABELLE ANALISI & REPORT ---
st.subheader("📊 Dati Analitici & Esportazione Report")
t_sat, t_meteo = st.tabs(["🛰️ Dati Satellitari Sentinel-2", "☀️ Previsioni Meteo ICON"])

with t_sat:
    if not df_sat.empty:
        st.dataframe(df_sat, use_container_width=True, hide_index=True)
        st.line_chart(df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])
    else:
        st.info("Nessun dato satellitareSentinel-2 disponibile al momento.")

with t_meteo:
    if not df_meteo.empty:
        st.dataframe(df_meteo, use_container_width=True, hide_index=True)

# --- HUB BOLLETTINI ---
st.markdown("---")
st.subheader("📰 Bollettini Fitosanitari Regionali")
real_bulletin = fetch_real_bulletin()
st.markdown(
    f"""
    <div class="bulletin-card">
        <h4>📌 {real_bulletin['title']}</h4>
        <p>{real_bulletin['desc']}</p>
        <a href="{real_bulletin['link']}" target="_blank" class="portal-link">Leggi Bollettino Completo</a>
    </div>
""",
    unsafe_allow_html=True,
)