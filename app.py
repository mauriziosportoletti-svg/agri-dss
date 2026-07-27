import io
import sqlite3
from datetime import datetime, timedelta, timezone
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

# --- CONFIGURAZIONE ABBONAMENTO ---
MAX_CAMPI_ABBONAMENTO = 3

# --- CONFIGURAZIONE PAGINA & CSS ---
st.set_page_config(
    page_title="AgriDSS - Gestione Campi & Allarmi",
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
    </style>
"""
st.markdown(css_custom, unsafe_allow_html=True)

DB_PATH = "agri_dss.db"


# --- DATABASE HELPERS ---
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


init_db()


def save_field(name, crop, lat, lon):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO fields (name, crop, lat, lon) VALUES (?, ?, ?, ?)",
            (name, crop, lat, lon),
        )
        conn.commit()


def delete_field(name):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fields WHERE name = ?", (name,))
        conn.commit()


def get_fields():
    with get_db_connection() as conn:
        return pd.read_sql("SELECT name, crop, lat, lon FROM fields", conn)


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


# --- API METEO ---
@st.cache_data(ttl=3600)
def fetch_weather_advanced(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,et0_fao_evapotranspiration,wind_speed_10m_max&hourly=relative_humidity_2m,soil_moisture_0_to_7cm&past_days=7&timezone=Europe/Berlin"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None


# --- API SATELLITE ---
@st.cache_data(ttl=3600)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"

    auth_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    try:
        auth_res = requests.post(
            auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10,
        )
        if auth_res.status_code != 200:
            return {
                "error": f"Errore Autenticazione OAuth Copernicus (Status {auth_res.status_code})"
            }

        token = auth_res.json().get("access_token")
    except Exception as e:
        return {"error": f"Eccezione durante la connessione OAUTH: {str(e)}"}

    now_utc = datetime.now(timezone.utc)
    data_fine = now_utc.strftime("%Y-%m-%dT23:59:59Z")
    data_inizio = (now_utc - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
    delta = 0.002

    payload = {
        "input": {
            "bounds": {
                "bbox": [lon - delta, lat - delta, lon + delta, lat + delta]
            },
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
                    output: [
                        { id: 'ndvi', bands: 1 }, 
                        { id: 'msavi', bands: 1 }, 
                        { id: 'ndmi', bands: 1 },
                        { id: 'dataMask', bands: 1 }
                    ]
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
            "msavi": {
                "statistics": {"default": {"percentiles": {"k": [10.0]}}}
            },
            "ndmi": {"statistics": {"default": {"percentiles": {"k": [10.0]}}}},
        },
    }
    try:
        res = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/statistics",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if res.status_code == 200:
            return res.json()
        else:
            return {
                "error": f"Errore API Copernicus Statistics (Status {res.status_code}): {res.text}"
            }
    except Exception as e:
        return {"error": f"Eccezione durante la richiesta Dati: {str(e)}"}


def parse_satellite_json(data):
    if not data:
        st.error("âš ï¸ Nessun dato ricevuto dal server.")
        return pd.DataFrame()

    if isinstance(data, dict) and "error" in data:
        st.error(f"ðŸ”§ **DEBUG API:** {data['error']}")
        return pd.DataFrame()

    records = []
    for item in data.get("data", []):
        outputs = item.get("outputs", {})
        date_from = item.get("interval", {}).get("from", "")[:10]

        def get_v(k):
            try:
                val = (
                    outputs.get(k, {})
                    .get("bands", {})
                    .get("B0", {})
                    .get("stats", {})
                    .get("mean")
                )
                return (
                    float(val)
                    if val is not None and str(val).lower() != "nan"
                    else None
                )
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
    if df.empty:
        st.info(
            "â„¹ï¸ API Copernicus collegata con successo, ma nessun passaggio satellitare privo di nuvole Ã¨ presente negli ultimi 45 giorni su queste coordinate."
        )

    return df.sort_values(by="Data", ascending=False) if not df.empty else df


# --- INIZIALIZZAZIONE SESSIONE ---
fields_df = get_fields()

if "active_field_name" not in st.session_state:
    if not fields_df.empty:
        st.session_state.active_field_name = fields_df.iloc[0]["name"]
        st.session_state.active_lat = fields_df.iloc[0]["lat"]
        st.session_state.active_lon = fields_df.iloc[0]["lon"]
    else:
        st.session_state.active_field_name = "Nessun Campo"
        st.session_state.active_lat = 43.007721
        st.session_state.active_lon = 12.146461

if "clicked_lat" not in st.session_state:
    st.session_state.clicked_lat = st.session_state.active_lat
if "clicked_lon" not in st.session_state:
    st.session_state.clicked_lon = st.session_state.active_lon


# --- SIDEBAR: GESTIONE CAMPI ---
st.sidebar.title("ðŸ¡ I Miei Campi")

if not fields_df.empty:
    options = fields_df["name"].tolist()
    curr_idx = (
        options.index(st.session_state.active_field_name)
        if st.session_state.active_field_name in options
        else 0
    )

    selected_field = st.sidebar.selectbox(
        "Seleziona Campo da Analizzare:", options, index=curr_idx
    )

    if selected_field != st.session_state.active_field_name:
        row = fields_df[fields_df["name"] == selected_field].iloc[0]
        st.session_state.active_field_name = row["name"]
        st.session_state.active_lat = row["lat"]
        st.session_state.active_lon = row["lon"]
        st.session_state.clicked_lat = row["lat"]
        st.session_state.clicked_lon = row["lon"]
        st.rerun()

    if st.sidebar.button(f"ðŸ—‘ï¸ Elimina '{selected_field}'"):
        delete_field(selected_field)
        st.sidebar.success("Campo eliminato!")
        st.session_state.pop("active_field_name", None)
        st.rerun()
else:
    st.sidebar.warning("Nessun campo salvato.")

st.sidebar.markdown("---")

num_campi = len(fields_df)
st.sidebar.caption(
    f"Campi salvati: **{num_campi}/{MAX_CAMPI_ABBONAMENTO}** (Piano Base)"
)

if num_campi < MAX_CAMPI_ABBONAMENTO:
    st.sidebar.subheader("âž• Aggiungi Nuovo Campo")
    st.sidebar.info("ðŸ‘‰ *Fai click sulla mappa per acquisire la posizione.*")

    new_name = st.sidebar.text_input("Nome Campo:")
    new_crop = st.sidebar.selectbox(
        "Coltura:", ["Oliveto", "Vigneto", "Seminativo", "Noccioleto", "Altro"]
    )

    saved_lat = st.sidebar.number_input(
        "Latitudine:", value=st.session_state.clicked_lat, format="%.6f"
    )
    saved_lon = st.sidebar.number_input(
        "Longitudine:", value=st.session_state.clicked_lon, format="%.6f"
    )

    if st.sidebar.button("ðŸ’¾ Salva Campo"):
        if new_name:
            save_field(new_name, new_crop, saved_lat, saved_lon)
            st.session_state.active_field_name = new_name
            st.session_state.active_lat = saved_lat
            st.session_state.active_lon = saved_lon
            st.sidebar.success("Campo aggiunto!")
            st.rerun()
        else:
            st.sidebar.error("Inserisci un nome!")
else:
    st.sidebar.error("âš ï¸ Limite massimo di campi raggiunto.")


# --- MAIN PAGE ---
st.title("ðŸŒ¾ AgriDSS: Monitoraggio & Allarmi")
st.caption(
    f"ðŸ“ **Campo Attivo**: {st.session_state.active_field_name} | **Lat**: {st.session_state.active_lat:.5f} | **Lon**: {st.session_state.active_lon:.5f}"
)

# Fetch Dati
w_data = fetch_weather_advanced(
    st.session_state.active_lat, st.session_state.active_lon
)
sat_json = fetch_satellite_statistics(
    st.session_state.active_lat, st.session_state.active_lon
)
df_sat = parse_satellite_json(sat_json)

# Prepariamo la tabella meteo (serve sia sotto che per l'export)
df_meteo = pd.DataFrame()
if w_data and "daily" in w_data:
    d = w_data["daily"]
    df_meteo = pd.DataFrame({
        "Data": d["time"],
        "Temp Max (Â°C)": d["temperature_2m_max"],
        "Temp Min (Â°C)": d["temperature_2m_min"],
        "Pioggia (mm)": d["precipitation_sum"],
        "ET0 (mm)": d["et0_fao_evapotranspiration"],
    })

# --- METRIC CARDS ---
col1, col2, col3 = st.columns(3)

last_ndvi = (
    df_sat["NDVI"].iloc[0]
    if (not df_sat.empty and "NDVI" in df_sat.columns)
    else None
)
col1.metric(
    "ðŸ›°ï¸ Indice Vigore (NDVI)",
    f"{last_ndvi:.2f}" if last_ndvi is not None else "N/D",
    "Ottimo" if (last_ndvi and last_ndvi > 0.5) else "Sotto controllo",
)

risk = "BASSO ðŸŸ¢"
if w_data and "daily" in w_data:
    if (sum(w_data["daily"]["temperature_2m_max"][:3]) / 3) > 22:
        risk = "MEDIO ðŸŸ¡"
col2.metric("ðŸ›¡ï¸ Rischio Fitosanitario", risk)

rain_sum = (
    sum(w_data["daily"]["precipitation_sum"][:7])
    if w_data and "daily" in w_data
    else 0.0
)
col3.metric("ðŸŒ§ï¸ Pioggia Ultimi 7gg", f"{rain_sum:.1f} mm")

st.markdown("---")

# --- MAPPA INTERATTIVA ---
st.subheader("ðŸ—ºï¸ Mappa Territoriale")

m = folium.Map(
    location=[st.session_state.active_lat, st.session_state.active_lon],
    zoom_start=13,
)

folium.Marker(
    [st.session_state.active_lat, st.session_state.active_lon],
    popup=f"Campo Attivo: {st.session_state.active_field_name}",
    icon=folium.Icon(color="green", icon="leaf"),
).add_to(m)

if (
    st.session_state.clicked_lat != st.session_state.active_lat
    or st.session_state.clicked_lon != st.session_state.active_lon
):
    folium.Marker(
        [st.session_state.clicked_lat, st.session_state.clicked_lon],
        popup="Punto Selezionato",
        icon=folium.Icon(color="orange", icon="info-sign"),
    ).add_to(m)

alerts_df = get_alerts()
if not alerts_df.empty:
    for _, alert in alerts_df.iterrows():
        folium.Marker(
            [alert["lat"], alert["lon"]],
            popup=f"<b>âš ï¸ {alert['alert_type']}</b><br>{alert['description']}<br><i>{alert['created_at']}</i>",
            icon=folium.Icon(color="red", icon="warning", prefix="fa"),
        ).add_to(m)

map_data = st_folium(m, width=750, height=350, key="agri_map")

if map_data and map_data.get("last_clicked"):
    cl_lat = map_data["last_clicked"]["lat"]
    cl_lon = map_data["last_clicked"]["lng"]
    if (
        abs(cl_lat - st.session_state.clicked_lat) > 0.0001
        or abs(cl_lon - st.session_state.clicked_lon) > 0.0001
    ):
        st.session_state.clicked_lat = cl_lat
        st.session_state.clicked_lon = cl_lon
        st.rerun()

# --- SEGNALAZIONI ---
with st.expander("ðŸ“¢ Invia una Segnalazione Anonima nella zona"):
    st.caption(
        f"Posizione: **Lat {st.session_state.clicked_lat:.5f}, Lon {st.session_state.clicked_lon:.5f}**"
    )
    a_type = st.selectbox(
        "Avvistamento / Anomalia:",
        [
            "Avvistamento Mosca Olearia",
            "Attacco Peronospora / Oidio",
            "SiccitÃ  Severa",
            "Danni da Gelata / Grandine",
            "Altro",
        ],
    )
    a_desc = st.text_input("Dettagli:")
    if st.button("ðŸš¨ Pubblica Segnalazione"):
        add_alert(
            a_type,
            a_desc or "Segnalazione generica",
            st.session_state.clicked_lat,
            st.session_state.clicked_lon,
        )
        st.success("Segnalazione inviata!")
        st.rerun()

st.markdown("---")

# --- TABELLE E DATI ---
st.subheader("ðŸ“Š Analisi Dettagliata & Storico")
t_sat, t_meteo = st.tabs(["ðŸ›°ï¸ Satellite Sentinel-2", "â˜€ï¸ Meteo & Suolo"])

with t_sat:
    if not df_sat.empty:
        st.dataframe(df_sat, use_container_width=True, hide_index=True)
        st.line_chart(df_sat.set_index("Data")[["NDVI", "MSAVI", "NDMI"]])

with t_meteo:
    if not df_meteo.empty:
        st.dataframe(df_meteo, use_container_width=True, hide_index=True)

# --- NUOVA SEZIONE: ESPORTAZIONE UNIFICATA REPORT (METEO + SATELLITE) ---
st.markdown("#### ðŸ“¥ Esporta Report Storico (Meteo + Satellite)")
exp_col1, exp_col2 = st.columns(2)

# 1. GENERAZIONE TABELLONE EXCEL / CSV UNIFICATO
# Uniamo i dati su base data
if not df_sat.empty or not df_meteo.empty:
    df_combined = pd.merge(df_meteo, df_sat, on="Data", how="outer").sort_values(
        by="Data", ascending=False
    )
    csv_report = df_combined.to_csv(index=False).encode("utf-8")

    exp_col1.download_button(
        label="ðŸ“Š Scarica Tabellone Completo CSV (Excel)",
        data=csv_report,
        file_name=f"report_meteo_sat_{st.session_state.active_field_name}.csv",
        mime="text/csv",
    )

# 2. GENERAZIONE REPORT HTML / PDF IMPAGINATO BENISSIMO
html_sat = (
    df_sat.to_html(index=False, classes="styled-table")
    if not df_sat.empty
    else "<p>Nessun dato satellitare disponibile</p>"
)
html_meteo = (
    df_meteo.to_html(index=False, classes="styled-table")
    if not df_meteo.empty
    else "<p>Nessun dato meteo disponibile</p>"
)

report_html = f"""
<html>
<head>
    <title>Report Agronomico - {st.session_state.active_field_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 30px; color: #333; }}
        h1 {{ color: #2e7d32; border-bottom: 2px solid #2e7d32; padding-bottom: 5px; }}
        h2 {{ color: #1b5e20; margin-top: 25px; }}
        .info-box {{ background-color: #f1f8e9; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
        .styled-table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 14px; }}
        .styled-table th, .styled-table td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
        .styled-table th {{ background-color: #4caf50; color: white; }}
        .styled-table tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .footer {{ margin-top: 40px; font-size: 11px; text-align: center; color: #777; border-top: 1px solid #ccc; padding-top: 10px; }}
    </style>
</head>
<body>
    <h1>ðŸŒ¾ Report Agronomico Integrato</h1>
    <div class="info-box">
        <p><b>Campo:</b> {st.session_state.active_field_name}</p>
        <p><b>Coordinate:</b> Lat {st.session_state.active_lat:.5f}, Lon {st.session_state.active_lon:.5f}</p>
        <p><b>Data Report:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
    </div>

    <h2>ðŸ›°ï¸ Dati Satellitari (Sentinel-2)</h2>
    {html_sat}

    <h2>â˜€ï¸ Storico Meteo</h2>
    {html_meteo}

    <div class="footer">
        Generato automaticamente da AgriDSS - Sistema Decisionale per l'Agricoltura
    </div>
</body>
</html>
"""

exp_col2.download_button(
    label="ðŸ“„ Scarica Report PDF / Stampabile",
    data=report_html,
    file_name=f"report_integrato_{st.session_state.active_field_name}.html",
    mime="text/html",
)

st.markdown("---")

# --- QUADERNO DI CAMPAGNA ---
st.subheader("ðŸ“‹ Registro Trattamenti & Esportazione")

col_form, col_table = st.columns([1, 1.2])

with col_form:
    st.markdown("##### âœï¸ Registra Operazione")
    t_date = st.date_input("Data Operazione:", datetime.now())
    t_op = st.text_input("Operatore:", value="Azienda")
    t_text = st.text_area("Prodotto / Dose / Note:")

    if st.button("ðŸ’¾ Salva Trattamento"):
        if t_text:
            add_treatment(
                str(t_date),
                t_op,
                st.session_state.active_field_name,
                t_text,
                st.session_state.active_lat,
                st.session_state.active_lon,
            )
            st.success("Registrato!")
            st.rerun()

with col_table:
    st.markdown(
        f"##### ðŸ“– Registro per: *{st.session_state.active_field_name}*"
    )
    treatments_df = get_treatments(st.session_state.active_field_name)

    if not treatments_df.empty:
        st.dataframe(treatments_df, use_container_width=True, hide_index=True)

        st.markdown("###### ðŸ“¥ Esporta Registro Trattamenti")
        c_exp1, c_exp2 = st.columns(2)

        csv_data = treatments_df.to_csv(index=False).encode("utf-8")
        c_exp1.download_button(
            label="ðŸ“„ Scarica CSV",
            data=csv_data,
            file_name=f"quaderno_campagna_{st.session_state.active_field_name}.csv",
            mime="text/csv",
        )

        html_table = treatments_df.to_html(index=False)
        full_html = f"<html><head><title>Quaderno di Campagna - {st.session_state.active_field_name}</title></head><body><h2>Quaderno di Campagna - {st.session_state.active_field_name}</h2>{html_table}</body></html>"
        c_exp2.download_button(
            label="ðŸ–¨ï¸ Scarica Report PDF/HTML",
            data=full_html,
            file_name=f"quaderno_campagna_{st.session_state.active_field_name}.html",
            mime="text/html",
        )
    else:
        st.info("Nessun trattamento regist
