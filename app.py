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

# --- CONFIGURAZIONE PAGINA ---
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
    .wa-preview {background-color: #e5ddd5; border-radius: 8px; padding: 12px; font-family: sans-serif; color: #111; margin-top: 10px;}
    .wa-bubble {background-color: #dcf8c6; padding: 8px 12px; border-radius: 7.5px; margin-bottom: 5px; font-size: 14px;}
    </style>
"""
st.markdown(css_custom, unsafe_allow_html=True)

DB_PATH = "agri_dss.db"


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


def force_seed_tavernelle_15_fields():
    """Forza il popolamento pulito dei 15 campi Tavernelle 001 - 015"""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM fields WHERE name LIKE 'Tavernelle %'")
        count = c.fetchone()[0]

        # Se non ci sono esattamente i 15 campi formattati correttamente, ricrea la griglia
        if count < 15:
            c.execute("DELETE FROM fields")  # Pulisce vecchi record errati come 'tavernelle'
            base_lat, base_lon = 43.007721, 12.146461
            crops = ["Vigneto", "Oliveto", "Seminativo", "Noccioleto"]

            idx = 1
            for row in range(4):
                for col in range(4):
                    if idx <= 15:
                        name = f"Tavernelle {idx:03d}"
                        crop = crops[(idx - 1) % len(crops)]
                        # Distanzia i campi in modo uniforme attorno a Tavernelle
                        f_lat = base_lat + (row - 1.5) * 0.006
                        f_lon = base_lon + (col - 1.5) * 0.009
                        c.execute(
                            "INSERT INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
                            (name, crop, f_lat, f_lon),
                        )
                        idx += 1
            conn.commit()


init_db()
force_seed_tavernelle_15_fields()


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


# --- GEOMETRIA PERFETTA PER L'ALVEARE ---
def get_hexagon_coords(center_lat, center_lon, radius_km=0.25):
    """Calcola i 6 vertici dell'esagono"""
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


def get_adjacent_hexagon_centers(center_lat, center_lon, radius_km=0.25):
    """Calcola i centri esatti dei 6 esagoni adiacenti perfettamente combacianti"""
    dist_km = radius_km * math.sqrt(3)  # Distanza esatta centro-centro
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
            "notes": "Rilievo agronomico nella norma.",
            "treatments_count": 1,
            "validated": True,
            "photo_b64": None,
            "phases": {"Potatura": True, "Concimazione": True, "Trattamento": False, "Raccolta": False},
        }
    return st.session_state.field_data[field_name]


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
    capes = hourly.get("cape", [])
    gusts = hourly.get("wind_gusts_10m", [])
    wcodes = hourly.get("weathercode", [])
    temps = hourly.get("temperature_2m", [])
    times = hourly.get("time", [])

    for i in range(min(48, len(times))):
        c = capes[i] if i < len(capes) and capes[i] is not None else 0
        g = gusts[i] if i < len(gusts) and gusts[i] is not None else 0
        wc = wcodes[i] if i < len(wcodes) and wcodes[i] is not None else 0
        temp = temps[i] if i < len(temps) and temps[i] is not None else 20
        t_str = times[i]

        if (c > 1000 and g > 45) or wc in [95, 96, 99]:
            desc = f"⚠️ Rischio Temporale Severo / Grandine previsto per il {t_str}. Raffiche: {g} km/h."
            return "ATTENZIONE - GRANDINE 🔴", desc, {"type": "Rischio Grandine", "desc": desc}

        if temp < 2.0:
            desc = f"❄️ Rischio Gelata ({temp}°C) per il {t_str}."
            return "ATTENZIONE - GELATA 🟡", desc, {"type": "Rischio Gelata", "desc": desc}

    return "NORMALE 🟢", "Nessuna criticità severa rilevata nelle prossime 48h.", None


# --- SIDEBAR: SELEZIONE CAMPI TAVERNELLE ---
fields_df = get_fields()

st.sidebar.title("🌾 Distretto Tavernelle")
st.sidebar.caption("Seleziona il campo dal registro (15 Campi)")

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

st.sidebar.markdown("---")
st.sidebar.info(
    f"**Campo Selezionato:**\n"
    f"- **Nome:** {st.session_state.active_field_name}\n"
    f"- **Coltura:** {st.session_state.active_crop}\n"
    f"- **Lat:** {st.session_state.active_lat:.5f}\n"
    f"- **Lon:** {st.session_state.active_lon:.5f}"
)

active_field_data = get_active_field_data(st.session_state.active_field_name)

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Control Room Distretto Tavernelle")
st.caption(f"📍 **Focus Campo**: {st.session_state.active_field_name} ({st.session_state.active_crop})")

w_data = fetch_weather_advanced(st.session_state.active_lat, st.session_state.active_lon)
risk_level, risk_description, pending_alert = analyze_weather_risks(w_data)

# --- METRIC CARDS ---
col1, col2, col3 = st.columns(3)
col1.metric("🛰️ Vigore Vegetativo (NDVI)", "0.72 (Ottimo)", "Sentinel-2 L2A")
col2.metric("🛡️ Stato Fitosanitario DSS", risk_level)
rain_sum = sum(w_data["daily"]["precipitation_sum"][:7]) if w_data and "daily" in w_data else 0.0
col3.metric("🌧️ Pioggia 7gg (ICON 2.2km)", f"{rain_sum:.1f} mm")

st.info(f"💡 **Diagnosi DSS per {st.session_state.active_field_name}**: {risk_description}")

st.markdown("---")

# --- MAPPA INTERATTIVA CON ALVEARE COMPATTO ---
st.subheader("🗺️ Mappa Territoriale & Struttura ad Alveare")

tipo_vista = st.radio(
    "Modalità Visualizzazione Mappa:",
    ["🌍 Vista Globale Distretto (Tutti i 15 Campi)", "🎯 Focus Campo Selezionato con Alveare"],
    horizontal=True,
)

# Impostazione centro e zoom ottimali
if tipo_vista.startswith("🌍"):
    center_lat, center_lon, zoom_level = 43.007721, 12.146461, 14
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

# 1. DISEGNA I 15 CAMPI COME RETTANGOLI BEN DEFINITI
all_bounds = []
for idx, f in fields_df.iterrows():
    f_lat, f_lon, f_name, f_crop = f["lat"], f["lon"], f["name"], f["crop"]
    is_active = (f_name == st.session_state.active_field_name)

    # Dimensioni rettangolo terreno
    delta_lat, delta_lon = 0.0012, 0.0018
    bounds = [[f_lat - delta_lat, f_lon - delta_lon], [f_lat + delta_lat, f_lon + delta_lon]]
    all_bounds.extend(bounds)

    rect_color = "#ffeb3b" if is_active else "#ffffff"
    fill_color = "#fbc02d" if is_active else "#37474f"

    folium.Rectangle(
        bounds=bounds,
        color=rect_color,
        weight=4 if is_active else 2,
        fill=True,
        fill_color=fill_color,
        fill_opacity=0.8 if is_active else 0.5,
        tooltip=f"<b>{f_name}</b> ({f_crop})",
    ).add_to(m)

    # 2. DISEGNA L'ALVEARE PERFETTO ATTORNO AL CAMPO SELEZIONATO
    if is_active or not tipo_vista.startswith("🌍"):
        radius_km = 0.22  # Dimensione ideale esagoni
        
        # Esagono centrale (Campo attivo)
        central_hex = get_hexagon_coords(f_lat, f_lon, radius_km=radius_km)
        folium.Polygon(
            locations=central_hex,
            color="#2e7d32",
            weight=3,
            fill=True,
            fill_color="#4caf50",
            fill_opacity=0.35,
            popup=f"<b>📍 Central: {f_name}</b>",
        ).add_to(m)

        # 6 Esagoni adiacenti perfettamente incastrati (Alveare)
        adj_centers = get_adjacent_hexagon_centers(f_lat, f_lon, radius_km=radius_km)
        for s_idx, (c_lat, c_lon) in enumerate(adj_centers):
            diag, hex_col = stati_fittizi[(idx + s_idx) % len(stati_fittizi)]
            sat_hex = get_hexagon_coords(c_lat, c_lon, radius_km=radius_km)
            folium.Polygon(
                locations=sat_hex,
                color=hex_col,
                weight=2,
                dash_array="4, 4",
                fill=True,
                fill_color=hex_col,
                fill_opacity=0.25,
                popup=f"<b>Settore Distretto #{s_idx+1}</b><br>Rischio: {diag}",
            ).add_to(m)

st_folium(m, width=900, height=520, key="tavernelle_map")

st.markdown("---")

# --- PANNELLO GESTIONE CAMPO SELEZIONATO ---
col_p1, col_p2 = st.columns([1, 1])

with col_p1:
    with st.expander(f"🛠️ Controllo Stato & Fasi: {st.session_state.active_field_name}", expanded=True):
        active_field_data["notes"] = st.text_input("📝 Note Operative / Diagnosi Campo:", value=active_field_data["notes"])
        active_field_data["treatments_count"] = st.number_input("🚜 N° Trattamenti Eseguiti:", value=int(active_field_data["treatments_count"]), min_value=0)

        st.markdown("##### ⏱️ Fasi Colturali")
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
        uploaded_file = st.file_uploader("Carica foto ispezione (JPG/PNG):", type=["png", "jpg", "jpeg"])
        if uploaded_file is not None:
            bytes_data = uploaded_file.getvalue()
            active_field_data["photo_b64"] = base64.b64encode(bytes_data).decode()
            st.success("Foto allegata con successo!")
            st.rerun()

        if active_field_data.get("photo_b64"):
            img_bytes = base64.b64decode(active_field_data["photo_b64"])
            st.image(img_bytes, caption=f"Rilevamento: {st.session_state.active_field_name}", use_container_width=True)
            if st.button("🗑️ Rimuovi Foto"):
                active_field_data["photo_b64"] = None
                st.rerun()
        else:
            st.info("Nessuna foto presente per questo campo.")

# --- GENERATORE WHATSAPP ---
with st.expander("📱 Invio Allerta WhatsApp al Proprietario"):
    msg_template = (
        f"Ciao! 🌾 Report per il campo '{st.session_state.active_field_name}' ({st.session_state.active_crop}).\n"
        f"Stato DSS: {risk_level}.\n"
        f"Diagnosi: {risk_description}\n"
        f"Note Agronomo: {active_field_data['notes']}"
    )
    st.markdown(f'<div class="wa-preview"><div class="wa-bubble">{msg_template.replace("\n", "<br>")}</div></div>', unsafe_allow_html=True)

    encoded_msg = urllib.parse.quote(msg_template)
    st.markdown(f"[🚀 Invia su WhatsApp](https://wa.me/?text={encoded_msg})", unsafe_allow_html=True)

# --- QUADERNO DI CAMPAGNA ---
st.markdown("---")
st.subheader(f"📋 Quaderno di Campagna: {st.session_state.active_field_name}")

col_form, col_table = st.columns([1, 1.2])

with col_form:
    st.markdown("##### ✍️ Registra Intervento")
    t_date = st.date_input("Data:", datetime.now())
    t_op = st.text_input("Operatore:", value="Azienda Agricola")
    t_text = st.text_area("Prodotto / Dose / Note:")

    if st.button("💾 Salva in Quaderno"):
        if t_text:
            add_treatment(
                str(t_date),
                t_op,
                st.session_state.active_field_name,
                t_text,
                st.session_state.active_lat,
                st.session_state.active_lon,
            )
            st.success("Operazione registrata!")
            st.rerun()

with col_table:
    st.markdown(f"##### 📖 Registro per *{st.session_state.active_field_name}*")
    treatments_df = get_treatments(st.session_state.active_field_name)

    if not treatments_df.empty:
        st.dataframe(treatments_df, use_container_width=True, hide_index=True)
    else:
        st.info("Nessuna operazione registrata per questo campo.")