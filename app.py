import folium
import pandas as pd
import requests
import streamlit as st
from folium.plugins import Draw
from geopy.geocoders import Nominatim
from streamlit_folium import st_folium

# Sayfa Yapılandırması
st.set_page_config(
    page_title="MeteoScope-TR - Global Meteoroloji & Uzman Ar-Ge Paneli",
    layout="wide",
)

# --- ŞİFRE KORUMA MEKANİZMASI ---
SIFRE = "meteo2026"


def check_password():
  """Şifre kontrol ekranı oluşturur."""

  def password_entered():
    if st.session_state["password"] == SIFRE:
      st.session_state["password_correct"] = True
      del st.session_state["password"]  # şifreyi hafızadan sil
    else:
      st.session_state["password_correct"] = False

  if "password_correct" not in st.session_state:
    st.text_input(
        "Şifre", type="password", on_change=password_entered, key="password"
    )
    st.info("Bu meteoroloji paneli kişisel kullanım için şifrelenmiştir.")
    return False
  elif not st.session_state["password_correct"]:
    st.text_input(
        "Şifre", type="password", on_change=password_entered, key="password"
    )
    st.error("😕 Şifre yanlış. Lütfen tekrar deneyin.")
    return False
  else:
    return True


if not check_password():
  st.stop()


# --- GİYİM ÖNERİSİ FONKSİYONU ---
def giyim_onerisi_olustur(ortalama_sicaklik, yagis_durumu):
  """Önümüzdeki günlerin sıcaklık ve yağış durumuna göre

  akıllı giyim önerisi üretir.
  """
  if yagis_durumu:
    return (
        "🌧️ **Giyim Önerisi:** Önümüzdeki günlerde yağış bekleniyor. Yanınıza"
        " mutlaka şemsiye alarak su geçirmez ve rüzgar kesen montlar tercih"
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
        " kıyafetler tercih edebilir, akşam serinliği için yanınıza hafif bir"
        " ceket alabilirsiniz."
    )


# --- ANA UYGULAMA ---
st.title("Global Meteoroloji & Uzman Ar-Ge Paneli")
st.write(
    "Dünya haritası üzerinden nokta seçin, bölge çizin (Polygon), canlı radarı"
    " inceleyin ve otomatik uzman raporu oluşturun."
)

col1, col2 = st.columns([2, 1])

with col2:
  st.subheader("Konum Ara")
  sehir_input = st.text_input(
      "Ülke, Şehir, İlçe veya Mahalle", value="Istanbul, Türkiye"
  )

# Koordinat bulma (Geopy)
geolocator = Nominatim(user_agent="meteoscopetr_app")
lat, lon = 41.0082, 28.9784  # Varsayılan İstanbul
try:
  location = geolocator.geocode(sehir_input)
  if location:
    lat, lon = location.latitude, location.longitude
except Exception:
  pass

with col1:
  st.subheader("İnteraktif Harita, Canlı Radar ve Bölge Çizim Aracı")
  st.caption(
      "Harita üzerindeki araçları kullanarak alan çizebilir veya dilediğiniz"
      " noktaya tıklayabilirsiniz."
  )

  # Folium Harita Oluşturma
  m = folium.Map(location=[lat, lon], zoom_start=10)
  folium.Marker(
      [lat, lon],
      popup=sehir_input,
      icon=folium.Icon(color="red", icon="cloud"),
  ).add_to(m)

  # Çizim aracı
  draw = Draw(
      export=False,
      position="topleft",
      draw_options={
          "polyline": False,
          "polygon": True,
          "circle": True,
          "rectangle": True,
          "marker": True,
          "circlemarker": False,
      },
  )
  draw.add_to(m)

  # Haritayı Streamlit'te göster
  output = st_folium(m, width=700, height=500)

# Uzman Raporu ve Giyim Önerisi Bölümü
st.markdown("---")
st.subheader("📋 Uzman Meteorolojik Rapor ve Tavsiyeler")

# Örnek sıcaklık ve yağış değerleri (İleride API ile dinamik bağlanabilir)
ornek_sicaklik = 14.5
ornek_yagis = True

rapor_metni = f"""
Seçilen **{sehir_input}** bölgesi için yapılan analizler tamamlanmıştır. 
* **Koordinatlar:** Enlem: {lat:.4f}, Boylam: {lon:.4f}
* **Durum:** Bölgesel model simülasyonu ve katman analizi aktif.

{giyim_onerisi_olustur(ortalama_sicaklik=ornek_sicaklik, yagis_durumu=ornek_yagis)}
"""

st.markdown(rapor_metni)
