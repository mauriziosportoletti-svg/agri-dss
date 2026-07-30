import base64
import io
import json
import math
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

# --- CONFIGURAZIONE ABBONAMENTO ---
MAX_CAMPI_ABBONAMENTO = 3

# --- CONFIGURAZIONE PAGINA & CSS ---
st.set_page_config(
    page_title="AgriDSS - Monitoraggio Campi & Allerte",
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


init_db()


def save_field(name, crop, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
            (name, crop, lat, lon),
        )
        conn.commit()


def delete_field(name):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fields WHERE name = ?", (name,))
        conn.commit()


def get_fields():
    with get_db_connection() as conn:
        return pd.read_sql("SELECT name, crop, lat, lon FROM fields", conn)


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


# --- FUNZIONI E STATO PER GRIGLIA AD ALVEARE ---
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


def init_honeycomb_state(crop="Oliveto"):
    risk_mapping = {
        "Oliveto": ["Rischio Mosca High", "Attacco Occhio di Pavone", "Stabile / Sotto Controllo"],
        "Vigneto": ["Rischio Elevato Peronospora", "Presenza Oidio / Botrite", "Stabile / Sotto Controllo"],
        "Seminativo": ["Rischio Fusariosi Spiga", "Presenza Afidi", "Stabile / Sotto Controllo"],
        "Noccioleto": ["Rischio Cimice Asiatica", "Presenza Balanino", "Stabile / Sotto Controllo"],
        "Altro": ["Alto Rischio Infezione", "Presenza Parassiti", "Stabile / Sotto Controllo"]
    }
    risks = risk_mapping.get(crop, risk_mapping["Altro"])

    if "honeycomb_cells" not in st.session_state:
        st.session_state.honeycomb_cells = [
            {"id": "c0", "name": "Settore Centro (Campo)", "r": 0, "c": 0, "color": "#4caf50", "risk": risks[2], "reports": 0, "treatments": 1, "validated": True, "photo_b64": None},
            {"id": "c1", "name": "Settore Est (Collina)", "r": 1, "c": 0, "color": "#f44336", "risk": risks[0], "reports": 6, "treatments": 4, "validated": True, "photo_b64": None},
            {"id": "c2", "name": "Settore Ovest (Valle)", "r": -1, "c": 0, "color": "#ff9800", "risk": risks[1], "reports": 3, "treatments": 2, "validated": False, "photo_b64": None},
            {"id": "c3", "name": "Settore Nord", "r": 0, "c": 1, "color": "#4caf50", "risk": risks[2], "reports": 0, "treatments": 1, "validated": True, "photo_b64": None},
            {"id": "c4", "name": "Settore Sud", "r": 0, "c": -1, "color": "#ff9800", "risk": risks[1], "reports": 2, "treatments": 1, "validated": False, "photo_b64": None},
            {"id": "c5", "name": "Settore Sud-Est", "r": 1, "c": -1, "color": "#4caf50", "risk": risks[2], "reports": 1, "treatments": 0, "validated": True, "photo_b64": None},
            {"id": "c6", "name": "Settore Nord-Ovest", "r": -1, "c": 1, "color": "#f44336", "risk": risks[0], "reports": 5, "treatments": 3, "validated": True, "photo_b64": None},
        ]


def generate_honeycomb_grid(center_lat, center_lon, radius_km=1.8):
    dx = math.sqrt(3) * radius_km
    dy = 1.5 * radius_km

    lat_deg_per_km = 1.0 / 111.0
    lon_deg_per_km = 1.0 / (111.0 * math.cos(math.radians(center_lat)))

    hexagons = []
    for cell in st.session_state.honeycomb_cells:
        r, c = cell["r"], cell["c"]
        x_km = c * dx + (dx / 2.0 if abs(r) % 2 == 1 else 0.0)
        y_km = r * dy

        c_lat = center_lat + (y_km * lat_deg_per_km)
        c_lon = center_lon + (x_km * lon_deg_per_km)

        hexagons.append({
            "id": cell["id"],
            "coords": get_hexagon_coords(c_lat, c_lon, radius_km),
            "color": cell["color"],
            "name": cell["name"],
            "risk": cell["risk"],
            "reports": cell["reports"],
            "treatments": cell["treatments"],
            "validated": cell.get("validated", False),
            "photo_b64": cell.get("photo_b64", None)
        })

    return hexagons


# --- API METEO AVANZATA ---
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
            desc = (f"⚠️ Rischio Temporale Severo / Grandine previsto per il {t_str}. "
                    f"Raffiche stimate a {g} km/h e instabilità elevata (CAPE {c:.0f} J/kg).")
            return "ATTENZIONE - GRANDINE/TEMPORALE 🔴", desc, {
                "type": "Rischio Temporale / Grandine",
                "desc": desc
            }

        if temp < 2.0:
            desc = f"❄️ Rischio Gelata / Temperature critiche ({temp}°C) previste per il {t_str}."
            return "ATTENZIONE - GELATA 🟡", desc, {
                "type": "Rischio Gelata",
                "desc": desc
            }

    return "NORMALE 🟢", "Nessuna criticità severa rilevata dai modelli ad alta risoluzione nelle prossime 48h.", None


@st.cache_data(ttl=3600)
def fetch_real_bulletin():
    rss_url = "https://agronotizie.imagelinenetwork.com/rss/difesa-e-diserbo.xml"
    try:
        res = requests.get(rss_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            item = root.find('.//item')
            if item is not None:
                title = item.findtext('title', default='Bollettino Fitosanitario')
                link = item.findtext('link', default='#')
                desc_raw = item.findtext('description', default='')
                desc_clean = re.sub('<[^<]+?>', '', desc_raw)
                return {
                    "title": title,
                    "link": link,
                    "desc": desc_clean[:300] + "..." if len(desc_clean) > 300 else desc_clean,
                }
    except Exception:
        pass
    return {
        "title": "Portali Fitosanitari & ARPA Regionali",
        "desc": "Consulta direttamente i bollettini ufficiali validati dai servizi fitosanitari della tua regione.",
        "link": "https://www.sian.it/portale-mipaaf/home.jsp"
    }


@st.cache_data(ttl=3600)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"
    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    try:
        auth_res = requests.post(
            auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10,
        )
        if auth_res.status_code != 200:
            return {"error": f"Errore Autenticazione OAuth Copernicus (Status {auth_res.status_code})"}
        token = auth_res.json().get("access_token")
    except Exception as e:
        return {"error": f"Eccezione OAUTH: {str(e)}"}

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
        else:
            return {"error": f"Errore API Copernicus Statistics (Status {res.status_code})"}
    except Exception as e:
        return {"error": f"Eccezione Dati: {str(e)}"}


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


# --- INIZIALIZZAZIONE SESSIONE ---
fields_df = get_fields()

if "active_field_name" not in st.session_state:
    if not fields_df.empty:
        st.session_state.active_field_name = fields_df.iloc[0]["name"]
        st.session_state.active_crop = fields_df.iloc[0]["crop"]
        st.session_state.active_lat = fields_df.iloc[0]["lat"]
        st.session_state.active_lon = fields_df.iloc[0]["lon"]
    else:
        st.session_state.active_field_name = "Nessun Campo"
        st.session_state.active_crop = "Oliveto"
        st.session_state.active_lat = 43.007721
        st.session_state.active_lon = 12.146461

if "clicked_lat" not in st.session_state:
    st.session_state.clicked_lat = st.session_state.active_lat
if "clicked_lon" not in st.session_state:
    st.session_state.clicked_lon = st.session_state.active_lon
if "last_registered_click" not in st.session_state:
    st.session_state.last_registered_click = (st.session_state.active_lat, st.session_state.active_lon)

init_honeycomb_state(st.session_state.active_crop)

# --- SIDEBAR: GESTIONE CAMPI ---
st.sidebar.title("🏡 I Miei Campi")

if not fields_df.empty:
    options = fields_df["name"].tolist()
    curr_idx = options.index(st.session_state.active_field_name) if st.session_state.active_field_name in options else 0

    selected_field = st.sidebar.selectbox("Seleziona Campo da Analizzare:", options, index=curr_idx)

    if selected_field != st.session_state.active_field_name:
        row = fields_df[fields_df["name"] == selected_field].iloc[0]
        st.session_state.active_field_name = row["name"]
        st.session_state.active_crop = row["crop"]
        st.session_state.active_lat = row["lat"]
        st.session_state.active_lon = row["lon"]
        st.session_state.clicked_lat = row["lat"]
        st.session_state.clicked_lon = row["lon"]
        st.session_state.last_registered_click = (row["lat"], row["lon"])
        st.rerun()

    if st.sidebar.button(f"🗑️ Elimina '{selected_field}'"):
        delete_field(selected_field)
        st.sidebar.success("Campo eliminato!")
        st.session_state.pop("active_field_name", None)
        st.rerun()
else:
    st.sidebar.warning("Nessun campo salvato.")

st.sidebar.markdown("---")
num_campi = len(fields_df)
st.sidebar.caption(f"Campi salvati: **{num_campi}/{MAX_CAMPI_ABBONAMENTO}** (Piano Base)")

if num_campi < MAX_CAMPI_ABBONAMENTO:
    st.sidebar.subheader("➕ Aggiungi Nuovo Campo")
    st.sidebar.info("👉 *Fai click sulla mappa per acquisire la posizione.*")

    new_name = st.sidebar.text_input("Nome Campo:")
    new_crop = st.sidebar.selectbox("Coltura:", ["Oliveto", "Vigneto", "Seminativo", "Noccioleto", "Altro"])

    saved_lat = st.sidebar.number_input("Latitudine:", value=st.session_state.clicked_lat, format="%.6f")
    saved_lon = st.sidebar.number_input("Longitudine:", value=st.session_state.clicked_lon, format="%.6f")

    if st.sidebar.button("💾 Salva Campo"):
        if new_name:
            save_field(new_name, new_crop, saved_lat, saved_lon)
            st.session_state.active_field_name = new_name
            st.session_state.active_crop = new_crop
            st.session_state.active_lat = saved_lat
            st.session_state.active_lon = saved_lon
            st.session_state.last_registered_click = (saved_lat, saved_lon)
            st.sidebar.success("Campo aggiunto!")
            st.rerun()
        else:
            st.sidebar.error("Inserisci un nome!")
else:
    st.sidebar.error("⚠️ Limite massimo di campi raggiunto.")


# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Monitoraggio & Allarmi")
st.caption(
    f"📍 **Campo Attivo**: {st.session_state.active_field_name} ({st.session_state.active_crop}) | **Lat**: {st.session_state.active_lat:.5f} | **Lon**: {st.session_state.active_lon:.5f}"
)

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
col1.metric(
    "🛰️ Indice Vigore (NDVI)",
    f"{last_ndvi:.2f}" if last_ndvi is not None else "N/D",
    "Ottimo" if (last_ndvi and last_ndvi > 0.5) else "Sotto controllo",
)

col2.metric("🛡️ Stato Fitosanitario & Meteo", risk_level)

rain_sum = sum(w_data["daily"]["precipitation_sum"][:7]) if w_data and "daily" in w_data else 0.0
col3.metric("🌧️ Pioggia 7gg (ICON 2.2km)", f"{rain_sum:.1f} mm")

st.info(f"💡 **Diagnosi DSS**: {risk_description}")

st.markdown("---")

# --- MAPPA INTERATTIVA CON ALVEARE E FOTO VALIDATE ---
st.subheader("🗺️ Mappa Territoriale & Mappatura ad Alveare")

m = folium.Map(
    location=[st.session_state.active_lat, st.session_state.active_lon],
    zoom_start=12,
)

# Marker Campo Attivo
folium.Marker(
    [st.session_state.active_lat, st.session_state.active_lon],
    popup=f"<b>Campo Attivo: {st.session_state.active_field_name}</b><br>Coltura: {st.session_state.active_crop}",
    icon=folium.Icon(color="green", icon="leaf"),
).add_to(m)

# Generazione Griglia ad Alveare (Raggio 1.8 km)
hex_grid = generate_honeycomb_grid(
    st.session_state.active_lat, 
    st.session_state.active_lon, 
    radius_km=1.8
)

for h in hex_grid:
    status_badge = "<span style='color: #2e7d32; font-weight: bold;'>🟢 VERIFICATO DA AGRONOMO</span>" if h["validated"] else "<span style='color: #e65100; font-weight: bold;'>🟡 IN ATTESA DI VERIFICA</span>"
    
    img_html = ""
    if h["photo_b64"]:
        img_html = f"""<div style='margin-top: 8px;'><b style='font-size:11px;'>📸 Foto dal Campo:</b><br><img src='data:image/png;base64,{h['photo_b64']}' style='width: 100%; max-width: 200px; border-radius: 6px; margin-top: 4px; border: 1px solid #ccc;'/></div>"""
    
    popup_html = f"""
    <div style='font-family: sans-serif; font-size: 13px; min-width: 200px;'>
        <b style='font-size: 14px; color: #1b5e20;'>{h['name']}</b><br>
        🌱 Coltura: <b>{st.session_state.active_crop}</b><br>
        ⚠️ Stato: <b>{h['risk']}</b><br>
        📋 Stato Validazione: {status_badge}<br>
        📲 Segnalazioni WhatsApp: <b>{h['reports']}</b><br>
        🚜 Trattamenti Registrati: <b>{h['treatments']}</b>
        {img_html}
    </div>
    """
    
    folium.Polygon(
        locations=h["coords"],
        color=h["color"],
        weight=2,
        fill=True,
        fill_color=h["color"],
        fill_opacity=0.35,
        popup=folium.Popup(popup_html, max_width=320)
    ).add_to(m)

if (st.session_state.clicked_lat != st.session_state.active_lat or st.session_state.clicked_lon != st.session_state.active_lon):
    folium.Marker(
        [st.session_state.clicked_lat, st.session_state.clicked_lon],
        popup="Punto Selezionato",
        icon=folium.Icon(color="orange", icon="info-sign"),
    ).add_to(m)

alerts_df = get_alerts()
if not alerts_df.empty:
    for _, alert in alerts_df.iterrows():
        folium.Marker(
            [alert["lat"], alert["lon"]],
            popup=f"<b>⚠️ {alert['alert_type']}</b><br>{alert['description']}<br><i>{alert['created_at']}</i>",
            icon=folium.Icon(color="red", icon="warning", prefix="fa"),
        ).add_to(m)

map_data = st_folium(m, width=750, height=450, key="agri_map")

if map_data and map_data.get("last_clicked"):
    cl_lat = round(map_data["last_clicked"]["lat"], 5)
    cl_lon = round(map_data["last_clicked"]["lng"], 5)

    if (cl_lat, cl_lon) != st.session_state.last_registered_click:
        st.session_state.clicked_lat = cl_lat
        st.session_state.clicked_lon = cl_lon
        st.session_state.last_registered_click = (cl_lat, cl_lon)
        st.rerun()


# --- PANNELLO DI AGGIORNAMENTO MANUALE & UPLOAD FOTO (ADMIN) ---
with st.expander("🛠️ Aggiorna Stato Esagoni / Carica Foto & Valida (Pannello Admin)"):
    st.caption("Aggiorna lo stato dei settori, carica la foto inviata dall'agricoltore e imposta la firma dell'agronomo.")
    
    sector_names = [cell["name"] for cell in st.session_state.honeycomb_cells]
    selected_sector_name = st.selectbox("Seleziona Settore da Modificare:", sector_names)
    
    target_cell = next(item for item in st.session_state.honeycomb_cells if item["name"] == selected_sector_name)
    
    c_edit1, c_edit2, c_edit3 = st.columns(3)
    
    new_color = c_edit1.selectbox(
        "Livello di Rischio (Colore):", 
        ["🔴 Alto Rischio (#f44336)", "🟡 Medio Rischio (#ff9800)", "🟢 Basso Rischio (#4caf50)"],
        index=0 if target_cell["color"] == "#f44336" else (1 if target_cell["color"] == "#ff9800" else 2)
    )
    
    new_risk_text = c_edit1.text_input("Descrizione Rischio:", value=target_cell["risk"])
    new_reports = c_edit2.number_input("📲 N° Segnalazioni WhatsApp:", value=int(target_cell["reports"]), min_value=0)
    new_treatments = c_edit3.number_input("🚜 N° Trattamenti Eseguiti:", value=int(target_cell["treatments"]), min_value=0)
    
    st.markdown("---")
    st.markdown("##### 📸 Validazione & Foto Campo")
    col_photo1, col_photo2 = st.columns(2)
    
    is_validated = col_photo1.checkbox("✅ Approvato / Validato da Agronomo", value=target_cell.get("validated", False))
    uploaded_file = col_photo2.file_uploader("Carica Foto Anomalia/Trappola (PNG/JPG):", type=["png", "jpg", "jpeg"])
    
    if st.button("💾 Applica Modifiche Esagone"):
        color_hex = "#f44336" if "🔴" in new_color else ("#ff9800" if "🟡" in new_color else "#4caf50")
        target_cell["color"] = color_hex
        target_cell["risk"] = new_risk_text
        target_cell["reports"] = new_reports
        target_cell["treatments"] = new_treatments
        target_cell["validated"] = is_validated
        
        if uploaded_file is not None:
            bytes_data = uploaded_file.getvalue()
            b64_str = base64.b64encode(bytes_data).decode()
            target_cell["photo_b64"] = b64_str
            
        st.success(f"Settore '{selected_sector_name}' aggiornato!")
        st.rerun()


# --- GENERATORE ALLERTA WHATSAPP ---
with st.expander("📱 Genera Allerta WhatsApp per Agricoltori (Test 1-Click)"):
    st.caption("Crea il messaggio personalizzato da inviare via WhatsApp per il campo attivo.")

    wa_crop = st.session_state.active_crop
    wa_field = st.session_state.active_field_name

    default_msg_content = risk_description if pending_alert else "Condizioni stabili. Verifica lo stato colturale."

    msg_template = (
        f"Ciao! 🌾 Aggiornamento dal campo '{wa_field}' ({wa_crop}).\n"
        f"{default_msg_content}\n"
        f"Controlla i bollettini ufficiali e rispondi se ci sono anomalie."
    )

    st.markdown("**Anteprima Messaggio WhatsApp:**")
    st.markdown(f"""
        <div class="wa-preview">
            <div class="wa-bubble">
                {msg_template.replace('\n', '<br>')}
            </div>
        </div>
    """, unsafe_allow_html=True)

    if pending_alert:
        if st.button("🚨 Registra questa Allerta nel Database Storico"):
            add_alert(pending_alert["type"], pending_alert["desc"], st.session_state.active_lat, st.session_state.active_lon)
            st.success("Allerta salvata con successo!")
            st.rerun()

    encoded_msg = urllib.parse.quote(msg_template)
    wa_url = f"https://wa.me/?text={encoded_msg}"

    st.markdown(f"[🚀 Apri su WhatsApp Web / App]({wa_url})", unsafe_allow_html=True)


# --- SEGNALAZIONI ---
with st.expander("📢 Invia una Segnalazione Anonima nella zona"):
    st.caption(f"Posizione: **Lat {st.session_state.clicked_lat:.5f}, Lon {st.session_state.clicked_lon:.5f}**")
    a_type = st.selectbox(
        "Avvistamento / Anomalia:",
        [
            "Avvistamento Parassiti",
            "Attacco Fungalico / Malattia",
            "Siccità Severa",
            "Danni da Grandine / Evento meteo",
            "Altro",
        ],
    )
    a_desc = st.text_input("Dettagli:")
    if st.button("🚨 Pubblica Segnalazione"):
        add_alert(
            a_type,
            a_desc or "Segnalazione generica",
            st.session_state.clicked_lat,
            st.session_state.clicked_lon,
        )
        st.success("Segnalazione inviata!")
        st.rerun()

st.markdown("---")

# --- TABELLE E DATI ---
st.subheader("📊 Analisi Dettagliata & Storico")
t_sat, t_meteo = st.tabs(["🛰️ Satellite Sentinel-2", "☀️ Meteo & Suolo"])

with t_sat:
    if not df_sat.empty:
        st.dataframe(df_sat, use_container_width=True, hide_index=True)
        st.line_chart(df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])
    else:
        st.info("Nessun dato satellitare disponibile al momento.")

with t_meteo:
    if not df_meteo.empty:
        st.dataframe(df_meteo, use_container_width=True, hide_index=True)


# --- ESPORTAZIONE UNIFICATA REPORT ---
st.markdown("#### 📥 Esporta Report Storico (Meteo + Satellite)")
exp_col1, exp_col2 = st.columns(2)

if not df_sat.empty and "Data" in df_sat.columns and not df_meteo.empty and "Data" in df_meteo.columns:
    df_combined = pd.merge(df_meteo, df_sat, on="Data", how="outer").sort_values(by="Data", ascending=False)
elif not df_meteo.empty:
    df_combined = df_meteo
elif not df_sat.empty:
    df_combined = df_sat
else:
    df_combined = pd.DataFrame()

if not df_combined.empty:
    csv_report = df_combined.to_csv(index=False).encode("utf-8")
    exp_col1.download_button(
        label="📊 Scarica Tabellone Completo CSV (Excel)",
        data=csv_report,
        file_name=f"report_meteo_sat_{st.session_state.active_field_name}.csv",
        mime="text/csv",
    )

html_sat = df_sat.to_html(index=False, classes="styled-table") if not df_sat.empty else "<p>Nessun dato satellitare disponibile</p>"
html_meteo = df_meteo.to_html(index=False, classes="styled-table") if not df_meteo.empty else "<p>Nessun dato meteo disponibile</p>"

report_html = f"""
<html>
<head>
    <title>Report Agronomico - {st.session_state.active_field_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 30px; color: #333; }}
        h1 {{ color: #2e7d32; border-bottom: 2px solid #2e7d32; padding-bottom: 5px; }}
        h2 {{ color: #1b5e20; margin-top: 25px; }}
        .info-box {{ background-color: #f1f8e9; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
        .styled-table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 14px; }}
        .styled-table th, .styled-table td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
        .styled-table th {{ background-color: #4caf50; color: white; }}
        .styled-table tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .footer {{ margin-top: 40px; font-size: 11px; text-align: center; color: #777; border-top: 1px solid #ccc; padding-top: 10px; }}
    </style>
</head>
<body>
    <h1>🌾 Report Agronomico Integrato</h1>
    <div class="info-box">
        <p><b>Campo:</b> {st.session_state.active_field_name} ({st.session_state.active_crop})</p>
        <p><b>Coordinate:</b> Lat {st.session_state.active_lat:.5f}, Lon {st.session_state.active_lon:.5f}</p>
        <p><b>Data Report:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
    </div>

    <h2>🛰️ Dati Satellitari (Sentinel-2)</h2>
    {html_sat}

    <h2>☀️ Storico Meteo</h2>
    {html_meteo}

    <div class="footer">
        Generato automaticamente da AgriDSS - Sistema Decisionale per l'Agricoltura
    </div>
</body>
</html>
"""

exp_col2.download_button(
    label="📄 Scarica Report PDF / Stampabile",
    data=report_html,
    file_name=f"report_integrato_{st.session_state.active_field_name}.html",
    mime="text/html",
)

st.markdown("---")

# --- QUADERNO DI CAMPAGNA ---
st.subheader("📋 Registro Trattamenti & Esportazione")

col_form, col_table = st.columns([1, 1.2])

with col_form:
    st.markdown("##### ✍️ Registra Operazione")
    t_date = st.date_input("Data Operazione:", datetime.now())
    t_op = st.text_input("Operatore:", value="Azienda")
    t_text = st.text_area("Prodotto / Dose / Note:")

    if st.button("💾 Salva Trattamento"):
        if t_text:
            add_treatment(
                str(t_date),
                t_op,
                st.session_state.active_field_name,
                t_text,
                st.session_state.active_lat,
                st.session_state.active_lon,
            )
            st.success("Registrato!")
            st.rerun()

with col_table:
    st.markdown(f"##### 📖 Registro per: *{st.session_state.active_field_name}*")
    treatments_df = get_treatments(st.session_state.active_field_name)

    if not treatments_df.empty:
        st.dataframe(treatments_df, use_container_width=True, hide_index=True)

        st.markdown("###### 📥 Esporta Registro Trattamenti")
        c_exp1, c_exp2 = st.columns(2)

        csv_data = treatments_df.to_csv(index=False).encode("utf-8")
        c_exp1.download_button(
            label="📄 Scarica CSV",
            data=csv_data,
            file_name=f"quaderno_campagna_{st.session_state.active_field_name}.csv",
            mime="text/csv",
        )

        html_table = treatments_df.to_html(index=False)
        full_html = f"<html><head><title>Quaderno di Campagna - {st.session_state.active_field_name}</title></head><body><h2>Quaderno di Campagna - {st.session_state.active_field_name}</h2>{html_table}</body></html>"
        c_exp2.download_button(
            label="🖨️ Scarica Report PDF/HTML",
            data=full_html,
            file_name=f"quaderno_campagna_{st.session_state.active_field_name}.html",
            mime="text/html",
        )
    else:
        st.info("Nessun trattamento registrato per questo campo.")

st.markdown("---")

# --- HUB BOLLETTINI FITOSANITARI (IN FONDO) ---
st.subheader("📰 Bollettini Fitosanitari & Portale Regionale Ufficiale")

real_bulletin = fetch_real_bulletin()

st.markdown(f"""
    <div class="bulletin-card">
        <h4>📌 {real_bulletin['title']}</h4>
        <p>{real_bulletin['desc']}</p>
        <div style="margin-top: 10px;">
            <b>🏛️ Portali Istituzionali di Riferimento:</b><br>
            <a href="https://www.sian.it" target="_blank" class="portal-link">Portale Nazionale SIAN</a>
            <a href="{real_bulletin['link']}" target="_blank" class="portal-link" style="border-color: #d32f2f; color: #d32f2f;">News Fitosanitarie Nazionali</a>
        </div>
    </div>
""", unsafe_allow_html=True)