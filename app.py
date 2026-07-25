import streamlit as st
import pandas as pd
import requests
from streamlit_folium import st_folium
import folium
import sqlite3
from datetime import datetime

# Configurazione della pagina
st.set_page_config(page_title="AgriDSS Community", layout="wide")

# --- DATABASE SETUP (SQLite per i commenti) ---
def init_db():
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS comments 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, text TEXT, crop TEXT)''')
    conn.commit()
    conn.close()

init_db()

def add_comment(author, text, crop):
    conn = sqlite3.connect("community_comments.db")
    c = conn.cursor()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO comments (date, author, text, crop) VALUES (?, ?, ?, ?)", (date_str, author, text, crop))
    conn.commit()
    conn.close()

def get_comments():
    conn = sqlite3.connect("community_comments.db")
    df_comments = pd.read_sql("SELECT date, author, text, crop FROM comments ORDER BY id DESC", conn)
    conn.close()
    return df_comments

# --- BANNER STILE GOOGLE / DOMANDE RAPIDE ---
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
    st.info("💡 **Risposta DSS:** Controlla la bacheca della community qui sotto per vedere le ultime segnalazioni geolocalizzate nella tua zona!")

st.title("🌾 AgriDSS: Mappa, Meteo e Community Locale")

# Selezione della coltura
coltura = st.sidebar.selectbox("Seleziona la Coltura:", ["Oliveto", "Vigneto"])

# Mappa interattiva
st.subheader("📍 Mappa Appezzamenti (Clicca sul tuo campo)")
m = folium.Map(location=[43.007721, 12.146461], zoom_start=12)

folium.Marker(
    [43.007721, 12.146461],
    popup="Uliveto Panicale (Test)",
    icon=folium.Icon(color="green", icon="leaf")
).add_to(m)

map_data = st_folium(m, width=700, height=350)

lat, lon = 43.007721, 12.146461
if map_data and map_data.get("last_clicked"):
    lat = map_data["last_clicked"]["lat"]
    lon = map_data["last_clicked"]["lng"]
    st.success(f"Coordinate selezionate: Lat {lat:.5f}, Lon {lon:.5f}")

if st.button("🚀 Esegui Analisi Meteo sul Campo"):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max&timezone=Europe/Berlin"
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

# --- SEZIONE COMMUNITY CON DATABASE ---
st.subheader("👥 Bacheca della Community (Segnalazioni dal Territorio)")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Lascia una segnalazione per i vicini:**")
    autore = st.text_input("Tuo Nome / Azienda:")
    testo_segnalazione = st.text_area("Cosa hai notato nel campo? (es. Mosca vista, Trattamento eseguito):")
    if st.button("Pubblica Segnalazione"):
        if autore and testo_segnalazione:
            add_comment(autore, testo_segnalazione, coltura)
            st.success("Segnalazione salvata nel database con successo!")
            st.rerun()
        else:
            st.warning("Inserisci nome e testo prima di pubblicare.")

with col2:
    st.markdown("**Ultime notizie dai campi della zona:**")
    df_comm = get_comments()
    if not df_comm.empty:
        for index, row in df_comm.iterrows():
            st.markdown(f"🗓️ *{row['date']}* | **{row['author']}** ({row['crop']}):\n> {row['text']}")
            st.markdown("---")
    else:
        st.info("Nessuna segnalazione recente. Sii il primo a scrivere!")