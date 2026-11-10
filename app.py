from datetime import date, timedelta
from contextlib import contextmanager
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from types import SimpleNamespace
import folium
from folium.plugins import Fullscreen, LocateControl, HeatMap
from folium.raster_layers import TileLayer
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium
import time
import threading
import os
import sqlite3
import json
import re
import math
import html as html_lib
from io import StringIO
import tempfile
import streamlit.components.v1 as components
from concurrent.futures import ThreadPoolExecutor, as_completed
from geopy.distance import geodesic


class TemporaryServiceError(RuntimeError):
  """Geçici servis/ağ sorunları için kullanıcıya güvenli mesaj taşıyan hata."""
  def __init__(self, service, reason, status_code=None):
    self.service = service
    self.status_code = status_code
    self.reason = str(reason)
    super().__init__(self.reason)


def _request_get(url, *args, **kwargs):
  """Uygulamadaki HTTP GET çağrılarını sınırlı ve kontrollü biçimde tekrarlar.

  429 yanıtında Retry-After başlığına uyar; uzun beklemelerde uygulamayı
  kilitlememek için tek denemede en fazla 4 saniye bekler.
  """
  service = str(url).split('/')[2] if '://' in str(url) else 'uzak servis'
  last_error = None
  for attempt in range(3):
    try:
      response = requests.get(url, *args, **kwargs)
      if response.status_code == 429:
        retry_after = response.headers.get('Retry-After', '')
        try:
          wait_seconds = min(4.0, max(0.5, float(retry_after))) if retry_after else (1.0 + attempt)
        except (TypeError, ValueError):
          wait_seconds = 1.0 + attempt
        last_error = TemporaryServiceError(service, 'HTTP 429 - istek sınırı', 429)
        if attempt < 2:
          time.sleep(wait_seconds)
          continue
        raise last_error
      if response.status_code in (500, 502, 503, 504):
        last_error = TemporaryServiceError(service, f'HTTP {response.status_code}', response.status_code)
        if attempt < 2:
          time.sleep(0.5 * (attempt + 1))
          continue
        raise last_error
      return response
    except (requests.Timeout, requests.ConnectionError) as exc:
      last_error = exc
      if attempt < 2:
        time.sleep(0.5 * (attempt + 1))
        continue
      raise TemporaryServiceError(service, type(exc).__name__) from exc
  if isinstance(last_error, TemporaryServiceError):
    raise last_error
  raise TemporaryServiceError(service, str(last_error or 'Bilinmeyen geçici ağ sorunu'))


def _is_temporary_service_problem(exc):
  text = f"{type(exc).__name__}: {exc}".lower()
  temporary_markers = (
      '429', 'too many requests', 'rate limit', 'geocoderratelimited',
      'geocodertimedout', 'geocoderunavailable', 'geocoderquotaexceeded',
      'timeout', 'timed out', 'connectionerror', 'connection aborted',
      'connection reset', 'temporarily unavailable', 'service unavailable',
      'bad gateway', 'gateway timeout', 'http 500', 'http 502', 'http 503', 'http 504',
      'temporaryserviceerror',
  )
  return isinstance(exc, TemporaryServiceError) or any(marker in text for marker in temporary_markers)


def show_service_issue(context, exc):
  """Geçici servis sorunlarını kırmızı hata yerine açıklayıcı uyarı olarak sunar.
  Programlama/veri doğrulama hataları uyarı gibi gizlenmez.
  """
  if _is_temporary_service_problem(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    if '429' in text or 'too many requests' in text or 'rate limit' in text or 'geocoderratelimited' in text:
      st.warning(f"⏳ **Yoğun istek nedeniyle şu anda yanıt alınamıyor.** {context} Servis geçici bir istek sınırı uyguladı. Lütfen kısa bir süre bekleyip yeniden deneyin. Diğer uygulama bölümleri kullanılmaya devam edebilir.")
    else:
      st.warning(f"⏳ **Servise şu anda ulaşılamıyor.** {context} Bağlantı veya servis yoğunluğu nedeniyle veri alınamadı. Lütfen biraz sonra yeniden deneyin. Mevcut olmayan veri kesin tahmin veya risksiz durum olarak yorumlanmadı.")
  else:
    st.error(f"{context} İşlem tamamlanamadı ({type(exc).__name__}). Bu bir geçici yoğunluk yanıtı olarak sınıflandırılmadı.")


def _safe_number(value, default=None):
  """Convert scalars (including pandas/numpy values) to finite floats safely."""
  try:
    if value is None:
      return default
    # Avoid treating NaN/None/empty strings as numeric values.
    if isinstance(value, str) and not value.strip():
      return default
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
      return default
    number = float(number)
    return number if number == number and abs(number) != float("inf") else default
  except (TypeError, ValueError, OverflowError):
    return default

# Sayfa Yapılandırması
st.set_page_config(
    page_title="MeteoScope-TR - Türkiye Geneli Meteoroloji & Uzman Paneli",
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


# --- GELİŞMİŞ GİYİM VE SAHA ÖNERİSİ MOTORU ---
def giyim_onerisi_olustur(ortalama_sicaklik, toplam_yagis, toplam_kar, max_ruzgar,
                          hissedilen_sicaklik=None, min_sicaklik=None,
                          max_sicaklik=None, yagis_olasiligi=None):
  """Tahmin verilerine göre uygulanabilir giyim/ hazırlık önerileri üretir."""
  if ortalama_sicaklik is None and hissedilen_sicaklik is None and min_sicaklik is None and max_sicaklik is None:
    return ("### 👕 Giyim ve Hazırlık Önerisi\n"
            "Sıcaklık tahmini eksik olduğu için güvenilir bir kıyafet önerisi oluşturulamıyor. "
            "Yağış ve rüzgâr verileri mevcutsa bu koşullara yönelik tedbirleri ayrıca dikkate alın.")

  referans_sicaklik = hissedilen_sicaklik if hissedilen_sicaklik is not None else ortalama_sicaklik
  oneriler = []
  if referans_sicaklik is not None:
    if referans_sicaklik <= 0:
      oneriler.append("**Soğuk:** Isı yalıtımlı kalın mont, bere, eldiven ve atkı kullanın; uzun süre dışarıda kalacaksanız katmanlı giyinin.")
    elif referans_sicaklik < 8:
      oneriler.append("**Serin/soğuk:** Kalın mont veya sıcak tutan kaban tercih edin; sabah ve akşam için ek bir katman bulundurun.")
    elif referans_sicaklik < 15:
      oneriler.append("**Ilık-serin:** İnce/orta kalınlıkta ceket ve çıkarılabilir ara katman tercih edin.")
    elif referans_sicaklik < 24:
      oneriler.append("**Ilıman:** Uzun kollu hafif üst veya ince ceket yeterli olabilir; gün içindeki sıcaklık değişimine göre katman kullanın.")
    elif referans_sicaklik < 30:
      oneriler.append("**Sıcak:** Nefes alabilen, açık renkli ve hafif kıyafetler tercih edin.")
    else:
      oneriler.append("**Çok sıcak:** Bol, açık renkli ve nefes alabilen kıyafetler giyin; şapka kullanın ve gölgede kalmaya özen gösterin.")

  if min_sicaklik is not None and max_sicaklik is not None and max_sicaklik - min_sicaklik >= 8:
    oneriler.append(f"**Gün içi sıcaklık farkı:** Tahmin edilen aralık {min_sicaklik:.1f}–{max_sicaklik:.1f} °C. Sabah/akşam için fazladan bir katman alın.")

  if toplam_yagis is not None and toplam_yagis >= 1.0:
    probability_text = f" Yağış olasılığı en yüksek saatlerde %{yagis_olasiligi:.0f} seviyesine çıkıyor." if yagis_olasiligi is not None else ""
    oneriler.append(f"**Yağış:** Üç günlük toplam yağış tahmini {toplam_yagis:.1f} mm.{probability_text} Suya dayanıklı ayakkabı/dış katman ve şemsiye bulundurun.")
  elif yagis_olasiligi is not None and yagis_olasiligi >= 40:
    oneriler.append(f"**Yağış ihtimali:** Saatlik olasılık en fazla %{yagis_olasiligi:.0f}. Dışarıda uzun süre kalacaksanız hafif yağmurluk bulundurun.")

  if toplam_kar is not None and toplam_kar > 0:
    oneriler.append("**Kar/buzlanma:** Su geçirmez, tabanı tutuşu iyi bot tercih edin; kaygan zeminlerde dikkatli yürüyün.")

  if max_ruzgar is not None and max_ruzgar >= 30:
    if max_ruzgar >= 60:
      oneriler.append(f"**Rüzgâr:** Hamlelerin {max_ruzgar:.0f} km/sa seviyesine ulaşması bekleniyor. Rüzgârı kesen, kapüşonlu bir dış katman giyin; şemsiye yerine yağmurluk tercih edin.")
    else:
      oneriler.append(f"**Rüzgâr:** Hamleler {max_ruzgar:.0f} km/sa seviyesine çıkabilir. Rüzgâr geçirmeyen ince bir ceket faydalı olur.")

  if not oneriler:
    oneriler.append("Belirgin bir giyim riski öne çıkmıyor. Mevsime uygun, rahat ve gerektiğinde çıkarılabilir bir katman tercih edin.")

  return "### 👕 Giyim ve Hazırlık Önerisi\n" + "\n".join(f"- {item}" for item in oneriler)


def gunluk_net_giyim_tavsiyesi(day_df, day_label):
  """Saatlik sıcaklık, hissedilen sıcaklık, yağış, kar ve rüzgârı birlikte yorumlar."""
  if day_df is None or day_df.empty or "temperature_2m" not in day_df.columns:
    return f"**{day_label}:** Yeterli tahmin verisi alınamadığı için güvenilir giyim tavsiyesi oluşturulamadı."

  def vals(column):
    if column not in day_df.columns:
      return pd.Series(dtype=float)
    return pd.to_numeric(day_df[column], errors="coerce").dropna()

  temps = vals("temperature_2m")
  if temps.empty:
    return f"**{day_label}:** Sıcaklık verisi eksik; güvenilir giyim tavsiyesi oluşturulamadı."

  feels = vals("apparent_temperature")
  tmin, tmax = float(temps.min()), float(temps.max())
  feels_min = float(feels.min()) if not feels.empty else tmin
  feels_max = float(feels.max()) if not feels.empty else tmax
  effective_min, effective_max = min(tmin, feels_min), max(tmax, feels_max)

  rain = vals("precipitation")
  rain_prob = vals("precipitation_probability")
  snow = vals("snowfall")
  gust = vals("wind_gusts_10m")
  wind = vals("wind_speed_10m")
  humidity = vals("relative_humidity_2m")

  rain_mm = float(rain.sum()) if not rain.empty else 0.0
  rain_probability = float(rain_prob.max()) if not rain_prob.empty else None
  snow_total = float(snow.sum()) if not snow.empty else 0.0
  max_gust = float(gust.max()) if not gust.empty else (float(wind.max()) if not wind.empty else 0.0)
  max_wind = float(wind.max()) if not wind.empty else 0.0
  max_humidity = float(humidity.max()) if not humidity.empty else None

  if effective_min <= 0:
    base = "kalın, ısı yalıtımlı kışlık mont"
  elif effective_min < 6:
    base = "kalın mont veya sıcak tutan kaban"
  elif effective_min < 11:
    base = "orta-kalın mont ve uzun kollu katman"
  elif effective_max < 18:
    base = "mevsimlik ceket veya hırka"
  elif effective_max < 25:
    base = "hafif ve nefes alabilen kıyafet"
  else:
    base = "ince, açık renkli ve nefes alabilen kıyafet"

  extras = []
  if effective_min <= 3:
    extras.append("bere ve eldiven")
  if effective_max >= 29:
    extras.append("şapka ve güneşten korunma")
  if effective_max - effective_min >= 8:
    extras.append("sabah-akşam serinliği için çıkarılabilir ek katman")
  if snow_total >= 0.2:
    extras.append("su geçirmez, tabanı kaymaz bot")
  elif rain_mm >= 1.0 or (rain_probability is not None and rain_probability >= 50):
    extras.append("suya dayanıklı yağmurluk ve kapalı ayakkabı")
  if max_gust >= 50:
    extras.append("rüzgârı kesen kapüşonlu dış katman; şemsiye yerine yağmurluk")
  elif max_gust >= 30 or max_wind >= 25:
    extras.append("rüzgârlık veya rüzgârı kesen ince dış katman")
  if max_humidity is not None and max_humidity >= 85 and effective_max >= 22:
    extras.append("terletmeyen, nefes alabilen kumaş")

  details = [f"sıcaklık {tmin:.0f}–{tmax:.0f} °C"]
  if not feels.empty:
    details.append(f"hissedilen {feels_min:.0f}–{feels_max:.0f} °C")
  if rain_mm > 0:
    details.append(f"yağış {rain_mm:.1f} mm")
  if rain_probability is not None:
    details.append(f"yağış olasılığı en fazla %{rain_probability:.0f}")
  if snow_total > 0:
    details.append(f"kar {snow_total:.1f} cm")
  if max_gust > 0:
    details.append(f"rüzgâr hamlesi {max_gust:.0f} km/sa")

  sentence = f"**{day_label}: {base.capitalize()} tercih edin.**"
  if extras:
    sentence += " " + "; ".join(dict.fromkeys(extras)).capitalize() + "."
  else:
    sentence += " Belirgin yağış, kar veya kuvvetli rüzgâr için ek ekipman sinyali yok."
  sentence += " (" + "; ".join(details) + ".)"
  return sentence


@st.cache_data(ttl=900, show_spinner=False)
def fetch_mgm_official_warnings():
  """MGM günlük tahmin sayfasındaki uyarı alanını ihtiyatlı biçimde okur.

  Sayfanın geri kalanında geçen benzer ifadelerden hareketle 'uyarı yok' sonucu üretmez.
  Uyarı alanı güvenilir biçimde ayrıştırılamazsa status='unavailable' döndürür.
  """
  urls = [
      "https://www.mgm.gov.tr/tahmin/gunluk-tahmin.aspx",
      "https://www.mgm.gov.tr/pda/gunluk-tahmin-PDA.aspx",
  ]
  last_error = None
  no_warning_patterns = (
      r"güncel\s+meteorolojik\s+uyarı\s+bulunmamaktadır",
      r"herhangi\s+bir\s+meteorolojik\s+uyarı\s+bulunmamaktadır",
      r"meteorolojik\s+uyarı\s+bulunmamaktadır",
      r"uyarı\s+bulunmamaktadır",
  )
  for url in urls:
    try:
      response = _request_get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 MeteoScope-TR/1.0"})
      response.raise_for_status()
      raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response.text, flags=re.I | re.S)
      plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw))
      plain = re.sub(r"\s+", " ", plain).strip()
      match = re.search(r"\bUYARILAR\b(.*?)(?:\bBÖLGELERİMİZDE HAVA\b|\bMARMARA\b|\bİL İL HAVA\b)", plain, flags=re.I | re.S)
      if not match:
        last_error = "MGM sayfası açıldı ancak resmî uyarı bölümü güvenilir biçimde ayrıştırılamadı."
        continue
      warning_text = re.sub(r"\s+", " ", match.group(1)).strip(" :-–—|")
      warning_text = re.sub(r"^(?:UYARILAR?\s*)+", "", warning_text, flags=re.I).strip()
      if any(re.search(pattern, warning_text, flags=re.I) for pattern in no_warning_patterns):
        return {"status": "none", "text": "MGM'nin günlük tahmin sayfasındaki UYARILAR bölümünde güncel meteorolojik uyarı bulunmadığı açıkça belirtiliyor.", "url": url}
      if warning_text and not re.fullmatch(r"[-–—| ]*", warning_text):
        return {"status": "active", "text": warning_text[:5000], "url": url}
      last_error = "MGM uyarı bölümü boş veya okunamayan biçimde döndü; uyarı yok sonucu çıkarılmadı."
    except Exception as exc:
      last_error = f"MGM uyarı servisine ulaşılamadı: {type(exc).__name__}: {str(exc)[:120]}"
  return {"status": "unavailable", "text": last_error or "Resmî uyarı bilgisi şu anda alınamadı.", "url": "https://www.mgm.gov.tr/meteouyari/turkiye.aspx"}


# --- SAAT BAZLI KONUM TEHLİKE TARAMA MOTORU ---
def konum_saatlik_tehlike_taramasi(df):
  """Saatlik tahminlerde mevcut ve sayısal veriler üzerinden tüm eşikleri tarar."""
  if df is None or df.empty or "time" not in df.columns:
    return []

  tehlikeli_saatler = []
  checks = [
      ("precipitation", 7.0, "Şiddetli saatlik yağış eşiği", "mm/saat", "kirmizi"),
      ("wind_gusts_10m", 50.0, "Kuvvetli rüzgâr hamlesi", "km/sa", "kirmizi"),
      ("cape", 300.0, "Yüksek atmosferik kararsızlık (CAPE)", "J/kg", "sari"),
  ]
  for _, row in df.iterrows():
    events = []
    for column, threshold, label, unit, level in checks:
      if column not in df.columns or pd.isna(row.get(column)):
        continue
      value = _safe_number(row.get(column), default=float("nan"))
      if pd.notna(value) and value >= threshold:
        events.append({"zaman": row["time"], "tur": label,
                       "deger": f"{value:.1f} {unit}", "seviye": level})
    temp = row.get("temperature_2m") if "temperature_2m" in df.columns else None
    precip = row.get("precipitation") if "precipitation" in df.columns else None
    snow = row.get("snowfall") if "snowfall" in df.columns else None
    if temp is not None and precip is not None and pd.notna(temp) and pd.notna(precip):
      if _safe_number(temp) <= 0 and (_safe_number(precip) > 0 or (snow is not None and pd.notna(snow) and _safe_number(snow) > 0)):
        events.append({"zaman": row["time"], "tur": "Don/buzlanma ile birlikte yağış ihtimali",
                       "deger": f"{_safe_number(temp):.1f} °C", "seviye": "kirmizi"})
    tehlikeli_saatler.extend(events)
  return tehlikeli_saatler


# --- HASSASLAŞTIRILMIŞ GENEL UYARI ANALİZİ ---
def konum_uyari_analizi(curr_data, df_3days):
  uyarilar = []
  curr_data = curr_data or {}
  temp = curr_data.get("temperature_2m")
  wind = curr_data.get("wind_speed_10m")
  gust = curr_data.get("wind_gusts_10m")
  snow = curr_data.get("snowfall")
  cape = curr_data.get("cape")
  precip_curr = curr_data.get("precipitation")

  if temp is not None:
    if temp <= 0:
      uyarilar.append(("kirmizi", "❄️ **Don / Buzlanma Riski:** Anlık sıcaklık 0°C veya altında."))
    elif temp < 5:
      uyarilar.append(("sari", "⚠️ **Düşük Sıcaklık:** Anlık sıcaklık 5°C'nin altında."))
  if (wind is not None and wind > 35) or (gust is not None and gust > 45):
    gust_text = f"{gust:.1f} km/sa" if gust is not None else "veri yok"
    uyarilar.append(("kirmizi", f"🌪️ **Kuvvetli Rüzgâr:** Rüzgâr hamlesi {gust_text}."))
  if snow is not None and snow > 0:
    uyarilar.append(("kirmizi", f"🌨️ **Kar Yağışı:** Anlık kar verisi {snow:.1f} cm."))
  if cape is not None and cape >= 300:
    uyarilar.append(("sari", f"⚡ **Atmosferik Kararsızlık:** CAPE {cape:.0f} J/kg; tek başına yıldırım oluştuğunu göstermez."))

  if df_3days is not None and not df_3days.empty:
    if "precipitation" in df_3days.columns:
      precip_series = pd.to_numeric(df_3days["precipitation"], errors="coerce").dropna()
      if not precip_series.empty:
        total_p = float(precip_series.sum())
        max_hourly_p = float(precip_series.max())
        if max_hourly_p >= 7.0:
          uyarilar.append(("kirmizi", f"🚨 **Şiddetli saatlik yağış eşiği:** En yüksek saatlik tahmin {max_hourly_p:.1f} mm/saat."))
        elif (precip_curr is not None and precip_curr > 0) or total_p > 10.0:
          uyarilar.append(("sari", f"🌧️ **Yağış Beklentisi:** Önümüzdeki 3 günlük gösterilen aralıkta toplam {total_p:.1f} mm."))

  has_current_signal = any(curr_data.get(k) is not None for k in ("temperature_2m", "wind_speed_10m", "wind_gusts_10m", "snowfall", "cape", "precipitation"))
  has_forecast_signal = (df_3days is not None and any(c in df_3days.columns and pd.to_numeric(df_3days[c], errors="coerce").notna().any() for c in ("precipitation", "wind_gusts_10m", "temperature_2m", "cape")))
  if not uyarilar:
    if not has_current_signal and not has_forecast_signal:
      uyarilar.append(("bilgi", "⚪ **Risk durumu bilinmiyor:** Uyarı değerlendirmesi için yeterli meteorolojik veri alınamadı."))
    else:
      uyarilar.append(("bilgi", "ℹ️ **Taranan verilerde belirgin eşik aşımı saptanmadı.** Bu, resmî meteorolojik uyarı olmadığı veya risk bulunmadığı anlamına gelmez."))
  return uyarilar


# --- TÜRKİYE GENELİ 81 İL VERİ MOTORU (V13) ---
# İl merkezleri yaklaşık koordinatlardır; ilçe/dağlık alan mikro-iklimini temsil etmez.
TURKEY_PROVINCES = {
"Adana":(37.0000,35.3213),"Adıyaman":(37.7648,38.2786),"Afyonkarahisar":(38.7507,30.5567),"Ağrı":(39.7191,43.0506),
"Aksaray":(38.3687,34.0370),"Amasya":(40.6539,35.8330),"Ankara":(39.9334,32.8597),"Antalya":(36.8969,30.7133),
"Ardahan":(41.1105,42.7022),"Artvin":(41.1828,41.8183),"Aydın":(37.8560,27.8416),"Balıkesir":(39.6484,27.8826),
"Bartın":(41.6344,32.3375),"Batman":(37.8812,41.1351),"Bayburt":(40.2552,40.2249),"Bilecik":(40.0567,30.0665),
"Bingöl":(38.8847,40.4939),"Bitlis":(38.4006,42.1095),"Bolu":(40.7395,31.6082),"Burdur":(37.7203,30.2908),
"Bursa":(40.1885,29.0610),"Çanakkale":(40.1553,26.4142),"Çankırı":(40.6013,33.6134),"Çorum":(40.5506,34.9556),
"Denizli":(37.7765,29.0864),"Diyarbakır":(37.9144,40.2306),"Düzce":(40.8438,31.1565),"Edirne":(41.6818,26.5623),
"Elazığ":(38.6810,39.2264),"Erzincan":(39.7500,39.5000),"Erzurum":(39.9043,41.2679),"Eskişehir":(39.7767,30.5206),
"Gaziantep":(37.0662,37.3833),"Giresun":(40.9128,38.3895),"Gümüşhane":(40.4603,39.4814),"Hakkari":(37.5744,43.7408),
"Hatay":(36.2021,36.1600),"Iğdır":(39.9237,44.0450),"Isparta":(37.7648,30.5566),"İstanbul":(41.0082,28.9784),
"İzmir":(38.4192,27.1287),"Kahramanmaraş":(37.5753,36.9228),"Karabük":(41.2061,32.6204),"Karaman":(37.1759,33.2287),
"Kars":(40.6013,43.0975),"Kastamonu":(41.3766,33.7765),"Kayseri":(38.7312,35.4787),"Kırıkkale":(39.8468,33.5153),
"Kırklareli":(41.7333,27.2167),"Kırşehir":(39.1458,34.1639),"Kilis":(36.7184,37.1212),"Kocaeli":(40.7654,29.9408),
"Konya":(37.8746,32.4932),"Kütahya":(39.4167,29.9833),"Malatya":(38.3552,38.3095),"Manisa":(38.6191,27.4289),
"Mardin":(37.3212,40.7245),"Mersin":(36.8121,34.6415),"Muğla":(37.2153,28.3636),"Muş":(38.9462,41.7539),
"Nevşehir":(38.6244,34.7239),"Niğde":(37.9698,34.6766),"Ordu":(40.9839,37.8764),"Osmaniye":(37.0742,36.2478),
"Rize":(41.0201,40.5234),"Sakarya":(40.6940,30.4358),"Samsun":(41.2867,36.3300),"Siirt":(37.9333,41.9500),
"Sinop":(42.0268,35.1551),"Sivas":(39.7477,37.0179),"Şanlıurfa":(37.1674,38.7955),"Şırnak":(37.5164,42.4611),
"Tekirdağ":(40.9780,27.5110),"Tokat":(40.3167,36.5500),"Trabzon":(41.0015,39.7178),"Tunceli":(39.1079,39.5401),
"Uşak":(38.6823,29.4082),"Van":(38.5012,43.3730),"Yalova":(40.6500,29.2667),"Yozgat":(39.8181,34.8147),"Zonguldak":(41.4564,31.7987)
}

# --- V14: Türkiye genelinde ECMWF / GFS / ICON karşılaştırma motoru ---
TURKEY_MODEL_SOURCES = {
  "ECMWF IFS": "ecmwf_ifs025",
  "GFS": "ncep_gfs_seamless",
  "ICON": "icon_seamless",
}

@st.cache_data(ttl=900, show_spinner=False)
def fetch_turkey_model_comparison():
  """Üç bağımsız modelden 81 il merkezi için ilk 3 günlük günlük tahminleri toplar.
  Kısmi model başarısı korunur; başarısız modelin verisi başka modelle doldurulmaz.
  """
  model_results = {label: {} for label in TURKEY_MODEL_SOURCES}
  model_errors = {}
  names = list(TURKEY_PROVINCES)
  batches = [names[i:i+15] for i in range(0, len(names), 15)]
  fields = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
            "precipitation_probability_max", "snowfall_sum", "wind_gusts_10m_max"]

  def request_batch(model_id, batch):
    coords = [TURKEY_PROVINCES[n] for n in batch]
    params = {
      "latitude": ",".join(f"{x[0]:.4f}" for x in coords),
      "longitude": ",".join(f"{x[1]:.4f}" for x in coords),
      "daily": ",".join(fields), "forecast_days": 3,
      "timezone": "Europe/Istanbul", "wind_speed_unit": "kmh",
      "precipitation_unit": "mm", "models": model_id,
    }
    last_error = None
    for attempt in range(2):
      try:
        resp = _request_get("https://api.open-meteo.com/v1/forecast", params=params, timeout=16)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict):
          if payload.get("error"):
            raise RuntimeError(payload.get("reason", "API model hatası"))
          payload = [payload]
        if not isinstance(payload, list) or len(payload) != len(batch):
          raise RuntimeError("Model yanıtındaki koordinat sayısı uyuşmadı")
        out = {}
        for name, item in zip(batch, payload):
          daily = item.get("daily") or {}
          dates = daily.get("time") or []
          if not dates:
            continue
          # Her modelin üç günlük değerlerini tarih anahtarıyla sakla.
          # Böylece örneğin üçüncü gün oluşan bir uyarı, ilk günün model tahminiyle
          # yanlışlıkla karşılaştırılmaz.
          daily_by_date = {}
          for day_index, day_text in enumerate(dates):
            day_values = {}
            for field in fields:
              vals = daily.get(field) or []
              day_values[field] = vals[day_index] if day_index < len(vals) and vals[day_index] is not None else None
            daily_by_date[str(day_text)] = day_values
          out[name] = {"date": str(dates[0]), "model": model_id, "daily_by_date": daily_by_date}
          out[name].update(daily_by_date.get(str(dates[0]), {}))
        return out
      except Exception as exc:
        last_error = exc
        # HTTP 429 zaten merkezi HTTP katmanında yeniden denendi;
        # burada tekrar döngüsüne girip istek sayısını katlama.
        if isinstance(exc, TemporaryServiceError) and exc.status_code == 429:
          break
        if attempt == 0:
          time.sleep(0.35)
    raise RuntimeError(f"{model_id}: {type(last_error).__name__}: {str(last_error)[:160]}")

  # Model bazında batch çağrıları; API'yi aşırı yüklememek için sınırlı paralellik.
  for label, model_id in TURKEY_MODEL_SOURCES.items():
    with ThreadPoolExecutor(max_workers=2) as pool:
      futures = {pool.submit(request_batch, model_id, batch): batch for batch in batches}
      for fut in as_completed(futures):
        batch = futures[fut]
        try:
          model_results[label].update(fut.result())
        except Exception as exc:
          model_errors.setdefault(label, []).append({"iller": batch, "hata": str(exc)[:200]})

  rows = []
  for city in names:
    city_models = {label: model_results[label].get(city) for label in TURKEY_MODEL_SOURCES}
    present = [x for x in city_models.values() if x]
    row = {"il": city, "models": city_models, "model_count": len(present)}
    for field in fields:
      values = [x.get(field) for x in present if x.get(field) is not None]
      row[field + "_model_mean"] = sum(values) / len(values) if values else None
      row[field + "_model_min"] = min(values) if values else None
      row[field + "_model_max"] = max(values) if values else None
      row[field + "_model_spread"] = (max(values)-min(values)) if len(values) >= 2 else None
    # Uyum: sıcaklık/yağış için kullanılabilir iki veya daha fazla model gerektirir.
    temp_spread = row.get("temperature_2m_max_model_spread")
    rain_spread = row.get("precipitation_sum_model_spread")
    if len(present) < 2:
      row["agreement"] = "Yetersiz model"
    elif temp_spread is not None and temp_spread <= 3 and (rain_spread is None or rain_spread <= 10):
      row["agreement"] = "Görece uyumlu"
    elif temp_spread is not None and temp_spread >= 6 or rain_spread is not None and rain_spread >= 20:
      row["agreement"] = "Belirgin ayrışma"
    else:
      row["agreement"] = "Kısmi uyum"
    rows.append(row)
  return {"rows": rows, "model_coverage": {label: len(model_results[label]) for label in TURKEY_MODEL_SOURCES},
          "model_errors": model_errors, "expected": len(TURKEY_PROVINCES),
          "updated_at": pd.Timestamp.now(tz="Europe/Istanbul").strftime("%d.%m.%Y %H:%M")}


def _temperature_color(value):
  if value is None: return "#64748b"
  try: value=float(value)
  except (TypeError, ValueError): return "#64748b"
  if value <= -5: return "#1d4ed8"
  if value <= 0: return "#38bdf8"
  if value <= 10: return "#14b8a6"
  if value <= 20: return "#84cc16"
  if value <= 30: return "#f59e0b"
  return "#ef4444"


def build_turkey_temperature_map(rows, metric="temperature_2m_max"):
  """Dünya görünümünü engelleyerek yalnızca Türkiye ölçeğinde çalışan meteoroloji haritası."""
  if not rows:
    return None
  turkey_bounds = [[35.7, 25.5], [42.2, 44.9]]
  m = folium.Map(
      location=[39.0, 35.0], zoom_start=5.8, tiles="OpenStreetMap",
      control_scale=True, prefer_canvas=True, min_zoom=5.5, max_zoom=10,
      max_bounds=True, zoom_control=True,
  )
  m.fit_bounds(turkey_bounds, padding=(12, 12))
  m.options["maxBounds"] = turkey_bounds
  m.options["maxBoundsViscosity"] = 1.0

  points = folium.FeatureGroup(name="İl merkezi sıcaklıkları", show=True)
  heat = []
  for row in rows:
    value = row.get(metric)
    try:
      lat, lon = float(row.get("enlem")), float(row.get("boylam"))
      if not (math.isfinite(lat) and math.isfinite(lon)):
        continue
    except (TypeError, ValueError):
      continue
    color = _temperature_color(value)
    city = html_lib.escape(str(row.get("il", "")))
    try:
      temp_text = f"{float(value):.1f} °C" if value is not None and pd.notna(value) else "Veri yok"
    except (TypeError, ValueError):
      temp_text = "Veri yok"
    rain = row.get("precipitation_sum")
    try:
      rain_text = f"{float(rain):.1f} mm" if rain is not None and pd.notna(rain) else "Veri yok"
    except (TypeError, ValueError):
      rain_text = "Veri yok"
    popup_html = (
      f"<div style='min-width:180px'><b>{city}</b><hr>"
      f"<b>Gösterilen sıcaklık:</b> {temp_text}<br>"
      f"<b>Günlük yağış:</b> {rain_text}<br>"
      f"<b>Min / maks:</b> {row.get('temperature_2m_min', '—')} / "
      f"{row.get('temperature_2m_max', '—')} °C<br>"
      f"<b>Kar:</b> {row.get('snowfall_sum', '—')} cm<br>"
      f"<b>Rüzgâr hamlesi:</b> {row.get('wind_gusts_10m_max', '—')} km/sa</div>"
    )
    folium.CircleMarker(
      location=[lat, lon], radius=7, color="#ffffff", weight=1.5,
      fill=True, fill_color=color, fill_opacity=0.95,
      tooltip=f"{city}: {temp_text}",
      popup=folium.Popup(popup_html, max_width=290),
    ).add_to(points)
    try:
      numeric_value = float(value)
      if math.isfinite(numeric_value):
        heat.append([lat, lon, max(0.05, min(1.0, (numeric_value + 20.0) / 60.0))])
    except (TypeError, ValueError):
      pass
  points.add_to(m)
  if heat:
    heat_layer = folium.FeatureGroup(name="Sıcaklık yoğunluk görünümü", show=False)
    HeatMap(heat, radius=25, blur=18, min_opacity=0.22).add_to(heat_layer)
    heat_layer.add_to(m)
  legend = """
  <div style="position:fixed;left:58px;bottom:28px;z-index:9999;background:rgba(255,255,255,.96);
       padding:9px 12px;border:1px solid #cbd5e1;border-radius:8px;
       box-shadow:0 1px 5px rgba(0,0,0,.16);font-size:12px;line-height:1.65">
    <b>Sıcaklık (°C)</b><br>
    <span style="color:#1d4ed8">●</span> ≤ -5&nbsp;
    <span style="color:#38bdf8">●</span> 0&nbsp;
    <span style="color:#14b8a6">●</span> 1–10<br>
    <span style="color:#84cc16">●</span> 11–20&nbsp;
    <span style="color:#f59e0b">●</span> 21–30&nbsp;
    <span style="color:#ef4444">●</span> &gt;30
  </div>
  """
  m.get_root().html.add_child(folium.Element(legend))
  folium.LayerControl(collapsed=True).add_to(m)
  folium.plugins.Fullscreen().add_to(m)
  return m


TURKEY_DAILY_FIELDS = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                       "precipitation_probability_max", "snowfall_sum", "wind_gusts_10m_max", "weather_code"]
TURKEY_HOURLY_FIELDS = ["precipitation", "wind_gusts_10m", "snowfall", "temperature_2m", "cape",
                        "freezing_level_height", "snowfall_height"]

@st.cache_data(ttl=900, show_spinner=False)
def fetch_turkey_national_forecast():
  """81 il merkezini çoklu-konum istekleriyle toplar; kısmi başarıyı korur."""
  names = list(TURKEY_PROVINCES)
  rows, errors = [], []
  def fetch_batch(batch_names, timeout=14, retry_attempts=2):
    coords = [TURKEY_PROVINCES[n] for n in batch_names]
    params = {
      "latitude": ",".join(f"{x[0]:.4f}" for x in coords),
      "longitude": ",".join(f"{x[1]:.4f}" for x in coords),
      "daily": ",".join(TURKEY_DAILY_FIELDS),
      "hourly": ",".join(TURKEY_HOURLY_FIELDS),
      "forecast_days": 3, "timezone": "Europe/Istanbul",
      "wind_speed_unit": "kmh", "precipitation_unit": "mm"
    }
    last_error = None
    for attempt in range(retry_attempts):
      try:
        response = _request_get("https://api.open-meteo.com/v1/forecast", params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        break
      except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
        last_error = exc
        if attempt < retry_attempts - 1: time.sleep(0.4 * (attempt + 1))
    else:
      raise RuntimeError(f"Open-Meteo toplu istek hatası: {last_error}")
    if isinstance(payload, dict):
      if payload.get("error"):
        raise RuntimeError(payload.get("reason", "API veri hatası"))
      payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(batch_names):
      raise RuntimeError("Çoklu il yanıtının konum sayısı eşleşmedi")
    result = []
    for name, item in zip(batch_names, payload):
      daily, hourly = item.get("daily") or {}, item.get("hourly") or {}
      dates = daily.get("time") or []
      if not dates:
        continue
      record = {"il":name, "enlem":TURKEY_PROVINCES[name][0], "boylam":TURKEY_PROVINCES[name][1],
                "tarih":dates[0], "veri_zamani":item.get("utc_offset_seconds")}
      for field in TURKEY_DAILY_FIELDS:
        vals = daily.get(field) or []
        record[field] = vals[0] if vals and vals[0] is not None else None
      # Üç günlük toplam/tepe değerleri; her saatlik dizi aynı zaman eksenine göre taranır.
      for field in TURKEY_HOURLY_FIELDS:
        vals = [v for v in (hourly.get(field) or []) if v is not None]
        record["hourly_max_"+field] = max(vals) if vals else None
      # Kar seviyesi değerlendirmesi: API'nin donma seviyesi / snowfall height alanları
      # ayrı tutulur; kar yağışının kesin ilçe-altitude sınırı olarak yorumlanmaz.
      freezing_vals = [v for v in (hourly.get("freezing_level_height") or []) if v is not None]
      snow_height_vals = [v for v in (hourly.get("snowfall_height") or []) if v is not None]
      record["freezing_level_height_min_m"] = min(freezing_vals) if freezing_vals else None
      record["snowfall_height_min_m"] = min(snow_height_vals) if snow_height_vals else None
      rain = hourly.get("precipitation") or []
      # Kayar 3/6 saatlik yağış toplamları: aşırı yağış sinyali tek saat eşiğine bağlı kalmasın.
      rain_numeric = [float(v) if v is not None else 0.0 for v in rain]
      hourly_times = hourly.get("time") or []
      def _rolling_rain_max(window):
        if len(rain_numeric) < window:
          return None, None
        sums = [sum(rain_numeric[i:i+window]) for i in range(len(rain_numeric)-window+1)]
        idx = max(range(len(sums)), key=lambda i:sums[i])
        event_idx = idx + window - 1
        return sums[idx], (hourly_times[event_idx] if event_idx < len(hourly_times) else None)
      record["max_rain_3h_mm"], record["max_rain_3h_time"] = _rolling_rain_max(3)
      record["max_rain_6h_mm"], record["max_rain_6h_time"] = _rolling_rain_max(6)
      gust = hourly.get("wind_gusts_10m") or []
      snow = hourly.get("snowfall") or []
      temp = hourly.get("temperature_2m") or []
      times = hourly.get("time") or []
      event_candidates = []
      for i, t in enumerate(times):
        p = rain[i] if i < len(rain) else None
        g = gust[i] if i < len(gust) else None
        sn = snow[i] if i < len(snow) else None
        te = temp[i] if i < len(temp) else None
        if p is not None and p >= 8: event_candidates.append(("Kuvvetli saatlik yağış", float(p), "mm/saat", t, "Yüksek"))
        if g is not None and g >= 70: event_candidates.append(("Çok kuvvetli rüzgâr hamlesi", float(g), "km/sa", t, "Yüksek"))
        elif g is not None and g >= 50: event_candidates.append(("Kuvvetli rüzgâr hamlesi", float(g), "km/sa", t, "Orta"))
        if sn is not None and sn >= 1: event_candidates.append(("Kar yağışı", float(sn), "cm/saat", t, "Orta"))
        cape_values = hourly.get("cape") or []
        cape_value = cape_values[i] if i < len(cape_values) else None
        if cape_value is not None and cape_value >= 500:
          event_candidates.append(("Yüksek atmosferik kararsızlık (CAPE)", float(cape_value), "J/kg", t, "Orta"))
        if te is not None and te <= 0 and ((p is not None and p >= 0.5) or (sn is not None and sn > 0)):
          event_candidates.append(("Don eşliğinde yağış / buzlanma", float(te), "°C", t, "Yüksek"))
      # Aynı ilde aynı olay için tek en kuvvetli kayıt; farklı olaylar korunur.
      best = {}
      for event in event_candidates:
        key = event[0]
        if key not in best or event[1] > best[key][1]: best[key] = event
      record["olaylar"] = [{"il":name,"tip":v[0],"deger":v[1],"birim":v[2],"zaman":v[3],"seviye":v[4]} for v in best.values()]
      rain_3h = record.get("max_rain_3h_mm")
      rain_6h = record.get("max_rain_6h_mm")
      if rain_3h is not None and rain_3h >= 30:
        record["olaylar"].append({"il":name,"tip":"Kısa süreli yoğun yağış (3 saat)","deger":float(rain_3h),"birim":"mm/3 saat","zaman":record.get("max_rain_3h_time") or dates[0],"seviye":"Yüksek" if rain_3h>=50 else "Orta"})
      if rain_6h is not None and rain_6h >= 50:
        record["olaylar"].append({"il":name,"tip":"Birikimli yoğun yağış (6 saat)","deger":float(rain_6h),"birim":"mm/6 saat","zaman":record.get("max_rain_6h_time") or dates[0],"seviye":"Yüksek" if rain_6h>=80 else "Orta"})
      daily_rain = record.get("precipitation_sum")
      if daily_rain is not None and daily_rain >= 50:
        record["olaylar"].append({"il":name,"tip":"Yüksek günlük yağış toplamı","deger":float(daily_rain),"birim":"mm/gün","zaman":dates[0]+" 00:00","seviye":"Yüksek"})
      low_temp = record.get("temperature_2m_min")
      if low_temp is not None and low_temp <= -5:
        record["olaylar"].append({"il":name,"tip":"Çok düşük sıcaklık / don","deger":float(low_temp),"birim":"°C","zaman":dates[0]+" 06:00","seviye":"Orta"})
      result.append(record)
    return result
  batches = [names[i:i+9] for i in range(0, len(names), 9)]
  with ThreadPoolExecutor(max_workers=2) as pool:
    future_map = {pool.submit(fetch_batch, batch): batch for batch in batches}
    for future in as_completed(future_map):
      try:
        rows.extend(future.result())
      except Exception as exc:
        batch = future_map[future]
        # Servis 429 ile sınır koyduysa tekil sorgularla yükü büyütme;
        # bu grubun verisini bilinmiyor olarak bırak ve kullanıcıya uyarı göster.
        if _is_temporary_service_problem(exc) and ('429' in str(exc) or 'rate limit' in str(exc).lower()):
          for city in batch:
            errors.append({"iller":[city],"hata":"Yoğun istek nedeniyle servis geçici sınır uyguladı (HTTP 429)."})
          continue
        # Diğer toplu sorgu sorunlarında sınırlı tekil kurtarma denemesi yap.
        with ThreadPoolExecutor(max_workers=3) as fallback_pool:
          fallback_map = {fallback_pool.submit(fetch_batch, [city], 10, 1): city for city in batch}
          for fallback_future in as_completed(fallback_map):
            city = fallback_map[fallback_future]
            try: rows.extend(fallback_future.result())
            except Exception as city_exc:
              errors.append({"iller":[city],"hata":f"{type(city_exc).__name__}: {str(city_exc)[:180]}"})
  rows.sort(key=lambda x:x["il"])
  return {"rows":rows,"errors":errors,"expected":len(TURKEY_PROVINCES),
          "updated_at":pd.Timestamp.now(tz="Europe/Istanbul").strftime("%d.%m.%Y %H:%M"),
          "coverage":len(rows)/len(TURKEY_PROVINCES) if TURKEY_PROVINCES else 0}


def turkey_national_alerts(national_data):
  alerts=[]
  for row in national_data.get("rows",[]): alerts.extend(row.get("olaylar",[]))
  priority={"Yüksek":0,"Orta":1,"Düşük":2}
  alerts.sort(key=lambda x:(priority.get(x.get("seviye"),3),x.get("zaman","")))
  return alerts


def build_turkey_share_svg(rows, report_date):
  """Küçük ve taşınabilir SVG günlük kartı; şehir konumları il merkezi koordinatlarıdır."""
  import html as _html
  if not rows: return ""
  width,height=1080,1350
  points=[]
  for r in rows:
    x=120+(r["boylam"]-26.0)/(45.0-26.0)*840
    y=280+(42.5-r["enlem"])/(42.5-35.5)*650
    t=r.get("temperature_2m_max")
    color="#ef4444" if t is not None and t>=30 else "#f59e0b" if t is not None and t>=20 else "#38bdf8" if t is not None and t>=8 else "#2563eb"
    label=_html.escape(r["il"])
    val=f'{t:.0f}°' if t is not None else "—"
    points.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="{color}" stroke="#fff" stroke-width="3"/><text x="{x:.1f}" y="{y+5:.1f}" text-anchor="middle" font-size="11" font-weight="700" fill="#fff">{val}</text>')
  precip=sorted([r for r in rows if r.get("precipitation_sum") is not None],key=lambda x:x["precipitation_sum"],reverse=True)[:3]
  top=" · ".join(f'{_html.escape(x["il"])} {x["precipitation_sum"]:.1f} mm' for x in precip) or "Yağış verisi sınırlı"
  return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><defs><linearGradient id="bg" x2="0" y2="1"><stop stop-color="#092b50"/><stop offset="1" stop-color="#14618a"/></linearGradient></defs><rect width="100%" height="100%" fill="url(#bg)"/><text x="60" y="100" font-size="34" fill="#9ddcff" font-weight="700">METEOSCOPE · TÜRKİYE</text><text x="60" y="170" font-size="58" fill="#fff" font-weight="800">GÜNLÜK HAVA PANORAMASI</text><text x="60" y="220" font-size="28" fill="#d8efff">{_html.escape(report_date)} · 81 il merkezi tahmini</text><rect x="40" y="255" width="1000" height="720" rx="34" fill="#ffffff" fill-opacity=".08" stroke="#9bdcff" stroke-opacity=".35"/>{''.join(points)}<text x="60" y="1050" font-size="27" fill="#fff" font-weight="700">En yüksek yağış tahmini (ilk 3 il)</text><text x="60" y="1100" font-size="24" fill="#bfeaff">{top}</text><text x="60" y="1190" font-size="20" fill="#d7eaff">Kaynak: Open-Meteo · İl merkezi koordinatları · 3 günlük tahminin ilk günü</text><text x="60" y="1230" font-size="18" fill="#d7eaff">Yerel mikroklima ve ilçe farklılıkları bu haritada gösterilmez.</text><text x="60" y="1290" font-size="20" fill="#fff" font-weight="700">MeteoScope-TR</text></svg>"""

# --- TÜRKİYE GENELİ ERKEN UYARI MASASI (EWS) ---
TURKEY_REGIONAL_CENTERS = {
    "Marmara": ["İstanbul","Edirne","Tekirdağ","Kırklareli","Çanakkale","Balıkesir","Bursa","Yalova","Kocaeli","Sakarya","Bilecik"],
    "Ege": ["İzmir","Aydın","Muğla","Manisa","Denizli","Uşak","Kütahya","Afyonkarahisar"],
    "Akdeniz": ["Antalya","Mersin","Adana","Hatay","Osmaniye","Kahramanmaraş","Isparta","Burdur"],
    "İç Anadolu": ["Ankara","Konya","Eskişehir","Kayseri","Sivas","Yozgat","Kırşehir","Nevşehir","Niğde","Aksaray","Karaman","Kırıkkale","Çankırı"],
    "Karadeniz": ["Bolu","Düzce","Zonguldak","Karabük","Bartın","Kastamonu","Sinop","Samsun","Amasya","Tokat","Ordu","Giresun","Trabzon","Rize","Artvin","Gümüşhane","Bayburt","Çorum"],
    "Doğu Anadolu": ["Erzurum","Erzincan","Kars","Ardahan","Ağrı","Iğdır","Van","Hakkari","Bitlis","Muş","Bingöl","Tunceli","Elazığ","Malatya"],
    "Güneydoğu Anadolu": ["Gaziantep","Kilis","Şanlıurfa","Diyarbakır","Mardin","Batman","Siirt","Şırnak","Adıyaman"],
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_turkey_wide_ews():
  national = fetch_turkey_national_forecast()
  model_comparison = fetch_turkey_model_comparison()
  model_by_city = {r["il"]: r for r in model_comparison.get("rows", [])}
  for row in national.get("rows", []):
    row["model_comparison"] = model_by_city.get(row.get("il"), {})
  rows = national.get("rows", [])
  alerts = turkey_national_alerts(national)
  # Uyarıyı, olayın gerçekleştiği GÜNÜN model tahminleriyle karşılaştır.
  # Veri yokluğu ile modellerin risk eşiğini desteklememesi ayrı durumlar olarak gösterilir.
  for alert in alerts:
    city_model = model_by_city.get(alert.get("il"), {})
    city_data = city_model.get("models", {}) if isinstance(city_model, dict) else {}
    typ = str(alert.get("tip", "")).lower()
    if "don" in typ or "sıcaklık" in typ or "buz" in typ:
      field, threshold = "temperature_2m_min", 0
    elif "kar" in typ or "snow" in typ:
      field, threshold = "snowfall_sum", 0.1
    elif "rüzgâr" in typ or "rüzgar" in typ:
      field, threshold = "wind_gusts_10m_max", 50
    elif "yağış" in typ or "rain" in typ:
      # Günlük model toplamı, saatlik/3-6 saatlik şiddet eşiğini doğrudan doğrulayamaz.
      if any(marker in typ for marker in ("saatlik", "3 saat", "6 saat")):
        field, threshold = None, None
      else:
        field, threshold = "precipitation_sum", 20
    else:
      field, threshold = None, None

    try:
      parsed_event_date = pd.to_datetime(alert.get("zaman"), errors="coerce")
      event_date = None if pd.isna(parsed_event_date) else parsed_event_date.strftime("%Y-%m-%d")
    except Exception:
      event_date = None
    support, comparable = [], []
    if field and event_date:
      for model_label, values in city_data.items():
        if not isinstance(values, dict) or not values:
          continue
        day_values = (values.get("daily_by_date") or {}).get(event_date)
        # Eski/eksik API yanıtında yalnızca ilk gün varsa sadece aynı tarihte kullan.
        if day_values is None and str(values.get("date", "")) == event_date:
          day_values = values
        value = day_values.get(field) if isinstance(day_values, dict) else None
        if value is None:
          continue
        comparable.append(model_label)
        agrees = (value >= threshold) if field != "temperature_2m_min" else (value <= threshold)
        if agrees:
          support.append(model_label)

    alert["model_destegi"] = len(support)
    alert["destekleyen_modeller"] = support
    alert["karsilastirilabilir_model_sayisi"] = len(comparable)
    alert["karsilastirilan_tarih"] = event_date
    if field is None:
      if ("yağış" in typ or "rain" in typ) and any(marker in typ for marker in ("saatlik", "3 saat", "6 saat")):
        status = "Kısa süreli yağış şiddeti günlük model verisiyle doğrudan doğrulanamaz"
      else:
        status = "Bu olay türü için karşılaştırılabilir model alanı yok"
    elif not comparable:
      status = "Olay gününe ait model karşılaştırma verisi alınamadı"
    elif len(support) >= 2:
      status = "2+ model risk sinyalini destekliyor"
    elif len(support) == 1:
      status = "Tek model risk sinyalini destekliyor"
    elif len(comparable) >= 2:
      status = "Model verisi mevcut; risk eşiği desteklenmiyor"
    else:
      status = "Yalnızca tek model verisi var; risk eşiği desteklenmiyor"
    alert["model_uyumu"] = status
  successful = [r["il"] for r in rows]
  failed = [name for name in TURKEY_PROVINCES if name not in successful]
  regional = {}
  for region, cities in TURKEY_REGIONAL_CENTERS.items():
    city_set=set(cities)
    region_rows=[r for r in rows if r["il"] in city_set]
    region_alerts=[a for a in alerts if a["il"] in city_set]
    high_alerts=sum(1 for a in region_alerts if a.get("seviye")=="Yüksek")
    alert_city_count=len({a.get("il") for a in region_alerts})
    agreement_counts={"Görece uyumlu":0,"Kısmi uyum":0,"Belirgin ayrışma":0,"Yetersiz model":0}
    for city_row in region_rows:
      agreement=(city_row.get("model_comparison") or {}).get("agreement")
      if agreement in agreement_counts: agreement_counts[agreement]+=1
    regional[region]={"checked":len(region_rows),"expected":len(cities),"alerts":region_alerts,
                      "alert_city_count":alert_city_count,"high_alerts":high_alerts,
                      "max_rain":max((r.get("precipitation_sum") for r in region_rows if r.get("precipitation_sum") is not None),default=None),
                      "min_temp":min((r.get("temperature_2m_min") for r in region_rows if r.get("temperature_2m_min") is not None),default=None),
                      "model_agreement":agreement_counts,
                      "risk_status":"Yüksek risk sinyali" if high_alerts else "Risk sinyali var" if region_alerts else "Eşik aşımı saptanmadı" if len(region_rows)==len(cities) else "Eksik veri / bilinmiyor"}
  return {"alerts":alerts,"successful_centers":successful,"failed_centers":[{"bolge":x,"hata":"İl verisi alınamadı"} for x in failed],
          "checked_centers":len(TURKEY_PROVINCES),"regional":regional,"national":national,
          "model_comparison":model_comparison}


# --- GİZLİ YÖNETİCİ PANELİ ---
with st.sidebar:
  st.image("https://images.unsplash.com/photo-1592210454359-9043f067919b?w=400&q=80", use_container_width=True)
  st.title("MeteoScope-TR")
  st.caption("Gelişmiş Meteoroloji & Uzman Paneli")
  st.markdown("---")
  st.subheader("🔐 Gizli Yönetici Girişi")
  yonetici_sifre = st.text_input("Yönetici Şifresi", type="password", key="admin_pass")
  try:
    admin_password = st.secrets.get("ADMIN_PASSWORD", "") or os.environ.get("ADMIN_PASSWORD", "")
  except Exception:
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
  if not admin_password:
    st.warning("Yönetici paneli kapalı: Streamlit Secrets içinde ADMIN_PASSWORD tanımlı değil.")
  elif yonetici_sifre and yonetici_sifre == admin_password:
    st.success("✅ Yönetici Modu Aktif")
    render_live_admin_metrics()
  elif yonetici_sifre:
    st.error("Hatalı şifre.")
  else:
    st.info("Yönetici istatistikleri için yönetici şifresini girin.")


# --- ANA UYGULAMA ---
st.title("🌍 MeteoScope-TR · Türkiye Geneli Meteoroloji & Uzman Ar-Ge Paneli")

# Türkiye geneli erken uyarı verisini kullanmadan önce oluştur.
# Önceki sürümde ews_result tanımlanmadan aşağıda kullanıldığı için NameError oluşuyordu.
try:
  ews_result = fetch_turkey_wide_ews()
  if not isinstance(ews_result, dict):
    raise TypeError("Türkiye geneli EWS sonucu beklenen sözlük formatında değil.")
except Exception as e:
  ews_result = {
    "alerts": [],
    "successful_centers": [],
    "failed_centers": [{"bolge": "Türkiye geneli", "hata": str(e)}],
    "regional": {},
    "national": {"updated_at": "veri alınamadı", "rows": []},
    "model_comparison": {"rows": []},
    "error": str(e),
  }

# Türkiye geneli erken uyarı bilgisi ayrı sekmede sunulur; ana ekranı kalabalıklaştırmaz.
if ews_result.get("error"):
  st.caption("🚨 Türkiye geneli erken uyarı verisi şu anda alınamadı. Ayrıntılar ‘Türkiye Geneli Erken Uyarı’ sekmesinde.")
else:
  st.caption(f"🚨 Türkiye geneli erken uyarı taraması hazır: {len(ews_result.get('successful_centers', []))}/81 il verisi · Ayrıntılar ‘Türkiye Geneli Erken Uyarı’ sekmesinde.")

st.markdown("Dünya haritasında arama yapın veya haritaya tıklayın. Arama kutusu ve işaretçi çift yönlü senkronize edilir; harita seçimi arama alanına yazılır.")

# Nominatim kamu servisi istekleri sınırlar. Güzergâh sorgularını önbelleğe alıp
# istekleri en az 1,2 saniye arayla göndererek HTTP 429 riskini azalt.
geolocator = Nominatim(user_agent="MeteoScope-TR/1.0", timeout=10)
route_geocode_limiter = RateLimiter(
    geolocator.geocode,
    min_delay_seconds=1.2,
    # Nominatim rate-limit errors are surfaced immediately to the UI; repeated
    # automatic retries can leave the route spinner running for a long time.
    max_retries=0,
    error_wait_seconds=1.5,
    swallow_exceptions=False,
)


def _route_request_get(url, *args, **kwargs):
    """Route-only HTTP policy: one bounded retry for transient network/5xx; never retry 429."""
    service = str(url).split('/')[2] if '://' in str(url) else 'rota servisi'
    last_exc = None
    for attempt in range(2):
        try:
            response = requests.get(url, *args, **kwargs)
            if response.status_code == 429:
                raise TemporaryServiceError(service, 'HTTP 429 - yoğun istek sınırı', 429)
            if response.status_code in (500, 502, 503, 504) and attempt == 0:
                time.sleep(0.7)
                continue
            return response
        except TemporaryServiceError:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.7)
                continue
            raise TemporaryServiceError(service, type(exc).__name__) from exc
    if last_exc:
        raise TemporaryServiceError(service, str(last_exc)) from last_exc
    return response


# Nominatim'ın 1 istek/s kuralını geocode ve reverse-geocode arasında ortak uygula.
# İki ayrı RateLimiter kullanmak tek başına yeterli değildir; eşzamanlı oturumlar
# farklı limiter'lardan aynı kamu servisine peş peşe istek gönderebilir.
_NOMINATIM_LOCK = threading.Lock()
_NOMINATIM_LAST_CALL = 0.0

def _nominatim_call(func, *args, **kwargs):
    global _NOMINATIM_LAST_CALL
    with _NOMINATIM_LOCK:
        wait_for = 1.15 - (time.monotonic() - _NOMINATIM_LAST_CALL)
        if wait_for > 0:
            time.sleep(wait_for)
        _NOMINATIM_LAST_CALL = time.monotonic()
        return func(*args, **kwargs)


@st.cache_data(ttl=1800, show_spinner=False)
def _route_weather_batch_cached(latitudes, longitudes, weather_vars, forecast_days=16):
    """Cache successful multi-coordinate route forecasts for 30 minutes."""
    params = {
        "latitude": latitudes, "longitude": longitudes,
        "hourly": weather_vars, "forecast_days": forecast_days,
        "timezone": "Europe/Istanbul", "wind_speed_unit": "kmh",
        "precipitation_unit": "mm"
    }
    response = _route_request_get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=86400, show_spinner=False)
def _route_geocode_photon(query):
    """Secondary geocoder used when public Nominatim is throttled/unavailable."""
    response = requests.get(
        "https://photon.komoot.io/api/",
        params={"q": query, "limit": 3, "lang": "tr", "lat": 39.0, "lon": 35.0},
        headers={"User-Agent": "MeteoScope-TR/1.0 (route geocoding fallback)"},
        timeout=10,
    )
    if response.status_code == 429:
        raise TemporaryServiceError("photon.komoot.io", "HTTP 429 - yedek adres servisi yoğun", 429)
    response.raise_for_status()
    features = (response.json() or {}).get("features", [])
    for feature in features:
        props = feature.get("properties") or {}
        country = _tr_norm(str(props.get("country") or ""))
        if country not in ("turkiye", "turkey"):
            continue
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        parts = [props.get(k) for k in ("name", "district", "city", "county", "state", "country")]
        address = ", ".join(dict.fromkeys(str(x).strip() for x in parts if x and str(x).strip()))
        return {"latitude": float(coords[1]), "longitude": float(coords[0]), "address": address or query}
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def geocode_route_location(query):
    """Cache route geocoding; fall back to Photon if Nominatim is rate-limited."""
    try:
        location = _nominatim_call(route_geocode_limiter, query, country_codes="tr", timeout=10)
        if location is not None:
            return {
                "latitude": float(location.latitude),
                "longitude": float(location.longitude),
                "address": str(location.address),
            }
    except Exception as nominatim_error:
        try:
            fallback = _route_geocode_photon(query)
            if fallback:
                return fallback
        except Exception as photon_error:
            raise TemporaryServiceError(
                "adres bulma servisleri",
                f"Nominatim yanıt vermedi ({str(nominatim_error)[:120]}); yedek servis de yanıt vermedi ({str(photon_error)[:120]})."
            ) from photon_error
        if _is_temporary_service_problem(nominatim_error):
            raise TemporaryServiceError("Nominatim/Photon", str(nominatim_error)) from nominatim_error
        raise
    # Nominatim sonuç bulamadıysa da ikinci sağlayıcıyı dene.
    return _route_geocode_photon(query)


route_reverse_limiter = RateLimiter(
    geolocator.reverse,
    min_delay_seconds=1.1,
    max_retries=0,
    # Geopy requires error_wait_seconds >= min_delay_seconds.
    error_wait_seconds=1.2,
    swallow_exceptions=True,
)

@st.cache_data(ttl=86400, show_spinner=False)
def reverse_route_risk_address(lat, lon):
    """Resolve a risk coordinate to a readable Turkish address; cache for 24h."""
    try:
        place = _nominatim_call(
            route_reverse_limiter,
            (float(lat), float(lon)), exactly_one=True,
            language="tr", addressdetails=True, timeout=8
        )
        if place and place.address:
            return str(place.address)
    except Exception:
        pass
    return f"Açık adres alınamadı (koordinat: {float(lat):.5f}, {float(lon):.5f})"

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
if "main_search_input" not in st.session_state:
  st.session_state["main_search_input"] = st.session_state.search_input_val
if "_main_map_last_click" not in st.session_state:
  st.session_state["_main_map_last_click"] = None

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
    # Karayolunda şehir takma-adı listesi sınırlayıcı olmamalı: Türkiye'deki
    # tüm iller ve geocode edilebilen ilçe/mahalleler OSRM koordinatlarıyla
    # doğrulanır. Böylece 81 ilin tamamı kullanılabilir.
    if mode in ("Özel araç", "Otobüs"):
        return True, "Karayolu bağlantısı OSRM yol ağı üzerinden kontrol edilecek."
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
      route_max_date = today_route + timedelta(days=15)
      planlanan_tarih = st.date_input("Planlanan seyahat tarihi", value=today_route,
          min_value=today_route, max_value=route_max_date, key="route_travel_date",
          help="Open-Meteo tahmin ufku en fazla 16 gündür; bugün dahil sayıldığı için en ileri tarih bugün +15 gündür.")

    rota_sorgula = st.button("Türkiye Rotasını ve Ulaşım Uygunluğunu Analiz Et 🇹🇷", type="primary")

    if rota_sorgula:
      # Yeni sorgu başlarken önceki rotayı ekranda tutma; bu sorgunun sonucu
      # ne olursa olsun aşağıda açık bir durum/rapor üretilecek.
      st.session_state.route_map_data = None
      if not baslangic_yer.strip() or not bitis_yer.strip():
        st.warning("Lütfen başlangıç ve varış konumlarını gir.")
      elif _tr_norm(baslangic_yer) == _tr_norm(bitis_yer):
        st.warning("Başlangıç ve varış aynı görünüyor. Farklı konumlar gir.")
      else:
        with st.spinner("Güzergâh hazırlanıyor: konumlar doğrulanıyor, yol ağı ve hava tahminleri kontrol ediliyor. Servis yanıt vermezse açıklayıcı uyarı gösterilecek..."):
          try:
            # Same place queries are served from cache; new Nominatim requests are
            # serialized/rate-limited to avoid public-service HTTP 429 responses.
            loc_b_data = geocode_route_location(baslangic_yer.strip())
            loc_v_data = geocode_route_location(bitis_yer.strip())
            loc_b = SimpleNamespace(**loc_b_data) if loc_b_data else None
            loc_v = SimpleNamespace(**loc_v_data) if loc_v_data else None
            if not loc_b or not loc_v:
              st.error("Başlangıç veya varış Türkiye içinde bulunamadı. İl/ilçe adıyla tekrar deneyin.")
            else:
              # Prevent accidentally geocoding an ambiguous non-Turkey result.
              if "turkiye" not in _tr_norm(loc_b.address) and "turkey" not in _tr_norm(loc_b.address):
                raise ValueError("Başlangıç konumunun Türkiye'de olduğu doğrulanamadı. İl/ilçe adıyla tekrar deneyin.")
              if "turkiye" not in _tr_norm(loc_v.address) and "turkey" not in _tr_norm(loc_v.address):
                raise ValueError("Varış konumunun Türkiye'de olduğu doğrulanamadı. İl/ilçe adıyla tekrar deneyin.")

              lat_b, lon_b = float(loc_b.latitude), float(loc_b.longitude)
              lat_v, lon_v = float(loc_v.latitude), float(loc_v.longitude)

              # İlk güvenilir çıktı: dış rota/hava servisleri çağrılmadan önce
              # başlangıç-varış haritası ve beklemede raporu oturuma yazılır.
              # Böylece sonraki herhangi bir servis hatası harita bölümünü boş bırakamaz.
              st.session_state.route_map_data = {
                "bas_ad": str(loc_b.address), "bit_ad": str(loc_v.address),
                "lat_b": lat_b, "lon_b": lon_b, "lat_v": lat_v, "lon_v": lon_v,
                "markers": [
                  {"lat": lat_b, "lon": lon_b, "name": "Başlangıç: " + str(loc_b.address), "risk": False, "desc": "Başlangıç konumu"},
                  {"lat": lat_v, "lon": lon_v, "name": "Varış: " + str(loc_v.address), "risk": False, "desc": "Varış konumu"}
                ],
                "route_geometry": [(lat_b, lon_b), (lat_v, lon_v)],
                "route_source": "Ön harita · analiz sürüyor (şematik çizgi, gerçek yol geometrisi değildir)",
                "report_table": [{
                  "Rota noktası": "Güzergâh ön kontrolü",
                  "Riskli konumun açık adresi": f"{loc_b.address} → {loc_v.address}",
                  "Koordinat": f"{lat_b:.4f}, {lon_b:.4f} → {lat_v:.4f}, {lon_v:.4f}",
                  "Hava durumu": "Analiz sürüyor",
                  "Risk": "Henüz hesaplanmadı",
                  "Açıklama": "Konumlar bulundu. Yol ve meteoroloji servisleri kontrol ediliyor."
                }],
                "status": "unknown", "weather_coverage": 0, "weather_total": 0,
                "date": planlanan_tarih.strftime("%d.%m.%Y"), "mode": ulasim_turu,
                "mode_reason": "Analiz sürüyor", "distance_m": None, "duration_s": None
              }

              straight_km = geodesic((lat_b, lon_b), (lat_v, lon_v)).km
              mode_ok, mode_reason = _tr_mode_availability(ulasim_turu, loc_b, loc_v, straight_km)
              route_geometry=[]; route_distance_m=None; route_duration_s=None; route_source=""

              if mode_ok is False:
                # Ulaşım türü eşleşmesi doğrulanamasa da konumlar çözümlendiyse
                # harita ve hava analizi kaybolmasın. Şematik çizgi açıkça etiketlenir.
                st.warning(f"Seçilen ulaşım türü doğrulanamadı: {mode_reason} Harita ve hava analizi şematik bağlantı üzerinden sürdürülecek.")
                force_schematic_route = True
              elif mode_ok is None:
                st.warning(f"{mode_reason} Harita ve hava analizi şematik bağlantı üzerinden sürdürülecek.")
                force_schematic_route = True
              else:
                force_schematic_route = False
              if force_schematic_route:
                route_geometry=[(lat_b,lon_b),(lat_v,lon_v)]
                route_source="Şematik bağlantı (ulaşım türü/hat eşleşmesi doğrulanamadı; gerçek yol veya sefer geometrisi değildir)"
              elif ulasim_turu in ("Özel araç", "Otobüs"):
                try:
                  osrm_url = (f"https://router.project-osrm.org/route/v1/driving/"
                    f"{lon_b:.6f},{lat_b:.6f};{lon_v:.6f},{lat_v:.6f}")
                  osrm_resp = _route_request_get(
                    osrm_url,
                    params={"overview":"full", "geometries":"geojson", "steps":"false"},
                    timeout=15,
                    headers={"User-Agent": "MeteoScope-TR/1.0"},
                  )
                  osrm_resp.raise_for_status(); osrm_json=osrm_resp.json()
                  if osrm_json.get("code") == "Ok" and osrm_json.get("routes"):
                    rr=osrm_json["routes"][0]
                    route_geometry=[(float(pt[1]), float(pt[0])) for pt in rr["geometry"]["coordinates"]]
                    route_distance_m=float(rr.get("distance", 0)); route_duration_s=float(rr.get("duration", 0))
                    route_source="OSRM / OpenStreetMap yol ağı"
                  else:
                    raise RuntimeError("OSRM bu iki nokta arasında karayolu rotası döndürmedi.")
                except Exception as route_err:
                  # Yol ağı servisi geçici olarak yanıt vermese de analiz akışını
                  # tamamen düşürme. Şematik bağlantıyı açıkça etiketle; gerçek
                  # yol geometrisi veya yol üstü adresiymiş gibi sunma.
                  show_service_issue("Gerçek yol geometrisi şu anda alınamadı. Güzergâhın şematik bağlantısı ve mevcut hava verileri gösterilecek.", route_err)
                  route_geometry=[(lat_b,lon_b),(lat_v,lon_v)]
                  route_source="Şematik bağlantı (OSRM yol ağı geçici olarak alınamadı; gerçek yol geometrisi değildir)"
                  route_distance_m=None
                  route_duration_s=None
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
                # Route sampling: distribute weather checks along the corridor, not only at endpoints.
                # For road routes, use distance-based density; for schematic connections, interpolate.
                route_km = (route_distance_m / 1000.0) if route_distance_m else straight_km
                n_samples = max(5, min(15, int(route_km / 100.0) + 2))
                if len(route_geometry) > 2:
                  sample_indices = sorted(set(round(i * (len(route_geometry)-1) / (n_samples-1)) for i in range(n_samples)))
                  sample_pts = [route_geometry[i] for i in sample_indices]
                else:
                  # Schematic route only: sample between endpoints and label that limitation.
                  sample_pts = [(lat_b + (lat_v-lat_b)*i/(n_samples-1),
                                 lon_b + (lon_v-lon_b)*i/(n_samples-1)) for i in range(n_samples)]

                weather_vars = "temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m,weather_code"

                def parse_date_weather(payload, target_date):
                  """Normalize one Open-Meteo location response into daily route metrics."""
                  if not isinstance(payload, dict):
                    return {"_error": "Hava servisi beklenen JSON biçimini döndürmedi"}
                  if payload.get("error"):
                    return {"_error": str(payload.get("reason") or payload.get("error"))}
                  hourly = payload.get("hourly") or {}
                  times = hourly.get("time") or []
                  if not times:
                    return {"_error": "API yanıtında saatlik tahmin bulunamadı"}
                  day_indices = [i for i, t in enumerate(times) if str(t)[:10] == target_date]
                  if not day_indices:
                    return {"_error": f"{target_date} tarihi tahmin yanıtında bulunamadı"}

                  def vals(key):
                    arr = hourly.get(key) or []
                    return [arr[i] for i in day_indices if i < len(arr) and arr[i] is not None]

                  temps = vals("temperature_2m")
                  precip = vals("precipitation")
                  snow = vals("snowfall")
                  wind = vals("wind_speed_10m")
                  gust = vals("wind_gusts_10m")
                  codes = vals("weather_code")
                  gust_arr = hourly.get("wind_gusts_10m") or []
                  precip_arr = hourly.get("precipitation") or []
                  peak_gust_i = max(day_indices, key=lambda i: gust_arr[i] if i < len(gust_arr) and gust_arr[i] is not None else -1)
                  peak_rain_i = max(day_indices, key=lambda i: precip_arr[i] if i < len(precip_arr) and precip_arr[i] is not None else -1)
                  if not any(v is not None for v in (temps or [None]) + (precip or [None]) + (snow or [None]) + (wind or [None]) + (gust or [None])):
                    return {"_error": "Seçilen gün için meteorolojik değişkenler boş döndü"}
                  return {
                    "temperature_2m": min(temps) if temps else None,
                    "temperature_max": max(temps) if temps else None,
                    "precipitation": sum(precip) if precip else None,
                    "precipitation_peak": max(precip) if precip else None,
                    "snowfall": sum(snow) if snow else None,
                    "wind_speed_10m": max(wind) if wind else None,
                    "wind_gusts_10m": max(gust) if gust else None,
                    "weather_code": codes[0] if codes else None,
                    "peak_gust_time": times[peak_gust_i] if peak_gust_i < len(times) else None,
                    "peak_rain_time": times[peak_rain_i] if peak_rain_i < len(times) else None,
                    "_error": None
                  }

                def get_date_weather_batch(points):
                  """Fetch all route coordinates in one request, then fall back per point."""
                  target_date = planlanan_tarih.isoformat()
                  latitudes = ",".join(str(round(float(xy[0]), 4)) for xy in points)
                  longitudes = ",".join(str(round(float(xy[1]), 4)) for xy in points)
                  batch_error = None
                  try:
                    payload = _route_weather_batch_cached(latitudes, longitudes, weather_vars, 16)
                    if isinstance(payload, list) and len(payload) == len(points):
                      return [parse_date_weather(item, target_date) for item in payload]
                    if len(points) == 1 and isinstance(payload, dict):
                      return [parse_date_weather(payload, target_date)]
                    if isinstance(payload, dict) and payload.get("error"):
                      batch_error = str(payload.get("reason") or payload.get("error"))
                    else:
                      batch_error = "Toplu hava yanıtı rota noktalarıyla eşleşmedi"
                  except Exception as exc:
                    batch_error = f"Toplu sorgu başarısız: {exc}"
                    if _is_temporary_service_problem(exc):
                      # Do not amplify a temporary outage/rate limit with up to
                      # 15 parallel fallback calls. Report unavailable data clearly.
                      if '429' in str(exc).lower() or 'rate limit' in str(exc).lower():
                        reason = "Yoğun istek nedeniyle hava servisi geçici sınır uyguladı (HTTP 429)."
                      else:
                        reason = "Hava servisine geçici olarak ulaşılamadı; rota noktaları için toplu tahmin alınamadı."
                      return [{"_error": reason} for _ in points]

                  # Only a non-temporary response/schema mismatch reaches fallback.
                  # Keep it sequential and bounded to avoid overloading the API.
                  def get_one(xy):
                    try:
                      one_params = {
                        "latitude": round(float(xy[0]), 4),
                        "longitude": round(float(xy[1]), 4),
                        "hourly": weather_vars, "forecast_days": 16,
                        "timezone": "Europe/Istanbul", "wind_speed_unit": "kmh",
                        "precipitation_unit": "mm"
                      }
                      one_resp = _route_request_get("https://api.open-meteo.com/v1/forecast", params=one_params, timeout=12)
                      one_resp.raise_for_status()
                      return parse_date_weather(one_resp.json(), target_date)
                    except Exception as exc:
                      return {"_error": f"{exc} (toplu sorgu: {batch_error})"}

                  fallback_results = []
                  for xy in points[:5]:
                    fallback_results.append(get_one(xy))
                  fallback_results.extend(
                    [{"_error": "Toplu yanıt biçimi uyuşmadı; ek sorgu güvenli istek sınırı nedeniyle yapılmadı."}
                     for _ in points[len(fallback_results):]]
                  )
                  return fallback_results

                route_weather = get_date_weather_batch(sample_pts)

                risk_markers=[]; report_rows=[]; route_status="unknown"
                reverse_address_count = 0
                valid_weather_count = sum(1 for item in route_weather if item and not item.get("_error") and any(item.get(k) is not None for k in ("temperature_2m","precipitation","wind_gusts_10m","snowfall")))
                if valid_weather_count:
                  route_status = "green" if valid_weather_count == len(sample_pts) else "partial"
                for i, (xy, wx) in enumerate(zip(sample_pts, route_weather)):
                  point_name = f"Rota kesimi {i+1}/{len(sample_pts)}"
                  if not wx or wx.get("_error") or not any(wx.get(k) is not None for k in ("temperature_2m","precipitation","wind_gusts_10m","snowfall")):
                    err = (wx or {}).get("_error", "Tahmin verisi yok")
                    point_address = f"Açık adres çözümlenmedi (koordinat: {xy[0]:.5f}, {xy[1]:.5f})"
                    report_rows.append({"Rota noktası":point_name,"Riskli konumun açık adresi":point_address,"Koordinat":f"{xy[0]:.4f}, {xy[1]:.4f}","Hava durumu":"Veri alınamadı","Risk":"Bilinmiyor","Açıklama":err})
                    continue
                  temp=wx.get("temperature_2m"); temp_max=wx.get("temperature_max")
                  snow=wx.get("snowfall"); precip=wx.get("precipitation")
                  peak_precip=wx.get("precipitation_peak")
                  wind=wx.get("wind_speed_10m"); gust=wx.get("wind_gusts_10m")
                  risks=[]; severity="green"
                  if temp is not None and temp<=0 and ((snow is not None and snow>0) or (precip is not None and precip>0)):
                    risks.append("Don/buzlanma ihtimali"); severity="red"
                  if snow is not None and snow>=1:
                    risks.append(f"Kar yağışı (günlük {snow:.1f} cm)"); severity="red"
                  if precip is not None and precip>=20:
                    risks.append(f"Yoğun günlük yağış ({precip:.1f} mm)"); severity="red"
                  elif precip is not None and precip>=10:
                    risks.append(f"Kuvvetli günlük yağış ({precip:.1f} mm)"); severity="orange" if severity!="red" else severity
                  if peak_precip is not None and peak_precip>=10:
                    risks.append(f"Saatlik yoğun yağış ({peak_precip:.1f} mm/sa)"); severity="red" if peak_precip>=20 else ("orange" if severity!="red" else severity)
                  if gust is not None and gust>=70:
                    risks.append(f"Çok kuvvetli rüzgâr hamlesi ({gust:.0f} km/sa)"); severity="red"
                  elif gust is not None and gust>=50:
                    risks.append(f"Kuvvetli rüzgâr hamlesi ({gust:.0f} km/sa)"); severity="orange" if severity!="red" else severity
                  risk_address = "Risk eşiği aşılmadı"
                  if risks:
                    if severity == "red": route_status = "red"
                    elif severity == "orange" and route_status not in ("red",): route_status = "orange"
                    # Kamu adres servisine 15 kez yüklenme. En fazla üç riskli
                    # noktanın açık adresini çöz; kalan riskler koordinatla korunur.
                    if reverse_address_count < 3:
                      risk_address = reverse_route_risk_address(round(float(xy[0]), 5), round(float(xy[1]), 5))
                      reverse_address_count += 1
                    else:
                      risk_address = f"Adres sorgusu sınırı: koordinat {xy[0]:.5f}, {xy[1]:.5f}"
                    risk_markers.append({"lat":xy[0],"lon":xy[1],"name":f"Risk: rota kesimi {i+1}","risk":True,"severity":severity,"desc":f"{risk_address} · " + "; ".join(risks)})
                  report_rows.append({
                    "Rota noktası":point_name,"Riskli konumun açık adresi":risk_address,"Koordinat":f"{xy[0]:.4f}, {xy[1]:.4f}",
                    "En düşük sıcaklık":f"{temp:.1f} °C" if temp is not None else "Veri yok",
                    "En yüksek sıcaklık":f"{temp_max:.1f} °C" if temp_max is not None else "Veri yok",
                    "Yağış toplamı":f"{precip:.1f} mm" if precip is not None else "Veri yok",
                    "Saatlik en yüksek yağış":f"{peak_precip:.1f} mm/sa" if peak_precip is not None else "Veri yok",
                    "Kar toplamı":f"{snow:.1f} cm" if snow is not None else "Veri yok",
                    "Rüzgâr hamlesi":f"{gust:.0f} km/sa" if gust is not None else "Veri yok",
                    "Risk":"; ".join(risks) if risks else "Belirgin eşik aşımı saptanmadı",
                    "Kritik saat":wx.get("peak_gust_time") or wx.get("peak_rain_time") or "Gün içi"
                  })

                sample_markers=[{"lat":xy[0],"lon":xy[1],"name":f"Rota hava noktası {i+1}","risk":False,"desc":("Tahmin verisi alınamadı" if not wx or wx.get("_error") else "Rota boyunca meteorolojik kontrol noktası")}
                                for i,(xy,wx) in enumerate(zip(sample_pts,route_weather))]
                markers=[{"lat":lat_b,"lon":lon_b,"name":"Başlangıç: "+loc_b.address,"risk":False,"desc":"Başlangıç konumu"},
                         {"lat":lat_v,"lon":lon_v,"name":"Varış: "+loc_v.address,"risk":False,"desc":"Varış konumu"}] + sample_markers + risk_markers
                st.session_state.route_map_data={"bas_ad":loc_b.address,"bit_ad":loc_v.address,
                  "lat_b":lat_b,"lon_b":lon_b,"lat_v":lat_v,"lon_v":lon_v,"markers":markers,
                  "route_geometry":route_geometry,"route_source":route_source,"report_table":report_rows,
                  "status":route_status,"weather_coverage":valid_weather_count,"weather_total":len(sample_pts),
                  "date":planlanan_tarih.strftime("%d.%m.%Y"),"mode":ulasim_turu,
                  "mode_reason":mode_reason,"distance_m":route_distance_m,"duration_s":route_duration_s}
                st.success(f"Ulaşım türü uygunluk kontrolü tamamlandı. {mode_reason}")
          except Exception as e:
            show_service_issue("Güzergâhın bazı analiz verileri alınamadı. Konumlar çözümlendiyse harita ve açıklayıcı rapor yine gösterilecek.", e)
            try:
              if "loc_b" in locals() and "loc_v" in locals() and loc_b and loc_v:
                _lb, _lob = float(loc_b.latitude), float(loc_b.longitude)
                _lv, _lov = float(loc_v.latitude), float(loc_v.longitude)
                st.session_state.route_map_data = {
                  "bas_ad":str(loc_b.address),"bit_ad":str(loc_v.address),
                  "lat_b":_lb,"lon_b":_lob,"lat_v":_lv,"lon_v":_lov,
                  "markers":[
                    {"lat":_lb,"lon":_lob,"name":"Başlangıç: "+str(loc_b.address),"risk":False,"desc":"Başlangıç konumu"},
                    {"lat":_lv,"lon":_lov,"name":"Varış: "+str(loc_v.address),"risk":False,"desc":"Varış konumu"}],
                  "route_geometry":[(_lb,_lob),(_lv,_lov)],
                  "route_source":"Şematik bağlantı (analiz sırasında geçici hata; gerçek yol geometrisi değildir)",
                  "report_table":[{
                    "Rota noktası":"Güzergâh genel durumu",
                    "Riskli konumun açık adresi":f"{loc_b.address} → {loc_v.address}",
                    "Koordinat":f"{_lb:.4f}, {_lob:.4f} → {_lv:.4f}, {_lov:.4f}",
                    "Hava durumu":"Analiz kısmen tamamlanamadı","Risk":"Bilinmiyor",
                    "Açıklama":f"Harita korunarak gösteriliyor. Eksik analiz nedeni: {str(e)[:350]}"}],
                  "status":"unknown","weather_coverage":0,"weather_total":0,
                  "date":planlanan_tarih.strftime("%d.%m.%Y"),"mode":ulasim_turu,
                  "mode_reason":"Kısmi analiz","distance_m":None,"duration_s":None}
            except Exception as fallback_error:
              # Preserve the earliest endpoint-only map rather than erasing it.
              if not st.session_state.get("route_map_data"):
                st.error(f"Güzergâh haritası hazırlanamadı: {fallback_error}")

    if st.session_state.get("route_map_data"):
      r_info=st.session_state.route_map_data
      st.markdown("### 📋 Seyahat uygunluğu ve yol üstü risk raporu")
      st.write(f"**Tarih:** {r_info['date']} · **Tür:** {r_info['mode']} · **Kaynak:** {r_info['route_source']}")
      if r_info.get("distance_m"):
        st.metric("Karayolu mesafesi", f"{r_info['distance_m']/1000:.1f} km")
        if r_info.get("duration_s"):
          st.caption(f"OSRM tahmini sürüş süresi: {r_info['duration_s']/3600:.1f} saat (trafik ve mola dahil değildir).")
      st.caption("Hava riskleri seçilen günün saatlik tahminlerinden hesaplanır. Rota boyunca yaklaşık 100 km aralıklarla örnekleme yapılır. Aynı rota için başarılı toplu hava tahmini 30 dakika önbellekten kullanılır. HTTP 429 durumunda tekrar tekrar istek gönderilmez; başlangıç-varış haritası ve hata açıklamalı rapor korunur. Riskli noktaların en fazla üçü için açık adres sorgulanır; diğerleri koordinatla gösterilir. Açık adres, yolun tam şerit/kapı numarası garantisi değildir. Tahmin ufku 16 güne kadardır; özellikle 5–7 günden sonraki tahminlerde belirsizlik artar. Kısa yerel olaylar ve yol/AFAD/KGM resmî uyarıları bu analizle garanti edilmez.")
      coverage = r_info.get("weather_coverage", 0); total_points = r_info.get("weather_total", 0)
      st.caption(f"Hava tahmini kapsaması: {coverage}/{total_points} rota noktası")
      if r_info.get("status") == "unknown":
        route_error_text = " ".join(str(item.get("Açıklama", "")) for item in r_info.get("report_table", []))
        if _is_temporary_service_problem(route_error_text):
          show_service_issue("Güzergâhın hava tahmin verileri şu anda alınamadı. Bu nedenle risk durumu bilinmiyor; risksiz kabul edilmemelidir.", route_error_text)
        else:
          st.info("⚪ Güzergâh hava riski bilinmiyor: seçilen tarih için tahmin kapsamı dışında kalan veya yanıt vermeyen rota noktaları var. Tablo hata ayrıntılarını gösterir; bu durum risksiz anlamına gelmez.")
      elif r_info.get("status") == "red":
        st.error("🔴 Güzergâhın en az bir örnek noktasında yüksek risk eşiği aşıldı.")
      elif r_info.get("status") == "orange":
        st.warning("🟠 Güzergâhın en az bir örnek noktasında dikkat gerektiren hava eşiği aşıldı.")
      elif r_info.get("status") == "partial":
        st.info("⚪ Güzergâh hava verisi kısmi: bazı örnek noktalarda tahmin alınamadı; tüm rota için risksiz sonucu çıkarılamaz.")
      if coverage < total_points and r_info.get("status") in ("red", "orange"):
        st.warning("⚠️ Risk sinyali tespit edildi ancak rota tahmin kapsaması kısmi. Kapsanmayan kesimler için risksiz sonucu çıkarılamaz.")
      _route_report = r_info.get("report_table") or [{
        "Rota noktası":"Rapor oluşturulamadı",
        "Riskli konumun açık adresi":f"{r_info.get('bas_ad','')} → {r_info.get('bit_ad','')}",
        "Risk":"Bilinmiyor",
        "Açıklama":"Rota geometrisi mevcut; ayrıntılı meteorolojik tablo oluşturulamadı."
      }]
      st.dataframe(pd.DataFrame(_route_report), use_container_width=True, hide_index=True)

      st.markdown("### 🗺️ Güzergâh haritası")
      all_pts=r_info.get("route_geometry") or [(r_info["lat_b"],r_info["lon_b"]),(r_info["lat_v"],r_info["lon_v"])]
      center_lat=sum(p[0] for p in all_pts)/len(all_pts); center_lon=sum(p[1] for p in all_pts)/len(all_pts)
      route_m=folium.Map(location=[center_lat,center_lon],zoom_start=6,tiles="OpenStreetMap")
      if len(all_pts) >= 2:
        route_m.fit_bounds(all_pts)
      Fullscreen().add_to(route_m); LocateControl().add_to(route_m)
      if r_info.get("mode") in ("Özel araç","Otobüs") and len(all_pts)>2:
        folium.PolyLine(all_pts,color="#2b7bff",weight=5,opacity=0.85,tooltip="OSRM gerçek yol ağı rotası").add_to(route_m)
      else:
        folium.PolyLine(all_pts,color="#888888",weight=3,opacity=0.8,dash_array="8,8",tooltip="Şematik bağlantı; gerçek hat değildir").add_to(route_m)
      for item in r_info["markers"]:
        if item.get("risk"):
          sev=item.get("severity", "orange")
          color="red" if sev=="red" else ("orange" if sev=="orange" else "yellow")
          icon_name="exclamation-sign"
        else:
          color="blue" if "Başlangıç" in item.get("name","") else "green"
          icon_name="play" if color=="blue" else "flag"
        popup=f"<b>{item['name']}</b><br>{item.get('desc','')}"
        folium.Marker([item["lat"],item["lon"]],popup=folium.Popup(popup,max_width=350),tooltip=item["name"],
          icon=folium.Icon(color=color,icon=icon_name,prefix="glyphicon")).add_to(route_m)
      route_map_data=st_folium(route_m,width="100%",height=460,key="route_map_turkey_smart_v1",returned_objects=["last_clicked","zoom"])
      route_clicked=route_map_data.get("last_clicked") if route_map_data else None
      if route_clicked:
        rlat=round(float(route_clicked.get("lat")),6); rlon=round(float(route_clicked.get("lng")),6)
        fingerprint=(rlat,rlon)
        if fingerprint!=st.session_state.get("_route_map_last_click"):
          st.session_state["_route_map_last_click"]=fingerprint
          try:
            place=reverse_route_risk_address(rlat, rlon)
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
    "wind_direction_10m", "wind_gusts_10m",
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
    ("ICON", "ICON", ["icon_seamless", "icon_global"]),
]

BACKUP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "meteoscope_backup_cache_v1")
os.makedirs(BACKUP_CACHE_DIR, exist_ok=True)


def _open_meteo_get(url, params, timeout=8):
  """Open-Meteo isteği: geçici ağ/5xx hatalarında sınırlı tekrar, hatalı JSON'u reddet."""
  last_error = None
  for attempt in range(3):
    try:
      response = _request_get(url, params=params, timeout=timeout)
      try:
        payload = response.json()
      except ValueError as json_exc:
        raise RuntimeError(f"Open-Meteo geçerli JSON döndürmedi (HTTP {response.status_code}).") from json_exc
      if not response.ok or payload.get("error"):
        reason = payload.get("reason") or payload.get("message") or response.text[:300]
        # Parametre/istek hatası tekrar denenerek düzelmez.
        if response.status_code < 500 and response.status_code != 429:
          raise RuntimeError(f"Open-Meteo HTTP {response.status_code}: {reason}")
        last_error = RuntimeError(f"Open-Meteo HTTP {response.status_code}: {reason}")
      else:
        return payload
    except (requests.Timeout, requests.ConnectionError) as exc:
      last_error = exc
    except RuntimeError as exc:
      if "HTTP 429" not in str(exc) and not re.search(r"HTTP 5\d\d", str(exc)):
        raise
      last_error = exc
    if attempt < 2:
      time.sleep(0.4 * (attempt + 1))
  raise RuntimeError(f"Open-Meteo isteği başarısız oldu: {last_error}")


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


def load_weather_from_backup_cache(latitude, longitude, max_age_seconds=86400):
  try:
    cache_path = _get_cache_filepath(latitude, longitude)
    if os.path.exists(cache_path):
      with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
      saved_at = float(payload.get("saved_at", 0))
      age_seconds = max(0.0, time.time() - saved_at)
      if not saved_at or age_seconds > max_age_seconds:
        return None, None, None, None, None
      f_data = payload.get("f_data")
      df_json = payload.get("dataframe_json")
      df = pd.read_json(StringIO(df_json), orient="split")
      if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
      metadata = {"saved_at": saved_at, "age_seconds": age_seconds,
                  "source": "yerel yedek önbellek"}
      return f_data, df, payload.get("model_status", {}), payload.get("model_errors", {}), metadata
  except Exception:
    pass
  return None, None, None, None, None


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
    cached_f, cached_df, cached_status, cached_errors, cache_meta = load_weather_from_backup_cache(latitude, longitude)
    if cached_f is not None and cached_df is not None:
      cached_status = dict(cached_status or {})
      cached_status["_cache_metadata"] = cache_meta
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


@st.cache_data(ttl=300, show_spinner=False)
def get_rainviewer_layers():
  """Return latest observed radar and short-range nowcast tile templates."""
  response = _request_get("https://api.rainviewer.com/public/weather-maps.json", timeout=8)
  response.raise_for_status()
  payload = response.json()
  host = payload.get("host")
  radar = payload.get("radar") or {}
  past = radar.get("past") or []
  nowcast = radar.get("nowcast") or []
  result = {"observed_url": None, "observed_time": None, "forecast_url": None, "forecast_time": None}
  if not host:
    return result
  if past:
    frame = past[-1]
    path = frame.get("path")
    if path:
      result["observed_url"] = f"{host}{path}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"
      result["observed_time"] = frame.get("time")
  if nowcast:
    frame = nowcast[-1]
    path = frame.get("path")
    if path:
      result["forecast_url"] = f"{host}{path}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"
      result["forecast_time"] = frame.get("time")
  return result


def _format_radar_timestamp(timestamp):
  try:
    return pd.to_datetime(timestamp, unit="s", utc=True).tz_convert("Europe/Istanbul").strftime("%d.%m.%Y %H:%M")
  except Exception:
    return "zaman bilgisi yok"


# --- 2 YÖNLÜ SENKRONİZE ARAMA FORMU ---
with st.form(key="global_search_form", clear_on_submit=False):
  col1, col2 = st.columns([3, 1])
  with col1:
    # Arama kutusu session_state ile reaktif olarak senkronize edilir
    search_query = st.text_input(
        "🔍 Dünyanın Herhangi Bir Yerini Ara (Ülke, Şehir, Mahalle) veya Haritadan Seç",
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
      # Arama sonrası st_folium eski last_clicked değerini geri döndürebilir.
      # İlk eski tıklamayı tüket; yeni kullanıcı tıklamalarını normal işle.
      st.session_state["_ignore_main_map_click_once"] = True
      st.session_state["_pending_main_search_value"] = location.address
      st.session_state["_pending_main_search"] = True
      st.success(f"Bulunan Konum: {location.address}")
      st.rerun()
    else:
      st.error("Aradığınız konum küresel harita veritabanında bulunamadı.")
  except Exception as e:
    show_service_issue("Global konum araması tamamlanamadı.", e)

# --- KUSURSUZ İNTERAKTİF HARİTA (ÇİFT YÖNLÜ SENKRONİZASYON) ---
st.markdown("#### Harita katmanları")
layer_col1, layer_col2 = st.columns(2)
with layer_col1:
  show_observed_radar = st.checkbox("Mevcut yağış radarı", value=False, key="main_show_observed_radar")
with layer_col2:
  show_radar_nowcast = st.checkbox("Kısa vadeli yağış tahmini", value=False, key="main_show_radar_nowcast")
st.caption("Harita varsayılan olarak sade açılır. İstediğin katmanı ayrıca etkinleştirebilirsin.")

m = folium.Map(
    location=[st.session_state.lat, st.session_state.lon],
    zoom_start=st.session_state.zoom,
    tiles="OpenStreetMap",
    control_scale=True,
)

Fullscreen(position="topright").add_to(m)
LocateControl(position="topleft", strings={"title": "Konumuma Git"}).add_to(m)

if show_observed_radar or show_radar_nowcast:
  try:
    radar_layers = get_rainviewer_layers()
    if show_observed_radar:
      if radar_layers.get("observed_url"):
        TileLayer(tiles=radar_layers["observed_url"], attr='Weather data by <a href="https://www.rainviewer.com/">RainViewer</a>',
                  name="Mevcut yağış radarı", overlay=True, opacity=0.62,
                  max_native_zoom=7, max_zoom=18, show=True).add_to(m)
        st.caption("Radar gözlem zamanı: " + _format_radar_timestamp(radar_layers.get("observed_time")))
      else:
        st.info("RainViewer şu anda kullanılabilir gözlem radarı karesi sunmuyor.")
    if show_radar_nowcast:
      if radar_layers.get("forecast_url"):
        TileLayer(tiles=radar_layers["forecast_url"], attr='Forecast by <a href="https://www.rainviewer.com/">RainViewer</a>',
                  name="Kısa vadeli yağış tahmini", overlay=True, opacity=0.58,
                  max_native_zoom=7, max_zoom=18, show=True).add_to(m)
        st.caption("Tahmin katmanı RainViewer nowcast ürünüdür; uzun vadeli hava tahmini değildir. Zaman: " + _format_radar_timestamp(radar_layers.get("forecast_time")))
      else:
        st.info("RainViewer bu istekte kısa vadeli nowcast karesi yayımlamamış. Bu, konum verisinin bozuk olduğu anlamına gelmez; harita tahmin karosu şu anda sağlayıcıdan alınamıyor. Mevcut yağış için ‘Mevcut yağış radarı’nı açabilir, seçili konumun saatlik yağış tahminini aşağıdaki hava tahmini bölümünden inceleyebilirsin.")
  except Exception as radar_exc:
    show_service_issue("Yağış radarı katmanı alınamadı; ana harita ve diğer tahminler kullanılabilir.", radar_exc)

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
  if not clicked and st.session_state.pop("_ignore_main_map_click_once", False):
    # Component refreshed without an old click payload; don't ignore next real click.
    st.session_state["_main_map_last_click"] = None
  if clicked:
    clicked_lat = float(clicked["lat"])
    clicked_lon = float(clicked["lng"])
    click_fingerprint = (round(clicked_lat, 5), round(clicked_lon, 5))
    if st.session_state.pop("_ignore_main_map_click_once", False):
      # Stale component click after text search: record it, don't change location.
      st.session_state["_main_map_last_click"] = click_fingerprint
    elif click_fingerprint != st.session_state.get("_main_map_last_click"):
      st.session_state["_main_map_last_click"] = click_fingerprint
      st.session_state.lat = clicked_lat
      st.session_state.lon = clicked_lon
      st.session_state.zoom = map_data.get("zoom", st.session_state.zoom)

      # Reverse geocoding; coordinates are a safe fallback if Nominatim fails.
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
      st.session_state["_pending_main_search_value"] = adres_str
      st.session_state["_pending_main_search"] = True
      st.rerun()

def degrees_to_cardinal(deg):
  if deg is None or pd.isna(deg): return "Veri yok"
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
        cached_f, cached_df, cached_status, cached_errors, cache_meta = load_weather_from_backup_cache(request_lat, request_lon)
        if cached_f is not None and cached_df is not None:
          f_data, df, model_status, model_errors = cached_f, cached_df, cached_status, cached_errors
          model_status = dict(model_status or {})
          model_status["_cache_metadata"] = cache_meta
          used_fallback_data = True
        else:
          raise

      current_payload = f_data.get("current") or {}
      current_data = {key: (_safe_number(current_payload.get(key), default=float("nan")) if current_payload.get(key) is not None else None)
                      for key in CURRENT_VARIABLES if key in current_payload}
      # CAPE is an hourly forecast field, not requested as a current condition.
      hourly_payload = f_data.get("hourly") or {}
      if "cape" in hourly_payload and hourly_payload.get("time"):
        try:
          target_now = pd.to_datetime(current_payload.get("time") or hourly_payload["time"][0])
          hourly_times = pd.to_datetime(hourly_payload["time"])
          nearest_idx = int(abs(hourly_times - target_now).argmin())
          cape_values = hourly_payload.get("cape", [])
          current_data["cape"] = _safe_number(cape_values[nearest_idx], default=float("nan")) if nearest_idx < len(cape_values) and cape_values[nearest_idx] is not None else None
        except Exception:
          current_data["cape"] = None
      current_data = {k: (None if v is None or pd.isna(v) else float(v)) for k, v in current_data.items()}
      local_time_text = current_payload.get("time") or (f_data.get("hourly") or {}).get("time", [None])[0]
      today = pd.to_datetime(local_time_text).date() if local_time_text else date.today()
      current_wind = current_data.get("wind_speed_10m")
      current_gust = current_data.get("wind_gusts_10m")
      current_snow = current_data.get("snowfall")
      current_cape = current_data.get("cape")

      archive_end_date = today - timedelta(days=1)
      start_date = today - timedelta(days=7)
      archive_error = None
      try:
        df_archive = fetch_archive_data(request_lat, request_lon, start_date.isoformat(), archive_end_date.isoformat())
      except Exception as archive_exc:
        df_archive = pd.DataFrame()
        archive_error = str(archive_exc)[:240]
      else:
        archive_error = None

      csv_data = df.to_csv(index=False).encode("utf-8")
    except Exception as exc:
      weather_error = str(exc)
      df = pd.DataFrame()
      df_archive = pd.DataFrame()
      archive_error = "Ana tahmin verisi alınamadığı için arşiv sorgulanmadı."
      current_data = {}
      current_wind = current_gust = current_snow = current_cape = None
      csv_data = b""

  if weather_error is None:
    cache_meta = (model_status or {}).get("_cache_metadata") if isinstance(model_status, dict) else None
    if cache_meta:
      age_hours = cache_meta.get("age_seconds", 0) / 3600
      st.warning(f"⚠️ Canlı API verisi alınamadı. {age_hours:.1f} saat önce kaydedilmiş yedek veri gösteriliyor; sonuçlar güncel olmayabilir.")
    elif used_fallback_data:
      st.warning("⚠️ Yedek önbellek verisi gösteriliyor; güncellik doğrulanamadı.")
    df_3days = df.head(72)

    st.markdown("### ⏰ Seçilen Konum İçin Saat Bazlı Erken Uyarı & Kritik Saatler")
    saatlik_tehlikeler = konum_saatlik_tehlike_taramasi(df_3days)
    
    risk_fields = [c for c in ("precipitation", "wind_gusts_10m", "cape") if c in df_3days.columns and pd.to_numeric(df_3days[c], errors="coerce").notna().any()]
    expected_risk_fields = ("precipitation", "wind_gusts_10m", "cape")
    available_risk_fields = [c for c in expected_risk_fields if c in df_3days.columns and pd.to_numeric(df_3days[c], errors="coerce").notna().any()]
    if not saatlik_tehlikeler and len(available_risk_fields) == 0:
      st.info("⚪ **Saatlik risk durumu bilinmiyor:** Önümüzdeki 72 saat için risk parametrelerinden sayısal veri alınamadı.")
    elif not saatlik_tehlikeler and len(available_risk_fields) < len(expected_risk_fields):
      st.info(f"⚪ **Kısmi meteorolojik veri:** Kullanılabilir {len(available_risk_fields)}/{len(expected_risk_fields)} risk parametresi var; görünen verilerde eşik aşımı bulunmadı. Eksik alanlar nedeniyle tüm riskler dışlanamaz.")
    elif not saatlik_tehlikeler:
      st.info("ℹ️ **Taranan verilerde belirgin eşik aşımı saptanmadı:** Bu, resmî uyarı olmadığı veya risk bulunmadığı anlamına gelmez.")
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
      elif seviye == "bilgi": st.info(mesaj)
      else: st.warning(mesaj)

    st.markdown("### 📅 Önümüzdeki 3 Günün Günlük Kırılımlı Hava Tahmini ve Şiddet Analizi")
    if not df_3days.empty:
      df_3days["date"] = df_3days["time"].dt.date
      unique_dates = df_3days["date"].unique()[:3]
      cols_day = st.columns(len(unique_dates) if len(unique_dates) > 0 else 1)
      
      for idx, d in enumerate(unique_dates):
        day_df = df_3days[df_3days["date"] == d]
        t_series = pd.to_numeric(day_df["temperature_2m"], errors="coerce").dropna() if "temperature_2m" in day_df.columns else pd.Series(dtype=float)
        d_min_t = t_series.min() if not t_series.empty else None
        d_max_t = t_series.max() if not t_series.empty else None
        precip_series = pd.to_numeric(day_df["precipitation"], errors="coerce").dropna() if "precipitation" in day_df.columns else pd.Series(dtype=float)
        snow_series = pd.to_numeric(day_df["snowfall"], errors="coerce").dropna() if "snowfall" in day_df.columns else pd.Series(dtype=float)
        d_precip = float(precip_series.sum()) if not precip_series.empty else None
        max_hourly_p = float(precip_series.max()) if not precip_series.empty else None
        # Open-Meteo snowfall alanı cm cinsindedir; ekranda kar birikim derinliği mm'ye çevrilir.
        d_snow_cm = float(snow_series.sum()) if not snow_series.empty else None
        d_snow_mm = d_snow_cm * 10.0 if d_snow_cm is not None else None
        d_wind = day_df["wind_gusts_10m"].max() if "wind_gusts_10m" in day_df.columns else (day_df["wind_speed_10m"].max() if "wind_speed_10m" in day_df.columns else None)

        if max_hourly_p is None:
          yagis_durum_str = "Veri yok"
        elif max_hourly_p >= 7.0:
          yagis_durum_str = f"🔴 Şiddetli saatlik yağış sinyali (en yüksek saat: {max_hourly_p:.1f} mm/saat)"
        elif max_hourly_p >= 5.0:
          yagis_durum_str = "🟠 Kuvvetli saatlik yağış sinyali"
        elif d_precip is not None and d_precip > 1.0:
          yagis_durum_str = "🟡 Hafif/normal yağış"
        else:
          yagis_durum_str = "🟢 Düşük yağış" if d_precip is not None else "Veri yok"

        if d_snow_mm is None:
          kar_durum_str = "Veri yok"
        elif d_snow_mm <= 0:
          kar_durum_str = "🟢 Kar yağışı beklenmiyor"
        elif d_snow_mm <= 5:
          kar_durum_str = "🟡 Düşük kar birikimi"
        elif d_snow_mm <= 20:
          kar_durum_str = "🟠 Hafif kar birikimi"
        else:
          kar_durum_str = "🔴 Çok kar birikimi"

        gun_ismi = pd.to_datetime(d).strftime('%A')
        gun_tr = {"Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba", "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar"}.get(gun_ismi, gun_ismi)

        with cols_day[idx]:
          with st.container(border=True):
            st.markdown(f"**📌 {d} ({gun_tr})**")
            st.metric(label="Min / Max Sıcaklık (Best Match)", value=(f"{d_min_t:.1f}°C / {d_max_t:.1f}°C" if d_min_t is not None and d_max_t is not None else "Veri yok"))
            st.metric(label="Yağış Durumu", value=yagis_durum_str)
            st.metric(label="Beklenen Toplam Yağış", value=(f"{d_precip:.1f} mm" if d_precip is not None else "Veri yok"))
            st.metric(label="Kar Yağışı Durumu", value=kar_durum_str)
            st.metric(label="Beklenen Kar Birikimi", value=(f"{d_snow_mm:.1f} mm" if d_snow_mm is not None else "Veri yok"))
            st.metric(label="Maksimum Rüzgâr Hamlesi", value=(f"{d_wind:.1f} km/sa" if d_wind is not None and pd.notna(d_wind) else "Veri yok"))

    if current_data:
      st.markdown("### ⚡ Anlık Meteorolojik Durum")
      wind_deg = current_data.get("wind_direction_10m")
      wind_cardinal = degrees_to_cardinal(wind_deg)
      curr_col1, curr_col2, curr_col3, curr_col4, curr_col5 = st.columns(5)
      def _fmt_metric(value, suffix="", digits=1):
        return f"{value:.{digits}f}{suffix}" if value is not None and pd.notna(value) else "Veri yok"
      with curr_col1: st.metric(label="Sıcaklık", value=_fmt_metric(current_data.get("temperature_2m"), " °C"))
      with curr_col2: st.metric(label="Hissedilen", value=_fmt_metric(current_data.get("apparent_temperature"), " °C"))
      with curr_col3: st.metric(label="Bağıl Nem", value=_fmt_metric(current_data.get("relative_humidity_2m"), " %"))
      with curr_col4: st.metric(label="Rüzgâr Hızı / Hamle", value=f"{_fmt_metric(current_wind)} / {_fmt_metric(current_gust)} km/sa")
      with curr_col5: st.metric(label="Rüzgâr Yönü / Yakın Saat CAPE", value=f"{wind_cardinal} / {_fmt_metric(current_cape, ' J/kg', 0)}")

    st.markdown("### 📊 Detaylı Model Tahminleri ve Uzman Raporu")
    tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9 = st.tabs([
        "🇹🇷 Türkiye Hava Panoraması", "🤝 3\'lü Model Konsensüsü", "🕰️ Geçmiş Arşiv", "📄 Uzman Bülteni",
        "🌡️ Sıcaklık & Nem", "💨 Hassas Rüzgar & Hamle", "☁️ Basınç & Bulut",
        "❄️ Yağış, Kar & Olasılık", "📋 7 Günlük Hava Özeti & İndirme", "🚨 Türkiye Geneli Erken Uyarı",
    ])

    with tab9:
      st.subheader("🚨 Türkiye Geneli Erken Uyarı Masası")
      st.caption("81 il merkezi tahmini üzerinden eşik tabanlı otomatik tarama. Model desteği, olayın gerçekleştiği günün ECMWF IFS / GFS / ICON günlük tahminleriyle karşılaştırılır. ‘Risk eşiği desteklenmiyor’ model verisinin bulunduğunu ancak ilgili eşiğin aşılmadığını; ‘veri alınamadı’ ise karşılaştırma verisinin eksik olduğunu belirtir. Bu sistem MGM/AFAD resmî uyarısı değildir; yerel koşullar farklılık gösterebilir.")
      st.caption(f"Veri zamanı: {ews_result.get('national', {}).get('updated_at', 'bilinmiyor')} · Kapsam: {len(ews_result.get('successful_centers', []))}/81 il")
      risk_alerts = ews_result.get("alerts", [])
      if risk_alerts:
        # Group same city/event to reduce repetitive clutter; keep highest severity first.
        severity_order = {"Yüksek": 0, "Orta": 1, "Düşük": 2}
        risk_alerts = sorted(risk_alerts, key=lambda a: (severity_order.get(a.get("seviye"), 9), a.get("il", ""), a.get("tip", "")))
        st.warning(f"{len(risk_alerts)} risk sinyali bulundu. Aşağıdaki liste il ve olay türüne göre düzenlenmiştir.")
        with st.expander("Risk sinyallerini görüntüle", expanded=True):
          for alert in risk_alerts[:60]:
            try:
              time_text = pd.to_datetime(alert.get("zaman")).strftime("%d.%m.%Y %H:%M")
            except Exception:
              time_text = str(alert.get("zaman", "Zaman bilinmiyor"))
            model_support = alert.get("model_uyumu", "Model desteği bilinmiyor")
            st.warning(f"**{alert.get('il','İl bilinmiyor')}** · {alert.get('tip','Risk')} ({_safe_number(alert.get('deger'), default=0):.1f} {alert.get('birim','')}) · {time_text} · {alert.get('seviye','Belirsiz')} · {model_support}")
          if len(risk_alerts) > 60:
            st.caption(f"İlk 60 kayıt gösteriliyor; toplam {len(risk_alerts)} sinyal mevcut.")
      elif not ews_result.get("successful_centers"):
        st.info("Türkiye geneli risk durumu bilinmiyor: veri alınan il merkezi yok.")
      elif ews_result.get("failed_centers"):
        st.info(f"Kısmi veri: {len(ews_result.get('successful_centers', []))} il kontrol edildi; {len(ews_result.get('failed_centers', []))} ilde veri alınamadı. Bu, ülke genelinde risk yok demek değildir.")
      else:
        st.success("Verisi alınan il merkezlerinde tanımlı eşikler aşılmadı. Bu, risk bulunmadığının veya resmî uyarı olmadığının garantisi değildir.")
      st.markdown("#### Bölgelere göre risk özeti")
      regional_data = ews_result.get("regional", {})
      if regional_data:
        regional_view = []
        for region, info in regional_data.items():
          regional_view.append({"Bölge": region, "Kontrol edilen il": f"{info.get('checked', 0)}/{info.get('expected', 0)}", "Durum": info.get("risk_status", "Bilinmiyor"), "Riskli il sayısı": info.get("alert_city_count", 0), "Yüksek sinyal": info.get("high_alerts", 0), "En düşük sıcaklık": f"{info['min_temp']:.1f} °C" if info.get("min_temp") is not None else "Veri yok", "En yüksek yağış": f"{info['max_rain']:.1f} mm" if info.get("max_rain") is not None else "Veri yok"})
        st.dataframe(pd.DataFrame(regional_view), hide_index=True, use_container_width=True)
      with st.expander("MGM resmî uyarılarına git"):
        st.markdown("Resmî uyarı ve duyurular için [MGM MeteoUYARI haritasını aç](https://www.mgm.gov.tr/meteouyari/turkiye.aspx).")

    with tab0:
      st.subheader("🇹🇷 Türkiye Hava Panoraması · 81 İl Merkezi")
      with st.expander("Bu bölüm nasıl çalışıyor?", expanded=True):
        st.markdown("""
        Bu ekran, Türkiye'deki **81 ilin merkez koordinatları** için hava tahminlerini toplar ve ülke genelini karşılaştırır. İlçelerin tamamını veya her ilin dağlık/kıyı mikroklimasını temsil etmez.

        - **Verisi alınan il:** Kaynaktan geçerli tahmin dönen il merkezi sayısıdır; 81'e ulaşmıyorsa kapsam eksiktir.
        - **Sıcaklık ve yağış göstergeleri:** Seçilen tahmin gününün günlük en yüksek/en düşük sıcaklık ve yağış toplamı gibi değerlerini özetler.
        - **Bölgesel durum:** İlleri yedi coğrafi bölge altında toplar; risk sinyalleri eşik tabanlıdır, resmî MGM uyarısı değildir.
        - **Model karşılaştırması:** ECMWF, GFS ve ICON verileri mevcutsa tahminlerin ortalamasını ve aralarındaki farkı gösterir. Modellerin ayrışması belirsizliğin arttığına işaret eder.
        - **Harita:** Renkli noktalar il merkezlerindeki tahmin değerlerini, yoğunluk katmanı ise yalnızca bu noktaların görsel dağılımını gösterir.
        - **Günlük özet:** Verisi bulunan iller arasındaki en yüksek sıcaklık, en düşük sıcaklık ve en yüksek yağış gibi öne çıkan değerleri otomatik metne dönüştürür.

        Veriler önbellek süresi boyunca tekrar kullanılabilir; bu nedenle her sayfa yenilemesinde mutlaka yeni model çalıştırması yapılmaz. Bu bölüm genel görünüm ve karar desteğidir; resmî uyarılar için MGM/AFAD duyuruları esas alınmalıdır.
        """)
      national = ews_result.get("national", {})
      national_rows = national.get("rows", [])
      st.caption("İl merkezleri için üç günlük tahminin ilk günü gösterilir. Veriler 15 dakika önbelleklenir; yenileme sonrası kaynak güncelse yeni tahmin alınır.")
      if national_rows:
        ndf = pd.DataFrame(national_rows)
        c1,c2,c3,c4=st.columns(4)
        temps=pd.to_numeric(ndf["temperature_2m_max"],errors="coerce").dropna()
        rains=pd.to_numeric(ndf["precipitation_sum"],errors="coerce").dropna()
        snow_count=int((pd.to_numeric(ndf["snowfall_sum"],errors="coerce").fillna(0)>0).sum())
        rain_count=int((pd.to_numeric(ndf["precipitation_sum"],errors="coerce").fillna(0)>=1).sum())
        c1.metric("Verisi alınan il",f"{len(national_rows)}/81")
        c2.metric("En yüksek gündüz sıcaklığı",f"{temps.max():.1f} °C" if not temps.empty else "Veri yok")
        c3.metric("Yağış beklenen il",f"{rain_count}")
        c4.metric("Kar sinyali bulunan il",f"{snow_count}")
        st.markdown("#### Bölgesel durum")
        reg=ews_result.get("regional",{})
        reg_rows=[]
        for region,info in reg.items():
          reg_rows.append({"Bölge":region,"Veri kapsamı":f"{info['checked']}/{info['expected']}","Durum":info.get("risk_status","Bilinmiyor"),"İl sayısı riskli":info.get("alert_city_count",0),"Yüksek risk sinyali":info.get("high_alerts",0),"En düşük tahmin":f"{info['min_temp']:.1f} °C" if info['min_temp'] is not None else "Veri yok","En yüksek il yağışı":f"{info['max_rain']:.1f} mm" if info['max_rain'] is not None else "Veri yok","Model ayrışması":info.get("model_agreement",{}).get("Belirgin ayrışma",0)})
        st.dataframe(pd.DataFrame(reg_rows),hide_index=True,use_container_width=True)
        st.markdown("#### İl bazında sıcaklık ve yağış")
        sort_col=st.selectbox("Sıralama",["temperature_2m_max","temperature_2m_min","precipitation_sum","wind_gusts_10m_max"],format_func=lambda x:{"temperature_2m_max":"En yüksek sıcaklık","temperature_2m_min":"En düşük sıcaklık","precipitation_sum":"Toplam yağış","wind_gusts_10m_max":"Rüzgâr hamlesi"}[x],key="national_sort")
        # Seçilen sıralama alanı min/max sıcaklık listesinde tekrar edebiliyordu.
        # Pandas duplicate column label ile sort_values çağrısını reddeder; sütunları tekilleştir.
        base_columns=["il","temperature_2m_min","temperature_2m_max","precipitation_sum","max_rain_3h_mm","max_rain_6h_mm","precipitation_probability_max","snowfall_sum","freezing_level_height_min_m","snowfall_height_min_m","wind_gusts_10m_max"]
        view_columns=["il"] + ([sort_col] if sort_col in ndf.columns else []) + [c for c in base_columns if c != "il" and c != sort_col and c in ndf.columns]
        view=ndf.loc[:, view_columns].copy()
        if sort_col in view.columns:
          view=view.sort_values(sort_col,ascending=(sort_col=="temperature_2m_min"),na_position="last")
        column_labels={"il":"İl","temperature_2m_min":"Min °C","temperature_2m_max":"Maks °C","precipitation_sum":"Yağış mm/gün","max_rain_3h_mm":"Maks 3 saat mm","max_rain_6h_mm":"Maks 6 saat mm","precipitation_probability_max":"Yağış olasılığı %","snowfall_sum":"Kar cm","freezing_level_height_min_m":"En düşük donma seviyesi m","snowfall_height_min_m":"En düşük kar yağışı seviyesi m","wind_gusts_10m_max":"Rüzgâr hamlesi km/sa"}
        view=view.rename(columns=column_labels)
        st.dataframe(view,hide_index=True,use_container_width=True)
        st.markdown("#### Türkiye haritası · il bazında sıcaklık ve hava verileri")
        map_metric=st.selectbox("Haritada gösterilecek sıcaklık",["temperature_2m_max","temperature_2m_min"],
          format_func=lambda x:"Gündüz en yüksek sıcaklık" if x=="temperature_2m_max" else "Gece/sabah en düşük sıcaklık",key="v20_turkey_map_metric")
        # Harita tarihi, tahmin satırından alınır; sistem tarihine sabitlenmez.
        # Böylece tahmin günü her yeni günlük veriyle otomatik ilerler.
        map_forecast_date = national_rows[0].get("tarih") if national_rows else None
        try:
          parsed_map_date = pd.to_datetime(map_forecast_date)
          tr_weekdays = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
          map_forecast_date_label = f"{parsed_map_date.strftime('%d.%m.%Y')} {tr_weekdays[parsed_map_date.weekday()]}"
        except Exception:
          map_forecast_date_label = str(map_forecast_date or "Tarih bilgisi alınamadı")
        map_fetch_time = national.get("updated_at", "bilinmiyor")
        map_metric_label = ("Günlük en yüksek sıcaklık" if map_metric == "temperature_2m_max"
                            else "Günlük en düşük sıcaklık")
        st.info(
          f"🗓️ **Tahmin tarihi:** {map_forecast_date_label}  ·  "
          f"🕒 **Verinin kaynaktan alınma zamanı:** {map_fetch_time} (Türkiye saati)  ·  "
          f"🌡️ **Harita gösterimi:** {map_metric_label}. Günlük ortalama sıcaklık gösterilmez."
        )
        turkey_map=build_turkey_temperature_map(national_rows,map_metric)
        if turkey_map is not None:
          st_folium(turkey_map,width=None,height=650,key="v20_turkey_temperature_map")
          st.caption("Harita Türkiye'ye odaklanır. Renkli il noktasına tıklayarak sıcaklık, yağış, kar ve rüzgâr ayrıntılarını görebilirsin. Yoğunluk katmanı haritanın katman menüsünden isteğe bağlı açılır. Veriler il merkezlerini temsil eder.")
        st.markdown("#### ECMWF / GFS / ICON model karşılaştırması")
        mc=ews_result.get("model_comparison",{})
        cov=mc.get("model_coverage",{})
        if cov:
          mcols=st.columns(3)
          for i,(model_name,count) in enumerate(cov.items()):
            mcols[i].metric(model_name, f"{count}/81 il")
          mdf=pd.DataFrame(mc.get("rows",[]))
          if not mdf.empty:
            mdf=mdf[mdf["model_count"]>0].copy()
            if not mdf.empty:
              mdf["temperature_2m_max_model_mean"]=pd.to_numeric(mdf["temperature_2m_max_model_mean"],errors="coerce").round(1)
              mdf["temperature_2m_max_model_spread"]=pd.to_numeric(mdf["temperature_2m_max_model_spread"],errors="coerce").round(1)
              mdf["precipitation_sum_model_mean"]=pd.to_numeric(mdf["precipitation_sum_model_mean"],errors="coerce").round(1)
              mdf["precipitation_sum_model_spread"]=pd.to_numeric(mdf["precipitation_sum_model_spread"],errors="coerce").round(1)
              mview=mdf[["il","model_count","temperature_2m_max_model_mean","temperature_2m_max_model_spread","precipitation_sum_model_mean","precipitation_sum_model_spread","agreement"]].copy()
              mview.columns=["İl","Model sayısı","Ort. gündüz °C","Sıcaklık farkı °C","Ort. yağış mm","Yağış farkı mm","Model uyumu"]
              st.dataframe(mview,hide_index=True,use_container_width=True)
              diverging=mdf[mdf["agreement"]=="Belirgin ayrışma"]["il"].tolist()
              if diverging:
                st.warning("Modellerin belirgin ayrıştığı iller: "+", ".join(diverging[:20])+ ("…" if len(diverging)>20 else "")+". Bu bölgelerde tahmin belirsizliği daha yüksek olabilir.")
              else:
                st.info("Verisi alınan il merkezlerinde model ayrışması belirgin eşiklere ulaşmadı; bu kesinlik garantisi değildir.")
          if mc.get("model_errors"):
            with st.expander("Model veri hataları / eksik kapsam"):
              for model_name, errs in mc["model_errors"].items():
                st.write(f"**{model_name}**")
                for err in errs: st.write(f"{', '.join(err['iller'])}: {err['hata']}")
        else:
          model_error_text = str(mc.get("model_errors", {}))
          if _is_temporary_service_problem(model_error_text):
            show_service_issue("Türkiye geneli çoklu model karşılaştırması şu anda alınamadı. Tek model tahmini çoklu model konsensüsü olarak sunulmadı.", model_error_text)
          else:
            st.info("Model karşılaştırması şu anda alınamadı. Tek model tahmini çoklu model konsensüsü olarak sunulmaz.")

        st.markdown("#### Günün Türkiye özeti")
        hottest=ndf.loc[pd.to_numeric(ndf["temperature_2m_max"],errors="coerce").idxmax()] if temps.size else None
        coldest=ndf.loc[pd.to_numeric(ndf["temperature_2m_min"],errors="coerce").idxmin()] if pd.to_numeric(ndf["temperature_2m_min"],errors="coerce").notna().any() else None
        wettest=ndf.loc[pd.to_numeric(ndf["precipitation_sum"],errors="coerce").idxmax()] if rains.size else None
        bullets=[]
        if hottest is not None: bullets.append(f"En yüksek gündüz sıcaklığı {hottest['il']} çevresinde {hottest['temperature_2m_max']:.1f} °C.")
        if coldest is not None: bullets.append(f"En düşük gece/sabah sıcaklığı {coldest['il']} çevresinde {coldest['temperature_2m_min']:.1f} °C.")
        if wettest is not None: bullets.append(f"En yüksek günlük yağış toplamı {wettest['il']} için {wettest['precipitation_sum']:.1f} mm.")
        bullets.append(f"{rain_count} il merkezinde en az 1 mm yağış; {snow_count} il merkezinde kar sinyali bulunuyor.")
        st.info(" ".join(bullets))
        st.markdown("#### 🏛️ MGM resmî Türkiye uyarıları")
        mgm_national_warning = fetch_mgm_official_warnings()
        if mgm_national_warning.get("status") == "active":
          st.warning(mgm_national_warning.get("text", "MGM uyarısı yayımlandı."))
        elif mgm_national_warning.get("status") == "none":
          st.info(mgm_national_warning.get("text", "MGM günlük tahmin raporunun uyarı bölümünde güncel uyarı bulunmadığı açıkça belirtiliyor."))
        else:
          mgm_text = str(mgm_national_warning.get("text", ""))
          if _is_temporary_service_problem(mgm_text):
            show_service_issue("MGM resmî uyarı metni şu anda alınamadı; uyarı var/yok şeklinde çıkarım yapılmadı.", mgm_text)
          else:
            st.info("MGM resmî uyarı metni şu anda alınamadı; uyarı var/yok şeklinde çıkarım yapılmadı. [MGM MeteoUYARI haritasını aç](https://www.mgm.gov.tr/meteouyari/turkiye.aspx)")
        national_alerts=turkey_national_alerts(national)
        st.markdown("#### 81 il erken uyarı taraması")
        if national_alerts:
          for a in national_alerts[:30]: st.warning(f"**{a['il']} · {a['tip']}** — {a['deger']:.1f} {a['birim']} · {pd.to_datetime(a['zaman']).strftime('%d.%m %H:%M')} · {a['seviye']} seviye · {a.get('model_uyumu','Model uyumu hesaplanmadı')}")
          if len(national_alerts)>30: st.caption(f"İlk 30 kayıt gösteriliyor; toplam {len(national_alerts)} risk sinyali bulundu.")
        else: st.success("Mevcut verisi alınan il merkezlerinde tanımlı erken uyarı eşikleri aşılmadı. Bu, risk bulunmadığının veya resmî uyarı olmadığının garantisi değildir.")
        svg=build_turkey_share_svg(national_rows,pd.Timestamp.now(tz="Europe/Istanbul").strftime("%d.%m.%Y"))
        st.download_button("📥 Günlük Türkiye hava görselini indir (SVG)",data=svg,file_name="meteoscope_turkiye_gunluk_hava.svg",mime="image/svg+xml",disabled=not bool(svg))
        st.caption(f"Son başarılı veri derleme: {national.get('updated_at','bilinmiyor')} · Kapsama: %{national.get('coverage',0)*100:.0f} · Kaynak: Open-Meteo. Harita/özet il merkezlerine dayalı otomatik model derlemesidir; MGM resmî uyarılarının yerine geçmez.")
        if national.get("errors"):
          with st.expander(f"Veri alınamayan il grupları ({len(national['errors'])})"):
            for err in national['errors']: st.write(f"{', '.join(err['iller'])}: {err['hata']}")
      else:
        national_errors = national.get("errors", []) if isinstance(national, dict) else []
        error_text = " ".join(str(item.get("hata", "")) for item in national_errors)
        if _is_temporary_service_problem(error_text):
          show_service_issue("Türkiye geneli tahmin verileri alınamadı. Eksik veriyle ülke geneli sonucu üretilmedi.", error_text)
        else:
          st.warning("Türkiye geneli tahmin verileri şu anda alınamadı. Eksik veriyle ülke geneli sonucu üretilmedi; lütfen daha sonra yeniden deneyin.")

    with tab1:
      st.subheader("🌐 Küresel Modeller Arası Karşılaştırma")
      for model_name, suffix, _ in FORECAST_MODELS:
        status = model_status.get(model_name) if isinstance(model_status, dict) else None
        if status:
          st.success(f"{model_name}: erişilebilir · {status.get('available_fields', 0)}/{status.get('requested_fields', len(HOURLY_VARIABLES))} alan")
        else:
          st.warning(f"{model_name}: kullanılamıyor · {(model_errors or {}).get(model_name, 'Model yanıtı alınamadı')}")
      model_temp_cols = [f"temperature_2m_{suffix}" for _, suffix, _ in FORECAST_MODELS if f"temperature_2m_{suffix}" in df.columns and df[f"temperature_2m_{suffix}"].notna().any()]
      if model_temp_cols:
        model_values = df.set_index("time")[model_temp_cols].apply(pd.to_numeric, errors="coerce")
        display_names = {"ECMWF_IFS": "ECMWF IFS", "GFS": "GFS", "ICON": "ICON"}
        model_values = model_values.rename(columns={c: next((display_names[suffix] for _, suffix, _ in FORECAST_MODELS if c == f"temperature_2m_{suffix}"), c) for c in model_temp_cols})
        if len(model_temp_cols) >= 2:
          consensus = model_values.copy()
          consensus["Modeller ortalaması"] = model_values.mean(axis=1, skipna=True)
          consensus["Model en düşük"] = model_values.min(axis=1, skipna=True)
          consensus["Model en yüksek"] = model_values.max(axis=1, skipna=True)
          consensus = consensus.dropna(how="all")
          st.caption(f"Ortalama ve aralık, yalnızca veri döndüren {len(model_temp_cols)} bağımsız modelin sıcaklık tahminlerinden hesaplanır. Best Match bu hesaba katılmaz.")
          if not consensus.empty:
            st.line_chart(consensus)
            spread = (model_values.max(axis=1) - model_values.min(axis=1)).dropna()
            if not spread.empty:
              st.metric("Modeller arası en yüksek sıcaklık farkı", f"{spread.max():.1f} °C")
        else:
          st.warning("Yalnızca bir bağımsız modelden veri geldi; bu nedenle çoklu model konsensüsü hesaplanmadı.")
          st.line_chart(model_values.dropna(how="all"))
      else:
        st.info("ECMWF, GFS veya ICON modellerinden karşılaştırılabilir sıcaklık verisi alınamadı. Aşağıdaki diğer sekmelerde Best Match tahmini gösterilir.")

    with tab2:
      st.subheader("Son 7 Tam Günün Arşiv Verileri")
      if not df_archive.empty:
        archive_cols = [c for c in ("temperature_2m", "precipitation") if c in df_archive.columns]
        if archive_cols:
          st.line_chart(df_archive.set_index("time")[archive_cols])
        else:
          st.info("Arşiv yanıtında çizilecek sıcaklık/yağış alanı bulunamadı.")
        st.dataframe(df_archive, use_container_width=True)
      else:
        st.info("Arşiv verisi bulunamadı. " + (archive_error or "API bu tarih aralığı için boş yanıt verdi."))

    with tab3:
      st.subheader("📄 Meteorolojik Bülten ve Uzman Kurallarına Dayalı Hazırlık Önerileri")
      rep_temp = pd.to_numeric(df_3days["temperature_2m"], errors="coerce").dropna() if "temperature_2m" in df_3days.columns else pd.Series(dtype=float)
      rep_min = float(rep_temp.min()) if not rep_temp.empty else None
      rep_max = float(rep_temp.max()) if not rep_temp.empty else None
      rep_precip_series = pd.to_numeric(df_3days["precipitation"], errors="coerce").dropna() if "precipitation" in df_3days.columns else pd.Series(dtype=float)
      rep_snow_series = pd.to_numeric(df_3days["snowfall"], errors="coerce").dropna() if "snowfall" in df_3days.columns else pd.Series(dtype=float)
      rep_prob_series = pd.to_numeric(df_3days["precipitation_probability"], errors="coerce").dropna() if "precipitation_probability" in df_3days.columns else pd.Series(dtype=float)
      rep_precip = float(rep_precip_series.sum()) if not rep_precip_series.empty else None
      rep_snow = float(rep_snow_series.sum()) if not rep_snow_series.empty else None
      # Open-Meteo snowfall cm verir; kullanıcıya kar birikim derinliğini mm olarak göster.
      rep_snow_mm = rep_snow * 10.0 if rep_snow is not None else None
      if rep_snow_mm is None:
        rep_snow_status = "Veri yok"
      elif rep_snow_mm <= 0:
        rep_snow_status = "Kar yağışı beklenmiyor"
      elif rep_snow_mm <= 5:
        rep_snow_status = "Düşük"
      elif rep_snow_mm <= 20:
        rep_snow_status = "Hafif"
      else:
        rep_snow_status = "Çok"
      rep_wind_series = pd.to_numeric(df_3days["wind_gusts_10m"], errors="coerce").dropna() if "wind_gusts_10m" in df_3days.columns else pd.Series(dtype=float)
      if rep_wind_series.empty and "wind_speed_10m" in df_3days.columns:
        rep_wind_series = pd.to_numeric(df_3days["wind_speed_10m"], errors="coerce").dropna()
      rep_wind = float(rep_wind_series.max()) if not rep_wind_series.empty else None
      rep_prob = float(rep_prob_series.max()) if not rep_prob_series.empty else None
      # Giyim önerisi bugünün ve yarının saatlik tahminlerinden ayrı ayrı hazırlanır.
      day_forecast = df_3days.copy()
      if not day_forecast.empty and "time" in day_forecast.columns:
        day_forecast["_forecast_date"] = pd.to_datetime(day_forecast["time"], errors="coerce").dt.date
      today_df = day_forecast[day_forecast["_forecast_date"] == today] if not day_forecast.empty and "_forecast_date" in day_forecast.columns else pd.DataFrame()
      tomorrow_date = today + timedelta(days=1)
      tomorrow_df = day_forecast[day_forecast["_forecast_date"] == tomorrow_date] if not day_forecast.empty and "_forecast_date" in day_forecast.columns else pd.DataFrame()
      giyim_tavsiyesi = (
          "### 👕 Bugün ve Yarın İçin Net Giyim Tavsiyesi\n"
          + gunluk_net_giyim_tavsiyesi(today_df, "Bugün") + "\n\n"
          + gunluk_net_giyim_tavsiyesi(tomorrow_df, "Yarın")
      )
      official_warning = fetch_mgm_official_warnings()
      selected_location = st.session_state.get("location_label", "Seçilen konum")
      active_models = [name for name in ("ECMWF IFS", "GFS", "ICON") if name in model_status]

      with st.container(border=True):
        st.markdown(f"**Analiz Konumu:** {selected_location}")
        st.markdown(f"**Koordinatlar:** {st.session_state.lat:.4f}, {st.session_state.lon:.4f}")
        st.markdown(f"**Tahmin zamanı:** {local_time_text}")
        st.markdown(f"**Anlık sıcaklık / hissedilen:** {current_data.get('temperature_2m'):.1f} °C / {current_data.get('apparent_temperature'):.1f} °C" if current_data.get("temperature_2m") is not None and current_data.get("apparent_temperature") is not None else "**Anlık sıcaklık / hissedilen:** Eksik veri")
        st.markdown(f"**Önümüzdeki 3 gün toplam yağış:** {rep_precip:.1f} mm" if rep_precip is not None else "**Önümüzdeki 3 gün toplam yağış:** Veri yok")
        st.markdown(f"**Önümüzdeki 3 gün kar birikimi:** {rep_snow_mm:.1f} mm · **Durum:** {rep_snow_status}" if rep_snow_mm is not None else "**Önümüzdeki 3 gün kar birikimi:** Veri yok")
        st.markdown(f"**Önümüzdeki 3 gün sıcaklık aralığı:** {rep_min:.1f}–{rep_max:.1f} °C" if rep_min is not None and rep_max is not None else "**Önümüzdeki 3 gün sıcaklık aralığı:** Veri yok")
        st.markdown(f"**Maksimum rüzgâr hamlesi:** {rep_wind:.1f} km/sa" if rep_wind is not None else "**Maksimum rüzgâr hamlesi:** Veri yok")
        st.caption("Tahmin kaynağı: Open-Meteo Best Match. Çoklu model durumu: " + (", ".join(active_models) if active_models else "bağımsız model verisi bulunamadı; Best Match kullanılıyor."))

      st.markdown("### 🏛️ MGM Resmî Meteorolojik Uyarısı")
      if official_warning["status"] == "active":
        st.warning(official_warning["text"])
        st.caption("Kaynak: Meteoroloji Genel Müdürlüğü günlük tahmin raporu. Bu bölüm ülke geneli resmî uyarı metnini gösterir; seçilen nokta için il/ilçe kapsamını resmî MeteoUYARI haritasından ayrıca kontrol edebilirsiniz.")
      elif official_warning["status"] == "none":
        st.info(official_warning["text"])
      else:
        st.info("MGM'nin resmî uyarı metni şu anda uygulamaya alınamadı. Uyarı var/yok şeklinde tahmin yürütülmedi; güncel durumu resmî sayfadan kontrol edin. " + official_warning["text"])
      st.markdown("[MGM Resmî MeteoUYARI haritasını aç](https://www.mgm.gov.tr/meteouyari/turkiye.aspx) · [MGM günlük tahmin ve uyarı raporunu aç](https://www.mgm.gov.tr/tahmin/gunluk-tahmin.aspx)")

      st.markdown("---")
      st.markdown(giyim_tavsiyesi)
      st.caption("Giyim tavsiyesi bugünün ve yarının saatlik sıcaklık, hissedilen sıcaklık, yağış miktarı ve olasılığı, kar, rüzgâr hızı/hamlesi ve mevcutsa nem verileri birlikte değerlendirilerek hazırlanır. Eksik veriden kesin sonuç çıkarılmaz.")
      st.markdown("[MGM meteorolojik hadiseler ve MeteoUYARI açıklamaları](https://www.mgm.gov.tr/Meteouyari/meteouyari-nedir.aspx) · [MGM meteorolojik şiddet sınıflandırması](https://www.mgm.gov.tr/site/yardim1.aspx?=HadSid)")

      current_temp_text = f"{current_data['temperature_2m']:.1f} °C" if current_data.get("temperature_2m") is not None else "Veri yok"
      rep_wind_text = f"{rep_wind:.1f} km/sa" if rep_wind is not None else "Veri yok"
      rep_temp_text = f"{rep_min:.1f}–{rep_max:.1f} °C" if rep_min is not None and rep_max is not None else "Veri yok"
      rep_precip_text = f"{rep_precip:.1f} mm" if rep_precip is not None else "Veri yok"
      rep_snow_text = f"{rep_snow_mm:.1f} mm ({rep_snow_status})" if rep_snow_mm is not None else "Veri yok"
      official_text = official_warning.get("text", "Resmî uyarı alınamadı.")
      report_text = (
          "METEOROLOJİK BÜLTEN VE OTOMATİK HAZIRLIK ÖNERİLERİ\n"
          f"Konum: {selected_location}\nKoordinatlar: {st.session_state.lat:.4f}, {st.session_state.lon:.4f}\n"
          f"Tahmin zamanı: {local_time_text}\nAnlık sıcaklık: {current_temp_text}\n"
          f"Best Match 3 günlük sıcaklık aralığı: {rep_temp_text}\n"
          f"Toplam yağış (3 gün): {rep_precip_text}\nKar birikimi (3 gün): {rep_snow_text}\nMaksimum rüzgâr hamlesi: {rep_wind_text}\n"
          f"Kullanılabilen modeller: {', '.join(active_models) if active_models else 'Yalnızca Best Match'}\n\n"
          "MGM RESMÎ UYARI BİLGİSİ\n" + official_text + "\n"
          "MGM MeteoUYARI: https://www.mgm.gov.tr/meteouyari/turkiye.aspx\n"
          "MGM Günlük Tahmin: https://www.mgm.gov.tr/tahmin/gunluk-tahmin.aspx\n\n"
          + re.sub(r"\*\*", "", giyim_tavsiyesi).replace("### ", "")
      )
      st.download_button("📥 Meteorolojik Bülteni İndir", report_text.encode("utf-8"), file_name="meteo_expert_report.txt", mime="text/plain")

    with tab4:
      cols = [c for c in df.columns if "temperature_2m" in c or "relative_humidity_2m" in c]
      st.caption("Saatlik Best Match ve erişilebilen model alanları; sıcaklık °C, bağıl nem %.")
      if cols: st.line_chart(df.set_index("time")[cols])
      else: st.info("Sıcaklık/nem verisi bulunamadı.")
    with tab5:
      cols = [c for c in df.columns if "wind_speed" in c or "wind_gusts" in c]
      st.caption("Rüzgâr hızı ve hamleleri km/sa cinsindedir.")
      if cols: st.line_chart(df.set_index("time")[cols])
      else: st.info("Rüzgâr verisi bulunamadı.")
    with tab6:
      cols = [c for c in df.columns if "pressure" in c or "cloud_cover" in c]
      if cols: st.line_chart(df.set_index("time")[cols])
      else: st.info("Basınç/bulut verisi bulunamadı.")
    with tab7:
      cols = [c for c in df.columns if "precipitation" in c or "snow" in c]
      st.caption("Yağış mm, kar derinliği/kar yağışı alanları Open-Meteo birimleriyle gösterilir.")
      if cols: st.line_chart(df.set_index("time")[cols])
      else: st.info("Yağış/kar verisi bulunamadı.")

    with tab8:
      st.subheader(f"📅 7 Günlük Özet Tablo ({st.session_state.get('location_label', 'Konum')})")
      if not df.empty:
        df["date_only"] = df["time"].dt.date
        seven_days_summary = []
        for d_val, group_df in list(df.groupby("date_only"))[:7]:
          t_series = pd.to_numeric(group_df["temperature_2m"], errors="coerce").dropna() if "temperature_2m" in group_df.columns else pd.Series(dtype=float)
          min_t = t_series.min() if not t_series.empty else None
          max_t = t_series.max() if not t_series.empty else None
          p_series = pd.to_numeric(group_df["precipitation"], errors="coerce").dropna() if "precipitation" in group_df.columns else pd.Series(dtype=float)
          s_series = pd.to_numeric(group_df["snowfall"], errors="coerce").dropna() if "snowfall" in group_df.columns else pd.Series(dtype=float)
          tot_p = float(p_series.sum()) if not p_series.empty else None
          max_hp = float(p_series.max()) if not p_series.empty else None
          tot_s = float(s_series.sum()) if not s_series.empty else None
          max_w = group_df["wind_gusts_10m"].max() if "wind_gusts_10m" in group_df.columns else (group_df["wind_speed_10m"].max() if "wind_speed_10m" in group_df.columns else None)
          
          if max_hp is None: şiddet_etiketi = "⚪ Veri yok"
          elif max_hp >= 10.0: şiddet_etiketi = "🔴 Şiddetli saatlik yağış eşiği"
          elif max_hp >= 5.0: şiddet_etiketi = "🟠 Kuvvetli yağış"
          elif tot_p is not None and tot_p > 1.0: şiddet_etiketi = "🟡 Hafif / normal yağış"
          else: şiddet_etiketi = "🟢 Düşük yağış"

          seven_days_summary.append({
              "Tarih": str(d_val),
              "Şiddet Sınıfı": şiddet_etiketi,
              "Min Sıcaklık Best Match (°C)": round(min_t, 1) if min_t is not None else None,
              "Max Sıcaklık Best Match (°C)": round(max_t, 1) if max_t is not None else None,
              "Toplam Yağış (mm)": round(tot_p, 1) if tot_p is not None else None,
              "Toplam Kar (cm)": round(tot_s, 1) if tot_s is not None else None,
              "Max Rüzgâr Hamlesi (km/sa)": round(max_w, 1) if max_w is not None and pd.notna(max_w) else None
          })
        st.dataframe(pd.DataFrame(seven_days_summary), use_container_width=True, hide_index=True)
      
      st.download_button("📥 Tüm Verileri İndir (CSV)", csv_data, file_name="meteo_forecast.csv", mime="text/csv")

  else:
    show_service_issue("Seçili konumun meteoroloji verileri alınamadı.", weather_error)
