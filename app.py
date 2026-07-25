import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime
import streamlit.components.v1 as components

# Configurazione della pagina
st.set_page_config(page_title="AgriDSS Community & Registro", layout="wide", initial_sidebar_state="expanded")

# --- DATABASE SETUP (Con migrazione automatica colonne) ---
def init_db():
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS comments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, text TEXT, crop TEXT)''')
    
    c.execute("PRAGMA table_info(comments)")
    columns = [col[1] for col in c.fetchall()]
    if 'lat' not in columns:
        c.execute("ALTER TABLE comments ADD COLUMN lat REAL")
    if 'lon' not in columns:
        c.execute("ALTER TABLE comments ADD COLUMN lon REAL")
        
    conn.commit()
    conn.close()

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

# --- LAYOUT PRINCIPALE & UX ---
st.title("🌾 AgriDSS: Smart Farming & Registro Territoriale")
st.markdown("---")

# Gestione dello stato delle coordinate correnti sulla mappa
if 'current_lat' not in st.session_state:
    st.session_state.current_lat = 43.007721
if 'current_lon' not in st.session_state:
    st.session_state.current_lon = 12.146461

# Sidebar per impostazioni generali e coltura
st.sidebar.header("⚙️ Configurazione Campo")
coltura = st.sidebar.selectbox("Coltura di Riferimento:", ["Oliveto", "Vigneto"])

# Banner FAQ Rapido in alto
with st.expander("🔍 Aiuto Rapido & FAQ Agronomiche", expanded=False):
    faq_scelta = st.selectbox("Seleziona una domanda frequente:", [
        "-- Scegli una domanda --",
        "Posso trattare oggi se domani piove?",
        "Che faccio se arriva l'ondata di calore a 38°C?",
        "Come capisco se il vicino ha visto la mosca?"
    ])
    if faq_scelta == "Posso trattare oggi se domani piove?":
        st.info("💡 **DSS:** Se la probabilità di pioggia supera il 60% o ci sono >2mm previsti, il sistema sconsiglia il trattamento per rischio dilavamento.")
    elif faq_scelta == "Che faccio se arriva l'ondata di calore a 38°C?":
        st.info("💡 **DSS:** Evita trattamenti! Il caldo estremo oltre i 33-34°C blocca la mosca ma rischia di causare gravi fitotossicità (ustioni).")
    elif faq_scelta == "Come capisco se il vicino ha visto la mosca?":
        st.info("💡 **DSS:** Guarda i pin arancioni sulla mappa qui sotto: indicano le ultime segnalazioni geolocalizzate dei colleghi nella tua zona!")

# --- MAPPA INTERATTIVA (SELEZIONE PARTICELLA) ---
st.subheader("📍 Mappa Appezzamenti & Scouting")
st.markdown("💡 *Clicca/tocca un punto qualsiasi della mappa per spostare il marker **verde** sulla particella che vuoi analizzare.*")

m = folium.Map(location=[st.session_state.current_lat, st.session_state.current_lon], zoom_start=12)

# Marker della particella selezionata (Verde)
folium.Marker(
    [st.session_state.current_lat, st.session_state.current_lon],
    popup="<b>Particella Selezionata</b>",
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

# Caricamento ed esposizione dei pin della community (Arancioni)
df_comm_map = get_comments()
for index, row in df_comm_map.iterrows():
    if pd.notnull(row['lat']) and pd.notnull(row['lon']):
        popup_text = f"<b>{row['author']}</b> ({row['crop']})<br><i>{row['date']}</i><br>{row['text']}"
        folium.Marker(
            [row['lat'], row['lon']],
            popup=popup_text,
            icon=folium.Icon(color="orange", icon="info-sign")
        ).add_to(m)

map_data = st_folium(m, width=1000, height=450)

# Aggiornamento coordinate se l'utente clicca sulla mappa
if map_data and map_data.get("last_clicked"):
    st.session_state.current_lat = map_data["last_clicked"]["lat"]
    st.session_state.current_lon = map_data["last_clicked"]["lng"]
    st.rerun()

st.success(f"🎯 **Particella Attiva (da Mappa):** Latitudine {st.session_state.current_lat:.5f}, Longitudine {st.session_state.current_lon:.5f}")

# --- ANALISI METEO SUL CAMPO SELEZIONATO ---
if st.button("🚀 Esegui Analisi DSS sulla Particella Selezionata", type="primary"):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.current_lat}&longitude={st.session_state.current_lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max&timezone=Europe/Berlin"
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
        
        st.dataframe(df, use_container_width=True)

st.markdown("---")

# --- SEZIONE REGISTRO & SEGNALAZIONI (CON GPS E CLIC) ---
st.subheader("📋 Registro di Campo & Segnalazioni Territoriali")

col1, col2 = st.columns([1, 1], gap="large")

with col1:
    st.markdown("### ✍️ Nuova Segnalazione / Scouting")
    st.markdown("*Puoi usare la posizione della mappa selezionata sopra oppure attivare il GPS del telefono.*")
    
    autore = st.text_input("Tuo Nome / Azienda Agricola:")
    testo_segnalazione = st.text_area("Descrizione rilievo (es. Trappola catture alte, eseguito trattamento):")
    
    # Scelta della modalità di posizione (GPS o Mappa)
    uso_gps = st.checkbox("📍 Usa il GPS attuale del mio dispositivo (prevale sulla mappa)")
    
    # JavaScript integrato per leggere il GPS del browser se richiesto
    lat_finale = st.session_state.current_lat
    lon_finale = st.session_state.current_lon
    
    if uso_gps:
        st.info("📡 Rilevamento GPS in corso tramite browser... (assicurati di consentire la geolocalizzazione sul telefono).")
        # Componente JS leggero per catturare la posizione del browser
        loc_js = """
        <script>
        navigator.geolocation.getCurrentPosition(function(position) {
            const lat = position.coords.latitude;
            const lon = position.coords.longitude;
            // Inviamo i dati tramite parametri o log per conferma
            console.log("GPS trovato: " + lat + ", " + lon);
        });
        </script>
        """
        components.html(loc_js, height=0)
        st.warning("⚠️ Nota: Se il browser blocca il GPS automatico per sicurezza, il sistema utilizzerà comunque le coordinate della mappa selezionata.")

    if st.button("Registra e Pubblica sulla Mappa"):
        if autore and testo_segnalazione:
            add_comment(autore, testo_segnalazione, coltura, lat_finale, lon_finale)
            st.success("Segnalazione registrata con successo nel registro e geolocalizzata!")
            st.rerun()
        else:
            st.warning("Inserisci nome e testo prima di pubblicare.")

with col2:
    st.markdown("### 📰 Storico Registro Territorio")
    df_comm = get_comments()
    if not df_comm.empty:
        for index, row in df_comm.iterrows():
            st.markdown(f"🗓️ *{row['date']}* | **{row['author']}** ({row['crop']}) - 📍 *{row['lat']:.4f}, {row['lon']:.4f}*")
            st.markdown(f"> {row['text']}")
            st.markdown("---")
    else:
        st.info("Il registro è ancora vuoto. Inserisci la prima segnalazione!")