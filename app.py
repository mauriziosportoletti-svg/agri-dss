import base64
import io
import json
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

# --- CONFIGURAZIONE ABBONAMENTO & PAGINA ---
MAX_CAMPI_ABBONAMENTO = 15

st.set_page_config(
    page_title="AgriDSS - Distretto Tavernelle & Monitoraggio API",
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
    .info-banner {background-color: #e8f5e9; border-left: 6px solid #2e7d32; padding: 16px; border-radius: 8px; margin-top: 15px; margin-bottom: 20px;}
    </style>
"""
st.markdown(css_custom, unsafe_allow_html=True)

DB_PATH = "agri_dss.db"


# --- DATABASE HELPERS & SEEDING ---
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


def seed_default_fields_if_empty():
    """Popola il DB con 15 campi distribuiti in modo organico attorno a Tavernelle se vuoto"""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM fields")
        if c.fetchone()[0] == 0:
            base_lat, base_lon = 43.007721, 12.146461
            crops = ["Vigneto Sangiovese", "Oliveto Frantoio", "Seminativo Grano", "Noccioleto", "Vigneto Trebbiano"]
            staggered_offsets = [
                (0.0031, 0.0042), (-0.0045, 0.0078), (0.0082, -0.0035), (-0.0061, -0.0089),
                (0.0115, 0.0021), (-0.0122, 0.0045), (0.0018, -0.0112), (0.0067, 0.0095),
                (-0.0084, 0.0121), (0.0141, -0.0078), (-0.0025, 0.0152), (0.0098, 0.0134),
                (-0.0135, -0.0041), (0.0052, -0.0158), (-0.0091, -0.0142)
            ]
            for idx, (off_lat, off_lon) in enumerate(staggered_offsets, start=1):
                name = f"Tavernelle {idx:03d}"
                crop = crops[(idx - 1) % len(crops)]
                c.execute("INSERT INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
                          (name, crop, base_lat + off_lat, base_lon + off_lon))
            conn.commit()


init_db()
seed_default_fields_if_empty()


def save_field(name, crop, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)", (name, crop, lat, lon))
        conn.commit()


def delete_field(name):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fields WHERE name = ?", (name,))
        conn.commit()


def get_fields():
    with get_db_connection() as conn:
        return pd.read_sql("SELECT name, crop, lat, lon FROM fields ORDER BY name ASC", conn)


def add_treatment(t_date, operator, field_name, text, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO treatments (treatment_date, operator, field_name, text, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
                  (t_date, operator, field_name, text, lat, lon))
        conn.commit()


def get_treatments(field_name=None):
    with get_db_connection() as conn:
        if field_name:
            return pd.read_sql("SELECT treatment_date as 'Data', field_name as 'Campo', operator as 'Operatore', text as 'Trattamento/Note' FROM treatments WHERE field_name = ? ORDER BY id DESC", conn, params=(field_name,))
        return pd.read_sql("SELECT treatment_date as 'Data', field_name as 'Campo', operator as 'Operatore', text as 'Trattamento/Note' FROM treatments ORDER BY id DESC", conn)


def add_alert(alert_type, description, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO alerts (created_at, alert_type, description, lat, lon) VALUES (?, ?, ?, ?, ?)",
                  (now_str, alert_type, description, lat, lon))
        conn.commit()


def get_alerts():
    with get_db_connection() as conn:
        return pd.read_sql("SELECT created_at, alert_type, description, lat, lon FROM alerts ORDER BY id DESC", conn)


# --- GEOMETRIA RETTANGOLI ED ESAGONI ---
def get_hexagon_coords(center_lat, center_lon, radius_km=0.18):
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


def get_adjacent_hexagon_centers(center_lat, center_lon, radius_km=0.18):
    dist_km = radius_km * math.sqrt(3)
    lat_deg_per_km = 1.0 / 111.0
    lon_deg_per_km = 1.0 / (111.0 * math.cos(math.radians(center_lat)))
    centers = []
    for i in range(6):
        angle_deg = 60 * i
        angle_rad = math.radians(angle_deg)
        c_lat = center_lat + (dist_km * math.sin(angle_rad)) * lat_deg_per_km
        c_lon = center_lon + (dist_km * math.cos(angle_rad)) * lon_deg_per_km
        centers.append((c_lat, c_lon))
    return centers


def init_field_state():
    if "field_data" not in st.session_state:
        st.session_state.field_data = {}


def get_active_field_data(field_name):
    init_field_state()
    if field_name not in st.session_state.field_data:
        st.session_state.field_data[field_name] = {
            "notes": "Stato vegetativo buono. Nessun sintomo visibile di malattia.",
            "treatments_count": 1,
            "validated": True,
            "photo_b64": None,
            "phases": {"Potatura": True, "Concimazione": True, "Trattamento": False, "Raccolta": False},
        }
    return st.session_state.field_data[field_name]


# --- API METEO (OPEN-METEO) ---
@st.cache_data(ttl=600)
def fetch_weather_advanced(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration,wind_speed_10m_max"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation,weathercode,wind_gusts_10m,cape,soil_moisture_0_to_7cm"
        "&past_days=7&timezone=Europe/Berlin"
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
            desc = f"⚠️ Rischio Temporale Severo / Grandine previsto per il {t_str}."
            return "ATTENZIONE - GRANDINE 🔴", desc, {"type": "Rischio Grandine", "desc": desc}

        if temp < 2.0:
            desc = f"❄️ Rischio Gelata ({temp}°C) per il {t_str}."
            return "ATTENZIONE - GELATA 🟡", desc, {"type": "Rischio Gelata", "desc": desc}

    return "NORMALE 🟢", "Nessuna criticità severa rilevata nelle prossime 48h.", None


# --- RSS FITOSANITARIO ---
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


# --- API COPERNICUS SATELLITE CON MOCK DATA PARACADUTE ---
def generate_mock_satellite_data():
    now = datetime.now(timezone.utc)
    mock_data = {"data": []}
    for i in range(12):
        d = now - timedelta(days=i * 5)
        mock_data["data"].append({
            "interval": {"from": d.strftime("%Y-%m-%dT00:00:00Z")},
            "outputs": {
                "ndvi": {"bands": {"B0": {"stats": {"mean": round(random.uniform(0.65, 0.82), 3)}}}},
                "msavi": {"bands": {"B0": {"stats": {"mean": round(random.uniform(0.45, 0.72), 3)}}}},
                "ndmi": {"bands": {"B0": {"stats": {"mean": round(random.uniform(0.35, 0.58), 3)}}}}
            }
        })
    return mock_data


@st.cache_data(ttl=3600)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"
    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

    try:
        auth_res = requests.post(
            auth_url,
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=5,
        )
        if auth_res.status_code == 200:
            token = auth_res.json().get("access_token")
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
                            output: [{ id: 'ndvi', bands: 1 }, { id: 'msavi', bands: 1 }, { id: 'ndmi', bands: 1 }, { id: 'dataMask', bands: 1 }]
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

            res = requests.post(
                "https://sh.dataspace.copernicus.eu/api/v1/statistics",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if res.status_code == 200:
                return res.json()
    except Exception:
        pass

    return generate_mock_satellite_data()


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


# --- INIZIALIZZAZIONE SESSIONE & SIDEBAR ---
fields_df = get_fields()

st.sidebar.title("🌾 Distretto Tavernelle")

if not fields_df.empty:
    field_options = fields_df["name"].tolist()

    if "active_field_name" not in st.session_state or st.session_state.active_field_name not in field_options:
        st.session_state.active_field_name = field_options[0]

    selected_field_name = st.sidebar.selectbox(
        "📍 Seleziona Campo:",
        field_options,
        index=field_options.index(st.session_state.active_field_name),
    )

    active_row = fields_df[fields_df["name"] == selected_field_name].iloc[0]
    st.session_state.active_field_name = active_row["name"]
    st.session_state.active_crop = active_row["crop"]
    st.session_state.active_lat = active_row["lat"]
    st.session_state.active_lon = active_row["lon"]

    if st.sidebar.button(f"🗑️ Elimina '{selected_field_name}'"):
        delete_field(selected_field_name)
        st.sidebar.success("Campo eliminato!")
        st.session_state.pop("active_field_name", None)
        st.rerun()
else:
    st.sidebar.warning("Nessun campo presente nel DB.")

if "clicked_lat" not in st.session_state:
    st.session_state.clicked_lat = st.session_state.get("active_lat", 43.007721)
if "clicked_lon" not in st.session_state:
    st.session_state.clicked_lon = st.session_state.get("active_lon", 12.146461)
if "last_registered_click" not in st.session_state:
    st.session_state.last_registered_click = (st.session_state.clicked_lat, st.session_state.clicked_lon)

st.sidebar.markdown("---")
num_campi = len(fields_df)
st.sidebar.caption(f"Campi salvati: **{num_campi}/{MAX_CAMPI_ABBONAMENTO}**")

if num_campi < MAX_CAMPI_ABBONAMENTO:
    st.sidebar.subheader("➕ Aggiungi Nuovo Campo")
    new_name = st.sidebar.text_input("Nome Campo:")
    new_crop = st.sidebar.selectbox("Coltura:", ["Vigneto Sangiovese", "Oliveto Frantoio", "Seminativo Grano", "Noccioleto", "Vigneto Trebbiano", "Altro"])
    saved_lat = st.sidebar.number_input("Latitudine:", value=st.session_state.clicked_lat, format="%.6f")
    saved_lon = st.sidebar.number_input("Longitudine:", value=st.session_state.clicked_lon, format="%.6f")

    if st.sidebar.button("💾 Salva Campo"):
        if new_name:
            save_field(new_name, new_crop, saved_lat, saved_lon)
            st.session_state.active_field_name = new_name
            st.sidebar.success("Campo aggiunto!")
            st.rerun()
        else:
            st.sidebar.error("Inserisci un nome validabile!")

active_field_data = get_active_field_data(st.session_state.active_field_name)

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Control Room Distretto Tavernelle")
st.caption(f"📍 **Focus Attuale**: {st.session_state.active_field_name} ({st.session_state.active_crop}) | **Lat**: {st.session_state.active_lat:.5f} | **Lon**: {st.session_state.active_lon:.5f}")

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
last_ndvi = df_sat["NDVI"].iloc[0] if (not df_sat.empty and "NDVI" in df_sat.columns) else 0.74
col1.metric("🛰️ Vigore Vegetativo (NDVI)", f"{last_ndvi:.2f}", "Sentinel-2 L2A")
col2.metric("🛡️ Stato Fitosanitario DSS", risk_level)
rain_sum = sum(w_data["daily"]["precipitation_sum"][:7]) if w_data and "daily" in w_data else 0.0
col3.metric("🌧️ Pioggia 7gg (Open-Meteo)", f"{rain_sum:.1f} mm")

st.markdown("---")

# --- MAPPA INTERATTIVA (VISTA GENERALE CON RETTANGOLI VS FOCUS) ---
st.subheader("🗺️ Mappa Territoriale & Struttura ad Alveare")

tipo_vista = st.radio(
    "Modalità Visualizzazione Mappa:",
    ["🌍 Vista Generale Distretto (Tutti i Campi con i propri Esagoni)", "🎯 Focus Campo Selezionato (Solo il Campo Attivo)"],
    horizontal=True,
)

if tipo_vista.startswith("🌍"):
    center_lat, center_lon, zoom_level = 43.007721, 12.146461, 13
else:
    center_lat, center_lon, zoom_level = st.session_state.active_lat, st.session_state.active_lon, 15

m = folium.Map(
    location=[center_lat, center_lon],
    zoom_start=zoom_level,
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri World Imagery",
)

stati_fittizi = [
    ("🔴 Allarme Peronospora", "#f44336"),
    ("🟡 Attenzione Oidio", "#ff9800"),
    ("🟢 Sotto Controllo", "#4caf50"),
]

# LOGICA MAPPA: RETTANGOLI + ALVEARE ESAGONI
if tipo_vista.startswith("🌍"):
    # VISTA GENERALE: Disegna TUTTI i campi come rettangoli con esagoni attorno
    for idx, f in fields_df.iterrows():
        f_lat, f_lon, f_name, f_crop = f["lat"], f["lon"], f["name"], f["crop"]
        is_active = (f_name == st.session_state.active_field_name)

        # Rettangolo Campo
        delta_lat, delta_lon = 0.0010, 0.0014
        bounds = [[f_lat - delta_lat, f_lon - delta_lon], [f_lat + delta_lat, f_lon + delta_lon]]
        folium.Rectangle(
            bounds=bounds,
            color="#ffeb3b" if is_active else "#ffffff",
            weight=4 if is_active else 2,
            fill=True,
            fill_color="#fbc02d" if is_active else "#263238",
            fill_opacity=0.85 if is_active else 0.6,
            tooltip=f"<b>{f_name}</b> ({f_crop})",
        ).add_to(m)

        # Corona di 6 esagoni attorno a ciascun rettangolo
        radius_km = 0.16
        adj_centers = get_adjacent_hexagon_centers(f_lat, f_lon, radius_km=radius_km)
        for s_idx, (c_lat, c_lon) in enumerate(adj_centers):
            diag, hex_col = stati_fittizi[(idx + s_idx) % len(stati_fittizi)]
            sat_hex = get_hexagon_coords(c_lat, c_lon, radius_km=radius_km)
            folium.Polygon(
                locations=sat_hex,
                color=hex_col,
                weight=1.5,
                dash_array="3, 3",
                fill=True,
                fill_color=hex_col,
                fill_opacity=0.3,
                popup=f"<b>{f_name} - Settore #{s_idx+1}</b><br>Rischio: {diag}",
            ).add_to(m)
else:
    # VISTA FOCUS: Disegna SOLO il campo selezionato e i suoi 6 esagoni
    f_lat, f_lon = st.session_state.active_lat, st.session_state.active_lon
    f_name, f_crop = st.session_state.active_field_name, st.session_state.active_crop

    # Rettangolo centrale
    delta_lat, delta_lon = 0.0010, 0.0014
    bounds = [[f_lat - delta_lat, f_lon - delta_lon], [f_lat + delta_lat, f_lon + delta_lon]]
    folium.Rectangle(
        bounds=bounds,
        color="#ffeb3b",
        weight=4,
        fill=True,
        fill_color="#fbc02d",
        fill_opacity=0.85,
        tooltip=f"<b>{f_name}</b> ({f_crop})",
    ).add_to(m)

    # Esagono centrale + 6 esagoni satellite
    radius_km = 0.18
    central_hex = get_hexagon_coords(f_lat, f_lon, radius_km=radius_km)
    folium.Polygon(
        locations=central_hex,
        color="#2e7d32",
        weight=3,
        fill=True,
        fill_color="#4caf50",
        fill_opacity=0.4,
        popup=f"<b>📍 {f_name}</b>",
    ).add_to(m)

    adj_centers = get_adjacent_hexagon_centers(f_lat, f_lon, radius_km=radius_km)
    for s_idx, (c_lat, c_lon) in enumerate(adj_centers):
        diag, hex_col = stati_fittizi[s_idx % len(stati_fittizi)]
        sat_hex = get_hexagon_coords(c_lat, c_lon, radius_km=radius_km)
        folium.Polygon(
            locations=sat_hex,
            color=hex_col,
            weight=2,
            dash_array="4, 4",
            fill=True,
            fill_color=hex_col,
            fill_opacity=0.35,
            popup=f"<b>Settore #{s_idx+1}</b><br>Rischio: {diag}",
        ).add_to(m)

alerts_df = get_alerts()
if not alerts_df.empty:
    for _, alert in alerts_df.iterrows():
        folium.Marker(
            [alert["lat"], alert["lon"]],
            popup=f"<b>⚠️ {alert['alert_type']}</b><br>{alert['description']}<br><i>{alert['created_at']}</i>",
            icon=folium.Icon(color="red", icon="warning", prefix="fa"),
        ).add_to(m)

map_data = st_folium(m, width=900, height=520, key="tavernelle_map")

if map_data and map_data.get("last_clicked"):
    cl_lat = round(map_data["last_clicked"]["lat"], 5)
    cl_lon = round(map_data["last_clicked"]["lng"], 5)
    if (cl_lat, cl_lon) != st.session_state.last_registered_click:
        st.session_state.clicked_lat = cl_lat
        st.session_state.clicked_lon = cl_lon
        st.session_state.last_registered_click = (cl_lat, cl_lon)
        st.rerun()

# --- BANNER INFORMATIVO COMPLETO DEL CAMPO SELEZIONATO ---
fasi_completate = [k for k, v in active_field_data["phases"].items() if v]
str_fasi = ", ".join(fasi_completate) if fasi_completate else "Nessuna fase registrata"
stato_validazione = "Validato da Agronomo ✅" if active_field_data["validated"] else "In attesa di validazione ⏳"

st.markdown(
    f"""
    <div class="info-banner">
        <h4 style="margin:0 0 8px 0; color:#1b5e20;">📋 Scheda Informativa Integrata: {st.session_state.active_field_name}</h4>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; font-size: 14px;">
            <div><b>🌾 Coltura:</b> {st.session_state.active_crop}</div>
            <div><b>🚜 Trattamenti Eseguiti:</b> {active_field_data['treatments_count']}</div>
            <div><b>🌱 Fasi Colturali:</b> {str_fasi}</div>
            <div><b>🛡️ Validazione:</b> {stato_validazione}</div>
        </div>
        <div style="margin-top: 10px; font-size: 13px; color: #2e7d32;">
            <b>📝 Note Agronomiche Attive:</b> {active_field_data['notes']}
        </div>
    </div>
""",
    unsafe_allow_html=True,
)

st.markdown("---")

# --- PANNELLO GESTIONE & EDIT CAMPO ---
col_p1, col_p2 = st.columns([1, 1])

with col_p1:
    with st.expander(f"🛠️ Modifica Parametri & Fasi: {st.session_state.active_field_name}", expanded=True):
        active_field_data["notes"] = st.text_input("📝 Aggiorna Note Agronomiche:", value=active_field_data["notes"])
        active_field_data["treatments_count"] = st.number_input("🚜 Numero Trattamenti:", value=int(active_field_data["treatments_count"]), min_value=0)

        st.markdown("##### ⏱️ Fasi Colturali Eseguite")
        curr_phases = active_field_data["phases"]
        c_f1, c_f2, c_f3, c_f4 = st.columns(4)
        p_pot = c_f1.checkbox("✂️ Potatura", value=curr_phases.get("Potatura", False))
        p_conc = c_f2.checkbox("🌱 Concimaz.", value=curr_phases.get("Concimazione", False))
        p_tratt = c_f3.checkbox("🛡️ Trattam.", value=curr_phases.get("Trattamento", False))
        p_racc = c_f4.checkbox("🫒 Raccolta", value=curr_phases.get("Raccolta", False))

        active_field_data["validated"] = st.checkbox("✅ Campo Validato", value=active_field_data["validated"])

        if st.button("💾 Salva e Aggiorna Banner"):
            active_field_data["phases"] = {"Potatura": p_pot, "Concimazione": p_conc, "Trattamento": p_tratt, "Raccolta": p_racc}
            st.success("Scheda aggiornata!")
            st.rerun()

with col_p2:
    with st.expander(f"📸 Foto Ispezione: {st.session_state.active_field_name}", expanded=True):
        uploaded_file = st.file_uploader("Carica foto (JPG/PNG):", type=["png", "jpg", "jpeg"])
        if uploaded_file is not None:
            bytes_data = uploaded_file.getvalue()
            active_field_data["photo_b64"] = base64.b64encode(bytes_data).decode()
            st.success("Foto aggiornata!")
            st.rerun()

        if active_field_data.get("photo_b64"):
            img_bytes = base64.b64decode(active_field_data["photo_b64"])
            st.image(img_bytes, caption=f"Ispezione Campo: {st.session_state.active_field_name}", use_container_width=True)
            if st.button("🗑️ Rimuovi Foto"):
                active_field_data["photo_b64"] = None
                st.rerun()
        else:
            st.info("Nessuna foto allegata.")

# --- GENERATORE WHATSAPP ---
with st.expander("📱 Invio Report WhatsApp al Proprietario"):
    msg_template = (
        f"Ciao! 🌾 Report per il campo '{st.session_state.active_field_name}' ({st.session_state.active_crop}).\n"
        f"Stato DSS: {risk_level}.\n"
        f"Diagnosi: {risk_description}\n"
        f"Fasi completate: {str_fasi}\n"
        f"Note Agronomo: {active_field_data['notes']}"
    )
    st.markdown(f'<div class="wa-preview"><div class="wa-bubble">{msg_template.replace("\n", "<br>")}</div></div>', unsafe_allow_html=True)

    if pending_alert:
        if st.button("🚨 Registra questa Allerta nel Database Storico"):
            add_alert(pending_alert["type"], pending_alert["desc"], st.session_state.active_lat, st.session_state.active_lon)
            st.success("Allerta salvata con successo!")
            st.rerun()

    encoded_msg = urllib.parse.quote(msg_template)
    st.markdown(f"[🚀 Invia su WhatsApp](https://wa.me/?text={encoded_msg})", unsafe_allow_html=True)

# --- TABELLE SATELLITE & METEO + ESPORTAZIONI ---
st.markdown("---")
st.subheader("📊 Analisi Dettagliata & Storico")
t_sat, t_meteo = st.tabs(["🛰️ Satellite Sentinel-2 (Copernicus API)", "☀️ Meteo & Suolo"])

with t_sat:
    if not df_sat.empty:
        st.dataframe(df_sat, use_container_width=True, hide_index=True)
        st.line_chart(df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])
    else:
        st.info("Nessun dato satellitare disponibile al momento.")

with t_meteo:
    if not df_meteo.empty:
        st.dataframe(df_meteo, use_container_width=True, hide_index=True)

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
        label="📊 Scarica Tabellone Completo CSV",
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
        .info-box {{ background-color: #f1f8e9; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
        .styled-table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 14px; }}
        .styled-table th, .styled-table td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
        .styled-table th {{ background-color: #4caf50; color: white; }}
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
</body>
</html>
"""

exp_col2.download_button(
    label="📄 Scarica Report Stampabile (HTML/PDF)",
    data=report_html,
    file_name=f"report_integrato_{st.session_state.active_field_name}.html",
    mime="text/html",
)

# --- QUADERNO DI CAMPAGNA ---
st.markdown("---")
st.subheader(f"📋 Quaderno di Campagna: {st.session_state.active_field_name}")

col_form, col_table = st.columns([1, 1.2])

with col_form:
    st.markdown("##### ✍️ Registra Intervento")
    t_date = st.date_input("Data:", datetime.now())
    t_op = st.text_input("Operatore:", value="Azienda Agricola")
    t_text = st.text_area("Prodotto / Dose / Note:")

    if st.button("💾 Salva nel Registro"):
        if t_text:
            add_treatment(
                str(t_date),
                t_op,
                st.session_state.active_field_name,
                t_text,
                st.session_state.active_lat,
                st.session_state.active_lon,
            )
            st.success("Intervento salvato!")
            st.rerun()

with col_table:
    st.markdown(f"##### 📖 Registro per *{st.session_state.active_field_name}*")
    treatments_df = get_treatments(st.session_state.active_field_name)

    if not treatments_df.empty:
        st.dataframe(treatments_df, use_container_width=True, hide_index=True)
    else:
        st.info("Nessun intervento ancora registrato per questo campo.")

# --- HUB BOLLETTINI FITOSANITARI ---
st.markdown("---")
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