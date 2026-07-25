# --- FUNZIONE DI LETTURA JSON COPERNICUS CORRETTA ---
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
                if val is not None and str(val).lower() != "nan" and str(val).lower() != "null":
                    return float(val)
            except Exception:
                pass
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

# --- CHIAMATA API COPERNICUS CON DEBUG ERRORE ---
@st.cache_data(ttl=86400)
def fetch_satellite_statistics(lat, lon):
    client_id = "sh-fea07070-5af9-419b-9bcf-9aa06c70b822"
    client_secret = "ryKBfLw9vwdFlDrGpjcgHkk1T4sRnSSD"
    
    token = get_cdse_token(client_id, client_secret)
    if not token:
        st.error("Errore autenticazione: Impossibile ottenere il token da Copernicus.")
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
        else:
            st.error(f"Errore Server Copernicus ({response.status_code}): {response.text}")
    except Exception as e:
        st.error(f"Eccezione di connessione a Copernicus: {e}")
    return None