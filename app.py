import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime, timedelta

st.set_page_config(page_title="AgriDSS Community & Satellite", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS comments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, text TEXT, crop TEXT, lat REAL, lon REAL)''')
    c.execute("PRAGMA table_info(comments)")
    columns = [col[1] for col in c.fetchall()]
    if 'lat' not in columns:
        c.execute("ALTER TABLE comments ADD COLUMN lat REAL")
    if 'lon' not in columns:
        c.execute("ALTER TABLE comments ADD COLUMN lon REAL")
    conn.commit()
    conn.close()

def reset_db():
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS comments")
    conn.commit()
    conn.close()
    init_db()

init_db()

def add_comment(author, text, crop, lat, lon):
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO comments (date, author, text, crop, lat, lon) VALUES (?, ?, ?, ?, ?, ?)", 
              (date_str, author, text, crop, lat, lon))
    conn.commit()
    conn.close()

def get_comments():
    conn = sqlite3.connect("community_comments.db")
    df_comments = pd.read_sql("SELECT date, author, text, crop, lat, lon FROM comments ORDER BY id DESC", conn)
    conn.close()
    return df_comments

# --- API METEO (Con Cache) ---
@st.cache_data(ttl=3600)
def fetch_weather(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max&timezone=Europe/Berlin"
    response = requests.get(url, timeout=5)
    return response.json()

# --- API COPERNICUS SATELLITE ---
def get_cdse_token(client_id, client_secret):
    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
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
                { id: 'ndvi', bands: 1 },
                { id: 'msavi', bands: 1 },
                { id: 'ndmi', bands: 1 },
                { id: 'ndwi', bands: 1 },
                { id: 'dataMask', bands: 1 }
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
            "resx": 10, "resy": 10,
            "evalscript": evalscript
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
    if not data or "output" not in data:
        return pd.DataFrame()
    responses = data.get("output", {}).get("responses", [])
    records = []
    for resp in responses:
        interval = resp.get("interval", {})
        date_from = interval.get("from", "")[:10]
        outputs = resp.get("outputs", {})
        
        def get_mean(key):
            try:
                val = outputs.get(key, {}).get("bands", {}).get("B0", {}).get("stats", {}).get("mean")
                return float(val) if val is not None else None
            except:
                return None
                
        ndvi = get_mean("ndvi")
        if ndvi is not None and not pd.isna(ndvi):
            records.append({
                "Data": date_from,
                "NDVI": ndvi,
                "MSAVI": get_mean("msavi"),
                "NDMI": get_mean("ndmi"),
                "NDWI": get_mean("ndwi")
            })
    return pd.DataFrame(records)

# --- SESSION STATE ---
if 'lat' not in st.session_state:
    st.session_state.lat = 43.007721
if 'lon' not in st.session_state:
    st.session_state.lon = 12.146461
if 'df_meteo' not in st.session_state:
    st.session_state.df_meteo = None
if 'df_sat' not in st.session_state:
    st.session_state.df_sat = None

# --- SIDEBAR ---
st.sidebar.header("⚙️ Configurazione Campo")
coltura = st.sidebar.selectbox("Seleziona la Coltura:", ["Oliveto", "Vigneto"])

st.sidebar.markdown("---")
st.sidebar.header("🗑️ Gestione Dati")
if st.sidebar.button("Svuota Registro Community", type="secondary"):
    reset_db()
    st.sidebar.success("Registro azzerato!")
    st.rerun()

st.title("🌾 AgriDSS: Mappa, Meteo, Satellite & Community")

# --- MAPPA ---
st.subheader("📍 Mappa Appezzamenti (Clicca sul tuo campo)")
m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=12)

folium.Marker(
    [st.session_state.lat, st.session_state.lon],
    popup="<b>Particella Selezionata</b>",
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

df_comm_map = get_comments()
for _, row in df_comm_map.iterrows():
    if pd.notnull(row['lat']) and pd.notnull(row['lon']):
        popup_text = f"<b>{row['author']}</b> ({row['crop']})<br>{row['text']}"
        folium.Marker(
            [row['lat'], row['lon']],
            popup=popup_text,
            icon=folium.Icon(color="orange", icon="info-sign")
        ).add_to(m)

map_data = st_folium(m, width=700, height=350)

if map_data and map_data.get("last_clicked"):
    st.session_state.lat = map_data["last_clicked"]["lat"]
    st.session_state.lon = map_data["last_clicked"]["lng"]
    st.rerun()

st.success(f"🎯 Particella attiva: Lat {st.session_state.lat:.5f}, Lon {st.session_state.lon:.5f}")

# --- ANALISI INTEGRATA (METEO + SATELLITE) ---
st.subheader("📊 Analisi DSS & Monitoraggio Satellitare Sentinel-2")

if st.button("🚀 Esegui Analisi Completa (Meteo + Satellite)", type="primary"):
    with st.spinner("Interrogazione server meteo e Copernicus in corso..."):
        w_data = fetch_weather(st.session_state.lat, st.session_state.lon)
        if "daily" in w_data:
            daily = w_data["daily"]
            df_m = pd.DataFrame({
                "Data": daily["time"],
                "Temp Max (°C)": daily["temperature_2m_max"],
                "Temp Min (°C)": daily["temperature_2m_min"],
                "Pioggia (mm)": daily["precipitation_sum"],
                "Prob. Pioggia (%)": daily["precipitation_probability_max"]
            })
            st.session_state.df_meteo = df_m
        
        sat_json = fetch_satellite_statistics(st.session_state.lat, st.session_state.lon)
        if sat_json:
            st.session_state.df_sat = parse_satellite_json(sat_json)
        else:
            st.session_state.df_sat = pd.DataFrame()

if st.session_state.df_meteo is not None:
    st.markdown("### 📋 Dati Meteo Recenti")
    st.dataframe(st.session_state.df_meteo, use_container_width=True)

if st.session_state.df_sat is not None and not st.session_state.df_sat.empty:
    st.markdown("### 🛰️ Storico Satellitare (Ultimi 45 giorni - Sentinel-2)")
    st.dataframe(st.session_state.df_sat, use_container_width=True)
    st.line_chart(st.session_state.df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])
elif st.session_state.df_sat is not None:
    st.warning("⚠️ Nessun dato satellitare valido trovato per quest'area o filtri nuvole attivi.")

st.markdown("---")

# --- COMMUNITY ---
st.subheader("👥 Bacheca della Community")
col1, col2 = st.columns(2)

with col1:
    st.markdown("**Lascia una segnalazione:**")
    autore = st.text_input("Tuo Nome / Azienda:")
    testo_segnalazione = st.text_area("Segnalazione (es. Mosca vista, Trattamento fatto):")
    if st.button("Pubblica"):
        if autore and testo_segnalazione:
            add_comment(autore, testo_segnalazione, coltura, st.session_state.lat, st.session_state.lon)
            st.success("Pubblicato!")
            st.rerun()
        else:
            st.warning("Compila tutti i campi.")

with col2:
    st.markdown("**Ultime notizie:**")
    df_comm = get_comments()
    if not df_comm.empty:
        for _, row in df_comm.iterrows():
            st.markdown(f"🗓️ *{row['date']}* | **{row['author']}** ({row['crop']})\n> {row['text']}")
            st.markdown("---")
    else:
        st.info("Nessuna segnalazione.")