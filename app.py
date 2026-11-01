from datetime import date, timedelta
from geopy.geocoders import Nominatim
import folium
from folium.plugins import Draw
from folium.raster_layers import TileLayer
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

# Sayfa Yapılandırması
st.set_page_config(
    page_title="MeteoScope-TR - Global Meteoroloji & Uzman Paneli",
    page_icon="🌍",
    layout="wide",
)

# --- ZİYARETÇİ SAYACI ---
if "toplam_ziyaret" not in st.session_state:
  st.session_state.toplam_ziyaret = 0
  st.session_state.aktif_oturumlar = 1

if "ziyaret_sayildi" not in st.session_state:
  st.session_state.ziyaret_sayildi = True
  st.session_state.toplam_ziyaret += 1


# --- AKILLI GİYİM ÖNERİSİ ---
def giyim_onerisi_olustur(ortalama_sicaklik, toplam_yagis, max_ruzgar):
  if max_ruzgar > 45:
    return "🌪️ **Giyim Önerisi:** Bölgede kuvvetli rüzgar ve fırtına hamleleri var. Rüzgarı kesen montlar tercih edin."
  elif ortalama_sicaklik <= 0:
    return "❄️ **Giyim Önerisi:** Hava sıfırın altında ve sert soğuk hakim. Kalın kışlık mont ve botlarınızı giyin."
  elif toplam_yagis > 1.0:
    return "🌧️ **Giyim Önerisi:** Yağış geçişleri var, yanınıza şemsiye alın."
  elif ortalama_sicaklik < 12:
    return "🧥 **Giyim Önerisi:** Hava oldukça soğuk, kat kat giyinin."
  elif ortalama_sicaklik > 25:
    return "👕 **Giyim Önerisi:** Hava sıcak, hafif kıyafetler tercih edebilirsiniz."
  else:
    return "👔 **Giyim Önerisi:** Hava mevsim normallerinde."


# --- GİZLİ YÖNETİCİ PANELİ ---
with st.sidebar:
  st.title("MeteoScope-TR")
  st.caption("Gelişmiş Meteoroloji Paneli")
  st.markdown("---")
  yonetici_sifre = st.text_input("Yönetici Şifresi", type="password", key="admin_pass")

  if yonetici_sifre == "meteo2026":
    st.success("✅ Yönetici Modu Aktif")
    st.metric(label="Toplam Ziyaretçi", value=f"{st.session_state.toplam_ziyaret:,}")
    st.metric(label="Son Aranan Konum", value=st.session_state.get("son_arama", "İstanbul / Merkez"))
  elif yonetici_sifre != "":
    st.error("Hatalı şifre.")


# --- ANA UYGULAMA ---
st.title("🌍 Global Meteoroloji & Uzman Ar-Ge Paneli")
st.markdown("Arama çubuğunu kullanabilir, harita üzerinden nokta seçebilir veya **Güzergah Analizi** yapabilirsiniz.")

geolocator = Nominatim(user_agent="meteo_app_ITU_clean")

if "lat" not in st.session_state:
  st.session_state.lat = 41.0082
if "lon" not in st.session_state:
  st.session_state.lon = 28.9784
if "zoom" not in st.session_state:
  st.session_state.zoom = 10


# --- İKİ KONUM ARASI GÜZERGAH HAVA DURUMU ÖZETİ ---
with st.expander("🚗 İki Konum Arası Güzergah Hava Durumu Özeti", expanded=False):
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
        url_b = f"https://api.open-meteo.com/v1/forecast?latitude={loc_b.latitude}&longitude={loc_b.longitude}¤t=temperature_2m,precipitation,wind_speed_10m"
        url_v = f"https://api.open-meteo.com/v1/forecast?latitude={loc_v.latitude}&longitude={loc_v.longitude}¤t=temperature_2m,precipitation,wind_speed_10m"
        
        res_b = requests.get(url_b, timeout=5).json().get("current", {})
        res_v = requests.get(url_v, timeout=5).json().get("current", {})

        st.markdown("---")
        st.subheader(f"🗺️ Güzergah Analizi: {baslangic_yer} ➡️ {bitis_yer}")
        col_r1, col_r2 = st.columns(2)
        with col_r1:
          st.info(f"📍 **Başlangıç ({baslangic_yer}):**\n- Sıcaklık: {res_b.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_b.get('wind_speed_10m', 0):.1f} km/s")
        with col_r2:
          st.info(f"🏁 **Varış ({bitis_yer}):**\n- Sıcaklık: {res_v.get('temperature_2m', 0):.1f}°C\n- Rüzgar: {res_v.get('wind_speed_10m', 0):.1f} km/s")
      else:
        st.error("Konumlar bulunamadı.")
    except Exception as e:
      st.error(f"Güzergah analiz hatası: {e}")

st.markdown("---")

col1, col2 = st.columns([3, 1])
with col1:
  search_query = st.text_input("🔍 Konum Ara (Şehir, İlçe)", placeholder="Örn: İstanbul, Ankara, Bursa", key="main_search")
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
      st.success(f"Bulunan Konum: {location.address}")
      st.rerun()
    else:
      st.error("Konum bulunamadı.")
  except Exception as e:
    st.error(f"Arama hatası: {e}")

# --- HARİTA ---
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

st.subheader("📍 İnteraktif Harita ve Veri Paneli")
map_data = st_folium(m, width="100%", height=450)

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

# --- VERİ ÇEKME VE GÖSTERİM ---
if st.session_state.lat and st.session_state.lon:
  st.markdown("---")
  st.success(f"Aktif Koordinat -> Enlem: {st.session_state.lat:.4f}, Boylam: {st.session_state.lon:.4f}")

  forecast_url = (
      f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}¤t="
      "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly="
      "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m&forecast_days=7"
  )

  try:
    response = requests.get(forecast_url, timeout=10)
    if response.status_code == 200:
      data = response.json()
      hourly_df = pd.DataFrame(data.get("hourly", {}))
      hourly_df["time"] = pd.to_datetime(hourly_df["time"])
      current = data.get("current", {})

      # Anlık Durum
      st.markdown("### ⚡ Anlık Meteorolojik Durum")
      c1, c2, c3, c4 = st.columns(4)
      c1.metric("Sıcaklık", f"{current.get('temperature_2m', 0):.1f} °C")
      c2.metric("Hissedilen", f"{current.get('apparent_temperature', 0):.1f} °C")
      c3.metric("Bağıl Nem", f"{current.get('relative_humidity_2m', 0):.1f} %")
      c4.metric("Rüzgar Hızı", f"{current.get('wind_speed_10m', 0):.1f} km/s")

      # Grafik Sekmeleri
      st.markdown("### 📊 Tahmin Trendleri")
      tab1, tab2, tab3 = st.tabs(["Sıcaklık (°C)", "Rüzgar (km/s)", "Yağış (mm)"])
      with tab1:
        st.line_chart(hourly_df.set_index("time")[["temperature_2m"]])
      with tab2:
        st.line_chart(hourly_df.set_index("time")[["wind_speed_10m"]])
      with tab3:
        st.line_chart(hourly_df.set_index("time")[["precipitation"]])
    else:
      st.error(f"API Hatası! Sunucu Kodu: {response.status_code}")
  except Exception as e:
    st.error(f"Bağlantı kurulamadı: {e}")
