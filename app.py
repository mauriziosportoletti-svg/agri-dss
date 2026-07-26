import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime, timedelta

st.set_page_config(page_title="AgriDSS - Gestione Campi & Satellite", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP (Gestione Campi & Commenti) ---
def init_db():
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    # Tabella Commenti
    c.execute('''CREATE TABLE IF NOT EXISTS comments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, text TEXT, crop TEXT, lat REAL, lon REAL)''')
    # Tabella Anagrafica Campi
    c.execute('''CREATE TABLE IF NOT EXISTS fields 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, crop TEXT, lat REAL, lon REAL)''')
    conn.commit()
    conn.close()

init_db()

# Funzioni Anagrafica Campi
def save_field(name, crop, lat, lon):
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)", (name, crop, lat, lon))
    conn.commit()
    conn.close()

def get_fields():
    conn = sqlite3.connect("agri_dss.db")
    df = pd.read_sql("SELECT name, crop, lat, lon FROM fields", conn)
    conn.close()
    return df

# Funzioni Community / Registro Attività
def add_comment(author, text, crop, lat, lon):
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO comments (date, author, text, crop, lat, lon) VALUES (?, ?, ?, ?, ?, ?)", 
              (date_str, author, text, crop, lat, lon))
    conn.commit()
    conn.close()

def get_comments():
    conn = sqlite3.connect("agri_dss.db")
    df = pd.read_sql("SELECT date, author, text, crop, lat, lon FROM comments ORDER BY id DESC", conn)
    conn.close()
    return df

# --- API METEO AVANZATO (Storico 7 giorni passati + 7 giorni previsione) ---
@st.cache_data(ttl=3600)
def fetch_weather_advanced(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
        f"et0_fao_evapotranspiration,wind_speed_10m_max,wind_direction_10m_dominant"
        f"&hourly=soil_moisture_0_to_7cm,relative_humidity_2m"
        f"&past_days=7"
        f"&timezone=Europe/Berlin"
    )
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return None

# --- API COPERNICUS SATELLITE ---
def get_cdse_token(client_id, client_secret):
    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    payload = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        response = requests.post(auth_url, data=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get("access_token")
    except Exception:
        pass
    return None

@st.cache_data(ttl=86400)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"
    
    token = get_cdse_token(client_id, client_secret)
    if not token:
        st.error("Errore autenticazione Copernicus.")
        return None
        
    data_fine = datetime.utcnow().strftime("%Y-%m-%dT23:59:59Z")
    data_inizio = (datetime.utcnow() - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
    
    delta = 0.002
    bbox = [lon - delta, lat - delta, lon + delta, lat + delta]
    url_dati = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
    
    evalscript = """
    //VERSION=3
    function setup() {
        return {
            input: ['B04', 'B08', 'B11', 'B03', 'SCL', 'dataMask'],
            output: [
                { id: 'ndvi', bands: 1 }, { id: 'msavi', bands: 1 },
                { id: 'ndmi', bands: 1 }, { id: 'ndwi', bands: 1 }, { id: 'dataMask', bands: 1 }
            ]
        };
    }
    function evaluatePixel(s) {
        if ([3, 8, 9, 10, 11].includes(s.SCL)) {
            return { ndvi: [NaN], msavi: [NaN], ndmi: [NaN], ndwi: [NaN], dataMask: [0] };
        }
        let v_ndvi = (s.B08 - s.B04) / (s.B08 + s.B04);
        let v_msavi = (2 * s.B08 + 1 - Math.sqrt(Math.pow(2 * s.B08 + 1, 2) - 8 * (s.B08 - s.B04))) / 2;
        let v_ndmi = (s.B08 - s.B11) / (s.B08 + s.B11);
        let v_ndwi = (s.B03 - s.B08) / (s.B03 + s.B08);
        return { ndvi: [v_ndvi], msavi: [v_msavi], ndmi: [v_ndmi], ndwi: [v_ndwi], dataMask: [s.dataMask] };
    }
    """
    
    payload = {
        "input": {"bounds": {"bbox": bbox}, "data": [{"type": "sentinel-2-l2a"}]},
        "aggregation": {
            "timeRange": {"from": data_inizio, "to": data_fine},
            "aggregationInterval": {"of": "P1D"},
            "resx": 10, "resy": 10, "evalscript": evalscript
        },
        "calculations": {
            "ndvi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
            "msavi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
            "ndmi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
            "ndwi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}}
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    try:
        response = requests.post(url_dati, json=payload, headers=headers, timeout=20)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return None

def parse_satellite_json(data):
    if not data or "data" not in data:
        return pd.DataFrame()
    items = data.get("data", [])
    records = []
    for item in items:
        outputs = item.get("outputs", {})
        interval = item.get("interval", {})
        date_from = interval.get("from", "")[:10]
        def get_mean(key):
            try:
                val = outputs.get(key, {}).get("bands", {}).get("B0", {}).get("stats", {}).get("mean")
                if val is not None and str(val).lower() != "nan": return float(val)
            except Exception: pass
            return None
        ndvi = get_mean("ndvi")
        if ndvi is not None:
            records.append({
                "Data": date_from, 
                "NDVI": round(ndvi, 3),
                "MSAVI": round(get_mean("msavi") or 0, 3),
                "NDMI": round(get_mean("ndmi") or 0, 3),
                "NDWI": round(get_mean("ndwi") or 0, 3)
            })
    df = pd.DataFrame(records)
    if not df.empty: 
        df = df.sort_values(by="Data", ascending=True)
    return df

# --- SESSION STATE ---
if 'lat' not in st.session_state: st.session_state.lat = 43.007721
if 'lon' not in st.session_state: st.session_state.lon = 12.146461
if 'current_field' not in st.session_state: st.session_state.current_field = "Posizione Iniziale"

# --- SIDEBAR (ANAGRAFICA CAMPI) ---
st.sidebar.title("🏡 I Miei Campi")

fields_df = get_fields()
if not fields_df.empty:
    field_names = ["-- Seleziona un Campo Saved --"] + fields_df["name"].tolist()
    selected_option = st.sidebar.selectbox("Carica Campo Salvato:", field_names)
    
    if selected_option != "-- Seleziona un Campo Saved --":
        row = fields_df[fields_df["name"] == selected_option].iloc[0]
        st.session_state.lat = row["lat"]
        st.session_state.lon = row["lon"]
        st.session_state.current_field = row["name"]
        st.sidebar.success(f"Caricato: **{row['name']}** ({row['crop']})")

st.sidebar.markdown("---")
st.sidebar.subheader("➕ Salva Posizione Attuale")
new_field_name = st.sidebar.text_input("Nome del Campo (es. Oliveto Casa):")
coltura_sel = st.sidebar.selectbox("Coltura:", ["Oliveto", "Vigneto", "Seminativo", "Altro"])

if st.sidebar.button("💾 Salva Campo"):
    if new_field_name:
        save_field(new_field_name, coltura_sel, st.session_state.lat, st.session_state.lon)
        st.sidebar.success(f"Campo '{new_field_name}' salvato!")
        st.rerun()
    else:
        st.sidebar.warning("Inserisci un nome prima di salvare.")

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Mappa, Meteo Completo & Satellite")
st.info(f"📍 **Campo Attivo**: {st.session_state.current_field} | **Lat**: {st.session_state.lat:.5f} | **Lon**: {st.session_state.lon:.5f}")

# --- MAPPA INTERATTIVA ---
m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=14)
folium.Marker(
    [st.session_state.lat, st.session_state.lon], 
    popup=st.session_state.current_field, 
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

map_data = st_folium(m, width=700, height=300)
if map_data and map_data.get("last_clicked"):
    st.session_state.lat = map_data["last_clicked"]["lat"]
    st.session_state.lon = map_data["last_clicked"]["lng"]
    st.session_state.current_field = "Posizione Selezionata su Mappa"
    st.rerun()

# --- BOTTONE ANALISI ---
if st.button("🚀 Scarica Meteo Completo & Satellite", type="primary"):
    with st.spinner("Scaricamento dati meteo (storico + previsioni) e satellite in corso..."):
        
        # 1. METEO
        w_data = fetch_weather_advanced(st.session_state.lat, st.session_state.lon)
        if w_data and "daily" in w_data:
            d = w_data["daily"]
            df_m = pd.DataFrame({
                "Data": d["time"],
                "Temp Max (°C)": d["temperature_2m_max"],
                "Temp Min (°C)": d["temperature_2m_min"],
                "Pioggia (mm)": d["precipitation_sum"],
                "Prob. Pioggia (%)": d.get("precipitation_probability_max", [None]*len(d["time"])),
                "Evapotraspirazione ET0 (mm)": d["et0_fao_evapotranspiration"],
                "Vento Max (km/h)": d["wind_speed_10m_max"],
                "Direz. Vento (°)": d["wind_direction_10m_dominant"]
            })
            st.markdown("### ☀️ Meteo Avanzato (Storico 7gg + Previsione 7gg)")
            st.dataframe(df_m, use_container_width=True)
        else:
            st.error("Impossibile recuperare i dati meteo.")

        # 2. SATELLITE
        sat_json = fetch_satellite_statistics(st.session_state.lat, st.session_state.lon)
        df_sat = parse_satellite_json(sat_json)
        if not df_sat.empty:
            st.markdown("### 🛰️ Indici Satellitari Sentinel-2 (Ultimi 45 giorni)")
            st.dataframe(df_sat, use_container_width=True)
            st.line_chart(df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])
        else:
            st.warning("Nessun passaggio satellitare privo di nuvole trovato negli ultimi 45 giorni per questa posizione.")

# --- NOTE & REGISTRO ATTIVITÀ ---
st.markdown("---")
st.subheader("📝 Registro Note & Trattamenti Campo")

with st.expander("➕ Aggiungi Nota per questo campo"):
    author = st.text_input("Nome/Operatore:")
    note_text = st.text_area("Note o Trattamento Effettuato (es. Rameico effettuato oggi):")
    if st.button("Salva Nota"):
        if note_text:
            add_comment(author or "Anonimo", note_text, coltura_sel, st.session_state.lat, st.session_state.lon)
            st.success("Nota salvata nel registro!")
            st.rerun()

comments_df = get_comments()
if not comments_df.empty:
    st.markdown("##### 📖 Storico Note Registrate")
    st.dataframe(comments_df, use_container_width=True)