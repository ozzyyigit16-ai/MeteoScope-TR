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
    page_title="Global Meteoroloji & Uzman Ar-Ge Paneli",
    page_icon="🌍",
    layout="wide",
)

# --- YENİ ÖZELLİK: Güvenlik / Şifre Koruması ---
# İstediğin şifreyi buradan değiştirebilirsin
GIZLI_SIFRE = "meteo2026"

if "authenticated" not in st.session_state:
  st.session_state.authenticated = False

if not st.session_state.authenticated:
  st.title("🔒 Yetkilendirme Gerekli")
  st.markdown(
      "Bu meteoroloji paneli kişisel kullanım için şifrelenmiştir. Lütfen"
      " erişim şifresini girin."
  )

  girilen_sifre = st.text_input("Şifre", type="password")
  if st.button("Giriş Yap"):
    if girilen_sifre == GIZLI_SIFRE:
      st.session_state.authenticated = True
      st.rerun()
    else:
      st.error("Hatalı şifre! Lütfen tekrar deneyin.")
  st.stop()  # Şifre doğru girilene kadar kodun geri kalanını çalıştırma


# --- KODUN DEVAMI (Şifre Doğrulandıktan Sonra Çalışan Paneller) ---
st.title("🌍 Global Meteoroloji & Uzman Ar-Ge Paneli")
st.markdown(
    "Dünya haritası üzerinden nokta seçin, **bölge çizin (Polygon)**, canlı"
    " radarı inceleyin ve **otomatik uzman raporu** oluşturun."
)

# Geocoder tanımlaması
geolocator = Nominatim(user_agent="meteo_app_ITU_expert")

# Session State ile koordinatları hafızada tutma
if "lat" not in st.session_state:
  st.session_state.lat = 20.0
if "lon" not in st.session_state:
  st.session_state.lon = 0.0
if "zoom" not in st.session_state:
  st.session_state.zoom = 2

# Üst Kısım: Arama Çubuğu
col1, col2 = st.columns([3, 1])
with col1:
  search_query = st.text_input(
      "🔍 Konum Ara (Ülke, Şehir, İlçe, Mahalle)",
      placeholder="Örn: Sarıyer, İstanbul veya Paris",
  )

with col2:
  st.write("")
  st.write("")
  search_button = st.button("Konuma Git")

if search_button and search_query:
  try:
    location = geolocator.geocode(search_query)
    if location:
      st.session_state.lat = location.latitude
      st.session_state.lon = location.longitude
      st.session_state.zoom = 12
      st.success(
          f"Bulunan Konum: {location.address} (Enlem: {location.latitude:.4f},"
          f" Boylam: {location.longitude:.4f})"
      )
    else:
      st.error("Aradığınız konum bulunamadı.")
  except Exception as e:
    st.error(f"Arama hatası: {e}")

# Harita ve Çizim (Polygon / Bölge Seçimi) Altyapısı
m = folium.Map(
    location=[st.session_state.lat, st.session_state.lon],
    zoom_start=st.session_state.zoom,
    tiles="OpenStreetMap",
)

# Canlı Yağış Radarı Katmanı
radar_layer = TileLayer(
    tiles="https://tile.rainviewer.com/v2/radar/nowcast_120/{z}/{x}/{y}/2/1_1.png",
    attr="RainViewer",
    name="Canlı Yağış Radarı",
    overlay=True,
    opacity=0.6,
)
radar_layer.add_to(m)

# Harita Üzerine Çizim Aracı
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

st.subheader("📍 İnteraktif Harita, Canlı Radar ve Bölge Çizim Aracı")
st.info(
    "Harita üzerindeki araçları kullanarak alan çizebilir veya dilediğiniz"
    " noktaya tıklayabilirsiniz."
)

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


# Verileri Çekme ve Analiz
if st.session_state.lat and st.session_state.lon:
  st.markdown("---")
  st.success(
      f"Aktif Analiz Alanı/Koordinatı -> Enlem: {st.session_state.lat:.4f},"
      f" Boylam: {st.session_state.lon:.4f}"
  )

  with st.spinner("3'lü model verileri, arşiv ve rapor hazırlanıyor..."):
    forecast_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}&current="
        "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m&hourly="
        "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,"
        "wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure,cloud_cover"
        "&models=ecmwf_ifs,gfs_seamless,icon_seamless&forecast_days=7"
    )

    today = date.today()
    start_date = today - timedelta(days=7)
    archive_url = (
        f"https://archive-api.open-meteo.com/v1/archive?latitude={st.session_state.lat}&longitude={st.session_state.lon}"
        f"&start_date={start_date}&end_date={today}&hourly=temperature_2m,precipitation"
    )

    f_response = requests.get(forecast_url)
    a_response = requests.get(archive_url)

    if f_response.status_code == 200:
      f_data = f_response.json()
      hourly_data = f_data.get("hourly", {})
      df = pd.DataFrame(hourly_data)
      df["time"] = pd.to_datetime(df["time"])
      csv_data = df.to_csv(index=False).encode("utf-8")

      df_archive = pd.DataFrame()
      if a_response.status_code == 200:
        a_data = a_response.json()
        df_archive = pd.DataFrame(a_data.get("hourly", {}))
        if not df_archive.empty:
          df_archive["time"] = pd.to_datetime(df_archive["time"])

      # Fırtına Uyarısı
      current_data = f_data.get("current", {})
      current_wind = current_data.get("wind_speed_10m", 0) if current_data else 0
      next_24h_precip = (
          df["precipitation"].head(24).sum()
          if "precipitation" in df.columns
          else 0
      )

      if current_wind > 40:
        st.error(
            "🚨 **FIRTINA UYARISI:** Anlık rüzgar hızı kritik seviyede"
            f" ({current_wind} km/s)!"
        )
      elif next_24h_precip > 15:
        st.warning(
            "⚠️ **KUVVETLİ YAĞIŞ UYARISI:** Önümüzdeki 24 saat içinde yoğun yağış"
            f" bekleniyor ({next_24h_precip:.1f} mm)."
        )

      # 3 Günlük Trend Özeti
      st.markdown("### 📈 Önümüzdeki 3 Günlük Dinamik Trend Özeti")
      df_3days = df.head(72)
      max_t, min_t, total_precip, avg_wind = 0, 0, 0, 0
      if not df_3days.empty:
        temp_cols = [c for c in df_3days.columns if "temperature_2m" in c]
        max_t = df_3days[temp_cols].max().max()
        min_t = df_3days[temp_cols].min().min()
        total_precip = (
            df_3days["precipitation"].sum()
            if "precipitation" in df.columns
            else 0.0
        )
        avg_wind = (
            df_3days["wind_speed_10m"].mean()
            if "wind_speed_10m" in df.columns
            else 0.0
        )

        with st.container(border=True):
          col_s1, col_s2, col_s3 = st.columns(3)
          with col_s1:
            st.metric(
                label="3 Günlük Sıcaklık Aralığı",
                value=f"{min_t:.1f}°C / {max_t:.1f}°C",
            )
          with col_s2:
            st.metric(
                label="3 Günlük Toplam Yağış",
                value=f"{total_precip:.1f} mm",
            )
          with col_s3:
            st.metric(
                label="Ortalama Rüzgar Hızı",
                value=f"{avg_wind:.1f} km/s",
            )

      # Anlık Durum Kartı
      if current_data:
        st.markdown("### ⚡ Anlık Meteorolojik Durum")
        wind_deg = current_data.get("wind_direction_10m", 0)
        wind_cardinal = degrees_to_cardinal(wind_deg)

        curr_col1, curr_col2, curr_col3, curr_col4, curr_col5 = st.columns(5)
        with curr_col1:
          st.metric(
              label="Sıcaklık",
              value=f"{current_data.get('temperature_2m', 'N/A')} °C",
          )
        with curr_col2:
          st.metric(
              label="Hissedilen",
              value=f"{current_data.get('apparent_temperature', 'N/A')} °C",
          )
        with curr_col3:
          st.metric(
              label="Bağıl Nem",
              value=f"{current_data.get('relative_humidity_2m', 'N/A')} %",
          )
        with curr_col4:
          st.metric(
              label="Rüzgar Hızı",
              value=f"{current_data.get('wind_speed_10m', 'N/A')} km/s",
          )
        with curr_col5:
          st.metric(
              label="Rüzgar Yönü",
              value=f"{wind_cardinal} ({wind_deg}°)",
          )

      st.markdown("### 📊 Detaylı Model Tahminleri ve Uzman Raporu")

      tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
          "🤝 3'lü Model Konsensüsü",
          "🕰️ Geçmiş Arşiv",
          "📄 Otomatik Uzman Raporu",
          "🌡️ Sıcaklık & Nem",
          "💨 Rüzgar & Yön/Hamle",
          "☁️ Basınç & Bulut",
          "🌧️ Yağış",
          "📋 Ham Veri & İndirme",
      ])

      with tab1:
        st.subheader("ECMWF, GFS ve ICON Sıcaklık Karşılaştırması")
        model_temp_cols = [
            c for c in df.columns if "temperature_2m" in c and "_" in c
        ]
        if model_temp_cols:
          st.line_chart(df.set_index("time")[model_temp_cols])
        else:
          st.info("Çoklu model sütunları yüklenemedi.")

      with tab2:
        st.subheader("Geçmiş 7 Günün Arşiv Verileri")
        if not df_archive.empty:
          st.line_chart(
              df_archive.set_index("time")[["temperature_2m", "precipitation"]]
          )
          st.dataframe(df_archive, use_container_width=True)
        else:
          st.info("Arşiv verisi bulunamadı.")

      with tab3:
        st.subheader("📄 Otomatik Meteorolojik Durum Raporu")
        st.markdown(
            "Seçilen bölgeye ait güncel model tahminleri ve trendler baz"
            " alınarak oluşturulan profesyonel özet rapor:"
        )

        report_text = f"""
        METEOROLOJİK UZMAN ANALİZ RAPORU
        ----------------------------------------------
        Konum Koordinatları: Enlem {st.session_state.lat:.4f}, Boylam {st.session_state.lon:.4f}
        Rapor Tarihi: {date.today().strftime('%Y-%m-%d')}
        
        1. ANLIK DURUM ÖZETİ:
        - Sıcaklık: {current_data.get('temperature_2m', 'N/A')} °C (Hissedilen: {current_data.get('apparent_temperature', 'N/A')} °C)
        - Bağıl Nem: {current_data.get('relative_humidity_2m', 'N/A')} %
        - Rüzgar: {current_data.get('wind_speed_10m', 'N/A')} km/s ({degrees_to_cardinal(current_data.get('wind_direction_10m', 0))})
        
        2. 3 GÜNLÜK TAHMİN TRENDİ:
        - Sıcaklık Aralığı: {min_t:.1f}°C ile {max_t:.1f}°C arası
        - Toplam Yağış Beklentisi (72 Saat): {total_precip:.1f} mm
        - Ortalama Rüzgar Hızı: {avg_wind:.1f} km/s
        
        3. MODEL KONSENSÜS DURUMU:
        - ECMWF, GFS ve ICON modelleri ortak analize alınmıştır.
        """

        st.text_area("Rapor Önizlemesi", report_text, height=250)

        st.download_button(
            label="📥 Uzman Raporunu Metin/Rapor Olarak İndir",
            data=report_text.encode("utf-8"),
            file_name=f"meteo_report_lat_{st.session_state.lat:.2f}_lon_{st.session_state.lon:.2f}.txt",
            mime="text/plain",
        )

      with tab4:
        st.subheader("Sıcaklık ve Bağıl Nem / Çiy Noktası")
        temp_cols = [
            col
            for col in df.columns
            if "temperature_2m" in col or "relative_humidity_2m" in col
        ]
        if temp_cols:
          st.line_chart(df.set_index("time")[temp_cols])

      with tab5:
        st.subheader("Rüzgar Hızı, Yönü ve Hamle (Gust)")
        wind_cols = [
            col
            for col in df.columns
            if "speed" in col or "gusts" in col or "direction" in col
        ]
        if wind_cols:
          st.line_chart(
              df.set_index("time")[
                  [c for c in wind_cols if "direction" not in c]
              ]
          )
          st.write(
              "**Rüzgar Yönü Değişimleri (Derece Cinsinden Özet):**"
          )
          st.line_chart(
              df.set_index("time")[
                  [c for c in wind_cols if "direction" in c]
              ]
          )

      with tab6:
        st.subheader("Atmosferik Basınç ve Bulut Örtüsü")
        press_cols = [
            col for col in df.columns if "pressure" in col or "cloud_cover" in col
        ]
        if press_cols:
          st.line_chart(df.set_index("time")[press_cols])

      with tab7:
        st.subheader("Yağış Miktarı")
        rain_cols = [col for col in df.columns if "precipitation" in col]
        if rain_cols:
          st.line_chart(df.set_index("time")[rain_cols])

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
      st.error("Veri çekilirken bir hata oluştu.")