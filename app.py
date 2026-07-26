import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime, timedelta

st.set_page_config(page_title="AgriDSS - Gestione Campi & Allarmi", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    # Tabella Registro Trattamenti / Note
    c.execute('''CREATE TABLE IF NOT EXISTS treatments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, treatment_date TEXT, operator TEXT, field_name TEXT, text TEXT, lat REAL, lon REAL)''')
    # Tabella Segnalazioni Anonime (Waze Agricolo)
    c.execute('''CREATE TABLE IF NOT EXISTS alerts 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, alert_type TEXT, description TEXT, lat REAL, lon REAL)''')
    # Tabella Anagrafica Campi
    c.execute('''CREATE TABLE IF NOT EXISTS fields 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, crop TEXT, lat REAL, lon REAL)''')
    conn.commit()
    conn.close()

init_db()

# --- FUNZIONI DATABASE ---
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

def add_treatment(t_date, operator, field_name, text, lat, lon):
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    c.execute("INSERT INTO treatments (treatment_date, operator, field_name, text, lat, lon) VALUES (?, ?, ?, ?, ?, ?)", 
              (t_date, operator, text, field_name, lat, lon))
    conn.commit()
    conn.close()

def get_treatments():
    conn = sqlite3.connect("agri_dss.db")
    df = pd.read_sql("SELECT treatment_date as 'Data Trattamento', field_name as 'Campo', operator as 'Operatore', text as 'Dettaglio Trattamento' FROM treatments ORDER BY id DESC", conn)
    conn.close()
    return df

def add_alert(alert_type, description, lat, lon):
    conn = sqlite3.connect("agri_dss.db")
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO alerts (created_at, alert_type, description, lat, lon) VALUES (?, ?, ?, ?, ?)", 
              (now_str, alert_type, description, lat, lon))
    conn.commit()
    conn.close()

def get_alerts():
    conn = sqlite3.connect("agri_dss.db")
    df = pd.read_sql("SELECT created_at, alert_type, description, lat, lon FROM alerts ORDER BY id DESC", conn)
    conn.close()
    return df

# --- API METEO AVANZATO (Con Umidità Aria e Suolo) ---
@st.cache_data(ttl=3600)
def fetch_weather_advanced(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
        f"et0_fao_evapotranspiration,wind_speed_10m_max,wind_direction_10m_dominant"
        f"&hourly=relative_humidity_2m,soil_moisture_0_to_7cm"
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

# --- API SATELLITE ---
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

# --- SIDEBAR: GESTIONE CAMPI ---
st.sidebar.title("🏡 I Miei Campi")

fields_df = get_fields()
if not fields_df.empty:
    field_names = ["-- Seleziona Campo Salvato --"] + fields_df["name"].tolist()
    selected_option = st.sidebar.selectbox("Carica un tuo Campo:", field_names)
    
    if selected_option != "-- Seleziona Campo Salvato --":
        row = fields_df[fields_df["name"] == selected_option].iloc[0]
        st.session_state.lat = row["lat"]
        st.session_state.lon = row["lon"]
        st.session_state.current_field = row["name"]
        st.sidebar.success(f"Caricato: **{row['name']}** ({row['crop']})")

st.sidebar.markdown("---")
st.sidebar.subheader("➕ Salva Posizione Attuale")
new_field_name = st.sidebar.text_input("Nome del Campo:")
coltura_sel = st.sidebar.selectbox("Coltura:", ["Oliveto", "Vigneto", "Seminativo", "Altro"])

if st.sidebar.button("💾 Salva Campo"):
    if new_field_name:
        save_field(new_field_name, coltura_sel, st.session_state.lat, st.session_state.lon)
        st.sidebar.success(f"Campo '{new_field_name}' salvato!")
        st.rerun()
    else:
        st.sidebar.warning("Inserisci un nome.")

# --- MAIN PAGE ---
st.title("🌾 AgriDSS: Monitoraggio & Allarmi Territoriali")
st.info(f"📍 **Campo Selezionato**: {st.session_state.current_field} | **Lat**: {st.session_state.lat:.5f} | **Lon**: {st.session_state.lon:.5f}")

# --- MAPPA INTERATTIVA CON SEGNALAZIONI ---
m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=13)

# Marker del campo attivo (Verde)
folium.Marker(
    [st.session_state.lat, st.session_state.lon], 
    popup=st.session_state.current_field, 
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

# Marker delle Segnalazioni Anonime (Rosso)
alerts_df = get_alerts()
if not alerts_df.empty:
    for idx, alert in alerts_df.iterrows():
        popup_html = f"<b>⚠️ {alert['alert_type']}</b><br>{alert['description']}<br><i>{alert['created_at']}</i>"
        folium.Marker(
            [alert["lat"], alert["lon"]],
            popup=popup_html,
            icon=folium.Icon(color="red", icon="warning", prefix="fa")
        ).add_to(m)

map_data = st_folium(m, width=700, height=350)
if map_data and map_data.get("last_clicked"):
    st.session_state.lat = map_data["last_clicked"]["lat"]
    st.session_state.lon = map_data["last_clicked"]["lng"]
    st.session_state.current_field = "Punto cliccato su Mappa"
    st.rerun()

# --- SEZIONE SEGNALAZIONE ANONIMA SULLA MAPPA ---
with st.expander("🚨 Invia una Segnalazione Anonima (Mosca, Peronospora, Gelata, ecc.)"):
    st.caption("Fai prima click sulla mappa nel punto esatto del problema, poi compila il modulo qui sotto:")
    alert_type = st.selectbox("Tipo di Problema/Avvistamento:", [
        "Avvistamento Mosca Olearia", 
        "Attacco Peronospora / Oidio", 
        "Siccità Severa / Stress Idrico", 
        "Danni da Gelata / Grandine", 
        "Altra Parassitosi / Anomalia"
    ])
    alert_desc = st.text_input("Dettagli aggiuntivi (es. Presenza 5% catture in trappola):")
    if st.button("📢 Invia Segnalazione Anonima", type="primary"):
        add_alert(alert_type, alert_desc or "Nessun dettaglio", st.session_state.lat, st.session_state.lon)
        st.success("Segnalazione pubblicata sulla mappa per tutti gli agricoltori della zona!")
        st.rerun()

st.markdown("---")

# --- BOTTONE ANALISI METEO & SATELLITE ---
if st.button("🚀 Scarica Dati Meteo Completi & Satellite", type="secondary"):
    with st.spinner("Elaborazione dati meteo e indici satellitari in corso..."):
        
        # 1. METEO AVANZATO
        w_data = fetch_weather_advanced(st.session_state.lat, st.session_state.lon)
        if w_data and "daily" in w_data:
            d = w_data["daily"]
            
            # Calcolo media Umidità Oraria per portarla a livello giornaliero
            df_hourly = pd.DataFrame(w_data["hourly"])
            df_hourly['date'] = df_hourly['time'].str[:10]
            daily_humidity = df_hourly.groupby('date')['relative_humidity_2m'].mean().round(1).tolist()
            daily_soil_m = df_hourly.groupby('date')['soil_moisture_0_to_7cm'].mean().round(3).tolist()

            df_m = pd.DataFrame({
                "Data": d["time"],
                "Temp Max (°C)": d["temperature_2m_max"],
                "Temp Min (°C)": d["temperature_2m_min"],
                "Umidità Aria Media (%)": daily_humidity[:len(d["time"])],
                "Umidità Suolo 0-7cm": daily_soil_m[:len(d["time"])],
                "Pioggia (mm)": d["precipitation_sum"],
                "Prob. Pioggia (%)": d.get("precipitation_probability_max", [None]*len(d["time"])),
                "Evapotraspirazione ET0 (mm)": d["et0_fao_evapotranspiration"],
                "Vento Max (km/h)": d["wind_speed_10m_max"]
            })
            st.markdown("### ☀️ Meteo Avanzato Completo (Storico 7gg + Previsione 7gg)")
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
            st.warning("Nessun passaggio satellitare senza nuvole trovato di recente.")

# --- SEZIONE REGISTRO TRATTAMENTI CON DATA PERSONALIZZABILE ---
st.markdown("---")
st.subheader("📋 Registro Trattamenti & Quaderno di Campagna")

col_form, col_hist = st.columns([1, 1])

with col_form:
    st.markdown("##### ✍️ Aggiungi un Trattamento")
    t_date = st.date_input("Data Effettiva del Trattamento:", datetime.now())
    t_operator = st.text_input("Operatore / Applicatore:", value="Azienda")
    t_details = st.text_area("Prodotto / Trattamento Effettuato (es. Rame metallico 2 kg/ha):")
    
    if st.button("💾 Salva nel Registro"):
        if t_details:
            add_treatment(str(t_date), t_operator, st.session_state.current_field, t_details, st.session_state.lat, st.session_state.lon)
            st.success("Trattamento salvato nel registro!")
            st.rerun()
        else:
            st.warning("Inserisci i dettagli del trattamento.")

with col_hist:
    st.markdown("##### 📖 Storico Trattamenti Effettuati")
    treat_df = get_treatments()
    if not treat_df.empty:
        st.dataframe(treat_df, use_container_width=True)
    else:
        st.info("Nessun trattamento ancora salvato.")