import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime

# Configurazione della pagina
st.set_page_config(page_title="AgriDSS Community", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP & GESTIONE ---
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

# --- GESTIONE STATO COORDINATE & ANALISI ---
if 'lat' not in st.session_state:
    st.session_state.lat = 43.007721
if 'lon' not in st.session_state:
    st.session_state.lon = 12.146461
if 'df_meteo' not in st.session_state:
    st.session_state.df_meteo = None

# --- SIDEBAR: CONFIGURAZIONE & GESTIONE ---
st.sidebar.header("⚙️ Configurazione Campo")
coltura = st.sidebar.selectbox("Seleziona la Coltura:", ["Oliveto", "Vigneto"])

st.sidebar.markdown("---")
st.sidebar.header("🗑️ Gestione Dati")
if st.sidebar.button("Svuota Registro (Elimina Tutti i Messaggi)", type="secondary"):
    reset_db()
    st.sidebar.success("Registro azzerato con successo!")
    st.rerun()

# --- BANNER FAQ RAPIDE ---
st.markdown("""
    <div style="background-color: #f0f2f6; padding: 15px; border-radius: 10px; border-left: 5px solid #ff4b4b; margin-bottom: 20px;">
        <h3 style="margin: 0; color: #31333F;">🔍 Aiuto Rapido & Domande Frequenti (FAQ)</h3>
        <p style="margin: 5px 0 0 0; color: #555;">Hai dubbi in campo? Cerca o seleziona una domanda tipica:</p>
    </div>
""", unsafe_allow_html=True)

faq_scelta = st.selectbox("Seleziona una domanda frequente:", [
    "-- Scegli una domanda --",
    "Posso trattare oggi se domani piove?",
    "Che faccio se arriva l'ondata di calore a 38°C?",
    "Come capisco se il vicino ha visto la mosca?"
])

if faq_scelta == "Posso trattare oggi se domani piove?":
    st.info("💡 **Risposta DSS:** Se la probabilità di pioggia supera il 60% o ci sono più di 2mm previsti, il sistema sconsiglia il trattamento perché il prodotto verrebbe dilavato.")
elif faq_scelta == "Che faccio se arriva l'ondata di calore a 38°C?":
    st.info("💡 **Risposta DSS:** Tieni il trattore in rimessa! Il caldo estremo oltre i 33-34°C blocca la mosca ma rischia di causare gravissime fitotossicità (ustioni) se applichi fitofarmaci.")
elif faq_scelta == "Come capisco se il vicino ha visto la mosca?":
    st.info("💡 **Risposta DSS:** Controlla la bacheca e i pin arancioni sulla mappa qui sotto per vedere le ultime segnalazioni geolocalizzate nella tua zona!")

st.title("🌾 AgriDSS: Mappa, Meteo e Community Locale")

# --- MAPPA INTERATTIVA ---
st.subheader("📍 Mappa Appezzamenti (Clicca sul tuo campo)")
st.markdown("💡 *Clicca un punto qualsiasi della mappa per spostare la particella verde.*")

m = folium.Map(location=[st.session_state.lat, st.session_state.lon], zoom_start=12)

# Marker Verde della posizione attiva
folium.Marker(
    [st.session_state.lat, st.session_state.lon],
    popup="<b>Particella Selezionata</b>",
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

# Caricamento Pin Arancioni della Community
df_comm_map = get_comments()
for _, row in df_comm_map.iterrows():
    if pd.notnull(row['lat']) and pd.notnull(row['lon']):
        popup_text = f"<b>{row['author']}</b> ({row['crop']})<br><i>{row['date']}</i><br>{row['text']}"
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

# --- ANALISI METEO DSS (Con pulsante sicuro e persistenza) ---
st.subheader("📊 Analisi DSS & Rischio Fitosanitario")

if st.button("🚀 Esegui Analisi Meteo sul Campo", type="primary"):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max&timezone=Europe/Berlin"
    response = requests.get(url).json()
    
    if "daily" in response:
        daily = response["daily"]
        df = pd.DataFrame({
            "Data": daily["time"],
            "Temp Max (°C)": daily["temperature_2m_max"],
            "Temp Min (°C)": daily["temperature_2m_min"],
            "Pioggia (mm)": daily["precipitation_sum"],
            "Prob. Pioggia (%)": daily["precipitation_probability_max"]
        })
        
        stati = []
        for i, row in df.iterrows():
            if row["Pioggia (mm)"] > 2:
                stati.append("🔴 [ALLERTA] Rischio Pioggia - Evita Trattamenti")
            elif row["Temp Max (°C)"] > 33:
                stati.append("🟡 [ATTENZIONE] Stress Termico / Caldo")
            else:
                stati.append("🟢 [OK] Condizioni Stabili")
        df["Stato Operativo"] = stati
        
        # Salviamo in memoria così non sparisce
        st.session_state.df_meteo = df

# Mostriamo la tabella se è stata calcolata
if st.session_state.df_meteo is not None:
    st.info(f"📋 Dati meteo attivi per le coordinate: `{st.session_state.lat:.4f}, {st.session_state.lon:.4f}`")
    st.dataframe(st.session_state.df_meteo, use_container_width=True)
else:
    st.warning("👆 Clicca sul pulsante sopra per caricare l'analisi meteo e di rischio della particella selezionata.")

st.markdown("---")

# --- SEZIONE COMMUNITY & REGISTRO ---
st.subheader("👥 Bacheca della Community & Registro Territoriale")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Lascia una segnalazione per i vicini:**")
    autore = st.text_input("Tuo Nome / Azienda:")
    testo_segnalazione = st.text_area("Cosa hai notato nel campo? (es. Mosca vista, Trattamento eseguito):")
    if st.button("Pubblica Segnalazione"):
        if autore and testo_segnalazione:
            add_comment(autore, testo_segnalazione, coltura, st.session_state.lat, st.session_state.lon)
            st.success("Segnalazione salvata e geolocalizzata con successo!")
            st.rerun()
        else:
            st.warning("Inserisci nome e testo prima di pubblicare.")

with col2:
    st.markdown("**Ultime notizie dai campi della zona:**")
    df_comm = get_comments()
    if not df_comm.empty:
        for index, row in df_comm.iterrows():
            lat_str = f"📍 {row['lat']:.4f}, {row['lon']:.4f}" if pd.notnull(row['lat']) else ""
            st.markdown(f"🗓️ *{row['date']}* | **{row['author']}** ({row['crop']}) - {lat_str}\n> {row['text']}")
            st.markdown("---")
    else:
        st.info("Nessuna segnalazione recente. Sii il primo a scrivere!")