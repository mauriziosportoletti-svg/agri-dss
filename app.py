import base64
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
    .info-banner {background-color: #e8f5e9; border-left: 6px solid #2e7d32; padding: 16px; border-radius: 8px; margin-top: 15px; margin-bottom: 20px;}
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


def force_seed_organic_tavernelle_fields():
    """Popola il DB con 15 campi distribuiti in modo organico e sfalsato attorno a Tavernelle"""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fields")  # Reset pulito per garantire le nuove coordinate sfalsate

        base_lat, base_lon = 43.007721, 12.146461
        crops = ["Vigneto Sangiovese", "Oliveto Frantoio", "Seminativo Grano", "Noccioleto", "Vigneto Trebbiano"]

        # Coordinate realistiche e sfalsate (non a scacchiera)
        staggered_offsets = [
            (0.0031, 0.0042),
            (-0.0045, 0.0078),
            (0.0082, -0.0035),
            (-0.0061, -0.0089),
            (0.0115, 0.0021),
            (-0.0122, 0.0045),
            (0.0018, -0.0112),
            (0.0067, 0.0095),
            (-0.0084, 0.0121),
            (0.0141, -0.0078),
            (-0.0025, 0.0152),
            (0.0098, 0.0134),
            (-0.0135, -0.0041),
            (0.0052, -0.0158),
            (-0.0091, -0.0142),
        ]

        for idx, (off_lat, off_lon) in enumerate(staggered_offsets, start=1):
            name = f"Tavernelle {idx:03d}"
            crop = crops[(idx - 1) % len(crops)]
            c.execute(
                "INSERT INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
                (name, crop, base_lat + off_lat, base_lon + off_lon),
            )
        conn.commit()


init_db()
force_seed_organic_tavernelle_fields()


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


# --- GEOMETRIA ESAGONI AD ALVEARE ---
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
            desc = f"⚠️ Rischio Temporale Severo / Grandine previsto per il {t_str}."
            return "ATTENZIONE - GRANDINE 🔴", desc, {"type": "Rischio Grandine", "desc": desc}

        if temp < 2.0:
            desc = f"❄️ Rischio Gelata ({temp}°C) per il {t_str}."
            return "ATTENZIONE - GELATA 🟡", desc, {"type": "Rischio Gelata", "desc": desc}

    return "NORMALE 🟢", "Nessuna criticità severa rilevata nelle prossime 48h.", None


# --- SIDEBAR: REGISTRO CAMPI ---
fields_df = get_fields()

st.sidebar.title("🌾 Distretto Tavernelle")
st.sidebar.caption("Seleziona un campo dal registro (15 Campi Sfalsati)")

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
    f"**Campo Attivo:**\n"
    f"- **Nome:** {st.session_state.active_field_name}\n"
    f"- **Coltura:** {st.session_state.active_crop}\n"
    f"- **Lat:** {st.session_state.active_lat:.5f}\n"
    f"- **Lon:** {st.session_state.active_lon:.5f}"
)

active_field_data = get_active_field_data(st.session_state.active_field_name)

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Control Room Distretto Tavernelle")
st.caption(f"📍 **Focus Attuale**: {st.session_state.active_field_name} ({st.session_state.active_crop})")

w_data = fetch_weather_advanced(st.session_state.active_lat, st.session_state.active_lon)
risk_level, risk_description, pending_alert = analyze_weather_risks(w_data)

# --- METRIC CARDS ---
col1, col2, col3 = st.columns(3)
col1.metric("🛰️ Vigore Vegetativo (NDVI)", "0.74 (Ottimo)", "Sentinel-2 L2A")
col2.metric("🛡️ Stato Fitosanitario DSS", risk_level)
rain_sum = sum(w_data["daily"]["precipitation_sum"][:7]) if w_data and "daily" in w_data else 0.0
col3.metric("🌧️ Pioggia 7gg (ICON 2.2km)", f"{rain_sum:.1f} mm")

st.markdown("---")

# --- MAPPA INTERATTIVA (VISTA GENERALE VS FOCUS) ---
st.subheader("🗺️ Mappa Territoriale & Struttura ad Alveare")

tipo_vista = st.radio(
    "Modalità Visualizzazione Mappa:",
    ["🌍 Vista Generale Distretto (Tutti i 15 Campi con i propri Esagoni)", "🎯 Focus Campo Selezionato (Solo il Campo Attivo)"],
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

# LOGICA MAPPA
if tipo_vista.startswith("🌍"):
    # VISTA GENERALE: Disegna TUTTI i campi con i loro rispettivi esagoni attorno
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

        # Corona di 6 esagoni per ogni campo
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

    # Esagono centrale + 6 esagoni
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

st_folium(m, width=900, height=520, key="tavernelle_map")

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