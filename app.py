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

# --- ŞİFRE KORUMASI ---
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
  st.stop()


# --- GELİŞTİRİLMİŞ AKILLI GİYİM VE KAR ÖNERİSİ FONKSİYONU ---
def giyim_onerisi_olustur(ortalama_sicaklik, toplam_yagis, toplam_kar, ortalama_yagis_ihtimali, max_ruzgar):
  """Sıcaklık, yağış, kar, yağış olasılığı ve rüzgara göre

  akıllı ve profesyonel giyim önerisi üretir.
  """
  if max_ruzgar > 45:
    return (
        "🌪️ **Giyim Önerisi:** Bölgede çok kuvvetli rüzgar ve fırtına hamleleri"
        " bekleniyor. Rüzgarı kesen montlar giymeniz ve dikkatli"
        " olmanız tavsiye edilir."
    )
  elif toplam_kar > 0.5 or ortalama_sicaklik <= 0:
    return (
        "❄️ **Giyim Önerisi:** Kar yağışı veya sıfırın altında sert soğuklar"
        " hakim. Kalın kışlık mont, kar botu, atkı, bere ve eldivenlerinizi"
        " mutlaka yanınıza alın."
    )
  elif toplam_yagis > 1.0 or ortalama_yagis_ihtimali > 50:
    return (
        "🌧️ **Giyim Önerisi:** Yağış geçişleri veya yüksek yağış ihtimali"
        " bekleniyor. Yanınıza şemsiye alarak su geçirmez dış katmanlar tercih"
        " etmeniz tavsiye edilir."
    )
  elif ortalama_sicaklik < 12:
    return (
        "🧥 **Giyim Önerisi:** Hava oldukça soğuk görünüyor. Kalın montlar,"
        " atkı ve bere gibi kışlık kıyafetlerle kat kat giyinmeniz"
        " tavsiye edilir."
    )
  elif ortalama_sicaklik > 25:
    return (
        "👕 **Giyim Önerisi:** Hava sıcak ve güneşli seyredecek. Hafif,"
        " nefes alabilen ve açık renkli ince kıyafetler giyinebilirsiniz."
    )
  else:
    return (
        "👔 **Giyim Önerisi:** Hava ılıman/mevsim normallerinde görünüyor. Günlük"
        " kıyafetler tercih edebilir, akşam serinliği için hafif bir"
        " ceket alabilirsiniz."
    )


# --- ANA UYGULAMA ---
st.title("🌍 Global Meteoroloji & Uzman Ar-Ge Paneli")
st.markdown(
    "Dünya haritası üzerinden nokta seçin, **bölge çizin (Polygon)**, canlı"
    " radarı inceleyin ve **otomatik uzman raporu** oluşturun."
)

geolocator = Nominatim(user_agent="meteo_app_ITU_expert")

if "lat" not in st.session_state:
  st.session_state.lat = 20.0
if "lon" not in st.session_state:
  st.session_state.lon = 0.0
if "zoom" not in st.session_state:
  st.session_state.zoom = 2

col1, col2 = st.columns([3, 1])
with col1:
  search_query = st.text_input(
      "🔍 Konum Ara (Ülke, Şehir, İlçe, Mahalle)",
      placeholder="Örn: Moskova, Erzurum veya İstanbul",
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


if st.session_state.lat and st.session_state.lon:
  st.markdown("---")
  st.success(
      f"Aktif Analiz Alanı/Koordinatı -> Enlem: {st.session_state.lat:.4f},"
      f" Boylam: {st.session_state.lon:.4f}"
  )

  with st.spinner("3'lü model verileri, kar, rüzgar ve yağış analizi yapılıyor..."):
    # API sorgusuna snowfall ve snow_depth eklendi
    forecast_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={st.session_state.lat}&longitude={st.session_state.lon}&current="
        "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,snowfall,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly="
        "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,snowfall,snow_depth,precipitation_probability,"
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

      current_data = f_data.get("current", {})
      current_wind = current_data.get("wind_speed_10m", 0.0) if current_data else 0.0
      current_gust = current_data.get("wind_gusts_10m", 0.0) if current_data else 0.0
      current_snow = current_data.get("snowfall", 0.0) if current_data else 0.0
      next_24h_precip = (
          df["precipitation"].head(24).sum()
          if "precipitation" in df.columns
          else 0.0
      )

      if current_wind > 40 or current_gust > 55:
        st.error(
            "🚨 **FIRTINA UYARISI:** Anlık rüzgar hızı veya hamlesi kritik seviyede!"
            f" (Hız: {current_wind:.1f} km/s, Hamle: {current_gust:.1f} km/s)"
        )
      elif current_snow > 0:
        st.info(f"❄️ **KAR YAĞIŞI BİLGİSİ:** Bulunulan konumda anlık kar yağışı tespit edildi ({current_snow:.1f} cm).")

      # 3 Günlük Dinamik Trend, Yağış, Kar ve Rüzgar Hesaplama
      st.markdown("### 📈 Önümüzdeki 3 Günlük Dinamik Trend Özeti")
      df_3days = df.head(72)
      max_t, min_t, total_precip, total_snow, avg_wind, max_gust_3d, avg_precip_prob = 0, 0, 0, 0, 0, 0, 0
      if not df_3days.empty:
        temp_cols = [c for c in df_3days.columns if "temperature_2m" in c]
        max_t = df_3days[temp_cols].max().max()
        min_t = df_3days[temp_cols].min().min()
        total_precip = (
            df_3days["precipitation"].sum()
            if "precipitation" in df.columns
            else 0.0
        )
        total_snow = (
            df_3days["snowfall"].sum()
            if "snowfall" in df_3days.columns
            else 0.0
        )
        avg_wind = (
            df_3days["wind_speed_10m"].mean()
            if "wind_speed_10m" in df_3days.columns
            else 0.0
        )
        max_gust_3d = (
            df_3days["wind_gusts_10m"].max()
            if "wind_gusts_10m" in df_3days.columns
            else 0.0
        )
        avg_precip_prob = (
            df_3days["precipitation_probability"].mean()
            if "precipitation_probability" in df_3days.columns
            else 0.0
        )

        with st.container(border=True):
          col_s1, col_s2, col_s3, col_s4 = st.columns(4)
          with col_s1:
            st.metric(
                label="3 Günlük Sıcaklık",
                value=f"{min_t:.1f}°C / {max_t:.1f}°C",
            )
          with col_s2:
            st.metric(
                label="Toplam Yağış / Kar",
                value=f"{total_precip:.1f} mm / {total_snow:.1f} cm",
            )
          with col_s3:
            st.metric(
                label="Ort. Yağış İhtimali",
                value=f"%{avg_precip_prob:.0f}",
            )
          with col_s4:
            st.metric(
                label="Ort. Hız / Max Hamle",
                value=f"{avg_wind:.1f} / {max_gust_3d:.1f} km/s",
            )

      if current_data:
        st.markdown("### ⚡ Anlık Meteorolojik Durum")
        wind_deg = current_data.get("wind_direction_10m", 0)
        wind_cardinal = degrees_to_cardinal(wind_deg)

        curr_col1, curr_col2, curr_col3, curr_col4, curr_col5 = st.columns(5)
        with curr_col1:
          st.metric(
              label="Sıcaklık",
              value=f"{current_data.get('temperature_2m', 0.0):.1f} °C",
          )
        with curr_col2:
          st.metric(
              label="Hissedilen",
              value=f"{current_data.get('apparent_temperature', 0.0):.1f} °C",
          )
        with curr_col3:
          st.metric(
              label="Bağıl Nem",
              value=f"{current_data.get('relative_humidity_2m', 0.0):.1f} %",
          )
        with curr_col4:
          st.metric(
              label="Rüzgar Hızı / Hamle",
              value=f"{current_wind:.1f} / {current_gust:.1f} km/s",
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
          "💨 Hassas Rüzgar & Hamle",
          "☁️ Basınç & Bulut",
          "❄️ Yağış, Kar & Olasılık",
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

        ortalama_sicaklik_degeri = (min_t + max_t) / 2 if "min_t" in locals() else 15.0
        giyim_tavsiyesi_metni = giyim_onerisi_olustur(
            ortalama_sicaklik_degeri, total_precip, total_snow, avg_precip_prob, max_gust_3d
        )

        report_text = f"""
        METEOROLOJİK UZMAN ANALİZ RAPORU
        ----------------------------------------------
        Konum Koordinatları: Enlem {st.session_state.lat:.4f}, Boylam {st.session_state.lon:.4f}
        Rapor Tarihi: {date.today().strftime('%Y-%m-%d')}
        
        1. ANLIK DURUM ÖZETİ:
        - Sıcaklık: {current_data.get('temperature_2m', 0.0):.1f} °C (Hissedilen: {current_data.get('apparent_temperature', 0.0):.1f} °C)
        - Bağıl Nem: {current_data.get('relative_humidity_2m', 0.0):.1f} %
        - Kar Yağışı (Anlık): {current_snow:.1f} cm
        - Rüzgar Hızı / Hamle: {current_wind:.1f} / {current_gust:.1f} km/s ({degrees_to_cardinal(current_data.get('wind_direction_10m', 0))})
        
        2. 3 GÜNLÜK TAHMİN TRENDİ VE HAVA ANALİZİ:
        - Sıcaklık Aralığı: {min_t:.1f}°C ile {max_t:.1f}°C arası
        - Toplam Yağış / Kar Beklentisi (72 Saat): {total_precip:.1f} mm / {total_snow:.1f} cm
        - Ortalama Yağış Olasılığı: %{avg_precip_prob:.0f}
        - Ortalama Rüzgar Hızı / Max Hamle: {avg_wind:.1f} / {max_gust_3d:.1f} km/s
        
        3. AKILLI GİYİM VE PLANLAMA TAVSİYESİ:
        {giyim_tavsiyesi_metni.replace('**', '').replace('❄️ ', '').replace('🌪️ ', '').replace('🌧️ ', '').replace('🧥 ', '').replace('👕 ', '').replace('👔 ', '')}
        
        4. MODEL KONSENSÜS DURUMU:
        - ECMWF, GFS ve ICON modelleri ortak analize alınmıştır.
        """

        st.info(giyim_tavsiyesi_metni)
        st.text_area("Rapor Önizlemesi", report_text, height=280)

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
        st.subheader("Hassas Rüzgar Hızı, Hamle (Gust) ve Yön Analizi")
        wind_cols = [
            col
            for col in df.columns
            if "speed" in col or "gusts" in col
        ]
        if wind_cols:
          st.line_chart(df.set_index("time")[wind_cols])

        wind_dir_cols = [col for col in df.columns if "direction" in col]
        if wind_dir_cols:
          st.write("**Rüzgar Yönü Değişimleri (Derece Cinsinden):**")
          st.line_chart(df.set_index("time")[wind_dir_cols])

      with tab6:
        st.subheader("Atmosferik Basınç ve Bulut Örtüsü")
        press_cols = [
            col for col in df.columns if "pressure" in col or "cloud_cover" in col
        ]
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
      st.error("Veri çekilirken bir hata oluştu.")
