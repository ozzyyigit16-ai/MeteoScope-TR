from datetime import date, timedelta
from contextlib import contextmanager
from geopy.geocoders import Nominatim
import folium
from folium.plugins import Draw, Fullscreen, LocateControl
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
from geopy.distance import geodesic

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
    st.markdown("### 📊 Yönetici İstatistikleri")
    st.metric(label="Toplam Benzersiz Ziyaretçi", value=f"{total_visitors:,}")
    st.metric(label="Anlık Aktif Oturum", value=active_count)
    st.metric(
        label="Son Aranan Konum",
        value=st.session_state.get("son_arama", "İstanbul / Merkez"),
    )
    st.caption("Bu veriler sadece şifre bilen yöneticiler tarafından görülebilir.")


# --- AKILLI GİYİM ÖNERİSİ MOTORU ---
def giyim_onerisi_olustur(ortalama_sicaklik, toplam_yagis, toplam_kar, max_ruzgar):
  if max_ruzgar > 45:
    return "🌪️ **Giyim Önerisi:** Bölgede kuvvetli rüzgar ve fırtına hamleleri var. Rüzgarı kesen montlar tercih edin."
  elif toplam_kar > 0.5 or ortalama_sicaklik <= 0:
    return "❄️ **Giyim Önerisi:** Hava sıfırın altında ve sert soğuk hakim. Kalın kışlık mont ve botlarınızı giyin."
  elif toplam_yagis > 1.0:
    return "🌧️ **Giyim Önerisi:** Yağış geçişleri var, yanınıza şemsiye alın."
  elif ortalama_sicaklik < 12:
    return "🧥 **Giyim Önerisi:** Hava oldukça soğuk, kat kat giyinin."
  elif ortalama_sicaklik > 25:
    return "👕 **Giyim Önerisi:** Hava sıcak, hafif kıyafetler tercih edebilirsiniz."
  else:
    return "👔 **Giyim Önerisi:** Hava mevsim normallerinde."


# --- SAAT BAZLI KONUM TEHLİKE TARAMA MOTORU ---
def konum_saatlik_tehlike_taramasi(df):
  if df.empty or "time" not in df.columns:
    return []
  
  tehlikeli_saatler = []
  for _, row in df.iterrows():
    t = row["time"]
    precip = _safe_number(row.get("precipitation", 0))
    gust = _safe_number(row.get("wind_gusts_10m", 0))
    cape = _safe_number(row.get("cape", 0))
    temp = _safe_number(row.get("temperature_2m", 15))

    if precip >= 7.0:
      tehlikeli_saatler.append({
          "zaman": t,
          "tur": "Şiddetli Sağanak / Su Baskını Riski",
          "deger": f"Saatlik Yağış: {precip:.1f} mm/s",
          "seviye": "kirmizi"
      })
    elif gust >= 50.0:
      tehlikeli_saatler.append({
          "zaman": t,
          "tur": "Kuvvetli Fırtına / Rüzgar Hamlesi",
          "deger": f"Max Gust: {gust:.1f} km/s",
          "seviye": "kirmizi"
      })
    elif cape >= 300.0:
      tehlikeli_saatler.append({
          "zaman": t,
          "tur": "Atmosferik Kararsızlık (Şimşek/Yıldırım Riski)",
          "deger": f"CAPE: {cape:.0f} J/kg",
          "seviye": "sari"
      })
    elif temp <= 0.0 and precip > 0:
      tehlikeli_saatler.append({
          "zaman": t,
          "tur": "Don / Buzlanma ve Kar Geçişi",
          "deger": f"Sıcaklık: {temp:.1f}°C, Yağış Var",
          "seviye": "kirmizi"
      })
  return tehlikeli_saatler


# --- HASSASLAŞTIRILMIŞ GENEL UYARI ANALİZİ ---
def konum_uyari_analizi(curr_data, df_3days):
  uyarilar = []
  temp = curr_data.get("temperature_2m", 15)
  wind = curr_data.get("wind_speed_10m", 0)
  gust = curr_data.get("wind_gusts_10m", 0)
  snow = curr_data.get("snowfall", 0)
  cape = curr_data.get("cape", 0)
  precip_curr = curr_data.get("precipitation", 0)

  if temp <= 0:
    uyarilar.append(("kirmizi", "❄️ **Don / Buzlanma Riski:** Sıcaklık 0°C veya altında!"))
  elif temp < 5:
    uyarilar.append(("sari", "⚠️ **Düşük Sıcaklık:** Hava oldukça soğuk."))

  if wind > 35 or gust > 45:
    uyarilar.append(("kirmizi", f"🌪️ **Kuvvetli Rüzgar / Fırtına Hamlesi:** Anlık rüzgar hamlesi {gust:.1f} km/s!"))

  if snow > 0:
    uyarilar.append(("kirmizi", f"🌨️ **Aktif Kar Yağışı:** Bölgede kar geçişi var ({snow:.1f} cm)."))

  if cape > 200:
    uyarilar.append(("kirmizi", f"⚡ **Atmosferik Kararsızlık (CAPE: {cape:.0f} J/kg):** Şimşek/sağanak riski yüksek!"))

  if not df_3days.empty:
    total_p = df_3days["precipitation"].sum() if "precipitation" in df_3days.columns else 0
    max_hourly_p = df_3days["precipitation"].max() if "precipitation" in df_3days.columns else 0
    if max_hourly_p >= 7.0:
      uyarilar.append(("kirmizi", f"🚨 **ŞİDDETLİ SAĞANAK / SEL RİSKİ:** Saatlik bazda **{max_hourly_p:.1f} mm/saat** yoğun yağış bekleniyor!"))
    elif precip_curr > 0 or total_p > 10.0:
      uyarilar.append(("sari", f"🌧️ **Kuvvetli Yağış / Sağanak:** Periyotta toplam {total_p:.1f} mm yağış öngörülüyor."))

  if not uyarilar:
    uyarilar.append(("yesil", "✅ **Hava Koşulları Stabil:** Ekstrem risk oluşturan bir parametre görünmüyor."))

  return uyarilar


# --- TÜRKİYE GENELİ ERKEN UYARI MASASI (EWS) ---
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

@st.cache_data(ttl=300, show_spinner=False)
def fetch_turkey_wide_ews():
  global_alerts = []
  def check_center(name, lat, lon):
    try:
      url = "https://api.open-meteo.com/v1/forecast"
      params = {
          "latitude": lat, "longitude": lon,
          "hourly": "precipitation,wind_gusts_10m,cape",
          "forecast_days": 3,
          "timezone": "auto"
      }
      res = requests.get(url, params=params, timeout=4)
      if res.ok:
        data = res.json().get("hourly", {})
        times = data.get("time", [])
        precips = data.get("precipitation", [])
        gusts = data.get("wind_gusts_10m", [])
        capes = data.get("cape", []) if "cape" in data else [0]*len(times)
        
        for t, p, g, c in zip(times, precips, gusts, capes):
          p_val = _safe_number(p)
          g_val = _safe_number(g)
          c_val = _safe_number(c)
          if p_val >= 8.0:
            return {"bolge": name, "tip": "Kuvvetli Sağanak / Su Baskını Riski", "deger": f"{p_val:.1f} mm/saat", "zaman": t}
          elif g_val >= 50.0 or c_val >= 350.0:
            return {"bolge": name, "tip": "Fırtına / Konvektif Aktivite", "deger": f"Gust: {g_val:.1f} km/s / CAPE: {c_val:.0f}", "zaman": t}
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
  elif yonetici_sifre != "":
    st.error("Hatalı şifre.")
  else:
    st.info("Yönetici istatistiklerini görmek için şifrenizi girin.")


# --- ANA UYGULAMA ---
st.title("🌍 Global Meteoroloji & Uzman Ar-Ge Paneli")

# --- 🚨 TÜRKİYE GENELİ ERKEN UYARI MASASI ---
turkiye_riskler = fetch_turkey_wide_ews()
if not turkiye_riskler:
  st.success("🟢 **TÜRKİYE GENELİ ERKEN UYARI MASASI — DURUM STABİL:** Stratejik merkezlerde önümüzdeki 3 gün boyunca ekstrem bir afet riski tespit edilmemiştir.")
else:
  st.error("🚨 **ALARM! TÜRKİYE GENELİNDE KRİTİK HAVA OLAYI / YAĞIŞ UYARISI TESPİT EDİLDİ!**")
  for r in turkiye_riskler:
    t_fmt = pd.to_datetime(r['zaman']).strftime('%d %B %Y, Saat: %H:00')
    st.warning(f"⚠️ **{r['bolge']}** bölgesinde **{r['tip']}** ({r['deger']}) bekleniyor! | Zaman: **{t_fmt}**")

st.markdown("Dünya haritası üzerinden global ölçekte nokta arayın, **bölge çizin (Polygon)**, canlı yağış radarını inceleyin veya **güzergah hava analizi** yapın.")

geolocator = Nominatim(user_agent="meteo_app_global_expert_v4", timeout=5)

if "lat" not in st.session_state:
  st.session_state.lat = 41.0082
if "lon" not in st.session_state:
  st.session_state.lon = 28.9784
if "zoom" not in st.session_state:
  st.session_state.zoom = 10
if "location_label" not in st.session_state:
  st.session_state.location_label = "İstanbul / Merkez"

if "route_map_data" not in st.session_state:
  st.session_state.route_map_data = None


# --- GLOBAL SEYAHAT VE GÜZERGAH RİSK ANALİZİ ---
seyahat_acik = st.toggle("🚗 Global İki Konum Arası Detaylı Güzergah & Yol Üstü Risk Analizi", value=False, help="Dünyanın herhangi iki noktası arasında coğrafi mantık ve hava risklerini denetleyen profesyonel seyahat masası.")

if seyahat_acik:
  with st.container(border=True):
    st.markdown("### 🚗 Profesyonel ve Coğrafi Uyumlu Global Seyahat Analiz Masası")
    st.markdown("Dünyanın dilediğiniz iki noktası arasında güvenli seyahat planlayın.")
    
    col_g1, col_g2 = st.columns(2)
    with col_g1:
      baslangic_yer = st.text_input("Başlangıç Konumu (Global)", placeholder="Örn: Roma veya İstanbul", key="bas_yer")
    with col_g2:
      bitis_yer = st.text_input("Varış Konumu (Global)", placeholder="Örn: Madrid veya Ankara", key="bit_yer")

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
      )

    rota_sorgula = st.button("Global Rotayı ve Coğrafi Mantığı Analiz Et 🚀")

    if rota_sorgula and baslangic_yer and bitis_yer:
      with st.spinner("Küresel konum veritabanından noktalar çözümleniyor ve riskler taranıyor..."):
        try:
          loc_b = geolocator.geocode(baslangic_yer, timeout=5)
          loc_v = geolocator.geocode(bitis_yer, timeout=5)
          if loc_b and loc_v:
            lat_b, lon_b = loc_b.latitude, loc_b.longitude
            lat_v, lon_v = loc_v.latitude, loc_v.longitude
            mesafe_km = geodesic((lat_b, lon_b), (lat_v, lon_v)).km

            hata_mesaji = None
            if ulasim_turu.startswith("Gemi") and mesafe_km < 30:
              hata_mesaji = f"❌ **Coğrafi Hata:** Seçtiğiniz konumlar arasındaki mesafe ({mesafe_km:.1f} km) gemi seferi için çok kısa."
            elif ulasim_turu.startswith("Özel Araç") and mesafe_km > 8000:
              hata_mesaji = f"❌ **Coğrafi Hata:** Mesafe ({mesafe_km:,.0f} km) kıtalar arası veya karayoluyla geçilemeyecek kadar uzaktır."
            elif ulasim_turu.startswith("Uçak") and mesafe_km < 80:
              hata_mesaji = f"❌ **Coğrafi Hata:** Mesafe ({mesafe_km:.1f} km) uçuş için çok kısadır."

            if hata_mesaji:
              st.error(hata_mesaji)
            else:
              d_lat = lat_v - lat_b
              d_lon = lon_v - lon_b
              lat_mid1 = lat_b + d_lat * 0.33
              lon_mid1 = lon_b + d_lon * 0.33
              lat_mid2 = lat_b + d_lat * 0.66
              lon_mid2 = lon_b + d_lon * 0.66

              route_current_vars = "temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m,cape"

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

              with ThreadPoolExecutor(max_workers=4) as executor:
                future_b = executor.submit(get_point_weather_safe, lat_b, lon_b)
                future_m1 = executor.submit(get_point_weather_safe, lat_mid1, lon_mid1)
                future_m2 = executor.submit(get_point_weather_safe, lat_mid2, lon_mid2)
                future_v = executor.submit(get_point_weather_safe, lat_v, lon_v)

                res_b = future_b.result()
                res_m1 = future_m1.result()
                res_m2 = future_m2.result()
                res_v = future_v.result()

              markers_for_route_map = []
              detailed_written_report = []
              durum_kodu = "yesil"

              point_records = [
                  {"name": f"Kalkış: {loc_b.address}", "lat": lat_b, "lon": lon_b, "data": res_b},
                  {"name": f"Yol Üstü Ara Geçiş 1", "lat": lat_mid1, "lon": lon_mid1, "data": res_m1},
                  {"name": f"Yol Üstü Ara Geçiş 2", "lat": lat_mid2, "lon": lon_mid2, "data": res_m2},
                  {"name": f"Varış: {loc_v.address}", "lat": lat_v, "lon": lon_v, "data": res_v}
              ]

              for p in point_records:
                p_data = p["data"]
                p_temp = p_data.get('temperature_2m', 15)
                p_snow = p_data.get('snowfall', 0)
                p_gust = p_data.get('wind_gusts_10m', 0)
                p_wind = p_data.get('wind_speed_10m', 0)
                p_cape = p_data.get('cape', 0)
                p_precip = p_data.get('precipitation', 0)

                is_risky = False
                risk_title = "Stabil / Güvenli Koşullar"
                risk_desc = f"Sıcaklık: {p_temp:.1f}°C | Rüzgar: {p_wind:.1f} km/s (Hamle: {p_gust:.1f}) | Yağış: {p_precip:.1f} mm"

                if ulasim_turu.startswith("Özel Araç"):
                  if p_temp <= 0:
                    durum_kodu = "kirmizi"
                    is_risky = True
                    risk_title = "❄️ Don / Gizli Buzlanma Tehlikesi"
                    risk_desc = f"Sıcaklık 0°C altında ({p_temp:.1f}°C). Don riski var!"
                  elif p_snow > 0:
                    durum_kodu = "kirmizi"
                    is_risky = True
                    risk_title = "🌨️ Aktif Kar Yağışı"
                    risk_desc = f"Kar geçişi ({p_snow:.1f} cm) var."
                  elif p_gust > 40 or p_cape > 250:
                    if durum_kodu != "kirmizi": durum_kodu = "sari"
                    is_risky = True
                    risk_title = "⚡ Fırtına / Kararsızlık Riski"
                    risk_desc = f"Rüzgar hamlesi {p_gust:.1f} km/s (CAPE: {p_cape:.0f})."

                markers_for_route_map.append({
                    "lat": p["lat"], "lon": p["lon"], "name": p["name"], "desc": risk_desc, "risk": is_risky
                })
                detailed_written_report.append({
                    "Nokta": p["name"], "Koordinat": f"({p['lat']:.4f}, {p['lon']:.4f})",
                    "Sıcaklık": f"{p_temp:.1f} °C", "Rüzgar / Hamle": f"{p_wind:.1f} / {p_gust:.1f} km/s",
                    "Yağış": f"{p_precip:.1f} mm", "Durum / Risk": risk_title, "Açıklama": risk_desc
                })

              st.session_state.route_map_data = {
                  "bas_ad": loc_b.address, "bit_ad": loc_v.address,
                  "lat_b": lat_b, "lon_b": lon_b, "lat_v": lat_v, "lon_v": lon_v,
                  "markers": markers_for_route_map, "report_table": detailed_written_report,
                  "durum": durum_kodu, "tarih": planlanan_tarih.strftime('%d.%m.%Y')
              }
          else:
            st.error("Girilen başlangıç veya varış konumu küresel haritada bulunamadı.")
        except Exception as e:
          st.error(f"Global güzergah analiz hatası: {e}")

    if st.session_state.get("route_map_data"):
      r_info = st.session_state.route_map_data
      st.markdown(f"### 📋 Rota Detaylı Yazılı Risk Raporu")
      report_df = pd.DataFrame(r_info["report_table"])
      st.dataframe(report_df, use_container_width=True, hide_index=True)

      st.markdown(f"### 🗺️ Global Güzergah Haritası")
      mid_lat = (r_info["lat_b"] + r_info["lat_v"]) / 2
      mid_lon = (r_info["lon_b"] + r_info["lon_v"]) / 2
      
      route_m = folium.Map(location=[mid_lat, mid_lon], zoom_start=5, tiles="OpenStreetMap")
      Fullscreen().add_to(route_m)
      LocateControl().add_to(route_m)

      poly_pts = [(m["lat"], m["lon"]) for m in r_info["markers"]]
      folium.PolyLine(poly_pts, color="#ff4b4b", weight=5, opacity=0.85, tooltip="Güzergah Koridoru").add_to(route_m)

      for m_item in r_info["markers"]:
        c_color = "red" if m_item["risk"] else "green"
        c_icon = "warning-sign" if m_item["risk"] else "ok-sign"
        popup_html = f"<b>{m_item['name']}</b><br>{m_item['desc']}"
        folium.Marker(
            [m_item["lat"], m_item["lon"]],
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=m_item["name"],
            icon=folium.Icon(color=c_color, icon=c_icon, prefix="glyphicon"),
        ).add_to(route_m)

      st_folium(route_m, width="100%", height=450, key="global_route_map_view_v3")


# --- OPEN-METEO VERİ MOTORU ---
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CURRENT_VARIABLES = [
    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
    "precipitation", "snowfall", "weather_code", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "cape",
]
HOURLY_VARIABLES = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "precipitation", "snowfall", "snow_depth", "precipitation_probability",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "surface_pressure", "cloud_cover", "cape",
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


# --- GLOBAL ENTER DESTEKLİ ARAMA FORMU ---
with st.form(key="global_search_form", clear_on_submit=False):
  col1, col2 = st.columns([3, 1])
  with col1:
    search_query = st.text_input(
        "🔍 Dünyanın Herhangi Bir Yerini Ara (Ülke, Şehir, Eyalet, Mahalle)",
        placeholder="Örn: Tokyo, New York, Nairobi veya Sarıyer",
        key="main_search_input"
    )
  with col2:
    st.write("")
    st.write("")
    search_button = st.form_submit_button("Global Konuma Git 🚀")

if search_button and search_query:
  try:
    location = geolocator.geocode(search_query, timeout=5)
    if location:
      st.session_state.lat = location.latitude
      st.session_state.lon = location.longitude
      st.session_state.zoom = 12
      st.session_state.son_arama = search_query
      st.session_state.location_label = location.address
      st.session_state.success_msg = f"Bulunan Global Konum: {location.address}"
      st.rerun()
    else:
      st.error("Aradığınız konum küresel harita veritabanında bulunamadı. Lütfen farklı bir ifade deneyin.")
  except Exception as e:
    st.error(f"Global arama hatası: {e}")

if "success_msg" in st.session_state:
  st.success(st.session_state.pop("success_msg"))

# --- KUSURSUZ VE HATASIZ OPENSTREETMAP STANDART HARİTASI ---
m = folium.Map(
    location=[st.session_state.lat, st.session_state.lon],
    zoom_start=st.session_state.zoom,
    tiles="OpenStreetMap",
)

Fullscreen(position="topright").add_to(m)
LocateControl(position="topleft", strings={"title": "Konumuma Git"}).add_to(m)

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
        "polyline": False, "marker": True, "circle": False,
        "rectangle": True, "polygon": True, "circlemarker": False,
    },
).add_to(m)

folium.Marker(
    [st.session_state.lat, st.session_state.lon],
    popup=st.session_state.get('location_label', 'Seçilen Konum'),
    tooltip="Aktif Global Konum",
    icon=folium.Icon(color="red", icon="cloud"),
).add_to(m)

folium.LayerControl().add_to(m)

st.subheader("📍 İnteraktif Global Harita ve Konum Analizi")
map_data = st_folium(m, width="100%", height=500, key="meteoscope_global_main_map_v29")

if map_data:
  clicked = map_data.get("last_clicked")
  if clicked:
    clicked_lat = float(clicked["lat"])
    clicked_lon = float(clicked["lng"])
    if (round(clicked_lat, 5) != round(st.session_state.lat, 5)) or (round(clicked_lon, 5) != round(st.session_state.lon, 5)):
      st.session_state.lat = clicked_lat
      st.session_state.lon = clicked_lon
      st.session_state.zoom = map_data.get("zoom", st.session_state.zoom)
      st.session_state.location_label = f"Koordinat: {clicked_lat:.4f}, {clicked_lon:.4f}"
      st.rerun()

def degrees_to_cardinal(deg):
  if deg is None: return "N/A"
  dirs = ["K", "KD", "D", "GD", "G", "GB", "B", "KB"]
  val = int((deg / 45) + 0.5)
  return dirs[(val % 8)]

if st.session_state.lat is not None and st.session_state.lon is not None:
  st.markdown("---")
  st.success(f"Aktif Analiz Konumu: {st.session_state.get('location_label', 'Seçilen konum')} | Enlem: {st.session_state.lat:.4f}, Boylam: {st.session_state.lon:.4f}")

  weather_error = None
  used_fallback_data = False
  with st.spinner("Küresel hava modelleri taranıyor..."):
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
      current_data = {key: _safe_number(current_payload.get(key)) for key in CURRENT_VARIABLES if key in current_payload}
      local_time_text = current_payload.get("time") or (f_data.get("hourly") or {}).get("time", [None])[0]
      today = pd.to_datetime(local_time_text).date() if local_time_text else date.today()
      current_wind = _safe_number(current_data.get("wind_speed_10m"))
      current_gust = _safe_number(current_data.get("wind_gusts_10m"))
      current_snow = _safe_number(current_data.get("snowfall"))
      current_cape = _safe_number(current_data.get("cape"))

      start_date = today - timedelta(days=7)
      try:
        df_archive = fetch_archive_data(request_lat, request_lon, start_date.isoformat(), today.isoformat())
      except Exception:
        df_archive = pd.DataFrame()

      csv_data = df.to_csv(index=False).encode("utf-8")
    except Exception as exc:
      weather_error = str(exc)
      df = pd.DataFrame()
      df_archive = pd.DataFrame()
      current_data = {}
      current_wind = current_gust = current_snow = current_cape = 0.0
      csv_data = b""

  if weather_error is None:
    df_3days = df.head(72)

    st.markdown("### ⏰ Seçilen Konum İçin Saat Bazlı Erken Uyarı & Kritik Saatler")
    saatlik_tehlikeler = konum_saatlik_tehlike_taramasi(df)
    
    if not saatlik_tehlikeler:
      st.success("🟢 **Saat Bazlı Durum Stabil:** Seçilen konum için önümüzdeki periyotta model verilerinde kritik bir saatlik eşik aşımı tespit edilmemiştir.")
    else:
      st.warning(f"⚠️ **DİKKAT! Model verilerine göre bu konumda kritik eşikleri aşan {len(saatlik_tehlikeler)} adet tehlikeli saat dilimi tespit edildi:**")
      saatlik_tablo_verisi = []
      for st_item in saatlik_tehlikeler:
        t_str = pd.to_datetime(st_item["zaman"]).strftime("%d %B %Y, Saat: %H:00")
        saatlik_tablo_verisi.append({
            "Tarih ve Saat": t_str,
            "Tehlike Türü": st_item["tur"],
            "Model Değeri": st_item["deger"],
            "Seviye": "Kritik 🔴" if st_item["seviye"] == "kirmizi" else "Dikkat 🟡"
        })
      st.dataframe(pd.DataFrame(saatlik_tablo_verisi), use_container_width=True, hide_index=True)

    st.markdown("### 🚨 Günün Meteorolojik Risk Özeti ve Canlı Uyarılar")
    uyarilar = konum_uyari_analizi(current_data, df_3days)
    for seviye, mesaj in uyarilar:
      if seviye == "kirmizi": st.error(mesaj)
      elif seviye == "sari": st.warning(mesaj)
      else: st.success(mesaj)

    st.markdown("### 📅 Önümüzdeki 3 Günün Günlük Kırılımlı Hava Tahmini ve Şiddet Analizi")
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
        max_hourly_p = day_df["precipitation"].max() if "precipitation" in day_df.columns else 0
        d_snow = day_df["snowfall"].sum() if "snowfall" in day_df.columns else 0
        d_wind = day_df["wind_speed_10m"].max() if "wind_speed_10m" in day_df.columns else 0

        if max_hourly_p >= 7.0:
          yagis_durum_str = f"🔴 Şiddetli Sağanak (Toplam: {d_precip:.1f} mm | Max Saatlik: {max_hourly_p:.1f} mm/s)"
        elif max_hourly_p >= 5.0:
          yagis_durum_str = f"🟠 Kuvvetli Yağış (Toplam: {d_precip:.1f} mm)"
        elif d_precip > 1.0:
          yagis_durum_str = f"🟡 Normal / Hafif Yağış (Toplam: {d_precip:.1f} mm)"
        else:
          yagis_durum_str = f"🟢 Yağış İhtimali Düşük (Toplam: {d_precip:.1f} mm)"

        gun_ismi = pd.to_datetime(d).strftime('%A')
        gun_tr = {"Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba", "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar"}.get(gun_ismi, gun_ismi)

        with cols_day[idx]:
          with st.container(border=True):
            st.markdown(f"**📌 {d} ({gun_tr})**")
            st.metric(label="Min / Max Sıcaklık", value=f"{d_min_t:.1f}°C / {d_max_t:.1f}°C")
            st.metric(label="Şiddet & mm Yağış Beklentisi", value=yagis_durum_str)
            st.metric(label="Max Rüzgar Hamlesi", value=f"{d_wind:.1f} km/s")

    if current_data:
      st.markdown("### ⚡ Anlık Meteorolojik Durum")
      wind_deg = current_data.get("wind_direction_10m", 0)
      wind_cardinal = degrees_to_cardinal(wind_deg)
      curr_col1, curr_col2, curr_col3, curr_col4, curr_col5 = st.columns(5)
      with curr_col1: st.metric(label="Sıcaklık", value=f"{current_data.get('temperature_2m', 0.0):.1f} °C")
      with curr_col2: st.metric(label="Hissedilen", value=f"{current_data.get('apparent_temperature', 0.0):.1f} °C")
      with curr_col3: st.metric(label="Bağıl Nem", value=f"{current_data.get('relative_humidity_2m', 0.0):.1f} %")
      with curr_col4: st.metric(label="Rüzgar Hızı / Hamle", value=f"{current_wind:.1f} / {current_gust:.1f} km/s")
      with curr_col5: st.metric(label="Rüzgar Yönü / CAPE", value=f"{wind_cardinal} ({current_cape:.0f} J/kg)")

    st.markdown("### 📊 Detaylı Model Tahminleri ve Uzman Raporu")
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "🤝 3'lü Model Konsensüsü", "🕰️ Geçmiş Arşiv", "📄 Uzman Bülteni",
        "🌡️ Sıcaklık & Nem", "💨 Hassas Rüzgar & Hamle", "☁️ Basınç & Bulut",
        "❄️ Yağış, Kar & Olasılık", "📋 7 Günlük Hava Özeti & İndirme",
    ])

    with tab1:
      st.subheader("🌐 Küresel Modeller Arası Karşılaştırma")
      model_temp_cols = [c for c in df.columns if c.startswith("temperature_2m_")]
      st.line_chart(df.set_index("time")[model_temp_cols] if model_temp_cols else df.set_index("time")[["temperature_2m"]])

    with tab2:
      st.subheader("Geçmiş 7 Günün Arşiv Verileri")
      if not df_archive.empty:
        st.line_chart(df_archive.set_index("time")[["temperature_2m", "precipitation"]])
        st.dataframe(df_archive, use_container_width=True)
      else:
        st.info("Arşiv verisi bulunamadı.")

    with tab3:
      st.subheader("📄 Meteoroloji Uzmanı Analiz Bülteni (Salt Okunur)")
      t_all = [c for c in df_3days.columns if "temperature_2m" in c]
      rep_min = df_3days[t_all].min().min() if t_all else 0
      rep_max = df_3days[t_all].max().max() if t_all else 0
      rep_precip = df_3days["precipitation"].sum() if "precipitation" in df_3days.columns else 0
      rep_snow = df_3days["snowfall"].sum() if "snowfall" in df_3days.columns else 0
      rep_wind = df_3days["wind_speed_10m"].max() if "wind_speed_10m" in df_3days.columns else 0
      
      giyim_tavsiyesi = giyim_onerisi_olustur(
          current_data.get('temperature_2m', 15),
          rep_precip,
          rep_snow,
          rep_wind
      )
      
      with st.container(border=True):
        st.markdown(f"**Konum Koordinatları:** Enlem {st.session_state.lat:.4f}, Boylam {st.session_state.lon:.4f}")
        st.markdown(f"**Anlık Sıcaklık:** {current_data.get('temperature_2m', 0.0):.1f} °C")
        st.markdown(f"**Önümüzdeki 3 Gün Toplam Yağış Beklentisi:** {rep_precip:.1f} mm")
        st.markdown(f"**Maksimum Rüzgar Hızı:** {rep_wind:.1f} km/s")
        st.markdown("---")
        st.markdown(giyim_tavsiyesi)
        st.markdown("---")
        st.info("💡 **Uzman Notu:** Bu bülten küresel sayısal hava tahmin modellerinin (ECMWF, GFS, ICON) sentezlenmesiyle otomatik olarak oluşturulmuştur ve salt okunurdur.")
      
      report_text = f"METEOROLOJİK UZMAN RAPORU\nKonum: Enlem {st.session_state.lat:.4f}, Boylam {st.session_state.lon:.4f}\nSıcaklık: {current_data.get('temperature_2m', 0.0):.1f}°C\nToplam Yağış (3 Gün): {rep_precip:.1f} mm\nMax Rüzgar: {rep_wind:.1f} km/s\n\n{giyim_tavsiyesi}"
      st.download_button("📥 Uzman Bültenini İndir", report_text.encode("utf-8"), file_name="meteo_expert_report.txt", mime="text/plain")

    with tab4: st.line_chart(df.set_index("time")[[c for c in df.columns if "temperature_2m" in c or "relative_humidity_2m" in c]])
    with tab5: st.line_chart(df.set_index("time")[[c for c in df.columns if "speed" in c or "gusts" in c]])
    with tab6: st.line_chart(df.set_index("time")[[c for c in df.columns if "pressure" in c or "cloud_cover" in c]])
    with tab7: st.line_chart(df.set_index("time")[[c for c in df.columns if "precipitation" in c or "snow" in c]])

    with tab8:
      st.subheader(f"📅 7 Günlük Özet Tablo ({st.session_state.get('location_label', 'Konum')})")
      if not df.empty:
        df["date_only"] = df["time"].dt.date
        seven_days_summary = []
        for d_val, group_df in list(df.groupby("date_only"))[:7]:
          t_col = [c for c in group_df.columns if "temperature_2m" in c]
          min_t = group_df[t_col].min().min() if t_col else 0
          max_t = group_df[t_col].max().max() if t_col else 0
          tot_p = group_df["precipitation"].sum() if "precipitation" in group_df.columns else 0
          max_hp = group_df["precipitation"].max() if "precipitation" in group_df.columns else 0
          tot_s = group_df["snowfall"].sum() if "snowfall" in group_df.columns else 0
          max_w = group_df["wind_speed_10m"].max() if "wind_speed_10m" in group_df.columns else 0
          
          if max_hp >= 10.0: şiddet_etiketi = "🔴 Şiddetli Sağanak"
          elif max_hp >= 5.0: şiddet_etiketi = "🟠 Kuvvetli Yağış"
          elif tot_p > 1.0: şiddet_etiketi = "🟡 Hafif / Normal"
          else: şiddet_etiketi = "🟢 Stabil"

          seven_days_summary.append({
              "Tarih": str(d_val),
              "Şiddet Sınıfı": şiddet_etiketi,
              "Min Sıcaklık (°C)": round(min_t, 1),
              "Max Sıcaklık (°C)": round(max_t, 1),
              "Toplam Yağış (mm)": round(tot_p, 1),
              "Toplam Kar (cm)": round(tot_s, 1),
              "Max Rüzgar (km/s)": round(max_w, 1)
          })
        st.dataframe(pd.DataFrame(seven_days_summary), use_container_width=True, hide_index=True)
      
      st.download_button("📥 Tüm Verileri İndir (CSV)", csv_data, file_name="meteo_forecast.csv", mime="text/csv")

  else:
    st.error(f"Meteoroloji verileri alınamadı: {weather_error}")
