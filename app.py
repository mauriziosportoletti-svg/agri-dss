import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime

# Configurazione della pagina
st.set_page_config(page_title="AgriDSS Community", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP & RESET ---
def init_db():
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS comments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, text TEXT, crop TEXT, lat REAL, lon REAL)''')
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

# --- FUNZIONE PER SCARICARE IL METEO (DSS) ---
def fetch_meteo(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max&timezone=Europe/Berlin"
    response = requests.get(url).json()
    
    if "daily" in response:
        daily = response["daily"]
        df_meteo = pd.DataFrame({
            "Data": daily["time"],
            "Temp Max (°C)": daily["temperature_2m_max"],
            "Temp Min (°C)": daily["temperature_2m_min"],
            "Pioggia (mm)": daily["precipitation_sum"],
            "Prob. Pioggia (%)": daily["precipitation_probability_max"]
        })
        
        stati = []
        for _, row in df_meteo.iterrows():
            if row["Pioggia (mm)"] > 2:
                stati.append("🔴 [ALLERTA] Rischio Pioggia - Evita Trattamenti")
            elif row["Temp Max (°C)"] > 33:
                stati.append("🟡 [ATTENZIONE] Stress Termico / Caldo")
            else:
                stati.append("🟢 [OK] Condizioni Stabili")
        df_meteo["Stato Operativo"] = stati
        return df_meteo
    return None

# --- GESTIONE STATO ---
if 'current_lat' not in st.session_state:
    st.session_state.current_lat = 43.007721
if 'current_lon' not in st.session_state:
    st.session_state.current_lon = 12.146461

# Calcoliamo l'analisi meteo iniziale se non esiste
if 'analysis_df' not in st.session_state:
    st.session_state.analysis_df = fetch_meteo(st.session_state.current_lat, st.session_state.current_lon)

# --- LAYOUT PRINCIPALE & SIDEBAR ---
st.title("🌾 AgriDSS: Smart Farming & Registro Territoriale")
st.markdown("---")

st.sidebar.header("⚙️ Configurazione & Gestione")
coltura = st.sidebar.selectbox("Coltura di Riferimento:", ["Oliveto", "Vigneto"])

st.sidebar.markdown("---")
if st.sidebar.button("🗑️ Svuota Registro (Cancella Tutti i Messaggi)", type="secondary"):
    reset_db()
    st.sidebar.success("Registro azzerato con successo!")
    st.rerun()

# --- MAPPA INTERATTIVA FLUIDA ---
st.subheader("📍 Mappa Appezzamenti & Scouting")
st.markdown("💡 *Tocca o clicca un punto qualsiasi della mappa per selezionare la particella e aggiornare istantaneamente il meteo.*")

m = folium.Map(location=[st.session_state.current_lat, st.session_state.current_lon], zoom_start=12)

# Marker verde (particella attiva)
folium.Marker(
    [st.session_state.current_lat, st.session_state.current_lon],
    popup="<b>Particella Selezionata</b>",
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

# Caricamento pin arancioni della community
conn = sqlite3.connect("community_comments.db")
df_comm = pd.read_sql("SELECT id, date, author, text, crop, lat, lon FROM comments ORDER BY id DESC", conn)
conn.close()

for _, row in df_comm.iterrows():
    if pd.notnull(row['lat']) and pd.notnull(row['lon']):
        popup_text = f"<b>{row['author']}</b> ({row['crop']})<br><i>{row['date']}</i><br>{row['text']}"
        folium.Marker(
            [row['lat'], row['lon']],
            popup=popup_text,
            icon=folium.Icon(color="orange", icon="info-sign")
        ).add_to(m)

# Mappa ottimizzata
map_data = st_folium(m, width=1000, height=400, returned_objects=["last_clicked"])

# Se l'utente clicca sulla mappa, aggiorniamo coordinate E ricalcoliamo il meteo al volo!
if map_data and map_data.get("last_clicked"):
    st.session_state.current_lat = map_data["last_clicked"]["lat"]
    st.session_state.current_lon = map_data["last_clicked"]["lng"]
    st.session_state.analysis_df = fetch_meteo(st.session_state.current_lat, st.session_state.current_lon)
    st.rerun()

st.success(f"🎯 **Particella Attiva Selezionata:** Latitudine {st.session_state.current_lat:.5f}, Longitudine {st.session_state.current_lon:.5f}")

# --- ANALISI DSS & METEO (Visibile e reattiva) ---
st.markdown("### 📊 Analisi DSS & Rischio Fitosanitario della Particella Selezionata")

if st.button("🚀 Aggiorna Analisi Meteo", type="primary"):
    st.session_state.analysis_df = fetch_meteo(st.session_state.current_lat, st.session_state.current_lon)
    st.rerun()

if st.session_state.analysis_df is not None:
    st.info(f"📋 Tabella rischio meteo per le coordinate attive: `{st.session_state.current_lat:.4f}, {st.session_state.current_lon:.4f}`")
    st.dataframe(st.session_state.analysis_df, use_container_width=True)
else:
    st.warning("⚠️ Impossibile caricare i dati meteo per questa posizione. Riprova a cliccare.")

st.markdown("---")

# --- SEZIONE REGISTRO & SEGNALAZIONI ---
st.subheader("📋 Registro di Campo & Storico Territoriale")

col1, col2 = st.columns([1, 1], gap="large")

with col1:
    st.markdown("### ✍️ Nuova Segnalazione")
    st.markdown("*La segnalazione verrà salvata esattamente sulle coordinate della particella verde selezionata sopra.*")
    autore = st.text_input("Tuo Nome / Azienda Agricola:")
    testo_segnalazione = st.text_area("Descrizione rilievo (es. Trappola catture alte, eseguito trattamento):")
    
    if st.button("Pubblica nel Registro"):
        if autore and testo_segnalazione:
            conn = sqlite3.connect("community_comments.db")
            c = conn.cursor()
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            c.execute("INSERT INTO comments (date, author, text, crop, lat, lon) VALUES (?, ?, ?, ?, ?, ?)", 
                      (date_str, autore, testo_segnalazione, coltura, st.session_state.current_lat, st.session_state.current_lon))
            conn.commit()
            conn.close()
            st.success("Segnalazione registrata con successo!")
            st.rerun()
        else:
            st.warning("Inserisci nome e testo prima di pubblicare.")

with col2:
    st.markdown("### 📰 Storico Registro")
    if not df_comm.empty:
        for _, row in df_comm.iterrows():
            st.markdown(f"🗓️ *{row['date']}* | **{row['author']}** ({row['crop']}) - 📍 *{row['lat']:.4f}, {row['lon']:.4f}*")
            st.markdown(f"> {row['text']}")
            st.markdown("---")
    else:
        st.info("Il registro è attualmente vuoto. Fai clic sul pulsante a sinistra per inserire il primo rilievo!")