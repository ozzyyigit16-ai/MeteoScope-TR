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

# Sayfa Yapılandırması
st.set_page_config(
    page_title="MeteoScope-TR - Global Meteoroloji & Uzman Paneli",
    page_icon="🌍",
    layout="wide",
)

# --- BENZERSİZ ZİYARETÇİ / AKTİF CİHAZ SAYACI (V6) ---
# Kimlik tarayıcı localStorage alanında tutulur. Aynı origin'deki sekmeler
# localStorage'ı paylaşır; telefon ve bilgisayar ise ayrı kimlik üretir.
# Bu küçük bileşen Streamlit'in kendi component API'sini kullanır; yeni paket gerekmez.
COUNTER_DB = "meteo_counters.db"
ACTIVE_TIMEOUT_SECONDS = 45


@st.cache_resource(show_spinner=False)
def get_browser_identity_component():
    """Tarayıcı localStorage/cookie kimliğini Python'a döndüren mini bileşen."""
    import streamlit.components.v1 as components
    import tempfile

    component_dir = os.path.join(tempfile.gettempdir(), "meteoscope_browser_identity_v6")
    os.makedirs(component_dir, exist_ok=True)
    html_path = os.path.join(component_dir, "index.html")

    component_html = r'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
<script>
(function () {
  "use strict";
  let alreadyReturned = false;

  function sendToStreamlit(type, data) {
    const message = Object.assign({isStreamlitMessage: true, type: type}, data || {});
    window.parent.postMessage(message, "*");
  }

  function makeId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
      return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
    }
    return "v6-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function readCookie(name) {
    const prefix = name + "=";
    const parts = document.cookie ? document.cookie.split(";") : [];
    for (let part of parts) {
      part = part.trim();
      if (part.startsWith(prefix)) {
        try { return decodeURIComponent(part.slice(prefix.length)); }
        catch (_) { return part.slice(prefix.length); }
      }
    }
    return null;
  }

  function writeCookie(name, value) {
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = name + "=" + encodeURIComponent(value) +
      "; Max-Age=315360000; Path=/; SameSite=Lax" + secure;
  }

  function readOrCreateId() {
    let id = null;
    try { id = window.localStorage.getItem("meteoscope_tr_visitor_id_v1"); }
    catch (_) { /* Cookie fallback below. */ }

    // V5'ten kalan cookie varsa yeni kimlik üretmek yerine onu devral.
    if (!id) id = readCookie("meteo_visitor_id");
    if (!id) id = makeId();

    try { window.localStorage.setItem("meteoscope_tr_visitor_id_v1", id); }
    catch (_) { /* Cookie remains the fallback store. */ }
    writeCookie("meteo_visitor_id", id);
    return id;
  }

  async function getOrCreateId() {
    // Web Locks, aynı anda açılan sekmelerin ilk kimliği farklı üretmesini önler.
    if (navigator.locks && typeof navigator.locks.request === "function") {
      try {
        return await navigator.locks.request(
          "meteoscope-tr-visitor-id-lock",
          {mode: "exclusive"},
          () => readOrCreateId()
        );
      } catch (_) { /* Continue with localStorage/cookie fallback. */ }
    }
    return readOrCreateId();
  }

  window.addEventListener("message", async function (event) {
    if (!event.data || event.data.type !== "streamlit:render" || alreadyReturned) return;
    alreadyReturned = true;
    try {
      const visitorId = await getOrCreateId();
      sendToStreamlit("streamlit:setComponentValue", {
        value: {ready: true, visitor_id: visitorId}
      });
    } catch (error) {
      sendToStreamlit("streamlit:setComponentValue", {
        value: {ready: false, error: String(error)}
      });
    }
  });

  sendToStreamlit("streamlit:componentReady", {apiVersion: 1});
  sendToStreamlit("streamlit:setFrameHeight", {height: 0});
})();
</script>
</body>
</html>'''

    # Streamlit component'inin statik HTML dosyasını yalnızca bir kez oluştur.
    if not os.path.exists(html_path):
        with open(html_path, "w", encoding="utf-8") as html_file:
            html_file.write(component_html)

    return components.declare_component("meteoscope_browser_identity_v6", path=component_dir)


browser_identity_component = get_browser_identity_component()
identity_result = browser_identity_component(key="meteoscope_browser_identity_v6", default=None, height=0)

if not isinstance(identity_result, dict) or not identity_result.get("ready"):
    if isinstance(identity_result, dict) and identity_result.get("error"):
        st.error("Tarayıcı kimliği okunamadı. Tarayıcınızın site verisi/cookie depolamasına izin verip sayfayı yenileyin.")
    else:
        st.info("Ziyaretçi kimliği hazırlanıyor...")
    st.stop()

visitor_id = str(identity_result.get("visitor_id", "")).strip()
if not visitor_id:
    st.error("Tarayıcı kimliği boş döndü; ziyaretçi sayacı güvenli şekilde başlatılamadı.")
    st.stop()
st.session_state["meteo_visitor_id"] = visitor_id


@contextmanager
def get_counter_connection():
    """Her sayaç işlemi için bağlantıyı güvenle aç, commit/rollback yap ve kapat."""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS counter_meta (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL
            )
        """)
        # Önceki denemelerin sayaç kayıtlarını yalnızca V6'nın ilk çalışmasında
        # temizle. Her Streamlit rerun'ında veya sekme açılışında sıfırlama yapma.
        conn.execute("BEGIN IMMEDIATE")
        reset_marker = conn.execute(
            "SELECT 1 FROM counter_meta WHERE meta_key = ?",
            ("unique_visitors_reset_v6",),
        ).fetchone()
        if not reset_marker:
            conn.execute("DELETE FROM unique_visitors")
            conn.execute("DELETE FROM active_sessions")
            conn.execute(
                "INSERT INTO counter_meta (meta_key, meta_value) VALUES (?, ?)",
                ("unique_visitors_reset_v6", str(time.time())),
            )
        conn.commit()


def register_unique_visitor(current_visitor_id):
    """Aynı tarayıcı/cihaz kimliği veritabanına yalnızca bir kez eklenir."""
    now = time.time()
    with get_counter_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO unique_visitors (visitor_id, first_seen) VALUES (?, ?)",
            (current_visitor_id, now),
        )
        return cursor.rowcount == 1


def refresh_active_session(current_visitor_id):
    """Sekmeler aynı visitor_id'yi kullandığından tek aktif cihaz satırı güncellenir."""
    now = time.time()
    cutoff = now - ACTIVE_TIMEOUT_SECONDS
    with get_counter_connection() as conn:
        conn.execute("DELETE FROM active_sessions WHERE last_seen < ?", (cutoff,))
        conn.execute("""
            INSERT INTO active_sessions (visitor_id, last_seen)
            VALUES (?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET last_seen = excluded.last_seen
        """, (current_visitor_id, now))


def read_counter_values():
    cutoff = time.time() - ACTIVE_TIMEOUT_SECONDS
    with get_counter_connection() as conn:
        total_visitors = conn.execute(
            "SELECT COUNT(*) FROM unique_visitors"
        ).fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM active_sessions WHERE last_seen >= ?",
            (cutoff,),
        ).fetchone()[0]
    return total_visitors, active_count


initialize_counter_db()
register_unique_visitor(visitor_id)
refresh_active_session(visitor_id)


@st.fragment(run_every="15s")
def maintain_active_heartbeat(current_visitor_id):
    refresh_active_session(current_visitor_id)


maintain_active_heartbeat(visitor_id)


@st.fragment(run_every="15s")
def render_live_admin_metrics():
    total_visitors, active_count = read_counter_values()
    st.markdown("### 📊 Canlı ve Dinamik İstatistikler")
    st.metric(label="Toplam Benzersiz Ziyaretçi", value=f"{total_visitors:,}")
    st.metric(label="Anlık Aktif Oturum", value=active_count)
    st.metric(
        label="Son Aranan Konum",
        value=st.session_state.get("son_arama", "İstanbul / Merkez"),
    )
    st.caption("Aynı tarayıcıdaki sekmeler tek ziyaretçi sayılır. Farklı cihazlar/tarayıcılar ayrı ziyaretçi sayılır. Aktif ziyaretçiler 45 saniye sinyal göndermezse pasif kabul edilir.")

# --- AKILLI GİYİM VE KAR ÖNERİSİ ---
def giyim_onerisi_olustur(
    ortalama_sicaklik, toplam_yagis, toplam_kar, ortalama_yagis_ihtimali, max_ruzgar
):
  if max_ruzgar > 45:
    return "🌪️ **Giyim Önerisi:** Bölgede çok kuvvetli rüzgar ve fırtına hamleleri bekleniyor. Rüzgarı kesen montlar giymeniz tavsiye edilir."
  elif toplam_kar > 0.5 or ortalama_sicaklik <= 0:
    return "❄️ **Giyim Önerisi:** Kar yağışı veya sıfırın altında sert soğuklar hakim. Kalın kışlık mont, kar botu ve eldivenlerinizi unutmayın."
  elif toplam_yagis > 1.0 or ortalama_yagis_ihtimali > 50:
    return "🌧️ **Giyim Önerisi:** Yağış geçişleri bekleniyor. Yanınıza şemsiye alarak su geçirmez katmanlar tercih edin."
  elif ortalama_sicaklik < 12:
    return "🧥 **Giyim Önerisi:** Hava oldukça soğuk. Kalın montlar ve kat kat giyinmeniz tavsiye edilir."
  elif ortalama_sicaklik > 25:
    return "👕 **Giyim Önerisi:** Hava sıcak ve güneşli. Hafif ve nefes alabilen kıyafetler giyinebilirsiniz."
  else:
    return "👔 **Giyim Önerisi:** Hava mevsim normallerinde. Günlük kıyafetler tercih edebilirsiniz."


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
st.markdown("Dünya haritası üzerinden nokta seçin, **bölge çizin (Polygon)**, canlı radarı inceleyin veya **güzergah hava analizi** yapın.")

geolocator = Nominatim(user_agent="meteo_app_ITU_expert")

if "lat" not in st.session_state:
  st.session_state.lat = 41.0082
if "lon" not in st.session_state:
  st.session_state.lon = 28.9784
if "zoom" not in st.session_state:
  st.session_state.zoom = 10


# --- İKİ KONUM ARASI GÜZERGAH HAVA DURUMU ÖZETİ ---
with st.expander("🚗 İki Konum Arası Güzergah Hava Durumu Özeti", expanded=False):
  st.markdown("Seyahat edeceğiniz başlangıç ve varış noktalarını yazarak güzergah üzerindeki muhtemel riskleri önceden görün.")
  col_g1, col_g2, col_g3 = st.columns([2, 2, 1])
  with col_g1:
    baslangic_yer = st.text_input("Başlangıç Konumu", placeholder="Örn: İstanbul", key="bas_yer")
  with col_g2:
    bitis_yer = st.text_input("Varış Konumu", placeholder="Örn: Bursa", key="bit_yer")
  with col_g3:
    st.write("")
    st.write("")
    rota_sorgula = st.button("Güzergahı Analiz Et")

  if rota_sorgula and baslangic_yer and bitis_yer:
    try:
      loc_b = geolocator.geocode(baslangic_yer, timeout=5)
      loc_v = geolocator.geocode(bitis_yer, timeout=5)
      if loc_b and loc_v:
        url_b = f"https://api.open-meteo.com/v1/forecast?latitude={loc_b.latitude}&longitude={loc_b.longitude}¤t=temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m"
        url_v = f"https://api.open-meteo.com/v1/forecast?latitude={loc_v.latitude}&longitude={loc_v.longitude}¤t=temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m"
        
        res_b = requests.get(url_b, timeout=5).json().get("current", {})
        res_v = requests.get(url_v, timeout=5).json().get("current", {})

        st.markdown("---")
        st.subheader(f"🗺️ Güzergah Analiz Özeti: {baslangic_yer} ➡️ {bitis_yer}")
        
        col_r1, col_r2 = st.columns(2)
        with col_r1:
          st.info(f"📍 **Başlangıç ({baslangic_yer}):**\n- Sıcaklık: {res_b.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_b.get('wind_speed_10m', 0):.1f} km/s\n- Kar/Yağış: {res_b.get('snowfall', 0):.1f}cm / {res_b.get('precipitation', 0):.1f}mm")
        with col_r2:
          st.info(f"🏁 **Varış ({bitis_yer}):**\n- Sıcaklık: {res_v.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_v.get('wind_speed_10m', 0):.1f} km/s\n- Kar/Yağış: {res_v.get('snowfall', 0):.1f}cm / {res_v.get('precipitation', 0):.1f}mm")

        riskler = []
        if res_b.get('temperature_2m', 15) <= 0 or res_v.get('temperature_2m', 15) <= 0:
          riskler.append("❄️ **Don / Buzlanma Tehlikesi:** Güzergah üzerinde sıfırın altında sıcaklık var, yollarda buzlanma riski yüksek!")
        if res_b.get('wind_gusts_10m', 0) > 45 or res_v.get('wind_gusts_10m', 0) > 45:
          riskler.append("🌪️ **Şiddetli Rüzgar/Fırtına:** Güzergah üzerinde kuvvetli rüzgar hamleleri sürücülere zor anlar yaşatabilir.")
        if res_b.get('snowfall', 0) > 0 or res_v.get('snowfall', 0) > 0:
          riskler.append("🌨️ **Kar Yağışı:** Rota üzerinde aktif kar geçişi mevcut.")

        if riskler:
          st.error("🚨 **GÜZERGAH RİSK RAPORU:**\n" + "\n".join(riskler))
        else:
          st.success("✅ **Güzergah Durumu Mükemmel:** Başlangıç ve varış noktalarında seyahatinizi olumsuz etkileyecek kritik bir hava muhalefeti görünmüyor. Yolculuğunuz için yeşil ışık! 🟢")
      else:
        st.error("Girilen konumlardan biri veya ikisi bulunamadı.")
    except Exception as e:
      st.error(f"Güzergah analiz hatası: {e}")

st.markdown("---")

col1, col2 = st.columns([3, 1])
with col1:
  search_query = st.text_input("🔍 Konum Ara (Ülke, Şehir, İlçe, Mahalle)", placeholder="Örn: Moskova, Erzurum veya İstanbul", key="main_search")
with col2:
  st.write("")
  st.write("")
  search_button = st.button("Konuma Git")

if search_button and search_query:
  try:
    location = geolocator.geocode(search_query, timeout=5)
    if location:
      st.session_state.lat = location.latitude
      st.session_state.lon = location.longitude
      st.session_state.zoom = 12
      st.session_state.son_arama = search_query
      st.success(f"Bulunan Konum: {location.address} (Enlem: {location.latitude:.4f}, Boylam: {location.longitude:.4f})")
    else:
      st.error("Aradığınız konum bulunamadı.")
  except Exception as e:
    st.error(f"Arama hatası: {e}")

# --- HARİTA VE KATMANLAR ---
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

folium.LayerControl().add_to(m)

st.subheader("📍 İnteraktif Harita, Gelişmiş Katmanlar ve Bölge Çizim Aracı")
st.info("Harita katmanlarını sağ üst köşeden değiştirebilir, alan çizebilir veya dilediğiniz noktaya tıklayabilirsiniz.")

map_data = st_folium(m, width="100%", height=480)

if map_data:
  if map_data.get("last_active_drawing"):
    geom = map_data["last_active_drawing"].get("geometry", {})
    if geom.get("type") == "Polygon":
      coords = geom.get("coordinates", [[]])[0]
      if coords:
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        st.session_state.lat = sum(lats) / len(lats)
        st.session_state.lon = sum(lons) / len(lons)
        st.rerun()

  elif map_data.get("last_clicked"):
    st.session_state.lat = map_data["last_clicked"]["lat"]
    st.session_state.lon = map_data["last_clicked"]["lng"]
    st.session_state.zoom = m.options["zoom"]
    st.rerun()

def degrees_to_cardinal(deg):
  if deg is None:
    return "N/A"
  dirs = ["K", "KD", "D", "GD", "G", "GB", "B", "KB"]
  val = int((deg / 45) + 0.5)
  return dirs[(val % 8)]

if st.session_state.lat and st.session_state.lon:
  st.markdown("---")
  st.success(f"Aktif Analiz Alanı/Koordinatı -> Enlem: {st.session_state.lat:.4f}, Boylam: {st.session_state.lon:.4f}")

  with st.spinner("Model verileri ve akıllı kademeli analiz yükleniyor..."):
    forecast_urls_to_try = [
        f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}¤t=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,snowfall,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,snowfall,snow_depth,precipitation_probability,wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure,cloud_cover&models=ecmwf_ifs,gfs_seamless,icon_seamless&forecast_days=7",
        f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}¤t=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,snowfall,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,snowfall,snow_depth,precipitation_probability,wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure,cloud_cover&models=ecmwf_ifs,gfs_seamless&forecast_days=7",
        f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}¤t=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,snowfall,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,snowfall,snow_depth,precipitation_probability,wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure,cloud_cover&forecast_days=7"
    ]

    today = date.today()
    start_date = today - timedelta(days=7)
    archive_url = (
        f"https://archive-api.open-meteo.com/v1/archive?latitude={st.session_state.lat}&longitude={st.session_state.lon}"
        f"&start_date={start_date}&end_date={today}&hourly=temperature_2m,precipitation"
    )

    f_response = None
    for url in forecast_urls_to_try:
      for attempt in range(2):
        try:
          res = requests.get(url, timeout=12)
          if res.status_code == 200:
            f_response = res
            break
          time.sleep(0.5)
        except Exception:
          time.sleep(0.5)
      if f_response and f_response.status_code == 200:
        break

    a_response = None
    for attempt in range(2):
      try:
        res_a = requests.get(archive_url, timeout=12)
        if res_a.status_code == 200:
          a_response = res_a
          break
        time.sleep(0.5)
      except Exception:
        time.sleep(0.5)

    if f_response and f_response.status_code == 200:
      f_data = f_response.json()
      hourly_data = f_data.get("hourly", {})
      df = pd.DataFrame(hourly_data)
      df["time"] = pd.to_datetime(df["time"])
      csv_data = df.to_csv(index=False).encode("utf-8")

      df_archive = pd.DataFrame()
      if a_response and a_response.status_code == 200:
        a_data = a_response.json()
        df_archive = pd.DataFrame(a_data.get("hourly", {}))
        if not df_archive.empty:
          df_archive["time"] = pd.to_datetime(df_archive["time"])

      current_data = f_data.get("current", {})
      current_wind = current_data.get("wind_speed_10m", 0.0) if current_data else 0.0
      current_gust = current_data.get("wind_gusts_10m", 0.0) if current_data else 0.0
      current_snow = current_data.get("snowfall", 0.0) if current_data else 0.0

      df_3days = df.head(72)

      # --- GÜNÜN RİSK ÖZETİ BANNERI ---
      st.markdown("### 🚨 Günün Meteorolojik Risk Özeti ve Canlı Uyarılar")
      uyarilar = konum_uyari_analizi(current_data, df_3days)
      for seviye, mesaj in uyarilar:
        if seviye == "kirmizi":
          st.error(mesaj)
        elif seviye == "sari":
          st.warning(mesaj)
        else:
          st.success(mesaj)

      # --- 3 GÜNLÜK GÜN GÜN (TARİH TARİH) TAHMİN KUTULARI ---
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
          "📋 Ham Veri & İndirme",
      ])

      with tab1:
        st.subheader("🌐 Küresel Modeller Arası Karşılaştırma ve Konsensüs Analizi")
        st.markdown("Aşağıdaki grafiklerde **Sıcaklık**, **Rüzgar** ve **Yağış** trendleri karşılaştırılmaktadır.")

        st.write("**1. Sıcaklık Modelleri Karşılaştırması (°C):**")
        model_temp_cols = [c for c in df.columns if "temperature_2m" in c and "_" in c]
        if model_temp_cols:
          st.line_chart(df.set_index("time")[model_temp_cols])
        else:
          st.line_chart(df.set_index("time")[["temperature_2m"]])

        st.write("**2. Rüzgar Hızı Modelleri Karşılaştırması (km/s):**")
        model_wind_cols = [c for c in df.columns if "wind_speed_10m" in c and "_" in c]
        if model_wind_cols:
          st.line_chart(df.set_index("time")[model_wind_cols])
        else:
          st.line_chart(df.set_index("time")[["wind_speed_10m"]])

        st.write("**3. Yağış Modelleri Karşılaştırması (mm):**")
        model_precip_cols = [c for c in df.columns if "precipitation" in c and "_" in c]
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
        st.subheader("Ham Veri Tablosu ve Dışa Aktarım")
        st.download_button(
            label="📥 Tahmin Verilerini CSV Olarak İndir",
            data=csv_data,
            file_name=f"meteo_forecast_lat_{st.session_state.lat:.2f}_lon_{st.session_state.lon:.2f}.csv",
            mime="text/csv",
        )
        st.dataframe(df, use_container_width=True)

    else:
      st.error("Veriler API sunucusundan alınamadı. Lütfen internet bağlantınızı kontrol edin.")
