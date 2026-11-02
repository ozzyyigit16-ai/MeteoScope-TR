from datetime import date, timedelta
from contextlib import contextmanager
from geopy.geocoders import Nominatim
import folium
from folium.plugins import Draw
from folium.raster_layers import TileLayer
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium
import time
import os
import sqlite3
import json
import tempfile
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor, as_completed

# Sayfa Yapılandırması
st.set_page_config(
    page_title="MeteoScope-TR - Global Meteoroloji & Uzman Paneli",
    page_icon="🌍",
    layout="wide",
)

# --- KUSURSUZ AKTİF OTURUM VE TOPLAM ZİYARETÇİ SİSTEMİ ---
COUNTER_DB = "meteo_counters.db"
HEARTBEAT_TIMEOUT_SECONDS = 20


@st.cache_resource(show_spinner=False)
def get_heartbeat_component():
    component_dir = os.path.join(tempfile.gettempdir(), "meteoscope_heartbeat_v7")
    os.makedirs(component_dir, exist_ok=True)
    html_path = os.path.join(component_dir, "index.html")

    component_html = r'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body>
<script>
(function () {
  "use strict";
  function sendToStreamlit(type, data) {
    const message = Object.assign({isStreamlitMessage: true, type: type}, data || {});
    window.parent.postMessage(message, "*");
  }

  function makeId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "v7-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function getVisitorId() {
    let id = null;
    try { id = window.localStorage.getItem("meteoscope_tr_visitor_id_v7"); } catch (_) {}
    if (!id) {
      id = makeId();
      try { window.localStorage.setItem("meteoscope_tr_visitor_id_v7", id); } catch (_) {}
    }
    return id;
  }

  const visitorId = getVisitorId();
  sendToStreamlit("streamlit:setComponentValue", { value: { action: "register", visitor_id: visitorId } });

  setInterval(function () {
    if (!document.hidden) {
      sendToStreamlit("streamlit:setComponentValue", { value: { action: "heartbeat", visitor_id: visitorId } });
    }
  }, 10000);

  sendToStreamlit("streamlit:componentReady", {apiVersion: 1});
  sendToStreamlit("streamlit:setFrameHeight", {height: 0});
})();
</script>
</body>
</html>'''

    if not os.path.exists(html_path):
        with open(html_path, "w", encoding="utf-8") as html_file:
            html_file.write(component_html)

    return components.declare_component("meteoscope_heartbeat_v7", path=component_dir)


@contextmanager
def get_counter_connection():
    conn = sqlite3.connect(COUNTER_DB, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_counter_db():
    with get_counter_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unique_visitors (
                visitor_id TEXT PRIMARY KEY,
                first_seen REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_sessions (
                visitor_id TEXT PRIMARY KEY,
                last_seen REAL NOT NULL
            )
        """)


initialize_counter_db()

heartbeat_component = get_heartbeat_component()
hb_result = heartbeat_component(key="meteoscope_hb_comp", default=None, height=0)

if isinstance(hb_result, dict):
    v_id = hb_result.get("visitor_id")
    action = hb_result.get("action")
    if v_id:
        now = time.time()
        with get_counter_connection() as conn:
            if action == "register":
                conn.execute(
                    "INSERT OR IGNORE INTO unique_visitors (visitor_id, first_seen) VALUES (?, ?)",
                    (v_id, now),
                )
            conn.execute("""
                INSERT INTO active_sessions (visitor_id, last_seen)
                VALUES (?, ?)
                ON CONFLICT(visitor_id) DO UPDATE SET last_seen = excluded.last_seen
            """, (v_id, now))


def read_counter_values():
    now = time.time()
    cutoff = now - HEARTBEAT_TIMEOUT_SECONDS
    with get_counter_connection() as conn:
        conn.execute("DELETE FROM active_sessions WHERE last_seen < ?", (cutoff,))
        total_visitors = conn.execute(
            "SELECT COUNT(*) FROM unique_visitors"
        ).fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM active_sessions WHERE last_seen >= ?",
            (cutoff,),
        ).fetchone()[0]
    return total_visitors, active_count


@st.fragment(run_every="5s")
def render_live_admin_metrics():
    total_visitors, active_count = read_counter_values()
    st.markdown("### 📊 Canlı ve Dinamik İstatistikler")
    st.metric(label="Toplam Benzersiz Ziyaretçi", value=f"{total_visitors:,}")
    st.metric(label="Anlık Aktif Oturum", value=active_count)
    st.metric(
        label="Son Aranan Konum",
        value=st.session_state.get("son_arama", "İstanbul / Merkez"),
    )
    st.caption("Cihazlar ve tarayıcılar tekil sayılır. Sekme kapatıldığında veya sayfa terkedildiğinde aktif oturum otomatik olarak 0'a düşer.")


# --- AKILLI GİYİM VE KAR ÖNERİSİ ---
def giyim_onerisi_olustur(
    ortalama_sicaklik, toplam_yagis, toplam_kar, ortalama_yagis_ihtimali, max_ruzgar
):
  if max_ruzgar > 45:
    return "🌪️ **Giyim Önerisi:** Bölgede kuvvetli rüzgar ve fırtına hamleleri var. Rüzgarı kesen montlar tercih edin."
  elif toplam_kar > 0.5 or ortalama_sicaklik <= 0:
    return "❄️ **Giyim Önerisi:** Hava sıfırın altında ve sert soğuk hakim. Kalın kışlık mont ve botlarınızı giyin."
  elif toplam_yagis > 1.0 or ortalama_yagis_ihtimali > 50:
    return "🌧️ **Giyim Önerisi:** Yağış geçişleri var, yanınıza şemsiye alın."
  elif ortalama_sicaklik < 12:
    return "🧥 **Giyim Önerisi:** Hava oldukça soğuk, kat kat giyinin."
  elif ortalama_sicaklik > 25:
    return "👕 **Giyim Önerisi:** Hava sıcak, hafif kıyafetler tercih edebilirsiniz."
  else:
    return "👔 **Giyim Önerisi:** Hava mevsim normallerinde."


# --- TEK KONUM AKILLI UYARI SİSTEMİ ---
def konum_uyari_analizi(curr_data, df_3days):
  uyarilar = []
  if curr_data.get("temperature_2m", 15) <= 0:
    uyarilar.append(("kirmizi", "❄️ **Don / Buzlanma Riski:** Sıcaklık 0°C veya altında! Yollarda gizli buzlanma görülebilir."))
  elif curr_data.get("temperature_2m", 15) < 5:
    uyarilar.append(("sari", "⚠️ **Düşük Sıcaklık:** Hava oldukça soğuk, tedbirli olunmalı."))
  elif curr_data.get("temperature_2m", 15) > 32:
    uyarilar.append(("sari", "🔥 **Yüksek Sıcaklık Uyarısı:** Aşırı sıcak değerler kaydediliyor."))

  curr_wind = curr_data.get("wind_speed_10m", 0)
  curr_gust = curr_data.get("wind_gusts_10m", 0)
  if curr_wind > 40 or curr_gust > 55:
    uyarilar.append(("kirmizi", f"🌪️ **Şiddetli Rüzgar / Fırtına:** Anlık rüzgar hamlesi {curr_gust:.1f} km/s seviyesinde!"))
  elif curr_wind > 25:
    uyarilar.append(("sari", f"💨 **Kuvvetli Rüzgar:** Rüzgar hızı {curr_wind:.1f} km/s, dikkatli olun."))

  curr_snow = curr_data.get("snowfall", 0)
  if curr_snow > 0:
    uyarilar.append(("kirmizi", f"🌨️ **Aktif Kar Yağışı:** Bölgede anlık kar yağışı/geçişi var ({curr_snow:.1f} cm)."))

  if not df_3days.empty:
    total_p = df_3days["precipitation"].sum() if "precipitation" in df_3days.columns else 0
    max_g = df_3days["wind_gusts_10m"].max() if "wind_gusts_10m" in df_3days.columns else 0
    if total_p > 10:
      uyarilar.append(("sari", f"🌧️ **Yoğun Yağış Beklentisi:** Periyotta toplam {total_p:.1f} mm yağış öngörülüyor."))
    if max_g > 60:
      uyarilar.append(("kirmizi", f"⚡ **Fırtına Uyarısı:** Yaklaşan günlerde max {max_g:.1f} km/s rüzgar hamlesi bekleniyor!"))

  if not uyarilar:
    uyarilar.append(("yesil", "✅ **Hava Koşulları Stabil:** Bölgede genel olarak risk oluşturacak olumsuz bir meteorolojik parametre görünmüyor."))

  return uyarilar


def ekstrem_erken_uyari_analizi(df_7days, konum_adi):
  if df_7days.empty:
    return []
  tehlikeler = []
  for _, row in df_7days.iterrows():
    t_time = row["time"]
    precip = row.get("precipitation", 0)
    gust = row.get("wind_gusts_10m", 0)
    if precip >= 15.0:
      tehlikeler.append({
          "tarih_str": t_time.strftime("%d %B %Y, Saat: %H:00"),
          "tip": "Şiddetli Sağanak ve Sel Riski",
          "detay": f"Seçilen konumda ({konum_adi}) saatlik bazda {precip:.1f} mm yoğun yağış öngörülüyor.",
          "seviye": "kirmizi"
      })
    elif gust >= 65.0:
      tehlikeler.append({
          "tarih_str": t_time.strftime("%d %B %Y, Saat: %H:00"),
          "tip": "Şiddetli Fırtına / Rüzgar Hamlesi",
          "detay": f"Seçilen konumda ({konum_adi}) anlık rüzgar hamlesi {gust:.1f} km/s seviyesine ulaşabilir.",
          "seviye": "kirmizi"
      })
  return tehlikeler


# --- TÜRKİYE GENELİ MERKEZİ AFET VE ERKEN UYARI MASASI ---
TURKEY_REGIONAL_CENTERS = {
    "Marmara (İstanbul)": (41.0082, 28.9784),
    "İç Anadolu (Ankara)": (39.9334, 32.8597),
    "Ege (İzmir)": (38.4192, 27.1287),
    "Akdeniz (Antalya)": (36.8969, 30.7133),
    "Karadeniz (Trabzon)": (41.0015, 39.7178),
    "Doğu Anadolu (Erzurum)": (39.9043, 41.2679),
    "Güneydoğu Anadolu (Diyarbakır)": (37.9144, 40.2306),
}

def _safe_number(value, default=0.0):
  try:
    if value is None or pd.isna(value):
      return float(default)
    return float(value)
  except (TypeError, ValueError):
    return float(default)

@st.cache_data(ttl=900, show_spinner=False)
def fetch_turkey_wide_ews():
  global_alerts = []
  def check_center(name, lat, lon):
    try:
      url = "https://api.open-meteo.com/v1/forecast"
      params = {
          "latitude": lat, "longitude": lon,
          "hourly": "precipitation,wind_gusts_10m",
          "forecast_days": 3,
          "timezone": "auto"
      }
      res = requests.get(url, params=params, timeout=4)
      if res.ok:
        data = res.json().get("hourly", {})
        times = data.get("time", [])
        precips = data.get("precipitation", [])
        gusts = data.get("wind_gusts_10m", [])
        for t, p, g in zip(times, precips, gusts):
          p_val = _safe_number(p)
          g_val = _safe_number(g)
          if p_val >= 18.0:
            return {"bolge": name, "tip": "Şiddetli Sağanak / Sel Riski", "deger": f"{p_val:.1f} mm/saat", "zaman": t}
          elif g_val >= 70.0:
            return {"bolge": name, "tip": "Şiddetli Fırtına Hamlesi", "deger": f"{g_val:.1f} km/s", "zaman": t}
    except Exception:
      pass
    return None

  with ThreadPoolExecutor(max_workers=len(TURKEY_REGIONAL_CENTERS)) as executor:
    futures = {executor.submit(check_center, name, coords[0], coords[1]): name for name, coords in TURKEY_REGIONAL_CENTERS.items()}
    for future in as_completed(futures):
      res = future.result()
      if res:
        global_alerts.append(res)
  return global_alerts


# --- GİZLİ YÖNETİCİ PANELİ ---
with st.sidebar:
  st.image("https://images.unsplash.com/photo-1592210454359-9043f067919b?w=400&q=80", use_container_width=True)
  st.title("MeteoScope-TR")
  st.caption("Gelişmiş Meteoroloji & Uzman Paneli")
  st.markdown("---")
  st.subheader("🔐 Gizli Yönetici Girişi")
  yonetici_sifre = st.text_input("Yönetici Şifresi", type="password", key="admin_pass")
  if yonetici_sifre == "meteo2026":
    st.success("✅ Yönetici Modu Aktif")
    render_live_admin_metrics()
    st.info("Bu dinamik veriler sadece senin yönetici panelinde görünür.")
  elif yonetici_sifre != "":
    st.error("Hatalı şifre.")
  else:
    st.info("Canlı istatistikleri görmek için şifrenizi girin.")


# --- ANA UYGULAMA ---
st.title("🌍 Global Meteoroloji & Uzman Ar-Ge Paneli")

# --- 🚨 DİKKAT ÇEKİCİ EN ÜST TÜRKİYE GENELİ ERKEN UYARI ÖZET MASASI ---
turkiye_riskler = fetch_turkey_wide_ews()
if not turkiye_riskler:
  st.success("🟢 **TÜRKİYE GENELİ ERKEN UYARI MASASI — DURUM STABİL:** Ülke genelindeki stratejik merkezlerde önümüzdeki 3 gün boyunca ekstrem bir afet riski (fırtına, şiddetli sel) tespit edilmemiştir.")
else:
  st.error("🚨 **ALARM! TÜRKİYE GENELİNDE KRİTİK METEOROLOJİK AFET UYARISI TESPİT EDİLDİ!**")
  for r in turkiye_riskler:
    t_fmt = pd.to_datetime(r['zaman']).strftime('%d %B %Y, Saat: %H:00')
    st.warning(f"⚠️ **{r['bolge']}** bölgesinde **{r['tip']}** ({r['deger']}) bekleniyor! | Tahmini Zaman: **{t_fmt}**")

st.markdown("Dünya haritası üzerinden nokta seçin, **bölge çizin (Polygon)**, canlı radarı inceleyin veya **güzergah hava analizi** yapın.")

geolocator = Nominatim(user_agent="meteo_app_ITU_expert", timeout=3)

if "lat" not in st.session_state:
  st.session_state.lat = 41.0082
if "lon" not in st.session_state:
  st.session_state.lon = 28.9784
if "zoom" not in st.session_state:
  st.session_state.zoom = 10
if "location_label" not in st.session_state:
  st.session_state.location_label = "İstanbul / Merkez"

if "route_markers" not in st.session_state:
  st.session_state.route_markers = []


# --- KONUM VE MODEL BAZLI OPEN-METEO VERİ MOTORU ---
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CURRENT_VARIABLES = [
    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
    "precipitation", "snowfall", "weather_code", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m",
]
HOURLY_VARIABLES = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "precipitation", "snowfall", "snow_depth", "precipitation_probability",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "surface_pressure", "cloud_cover",
]
FORECAST_MODELS = [
    ("ECMWF IFS", "ECMWF_IFS", ["ecmwf_ifs"]),
    ("GFS", "GFS", ["ncep_gfs_seamless", "gfs_seamless"]),
    ("ICON", "ICON", ["icon_seamless", "dwd_icon_seamless"]),
]

BACKUP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "meteoscope_backup_cache_v1")
os.makedirs(BACKUP_CACHE_DIR, exist_ok=True)


def _open_meteo_get(url, params, timeout=8):
  response = requests.get(url, params=params, timeout=timeout)
  try:
    payload = response.json()
  except Exception:
    payload = {}
  if not response.ok or payload.get("error"):
    reason = payload.get("reason") or payload.get("message") or response.text[:300]
    raise RuntimeError(f"Open-Meteo HTTP {response.status_code}: {reason}")
  return payload


def _get_cache_filepath(latitude, longitude):
  lat_key = round(float(latitude), 3)
  lon_key = round(float(longitude), 3)
  return os.path.join(BACKUP_CACHE_DIR, f"weather_cache_{lat_key}_{lon_key}.json")


def save_weather_to_backup_cache(latitude, longitude, f_data, df_serialized, model_status, model_errors):
  try:
    cache_path = _get_cache_filepath(latitude, longitude)
    payload = {
        "f_data": f_data,
        "dataframe_json": df_serialized.to_json(date_format="iso", orient="split"),
        "model_status": model_status,
        "model_errors": model_errors,
        "saved_at": time.time(),
    }
    with open(cache_path, "w", encoding="utf-8") as f:
      json.dump(payload, f)
  except Exception:
    pass


def load_weather_from_backup_cache(latitude, longitude):
  try:
    cache_path = _get_cache_filepath(latitude, longitude)
    if os.path.exists(cache_path):
      with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
      f_data = payload.get("f_data")
      df_json = payload.get("dataframe_json")
      df = pd.read_json(df_json, orient="split")
      if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
      return f_data, df, payload.get("model_status", {}), payload.get("model_errors", {})
  except Exception:
    pass
  return None, None, None, None


@st.cache_data(ttl=600, show_spinner=False)
def fetch_weather_bundle(latitude, longitude):
  common_params = {
      "latitude": float(latitude),
      "longitude": float(longitude),
      "hourly": ",".join(HOURLY_VARIABLES),
      "forecast_days": 7,
      "timezone": "auto",
      "temperature_unit": "celsius",
      "wind_speed_unit": "kmh",
      "precipitation_unit": "mm",
  }
  best_params = dict(common_params)
  best_params["current"] = ",".join(CURRENT_VARIABLES)
  
  try:
    best_data = _open_meteo_get(OPEN_METEO_FORECAST_URL, best_params, timeout=10)
  except Exception as live_exc:
    cached_f, cached_df, cached_status, cached_errors = load_weather_from_backup_cache(latitude, longitude)
    if cached_f is not None and cached_df is not None:
      return cached_f, cached_df, cached_status, cached_errors
    raise live_exc

  best_hourly = best_data.get("hourly") or {}
  if not best_hourly.get("time"):
    raise RuntimeError("Best Match tahmin yanıtında saatlik zaman dizisi bulunamadı.")

  df = pd.DataFrame(best_hourly)
  df["time"] = pd.to_datetime(df["time"], errors="coerce")
  df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
  for variable in HOURLY_VARIABLES:
    if variable in df.columns:
      df[f"{variable}_BEST_MATCH"] = df[variable]

  model_status = {}
  model_errors = {}

  def fetch_one_model(model_spec):
    display_name, suffix, model_ids = model_spec
    last_error = None
    for model_id in model_ids:
      params = dict(common_params)
      params["models"] = model_id
      try:
        data = _open_meteo_get(OPEN_METEO_FORECAST_URL, params, timeout=8)
        hourly = data.get("hourly") or {}
        if not hourly.get("time"):
          raise RuntimeError("Model yanıtında saatlik zaman dizisi yok.")
        model_df = pd.DataFrame(hourly)
        model_df["time"] = pd.to_datetime(model_df["time"], errors="coerce")
        model_df = model_df.dropna(subset=["time"])
        rename = {col: f"{col}_{suffix}" for col in model_df.columns if col != "time"}
        model_df = model_df.rename(columns=rename)
        return display_name, suffix, model_df, model_id
      except Exception as exc:
        last_error = exc
    raise RuntimeError(f"{display_name}: {last_error}")

  with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(fetch_one_model, spec): spec for spec in FORECAST_MODELS}
    for future in as_completed(futures):
      spec = futures[future]
      display_name, suffix = spec[0], spec[1]
      try:
        result_name, result_suffix, model_df, model_id = future.result()
        df = df.merge(model_df, on="time", how="left")
        model_columns = [c for c in model_df.columns if c != "time"]
        populated = sum(1 for col in model_columns if col in df.columns and df[col].notna().any())
        model_status[result_name] = {
            "model_id": model_id,
            "available_fields": populated,
            "requested_fields": len(HOURLY_VARIABLES),
        }
      except Exception as exc:
        model_errors[display_name] = str(exc)

  df = df.sort_values("time").reset_index(drop=True)
  save_weather_to_backup_cache(latitude, longitude, best_data, df, model_status, model_errors)
  return best_data, df, model_status, model_errors


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_archive_data(latitude, longitude, start_date, end_date):
  params = {
      "latitude": float(latitude),
      "longitude": float(longitude),
      "start_date": str(start_date),
      "end_date": str(end_date),
      "hourly": "temperature_2m,precipitation",
      "timezone": "auto",
      "temperature_unit": "celsius",
      "precipitation_unit": "mm",
  }
  payload = _open_meteo_get(OPEN_METEO_ARCHIVE_URL, params, timeout=10)
  hourly = payload.get("hourly") or {}
  if not hourly.get("time"):
    return pd.DataFrame()
  archive_df = pd.DataFrame(hourly)
  archive_df["time"] = pd.to_datetime(archive_df["time"], errors="coerce")
  return archive_df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


# --- İKİ KONUM ARASI HIZLI VE OPTİMİZE GÜZERGAH RİSK ANALİZİ ---
with st.expander("🚗 İki Konum Arası Detaylı Güzergah & Yol Üstü Risk Analizi", expanded=False):
  st.markdown("Seyahat edeceğiniz güzergahı, **ulaşım aracınızı** ve planlanan tarihi girerek **sadece kalkış/varış değil, yol üstündeki ara geçişlerdeki gizli riskleri** anında tespit edin.")
  
  col_g1, col_g2 = st.columns(2)
  with col_g1:
    baslangic_yer = st.text_input("Başlangıç Konumu", placeholder="Örn: İstanbul", key="bas_yer")
  with col_g2:
    bitis_yer = st.text_input("Varış Konumu", placeholder="Örn: Ankara veya Bursa", key="bit_yer")

  col_t1, col_t2 = st.columns(2)
  with col_t1:
    ulasim_turu = st.selectbox(
        "Seçilen Ulaşım Aracı",
        ["Özel Araç / Otobüs (Kara Yolu)", "Uçak (Hava Yolu)", "Gemi / Feribot (Deniz Yolu)"],
        index=0
    )
  with col_t2:
    bugun_tarih = date.today()
    max_tarih = bugun_tarih + timedelta(days=7)
    planlanan_tarih = st.date_input(
        "Planlanan Seyahat Tarihi (Max 7 Gün Sınırı)",
        value=bugun_tarih,
        min_value=bugun_tarih,
        max_value=max_tarih,
        help="Küresel tahmin modellerinin güvenilir simülasyonu için 7 günlük sınır uygulanır."
    )

  rota_sorgula = st.button("Rotayı ve Yol Üstü Noktaları Hızlıca Analiz Et")

  if rota_sorgula and baslangic_yer and bitis_yer:
    with st.spinner("Güzergah ve yol üstü hava durumları eşzamanlı taranıyor..."):
      try:
        # Konum çözümleme (Geocoding) paralel veya hızlı timeout ile
        loc_b = geolocator.geocode(baslangic_yer, timeout=3)
        loc_v = geolocator.geocode(bitis_yer, timeout=3)
        if loc_b and loc_v:
          route_current_vars = "temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m"
          
          lat_b, lon_b = loc_b.latitude, loc_b.longitude
          lat_v, lon_v = loc_v.latitude, loc_v.longitude
          
          lat_mid1 = lat_b + (lat_v - lat_b) * 0.33
          lon_mid1 = lon_b + (lon_v - lon_b) * 0.33
          
          lat_mid2 = lat_b + (lat_v - lat_b) * 0.66
          lon_mid2 = lon_b + (lon_v - lon_b) * 0.66

          def get_point_weather_safe(lat, lon):
            try:
              params = {
                  "latitude": lat, "longitude": lon,
                  "current": route_current_vars, "timezone": "auto",
                  "wind_speed_unit": "kmh", "precipitation_unit": "mm",
              }
              res = _open_meteo_get(OPEN_METEO_FORECAST_URL, params, timeout=6).get("current", {})
              return {k: _safe_number(res.get(k)) for k in route_current_vars.split(",")}
            except Exception:
              return {k: 0.0 for k in route_current_vars.split(",")}

          # 4 noktayı aynı anda (paralel) sorgula - Süreyi minimuma indirir
          with ThreadPoolExecutor(max_workers=4) as executor:
            future_b = executor.submit(get_point_weather_safe, lat_b, lon_b)
            future_m1 = executor.submit(get_point_weather_safe, lat_mid1, lon_mid1)
            future_m2 = executor.submit(get_point_weather_safe, lat_mid2, lon_mid2)
            future_v = executor.submit(get_point_weather_safe, lat_v, lon_v)

            res_b = future_b.result()
            res_m1 = future_m1.result()
            res_m2 = future_m2.result()
            res_v = future_v.result()

          st.markdown("---")
          st.subheader(f"🗺️ Rota Analiz Özeti ({planlanan_tarih.strftime('%d.%m.%Y')}): {baslangic_yer} ➡️ {bitis_yer}")
          
          col_r1, col_r2, col_r3, col_r4 = st.columns(4)
          with col_r1:
            st.info(f"📍 **Kalkış ({baslangic_yer}):**\n- Sıcaklık: {res_b.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_b.get('wind_speed_10m', 0):.1f} km/s")
          with col_r2:
            st.info(f"🛣️ **Yol Üstü (1. Geçiş):**\n- Sıcaklık: {res_m1.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_m1.get('wind_speed_10m', 0):.1f} km/s")
          with col_r3:
            st.info(f"🛣️ **Yol Üstü (2. Geçiş):**\n- Sıcaklık: {res_m2.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_m2.get('wind_speed_10m', 0):.1f} km/s")
          with col_r4:
            st.info(f"🏁 **Varış ({bitis_yer}):**\n- Sıcaklık: {res_v.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_v.get('wind_speed_10m', 0):.1f} km/s")

          sorunlar = []
          durum_kodu = "yesil"
          route_markers_temp = []

          route_markers_temp.append({"lat": lat_b, "lon": lon_b, "name": f"Kalkış: {baslangic_yer}", "risk": False})
          route_markers_temp.append({"lat": lat_v, "lon": lon_v, "name": f"Varış: {bitis_yer}", "risk": False})

          points = [
              ("Kalkış Noktası", lat_b, lon_b, res_b),
              ("Yol Üstü Geçiş Noktası 1", lat_mid1, lon_mid1, res_m1),
              ("Yol Üstü Geçiş Noktası 2", lat_mid2, lon_mid2, res_m2),
              ("Varış Noktası", lat_v, lon_v, res_v)
          ]

          for p_name, p_lat, p_lon, p_data in points:
            p_temp = p_data.get('temperature_2m', 15)
            p_snow = p_data.get('snowfall', 0)
            p_gust = p_data.get('wind_gusts_10m', 0)

            if ulasim_turu.startswith("Özel Araç"):
              if p_temp <= 0:
                durum_kodu = "kirmizi"
                sorunlar.append(f"❄️ **Buzlanma Tehlikesi ({p_name}):** Sıcaklık 0°C altında ({p_temp:.1f}°C), gizli buzlanma riski var!")
                route_markers_temp.append({"lat": p_lat, "lon": p_lon, "name": f"Buzlanma Riski: {p_name}", "risk": True})
              if p_snow > 0:
                durum_kodu = "kirmizi"
                sorunlar.append(f"🌨️ **Kar Yağışı ({p_name}):** Rota üzerinde aktif kar geçişi ({p_snow:.1f} cm) var.")
                route_markers_temp.append({"lat": p_lat, "lon": p_lon, "name": f"Kar Yağışı: {p_name}", "risk": True})
              if p_gust > 50:
                if durum_kodu != "kirmizi": durum_kodu = "sari"
                sorunlar.append(f"💨 **Şiddetli Rüzgar Hamlesi ({p_name}):** Rüzgar {p_gust:.1f} km/s seviyesinde.")

            elif ulasim_turu.startswith("Uçak"):
              if p_gust > 55:
                durum_kodu = "kirmizi"
                sorunlar.append(f"✈️ **Uçuş Güvenlik Riski ({p_name}):** {p_gust:.1f} km/s fırtına hamlesi uçuşları aksatabilir.")
                route_markers_temp.append({"lat": p_lat, "lon": p_lon, "name": f"Fırtına Riski: {p_name}", "risk": True})

            elif ulasim_turu.startswith("Gemi"):
              if p_gust > 40:
                durum_kodu = "kirmizi"
                sorunlar.append(f"🌊 **Feribot Sefer İptal Riski ({p_name}):** Rüzgar {p_gust:.1f} km/s.")
                route_markers_temp.append({"lat": p_lat, "lon": p_lon, "name": f"Deniz Rüzgarı Riski: {p_name}", "risk": True})

          st.session_state.route_markers = route_markers_temp

          if not sorunlar:
            st.success("✅ **Güzergah Mükemmel:** Kalkış, yol üstü geçişleri ve varış noktalarında seyahatinizi olumsuz etkileyecek hiçbir risk bulunmuyor.")
          else:
            if durum_kodu == "kirmizi":
              st.error("🚨 **YOL ÜSTÜ VE ROTA RİSK RAPORU (KRİTİK UYARI):**\n\n" + "\n".join(sorunlar))
            else:
              st.warning("⚠️ **YOL ÜSTÜ VE ROTA RİSK RAPORU (TEDBİRLİ OLUN):**\n\n" + "\n".join(sorunlar))
        else:
          st.error("Girilen başlangıç veya varış konumu haritada bulunamadı.")
      except Exception as e:
        st.error(f"Güzergah analiz hatası: {e}")

st.markdown("---")

# --- ENTER TUŞUYLA ÇALIŞAN AKILLI ARAMA FORMU ---
with st.form(key="search_form", clear_on_submit=False):
  col1, col2 = st.columns([3, 1])
  with col1:
    search_query = st.text_input("🔍 Konum Ara (Ülke, Şehir, İlçe, Mahalle)", placeholder="Örn: Moskova, Erzurum veya İstanbul", key="main_search_input")
  with col2:
    st.write("")
    st.write("")
    search_button = st.form_submit_button("Konuma Git 🚀")

if search_button and search_query:
  try:
    location = geolocator.geocode(search_query, timeout=3)
    if location:
      st.session_state.lat = location.latitude
      st.session_state.lon = location.longitude
      st.session_state.zoom = 12
      st.session_state.son_arama = search_query
      st.session_state.location_label = location.address
      st.session_state.skip_map_event_once = True
      st.success(f"Bulunan Konum: {location.address} (Enlem: {location.latitude:.4f}, Boylam: {location.longitude:.4f})")
    else:
      st.error("Aradığınız konum bulunamadı.")
  except Exception as e:
    st.error(f"Arama hatası: {e}")

# --- HARİTA VE RİSK KATMANLARI ---
m = folium.Map(
    location=[st.session_state.lat, st.session_state.lon],
    zoom_start=st.session_state.zoom,
    tiles="OpenStreetMap",
)

radar_layer = TileLayer(
    tiles="https://tile.rainviewer.com/v2/radar/nowcast_120/{z}/{x}/{y}/2/1_1.png",
    attr="RainViewer",
    name="Canlı Yağış Radarı",
    overlay=True,
    opacity=0.6,
)
radar_layer.add_to(m)

Draw(
    export=False,
    draw_options={
        "polyline": False,
        "marker": True,
        "circle": False,
        "rectangle": True,
        "polygon": True,
        "circlemarker": False,
    },
).add_to(m)

folium.Marker(
    [st.session_state.lat, st.session_state.lon],
    popup="Seçilen Konum",
    tooltip="Aktif Konum",
    icon=folium.Icon(color="red", icon="cloud"),
).add_to(m)

for r_marker in st.session_state.get("route_markers", []):
  marker_color = "red" if r_marker["risk"] else "green"
  marker_icon = "warning-sign" if r_marker["risk"] else "ok-sign"
  folium.Marker(
      [r_marker["lat"], r_marker["lon"]],
      popup=r_marker["name"],
      tooltip=r_marker["name"],
      icon=folium.Icon(color=marker_color, icon=marker_icon, prefix="glyphicon"),
  ).add_to(m)

folium.LayerControl().add_to(m)

st.subheader("📍 İnteraktif Harita, Gelişmiş Katmanlar ve Rota Risk İşaretleri")
st.info("Harita katmanlarını sağ üst köşeden değiştirebilir, alan çizebilir veya güzergahtaki riskli noktaları harita üzerinde inceleyebilirsiniz.")

map_data = st_folium(m, width="100%", height=480, key="meteoscope_main_map_v14")

skip_map_event = st.session_state.pop("skip_map_event_once", False)
if map_data and skip_map_event:
  clicked = map_data.get("last_clicked")
  if clicked:
    st.session_state.processed_map_click_signature = (
        round(float(clicked["lat"]), 6), round(float(clicked["lng"]), 6)
    )
  drawing = map_data.get("last_active_drawing")
  if drawing:
    geom = drawing.get("geometry", {})
    if geom.get("type") == "Polygon":
      coords = geom.get("coordinates", [[]])[0]
      open_ring = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
      st.session_state.processed_polygon_signature = tuple(
          (round(float(c[0]), 6), round(float(c[1]), 6)) for c in open_ring
      )

if map_data and not skip_map_event:
  drawing = map_data.get("last_active_drawing")
  if drawing:
    geom = drawing.get("geometry", {})
    if geom.get("type") == "Polygon":
      coords = geom.get("coordinates", [[]])[0]
      if coords:
        open_ring = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
        polygon_signature = tuple((round(float(c[0]), 6), round(float(c[1]), 6)) for c in open_ring)
        if polygon_signature and polygon_signature != st.session_state.get("processed_polygon_signature"):
          st.session_state.processed_polygon_signature = polygon_signature
          lats = [float(c[1]) for c in open_ring]
          lons = [float(c[0]) for c in open_ring]
          st.session_state.lat = sum(lats) / len(lats)
          st.session_state.lon = sum(lons) / len(lons)
          st.session_state.location_label = "Haritada çizilen bölgenin merkezi"
          clicked_after_draw = map_data.get("last_clicked")
          if clicked_after_draw:
            st.session_state.processed_map_click_signature = (
                round(float(clicked_after_draw["lat"]), 6),
                round(float(clicked_after_draw["lng"]), 6),
            )
          st.rerun()

  clicked = map_data.get("last_clicked")
  if clicked:
    click_signature = (round(float(clicked["lat"]), 6), round(float(clicked["lng"]), 6))
    if click_signature != st.session_state.get("processed_map_click_signature"):
      st.session_state.processed_map_click_signature = click_signature
      st.session_state.lat = float(clicked["lat"])
      st.session_state.lon = float(clicked["lng"])
      st.session_state.zoom = m.options["zoom"]
      st.session_state.location_label = "Harita üzerinde seçilen nokta"
      st.rerun()

def degrees_to_cardinal(deg):
  if deg is None:
    return "N/A"
  dirs = ["K", "KD", "D", "GD", "G", "GB", "B", "KB"]
  val = int((deg / 45) + 0.5)
  return dirs[(val % 8)]

if st.session_state.lat is not None and st.session_state.lon is not None:
  st.markdown("---")
  st.success(
      f"Aktif Analiz Konumu: {st.session_state.get('location_label', 'Seçilen konum')} "
      f"| Enlem: {st.session_state.lat:.4f}, Boylam: {st.session_state.lon:.4f}"
  )

  weather_error = None
  used_fallback_data = False
  with st.spinner("Seçilen koordinat için Best Match ve ECMWF / GFS / ICON modelleri alınıyor..."):
    try:
      request_lat = float(st.session_state.lat)
      request_lon = float(st.session_state.lon)
      
      try:
        f_data, df, model_status, model_errors = fetch_weather_bundle(request_lat, request_lon)
      except Exception:
        cached_f, cached_df, cached_status, cached_errors = load_weather_from_backup_cache(request_lat, request_lon)
        if cached_f is not None and cached_df is not None:
          f_data, df, model_status, model_errors = cached_f, cached_df, cached_status, cached_errors
          used_fallback_data = True
        else:
          raise

      current_payload = f_data.get("current") or {}
      current_data = {
          key: _safe_number(current_payload.get(key))
          for key in CURRENT_VARIABLES if key in current_payload
      }
      local_time_text = current_payload.get("time")
      if not local_time_text:
        local_time_text = (f_data.get("hourly") or {}).get("time", [None])[0]
      today = pd.to_datetime(local_time_text).date() if local_time_text else date.today()
      current_wind = _safe_number(current_data.get("wind_speed_10m"))
      current_gust = _safe_number(current_data.get("wind_gusts_10m"))
      current_snow = _safe_number(current_data.get("snowfall"))

      start_date = today - timedelta(days=7)
      try:
        df_archive = fetch_archive_data(request_lat, request_lon, start_date.isoformat(), today.isoformat())
      except Exception:
        df_archive = pd.DataFrame()

      csv_data = df.to_csv(index=False).encode("utf-8")

      if used_fallback_data:
        st.warning("⚠️ **Gece Modu / API Yoğunluk Uyarısı:** Gece saatlerindeki sunucu yoğunluğu veya bağlantı engeli nedeniyle canlı model verilerine anlık erişilemedi. Kullanıcılara kesintisiz hizmet sunulabilmesi için sistemde kayıtlı en son güncel veriler gösterilmektedir.")

      status_cols = st.columns(4)
      with status_cols[0]:
        st.metric("Veri Kaynağı", "Yedek Önbellek" if used_fallback_data else "Canlı API")
      for idx, model_name in enumerate(["ECMWF IFS", "GFS", "ICON"], start=1):
        with status_cols[idx]:
          if model_name in model_status:
            info = model_status[model_name]
            st.metric(model_name, f"{info['available_fields']}/{info['requested_fields']} parametre")
          else:
            st.metric(model_name, "Veri yok")
    except Exception as exc:
      weather_error = str(exc)
      df = pd.DataFrame()
      df_archive = pd.DataFrame()
      current_data = {}
      current_wind = 0.0
      current_gust = 0.0
      current_snow = 0.0
      csv_data = b""

  if weather_error is None:
    df_3days = df.head(72)

    aktif_konum_adi = st.session_state.get('location_label', 'Seçilen Konum')
    ekstrem_tehlikeler = ekstrem_erken_uyari_analizi(df, aktif_konum_adi)
    if ekstrem_tehlikeler:
      st.error("🚨 **METEOROLOJİK ERKEN UYARI SİSTEMİ (EWS) - SEÇİLEN KONUM UYARISI**")
      for tehlike in ekstrem_tehlikeler:
        st.warning(f"⚠️ **{tehlike['tarih_str']}** $\rightarrow$ **{tehlike['tip']}**: {tehlike['detay']}")

    st.markdown("### 🚨 Günün Meteorolojik Risk Özeti ve Canlı Uyarılar")
    uyarilar = konum_uyari_analizi(current_data, df_3days)
    for seviye, mesaj in uyarilar:
      if seviye == "kirmizi":
        st.error(mesaj)
      elif seviye == "sari":
        st.warning(mesaj)
      else:
        st.success(mesaj)

    st.markdown("### 📅 Önümüzdeki 3 Günün Günlük Kırılımlı Hava Tahmini")
    if not df_3days.empty:
      df_3days["date"] = df_3days["time"].dt.date
      unique_dates = df_3days["date"].unique()[:3]

      cols_day = st.columns(len(unique_dates) if len(unique_dates) > 0 else 1)
      
      for idx, d in enumerate(unique_dates):
        day_df = df_3days[df_3days["date"] == d]
        t_cols = [c for c in day_df.columns if "temperature_2m" in c]
        d_min_t = day_df[t_cols].min().min() if t_cols else 0
        d_max_t = day_df[t_cols].max().max() if t_cols else 0
        d_precip = day_df["precipitation"].sum() if "precipitation" in day_df.columns else 0
        d_snow = day_df["snowfall"].sum() if "snowfall" in day_df.columns else 0
        d_wind = day_df["wind_speed_10m"].max() if "wind_speed_10m" in day_df.columns else 0

        gun_ismi = pd.to_datetime(d).strftime('%A')
        gun_tr = {"Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba", "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar"}.get(gun_ismi, gun_ismi)

        with cols_day[idx]:
          with st.container(border=True):
            st.markdown(f"**📌 {d} ({gun_tr})**")
            st.metric(label="Min / Max Sıcaklık", value=f"{d_min_t:.1f}°C / {d_max_t:.1f}°C")
            st.metric(label="Toplam Yağış / Kar", value=f"{d_precip:.1f} mm / {d_snow:.1f} cm")
            st.metric(label="Max Rüzgar Hamlesi", value=f"{d_wind:.1f} km/s")

    if current_data:
      st.markdown("### ⚡ Anlık Meteorolojik Durum")
      wind_deg = current_data.get("wind_direction_10m", 0)
      wind_cardinal = degrees_to_cardinal(wind_deg)

      curr_col1, curr_col2, curr_col3, curr_col4, curr_col5 = st.columns(5)
      with curr_col1:
        st.metric(label="Sıcaklık", value=f"{current_data.get('temperature_2m', 0.0):.1f} °C")
      with curr_col2:
        st.metric(label="Hissedilen", value=f"{current_data.get('apparent_temperature', 0.0):.1f} °C")
      with curr_col3:
        st.metric(label="Bağıl Nem", value=f"{current_data.get('relative_humidity_2m', 0.0):.1f} %")
      with curr_col4:
        st.metric(label="Rüzgar Hızı / Hamle", value=f"{current_wind:.1f} / {current_gust:.1f} km/s")
      with curr_col5:
        st.metric(label="Rüzgar Yönü", value=f"{wind_cardinal} ({wind_deg}°)")

    st.markdown("### 📊 Detaylı Model Tahminleri ve Uzman Raporu")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "🤝 3'lü Model Konsensüsü (Gelişmiş)",
        "🕰️ Geçmiş Arşiv",
        "📄 Otomatik Uzman Raporu",
        "🌡️ Sıcaklık & Nem",
        "💨 Hassas Rüzgar & Hamle",
        "☁️ Basınç & Bulut",
        "❄️ Yağış, Kar & Olasılık",
        "📋 7 Günlük Hava Özeti & İndirme",
    ])

    with tab1:
      st.subheader("🌐 Küresel Modeller Arası Karşılaştırma ve Konsensüs Analizi")
      st.markdown("Aşağıdaki grafiklerde **Sıcaklık**, **Rüzgar** ve **Yağış** trendleri karşılaştırılmaktadır.")

      st.write("**1. Sıcaklık Modelleri Karşılaştırması (°C):**")
      model_temp_cols = [c for c in df.columns if c.startswith("temperature_2m_")]
      if model_temp_cols:
        st.line_chart(df.set_index("time")[model_temp_cols])
      else:
        st.line_chart(df.set_index("time")[["temperature_2m"]])

      st.write("**2. Rüzgar Hızı Modelleri Karşılaştırması (km/s):**")
      model_wind_cols = [c for c in df.columns if c.startswith("wind_speed_10m_")]
      if model_wind_cols:
        st.line_chart(df.set_index("time")[model_wind_cols])
      else:
        st.line_chart(df.set_index("time")[["wind_speed_10m"]])

      st.write("**3. Yağış Modelleri Karşılaştırması (mm):**")
      model_precip_cols = [c for c in df.columns if c.startswith("precipitation_")]
      if model_precip_cols:
        st.line_chart(df.set_index("time")[model_precip_cols])
      else:
        st.line_chart(df.set_index("time")[["precipitation"]])

    with tab2:
      st.subheader("Geçmiş 7 Günün Arşiv Verileri")
      if not df_archive.empty:
        st.line_chart(df_archive.set_index("time")[["temperature_2m", "precipitation"]])
        st.dataframe(df_archive, use_container_width=True)
      else:
        st.info("Arşiv verisi bulunamadı.")

    with tab3:
      st.subheader("📄 Otomatik Meteorolojik Durum Raporu")
      t_all = [c for c in df_3days.columns if "temperature_2m" in c]
      rep_min = df_3days[t_all].min().min() if t_all else 0
      rep_max = df_3days[t_all].max().max() if t_all else 0
      rep_precip = df_3days["precipitation"].sum() if "precipitation" in df_3days.columns else 0
      rep_snow = df_3days["snowfall"].sum() if "snowfall" in df_3days.columns else 0
      rep_wind = df_3days["wind_speed_10m"].max() if "wind_speed_10m" in df_3days.columns else 0

      ortalama_sicaklik_degeri = (rep_min + rep_max) / 2
      giyim_tavsiyesi_metni = giyim_onerisi_olustur(
          ortalama_sicaklik_degeri, rep_precip, rep_snow, 50, rep_wind
      )

      report_text = f"""
      METEOROLOJİK UZMAN ANALİZ RAPORU
      ----------------------------------------------
      Konum Koordinatları: Enlem {st.session_state.lat:.4f}, Boylam {st.session_state.lon:.4f}
      Rapor Tarihi: {date.today().strftime('%Y-%m-%d')}
      
      1. GÜNÜN RİSK ÖZETİ VE ANLIK DURUM:
      - Sıcaklık: {current_data.get('temperature_2m', 0.0):.1f} °C (Hissedilen: {current_data.get('apparent_temperature', 0.0):.1f} °C)
      - Kar Yağışı (Anlık): {current_snow:.1f} cm
      - Rüzgar Hızı / Hamle: {current_wind:.1f} / {current_gust:.1f} km/s
      
      2. 3 GÜNLÜK GÜN GÜN (BÖLÜNMÜŞ) TAHMİN TRENDİ:
      - Önümüzdeki 3 günlük periyotta sıcaklık {rep_min:.1f}°C ile {rep_max:.1f}°C arasında seyredecek olup, veriler günlük bazda (tarih kırılımlı) izlenmektedir.
      - Toplam Yağış / Kar Beklentisi: {rep_precip:.1f} mm / {rep_snow:.1f} cm
      
      3. AKILLI GİYİM TAVSİYESİ:
      {giyim_tavsiyesi_metni.replace('**', '').replace('❄️ ', '').replace('🌪️ ', '').replace('🌧️ ', '').replace('🧥 ', '').replace('👕 ', '').replace('👔 ', '')}
      """

      st.info(giyim_tavsiyesi_metni)
      st.text_area("Rapor Önizlemesi", report_text, height=280)

      st.download_button(
          label="📥 Uzman Raporunu Metin Olarak İndir",
          data=report_text.encode("utf-8"),
          file_name=f"meteo_report_lat_{st.session_state.lat:.2f}_lon_{st.session_state.lon:.2f}.txt",
          mime="text/plain",
      )

    with tab4:
      st.subheader("Sıcaklık ve Bağıl Nem / Çiy Noktası")
      temp_cols = [col for col in df.columns if "temperature_2m" in col or "relative_humidity_2m" in col]
      if temp_cols:
        st.line_chart(df.set_index("time")[temp_cols])

    with tab5:
      st.subheader("Hassas Rüzgar Hızı, Hamle (Gust) ve Yön Analizi")
      wind_cols = [col for col in df.columns if "speed" in col or "gusts" in col]
      if wind_cols:
        st.line_chart(df.set_index("time")[wind_cols])

    with tab6:
      st.subheader("Atmosferik Basınç ve Bulut Örtüsü")
      press_cols = [col for col in df.columns if "pressure" in col or "cloud_cover" in col]
      if press_cols:
        st.line_chart(df.set_index("time")[press_cols])

    with tab7:
      st.subheader("Yağış, Kar Yağışı ve Yağış İhtimali (%)")
      rain_snow_cols = [col for col in df.columns if "precipitation" in col or "snow" in col]
      if rain_snow_cols:
        st.line_chart(df.set_index("time")[rain_snow_cols])

    with tab8:
      st.subheader(f"📅 Seçilen Konum İçin 7 Günlük Özet Tablo ({st.session_state.get('location_label', 'Konum')})")
      st.markdown("Karmaşık ham veriler yerine, seçtiğiniz konum için önümüzdeki 7 günün en önemli hava parametreleri sadeleştirilmiş olarak aşağıda listelenmiştir.")
      
      if not df.empty:
        df["date_only"] = df["time"].dt.date
        seven_days_summary = []
        for d_val, group_df in list(df.groupby("date_only"))[:7]:
          t_col_name = [c for c in group_df.columns if "temperature_2m" in c]
          min_t = group_df[t_col_name].min().min() if t_col_name else 0
          max_t = group_df[t_col_name].max().max() if t_col_name else 0
          tot_p = group_df["precipitation"].sum() if "precipitation" in group_df.columns else 0
          max_w = group_df["wind_speed_10m"].max() if "wind_speed_10m" in group_df.columns else 0
          
          gun_adi_en = pd.to_datetime(d_val).strftime('%A')
          gun_tr = {"Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba", "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar"}.get(gun_adi_en, gun_adi_en)
          
          seven_days_summary.append({
              "Tarih": str(d_val),
              "Gün": gun_tr,
              "En Düşük Sıcaklık (°C)": round(min_t, 1),
              "En Yüksek Sıcaklık (°C)": round(max_t, 1),
              "Toplam Yağış (mm)": round(tot_p, 1),
              "Maksimum Rüzgar (km/s)": round(max_w, 1)
          })
        
        summary_df = pd.DataFrame(seven_days_summary)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
      
      st.markdown("---")
      col_dl1, col_dl2 = st.columns([2, 2])
      with col_dl1:
        st.download_button(
            label="📥 Tüm Detaylı Tahmin Verilerini İndir (CSV)",
            data=csv_data,
            file_name=f"meteo_forecast_lat_{st.session_state.lat:.2f}_lon_{st.session_state.lon:.2f}.csv",
            mime="text/csv",
        )

  else:
    st.error(f"Meteoroloji verileri alınamadı: {weather_error}")
