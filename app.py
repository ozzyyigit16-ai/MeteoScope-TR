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

st.markdown("Dünya haritasında arama yapın veya haritaya tıklayın. Arama kutusu ve işaretçi çift yönlü senkronize edilir; harita seçimi arama alanına yazılır.")

geolocator = Nominatim(user_agent="meteo_app_global_expert_v6", timeout=5)

if "lat" not in st.session_state:
  st.session_state.lat = 41.0082
if "lon" not in st.session_state:
  st.session_state.lon = 28.9784
if "zoom" not in st.session_state:
  st.session_state.zoom = 10
if "location_label" not in st.session_state:
  st.session_state.location_label = "İstanbul / Merkez"
if "search_input_val" not in st.session_state:
  st.session_state.search_input_val = "İstanbul"

if "route_map_data" not in st.session_state:
  st.session_state.route_map_data = None
# Eski sürümün oturum state verisi yeni rapor şemasıyla uyumlu değilse temizle.
if st.session_state.get("route_map_data") and "route_geometry" not in st.session_state.route_map_data:
  st.session_state.route_map_data = None

# Harita seciminden gelen metinleri widget olusturulmadan once uygula.
# Bu, Streamlit'in widget olusturulduktan sonra session_state degistirme
# kisitlamasini onler ve arama alanini bir sonraki rerun'da senkronlar.
if st.session_state.pop("_pending_main_search", None) is not None:
  st.session_state["main_search_input"] = st.session_state.get("_pending_main_search_value", "")
  st.session_state.search_input_val = st.session_state.get("_pending_main_search_value", "")
  st.session_state.pop("_pending_main_search_value", None)

if st.session_state.pop("_pending_route_start", None) is not None:
  st.session_state["bas_yer"] = st.session_state.get("_pending_route_start_value", "")
  st.session_state.pop("_pending_route_start_value", None)
if st.session_state.pop("_pending_route_end", None) is not None:
  st.session_state["bit_yer"] = st.session_state.get("_pending_route_end_value", "")
  st.session_state.pop("_pending_route_end_value", None)


# --- TÜRKİYE İÇİ AKILLI SEYAHAT / GÜZERGÂH ANALİZİ ---
# Ulaşım bağlantı bilgileri kontrollü bir başlangıç ağıdır. Sefer saatleri/koltuk
# uygunluğu değildir; uygulama bunları doğrulanmış canlı sefer gibi sunmaz.
# Şehirler arası demiryolu ağı: hat/koridor ilişkileri (sefer saati veya
# güncel bilet bulunabilirliği anlamına gelmez). BFS, ara bağlantıları da bulur.
TR_RAIL_CORRIDORS = [
    ["istanbul", "kocaeli", "sakarya", "bilecik", "eskisehir", "ankara"],
    ["ankara", "eskisehir", "kutahya", "afyon", "usak", "manisa", "izmir"],
    ["ankara", "konya", "karaman"],
    ["istanbul", "eskisehir", "konya"],
    ["ankara", "kirikkale", "yozgat", "sivas"],
    ["ankara", "kayseri", "sivas"],
    ["ankara", "adana", "mersin"],
    ["izmir", "manisa", "balikesir", "bandirma"],
    ["istanbul", "bandirma"],
    ["ankara", "zonguldak"],
    ["ankara", "erzurum", "kars"],
    ["ankara", "diyarbakir"],
    ["ankara", "malatya", "elazig", "diyarbakir"],
    ["mersin", "tarsus", "adana", "osmaniye", "gaziantep"],
]
TR_RAIL_LINKS = set()
TR_RAIL_GRAPH = {}
for _corridor in TR_RAIL_CORRIDORS:
    for _i in range(len(_corridor)-1):
        _a, _b = _corridor[_i], _corridor[_i+1]
        TR_RAIL_LINKS.add(frozenset((_a, _b)))
        TR_RAIL_GRAPH.setdefault(_a, set()).add(_b)
        TR_RAIL_GRAPH.setdefault(_b, set()).add(_a)

def _tr_rail_path(start, end):
    """Return a city path in the known rail corridor graph, or None."""
    if start == end:
        return [start]
    from collections import deque
    queue = deque([[start]])
    visited = {start}
    while queue:
        path = queue.popleft()
        for nxt in TR_RAIL_GRAPH.get(path[-1], ()):
            if nxt == end:
                return path + [nxt]
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])
    return None
TR_AIRPORT_CITIES = {
    "istanbul", "ankara", "izmir", "antalya", "adana", "mersin", "gaziantep",
    "diyarbakir", "van", "trabzon", "samsun", "erzurum", "kayseri", "konya",
    "eskisehir", "malatya", "elazig", "batman", "mardin", "bodrum", "dalaman",
    "rize", "ordu", "tokat", "sivas", "denizli", "hatay", "kahramanmaras",
    "sanliurfa", "igdir", "kars", "agri", "canakkale", "balikesir", "mus",
    "sirnak", "tekirdag", "zonguldak", "sinop", "kastamonu", "corum", "amasya",
    "usak", "afyon", "isparta", "burdur", "bursa", "yalova", "kocaeli",
}
TR_FERRY_LINKS = {
    frozenset(x) for x in [
        ("istanbul", "yalova"), ("istanbul", "bandirma"), ("istanbul", "bursa"),
        ("istanbul", "tekirdag"), ("yalova", "pendik"), ("yalova", "gebze"),
        ("canakkale", "gelibolu"), ("canakkale", "eceabat"),
        ("izmir", "karsiyaka"), ("izmir", "bostanli"), ("mersin", "tashucu"),
        ("antalya", "kas"), ("bodrum", "datca"), ("bodrum", "kos"),
    ]
}

def _tr_norm(text):
    import unicodedata
    text = (text or "").lower().replace("ı", "i").replace("İ", "i")
    text = text.replace("ş", "s").replace("ğ", "g").replace("ü", "u").replace("ö", "o").replace("ç", "c")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))

def _tr_place_tokens(loc):
    # Nominatim address field may contain city, province and district. Match a
    # known city token only; do not infer a transport connection from distance.
    raw = _tr_norm(getattr(loc, "address", ""))
    aliases = {
        "istanbul": ["istanbul"], "ankara": ["ankara"], "izmir": ["izmir"],
        "eskisehir": ["eskisehir"], "konya": ["konya"], "sivas": ["sivas"],
        "karaman": ["karaman"], "kayseri": ["kayseri"], "adana": ["adana"],
        "mersin": ["mersin", "icel"], "balikesir": ["balikesir"],
        "bandirma": ["bandirma"], "manisa": ["manisa"], "antalya": ["antalya"],
        "gaziantep": ["gaziantep"], "diyarbakir": ["diyarbakir"], "van": ["van"],
        "trabzon": ["trabzon"], "samsun": ["samsun"], "erzurum": ["erzurum"],
        "bursa": ["bursa"], "yalova": ["yalova"], "kocaeli": ["kocaeli", "izmit"],
        "canakkale": ["canakkale"], "tekirdag": ["tekirdag"], "bodrum": ["bodrum"],
        "datca": ["datca"], "gelibolu": ["gelibolu"], "eceabat": ["eceabat"],
        "sakarya": ["sakarya", "adapazari"], "bilecik": ["bilecik"],
        "kutahya": ["kutahya"], "afyon": ["afyon", "afyonkarahisar"],
        "usak": ["usak"], "kirikkale": ["kirikkale"], "yozgat": ["yozgat"],
        "zonguldak": ["zonguldak"], "kars": ["kars"], "malatya": ["malatya"],
        "elazig": ["elazig"], "osmaniye": ["osmaniye"], "tarsus": ["tarsus"],
        "pendik": ["pendik"], "gebze": ["gebze"], "tashucu": ["tasucu", "tashucu"],
        "karsiyaka": ["karsiyaka"], "bostanli": ["bostanli"], "kas": ["kas"],
    }
    found=[]
    for canonical, names in aliases.items():
        if any(name in raw for name in names):
            found.append(canonical)
    return found

def _tr_mode_availability(mode, loc_b, loc_v, dist_km):
    a, b = _tr_place_tokens(loc_b), _tr_place_tokens(loc_v)
    if not a or not b:
        return None, "Konumlar Türkiye içinde çözümlendi ancak şehir/hat eşleşmesi yapılamadı. Ulaşım uygunluğu kesin doğrulanamadı."
    if mode == "Uçak":
        if a[0] in TR_AIRPORT_CITIES and b[0] in TR_AIRPORT_CITIES and dist_km >= 150:
            return True, "Her iki uç noktada da havalimanı/şehir eşleşmesi var. Bu, seçilen tarihte direkt uçuş veya bilet bulunduğunu garanti etmez."
        return False, "Uçak için iki uçta da uygun havalimanı eşleşmesi ve makul şehirler arası mesafe doğrulanamadı."
    if mode == "Tren":
        rail_path = _tr_rail_path(a[0], b[0])
        if rail_path:
            if len(rail_path) == 2:
                return True, "Bilinen demiryolu koridorunda bağlantı eşleşmesi var. Bu, seçilen tarihte sefer veya bilet bulunduğunu garanti etmez."
            transfer_names = " → ".join(rail_path[1:-1])
            return True, f"Bilinen demiryolu koridoru üzerinden bağlantı yolu bulundu (ara şehirler: {transfer_names}). Bu, aktarmalı seferin seçilen tarihte çalıştığını garanti etmez."
        return False, "Türkiye içindeki bilinen demiryolu koridoru ağında bu iki şehir arasında bağlantı bulunamadı."
    if mode == "Deniz yolu":
        if frozenset((a[0], b[0])) in TR_FERRY_LINKS or a[0] == b[0]:
            return True, "Bilinen yolcu feribotu/deniz bağlantısı eşleşmesi var; seferin seçilen tarihte çalıştığı ayrıca doğrulanmalıdır."
        return False, "Bu iki şehir arasında bilinen doğrudan yolcu deniz bağlantısı eşleşmedi."
    return True, "Karayolu bağlantısı OSRM yol ağı üzerinden ayrıca sorgulanacak."

seyahat_acik = st.toggle("🚗 Türkiye İçi Akıllı Güzergâh & Yol Üstü Risk Analizi", value=False,
    help="Türkiye içi karayolu rotası, ulaşım türü uygunluğu ve seçilen tarihe yönelik hava risklerini analiz eder.")

if seyahat_acik:
  with st.container(border=True):
    st.markdown("### 🇹🇷 Türkiye İçi Akıllı Seyahat Planlama")
    st.caption("Türkiye içi şehir/hat ağı kontrolü yapılır. Bu sürüm sefer saati, koltuk veya bilet uygunluğunu canlı doğrulamaz; haritadaki tren/uçak/deniz çizgileri şematiktir.")

    route_map_target = st.radio("Güzergâh haritasına tıklayınca hangi konum güncellensin?",
        ["Başlangıç", "Varış"], horizontal=True, key="route_map_target")
    col_g1, col_g2 = st.columns(2)
    with col_g1:
      baslangic_yer = st.text_input("Başlangıç (Türkiye)", placeholder="Örn: İstanbul, Kadıköy", key="bas_yer")
    with col_g2:
      bitis_yer = st.text_input("Varış (Türkiye)", placeholder="Örn: Kocaeli, İzmit", key="bit_yer")

    col_t1, col_t2 = st.columns(2)
    with col_t1:
      ulasim_turu = st.selectbox("Seyahat türü", ["Özel araç", "Otobüs", "Uçak", "Deniz yolu", "Tren"], key="route_transport_mode")
    with col_t2:
      today_route = date.today()
      route_max_date = today_route + timedelta(days=16)
      planlanan_tarih = st.date_input("Planlanan seyahat tarihi", value=today_route,
          min_value=today_route, max_value=route_max_date, key="route_travel_date",
          help="Hava tahmini için Open-Meteo'nun desteklediği yakın dönem aralığıyla sınırlıdır.")

    rota_sorgula = st.button("Türkiye Rotasını ve Ulaşım Uygunluğunu Analiz Et 🇹🇷", type="primary")

    if rota_sorgula:
      if not baslangic_yer.strip() or not bitis_yer.strip():
        st.warning("Lütfen başlangıç ve varış konumlarını gir.")
      elif _tr_norm(baslangic_yer) == _tr_norm(bitis_yer):
        st.warning("Başlangıç ve varış aynı görünüyor. Farklı konumlar gir.")
      else:
        with st.spinner("Türkiye içindeki konumlar, ulaşım bağlantıları ve rota ağı kontrol ediliyor..."):
          try:
            loc_b = geolocator.geocode(baslangic_yer, country_codes="tr", timeout=8)
            loc_v = geolocator.geocode(bitis_yer, country_codes="tr", timeout=8)
            if not loc_b or not loc_v:
              st.error("Başlangıç veya varış Türkiye içinde bulunamadı. İl/ilçe adıyla tekrar deneyin.")
            else:
              # Prevent accidentally geocoding an ambiguous non-Turkey result.
              if not _tr_norm(loc_b.address).endswith("turkiye") and "turkiye" not in _tr_norm(loc_b.address):
                st.warning("Başlangıç konumunun Türkiye'de olduğu doğrulanamadı.")
              if "turkiye" not in _tr_norm(loc_v.address) and "turkey" not in _tr_norm(loc_v.address):
                st.warning("Varış konumunun Türkiye'de olduğu doğrulanamadı.")

              lat_b, lon_b = float(loc_b.latitude), float(loc_b.longitude)
              lat_v, lon_v = float(loc_v.latitude), float(loc_v.longitude)
              straight_km = geodesic((lat_b, lon_b), (lat_v, lon_v)).km
              mode_ok, mode_reason = _tr_mode_availability(ulasim_turu, loc_b, loc_v, straight_km)

              if mode_ok is False:
                alternatives=[]
                for candidate in ["Özel araç", "Otobüs", "Uçak", "Deniz yolu", "Tren"]:
                  if candidate == ulasim_turu: continue
                  candidate_ok, _ = _tr_mode_availability(candidate, loc_b, loc_v, straight_km)
                  if candidate_ok is True: alternatives.append(candidate)
                if alternatives:
                  st.error(f"Seçilen seyahat türü doğrulanamadı. {mode_reason}\n\n**Bu iki konum için uygun görünen seçenekler:** " + " ve ".join(alternatives) + ".")
                else:
                  st.error(f"Seçilen seyahat türü doğrulanamadı. {mode_reason}\n\n**Uygun ulaşım türü bu veriyle doğrulanamadı.**")
                st.session_state.route_map_data = None
              elif mode_ok is None:
                st.warning(mode_reason)
                st.session_state.route_map_data = None
              else:
                route_geometry=[]; route_distance_m=None; route_duration_s=None; route_source=""
                if ulasim_turu in ("Özel araç", "Otobüs"):
                  try:
                    osrm_url = (f"https://router.project-osrm.org/route/v1/driving/"
                      f"{lon_b:.6f},{lat_b:.6f};{lon_v:.6f},{lat_v:.6f}")
                    osrm_resp = requests.get(osrm_url, params={"overview":"full", "geometries":"geojson", "steps":"false"}, timeout=15)
                    osrm_resp.raise_for_status(); osrm_json=osrm_resp.json()
                    if osrm_json.get("code") == "Ok" and osrm_json.get("routes"):
                      rr=osrm_json["routes"][0]
                      route_geometry=[(float(pt[1]), float(pt[0])) for pt in rr["geometry"]["coordinates"]]
                      route_distance_m=float(rr.get("distance", 0)); route_duration_s=float(rr.get("duration", 0))
                      route_source="OSRM / OpenStreetMap yol ağı"
                    else:
                      raise RuntimeError("OSRM bu iki nokta arasında karayolu rotası döndürmedi.")
                  except Exception as route_err:
                    st.error(f"Gerçek karayolu rotası alınamadı: {route_err}. Düz çizgiyle rota varmış gibi gösterilmeyecek.")
                    st.session_state.route_map_data=None
                else:
                  # Non-road modes: show only a clearly labelled schematic connection.
                  route_geometry=[(lat_b,lon_b),(lat_v,lon_v)]
                  route_source="Şematik bağlantı (gerçek sefer/hat geometrisi değildir)"
                  if ulasim_turu == "Tren":
                    a_tokens, b_tokens = _tr_place_tokens(loc_b), _tr_place_tokens(loc_v)
                    rail_path = _tr_rail_path(a_tokens[0], b_tokens[0]) if a_tokens and b_tokens else None
                    if rail_path:
                      route_source += " · demiryolu şehir koridoru: " + " → ".join(rail_path)

                if route_geometry:
                  # Select 5 representative points along actual road geometry, or endpoints for schematic routes.
                  if len(route_geometry)>2:
                    indices=sorted(set([0, len(route_geometry)//4, len(route_geometry)//2, (3*len(route_geometry))//4, len(route_geometry)-1]))
                    sample_pts=[route_geometry[i] for i in indices]
                  else:
                    sample_pts=route_geometry
                  route_weather=[]
                  weather_vars="temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m,weather_code"
                  def get_date_weather(lat,lon):
                    try:
                      params={"latitude":lat,"longitude":lon,"hourly":weather_vars,
                        "start_date":planlanan_tarih.isoformat(),"end_date":planlanan_tarih.isoformat(),
                        "timezone":"auto","wind_speed_unit":"kmh","precipitation_unit":"mm"}
                      data=requests.get("https://api.open-meteo.com/v1/forecast",params=params,timeout=12).json()
                      hourly=data.get("hourly",{}); times=hourly.get("time",[])
                      if not times: return None
                      # Pick midday sample (12:00), fallback to first available hour.
                      idx=next((i for i,t in enumerate(times) if str(t).endswith("T12:00")),0)
                      return {k:(hourly.get(k,[None]*len(times))[idx] if idx<len(hourly.get(k,[])) else None) for k in weather_vars.split(",")}
                    except Exception: return None
                  with ThreadPoolExecutor(max_workers=min(5,len(sample_pts))) as ex:
                    route_weather=list(ex.map(lambda xy:get_date_weather(xy[0],xy[1]), sample_pts))

                  risk_markers=[]; report_rows=[]; route_status="green"
                  for i,(xy,wx) in enumerate(zip(sample_pts,route_weather)):
                    if not wx:
                      report_rows.append({"Rota noktası":f"Nokta {i+1}","Hava durumu":"Seçilen tarih için veri alınamadı","Risk":"Bilinmiyor"})
                      continue
                    temp=wx.get("temperature_2m"); snow=wx.get("snowfall") or 0; precip=wx.get("precipitation") or 0
                    wind=wx.get("wind_speed_10m") or 0; gust=wx.get("wind_gusts_10m") or 0
                    code=wx.get("weather_code")
                    risks=[]; severity="green"
                    if temp is not None and temp<=0 and (snow>0 or precip>0): risks.append("Don/buzlanma ihtimali"); severity="red"
                    if snow>0: risks.append("Kar yağışı"); severity="red"
                    if precip>=10: risks.append("Kuvvetli yağış"); severity="orange" if severity!="red" else severity
                    if gust>=60: risks.append("Kuvvetli rüzgâr hamlesi"); severity="red"
                    elif gust>=40: risks.append("Rüzgâr hamlesi yüksek"); severity="orange" if severity!="red" else severity
                    if risks:
                      route_status="red" if severity=="red" else ("orange" if route_status=="green" else route_status)
                      risk_markers.append({"lat":xy[0],"lon":xy[1],"name":f"Riskli rota noktası {i+1}","risk":True,"severity":severity,"desc":"; ".join(risks)})
                    report_rows.append({"Rota noktası":f"Nokta {i+1}","Koordinat":f"{xy[0]:.4f}, {xy[1]:.4f}",
                      "Sıcaklık":f"{temp} °C" if temp is not None else "N/A","Yağış":f"{precip} mm",
                      "Kar":f"{snow} cm","Rüzgâr hamlesi":f"{gust} km/sa","Risk":"; ".join(risks) if risks else "Belirgin risk eşiği aşılmadı"})

                  markers=[{"lat":lat_b,"lon":lon_b,"name":"Başlangıç: "+loc_b.address,"risk":False,"desc":"Başlangıç konumu"},
                           {"lat":lat_v,"lon":lon_v,"name":"Varış: "+loc_v.address,"risk":False,"desc":"Varış konumu"}] + risk_markers
                  st.session_state.route_map_data={"bas_ad":loc_b.address,"bit_ad":loc_v.address,
                    "lat_b":lat_b,"lon_b":lon_b,"lat_v":lat_v,"lon_v":lon_v,"markers":markers,
                    "route_geometry":route_geometry,"route_source":route_source,"report_table":report_rows,
                    "status":route_status,"date":planlanan_tarih.strftime("%d.%m.%Y"),"mode":ulasim_turu,
                    "mode_reason":mode_reason,"distance_m":route_distance_m,"duration_s":route_duration_s}
                  st.success(f"Ulaşım türü uygunluk kontrolü tamamlandı. {mode_reason}")
          except Exception as e:
            st.error(f"Türkiye içi güzergâh analiz hatası: {e}")

    if st.session_state.get("route_map_data"):
      r_info=st.session_state.route_map_data
      st.markdown("### 📋 Seyahat uygunluğu ve yol üstü risk raporu")
      st.write(f"**Tarih:** {r_info['date']} · **Tür:** {r_info['mode']} · **Kaynak:** {r_info['route_source']}")
      if r_info.get("distance_m"):
        st.metric("Karayolu mesafesi", f"{r_info['distance_m']/1000:.1f} km")
        if r_info.get("duration_s"):
          st.caption(f"OSRM tahmini sürüş süresi: {r_info['duration_s']/3600:.1f} saat (trafik ve mola dahil değildir).")
      st.caption("Hava riskleri seçilen tarihin tahmin verilerinden, örnekleme amacıyla saat 12:00 civarında alınır. Uzak tarih veya eksik tahmin varsa raporda veri eksikliği belirtilir; bu bir resmi yol/AFAD/KGM uyarısı değildir.")
      st.dataframe(pd.DataFrame(r_info["report_table"]), use_container_width=True, hide_index=True)

      st.markdown("### 🗺️ Güzergâh haritası")
      all_pts=r_info.get("route_geometry") or [(r_info["lat_b"],r_info["lon_b"]),(r_info["lat_v"],r_info["lon_v"])]
      center_lat=sum(p[0] for p in all_pts)/len(all_pts); center_lon=sum(p[1] for p in all_pts)/len(all_pts)
      route_m=folium.Map(location=[center_lat,center_lon],zoom_start=6,tiles="OpenStreetMap")
      Fullscreen().add_to(route_m); LocateControl().add_to(route_m)
      if r_info.get("mode") in ("Özel araç","Otobüs") and len(all_pts)>2:
        folium.PolyLine(all_pts,color="#2b7bff",weight=5,opacity=0.85,tooltip="OSRM gerçek yol ağı rotası").add_to(route_m)
      else:
        folium.PolyLine(all_pts,color="#888888",weight=3,opacity=0.8,dash_array="8,8",tooltip="Şematik bağlantı; gerçek hat değildir").add_to(route_m)
      for item in r_info["markers"]:
        color="red" if item.get("risk") else ("blue" if "Başlangıç" in item.get("name","") else "green")
        popup=f"<b>{item['name']}</b><br>{item.get('desc','')}"
        folium.Marker([item["lat"],item["lon"]],popup=folium.Popup(popup,max_width=350),tooltip=item["name"],
          icon=folium.Icon(color=color,icon="warning-sign" if item.get("risk") else "info-sign",prefix="glyphicon")).add_to(route_m)
      route_map_data=st_folium(route_m,width="100%",height=460,key="route_map_turkey_smart_v1",returned_objects=["last_clicked","zoom"])
      route_clicked=route_map_data.get("last_clicked") if route_map_data else None
      if route_clicked:
        rlat=round(float(route_clicked.get("lat")),6); rlon=round(float(route_clicked.get("lng")),6)
        fingerprint=(rlat,rlon)
        if fingerprint!=st.session_state.get("_route_map_last_click"):
          st.session_state["_route_map_last_click"]=fingerprint
          try:
            reverse=geolocator.reverse((rlat,rlon),timeout=3)
            place=reverse.address if reverse and reverse.address else f"{rlat:.5f}, {rlon:.5f}"
          except Exception: place=f"{rlat:.5f}, {rlon:.5f}"
          if route_map_target=="Başlangıç":
            st.session_state["_pending_route_start_value"]=place; st.session_state["_pending_route_start"]=True
          else:
            st.session_state["_pending_route_end_value"]=place; st.session_state["_pending_route_end"]=True
          st.rerun()


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


# --- 2 YÖNLÜ SENKRONİZE ARAMA FORMU ---
with st.form(key="global_search_form", clear_on_submit=False):
  col1, col2 = st.columns([3, 1])
  with col1:
    # Arama kutusu session_state ile reaktif olarak senkronize edilir
    search_query = st.text_input(
        "🔍 Dünyanın Herhangi Bir Yerini Ara (Ülke, Şehir, Mahalle) veya Haritadan Seç",
        value=st.session_state.search_input_val,
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
      st.session_state.search_input_val = location.address
      # Widget key'i sonraki rerun'da, widget olusturulmadan once guncellenecek.
      st.session_state["_pending_main_search_value"] = location.address
      st.session_state["_pending_main_search"] = True
      st.success(f"Bulunan Konum: {location.address}")
      st.rerun()
    else:
      st.error("Aradığınız konum küresel harita veritabanında bulunamadı.")
  except Exception as e:
    st.error(f"Arama hatası: {e}")

# --- KUSURSUZ İNTERAKTİF HARİTA (ÇİFT YÖNLÜ SENKRONİZASYON) ---
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
map_data = st_folium(m, width="100%", height=500, key="meteoscope_global_main_map_v32_sync")

# Haritaya Tıklama Durumu: Tıklanan yerin adresi çözülür ve arama kutusuna işlenir (1-2 saniye)
if map_data:
  clicked = map_data.get("last_clicked")
  if clicked:
    clicked_lat = float(clicked["lat"])
    clicked_lon = float(clicked["lng"])
    if (round(clicked_lat, 5) != round(st.session_state.lat, 5)) or (round(clicked_lon, 5) != round(st.session_state.lon, 5)):
      st.session_state.lat = clicked_lat
      st.session_state.lon = clicked_lon
      st.session_state.zoom = map_data.get("zoom", st.session_state.zoom)
      
      # Ters coğrafi kodlama ile tıklanan koordinatın yer adını buluyoruz
      try:
        reverse_loc = geolocator.reverse((clicked_lat, clicked_lon), timeout=2)
        if reverse_loc and reverse_loc.address:
          adres_str = reverse_loc.address
        else:
          adres_str = f"Enlem: {clicked_lat:.4f}, Boylam: {clicked_lon:.4f}"
      except Exception:
        adres_str = f"Enlem: {clicked_lat:.4f}, Boylam: {clicked_lon:.4f}"

      st.session_state.location_label = adres_str
      st.session_state.search_input_val = adres_str
      st.session_state.son_arama = adres_str
      # Widget key'i sonraki rerun'da, widget olusturulmadan once guncellenecek.
      st.session_state["_pending_main_search_value"] = adres_str
      st.session_state["_pending_main_search"] = True
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
