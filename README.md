<h1 align="center">MGM Unofficial Weather API</h1>

<p align="center">
  <a href="https://github.com/metezd/mgm-api/actions/workflows/main.yml">
    <img src="https://github.com/metezd/mgm-api/actions/workflows/main.yml/badge.svg" alt="CI Status" />
  </a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
</p>

<p align="center">
  <i>Gayriresmi REST API servisidir. Meteorolojik veriler <a href="https://www.mgm.gov.tr">mgm.gov.tr</a> adresinden anlık olarak çekilir.</i>
</p>

---

## Başlatma

```bash
docker compose up --build
```

Docker olmadan çalıştırmak, geliştirme ortamı kurmak veya buluta (Render vb.) deploy etmek için detaylı yönergeleri [docs/development.md](docs/development.md) dosyasında bulabilirsiniz.

## Gömülü Kullanım

`mgm_client.py` Flask veya bir HTTP sunucusuna ihtiyaç duymaz. Herhangi bir uygulamaya gömülüp çağırılabilir:

```bash
pip install .          # sadece requests + tzdata kurulur
```

```python
from mgm_client import MGMWeather

mgm = MGMWeather()  # sıfır yapılandırma, env değişkeni gerekmez
print(mgm.hava_durumu("İstanbul", "Kadıköy"))
```

Redis ve tam sunucu bağımlılıkları (Flask, prometheus-client vb.) isteğe bağlıdır:

```bash
pip install mgm-tr[redis]     # cache ve rate limit için Redis desteği
pip install mgm-tr[sunucu]    # app.py'yi çalıştırmak için tam sunucu bağımlılıkları
pip install mgm-tr[zamanlayici]  # opsiyonel iç alert zamanlayıcısı (APScheduler)
```

Hızlı komut satırı testi:

```bash
python mgm_client.py İstanbul Kadıköy
```

## Kullanım

```bash
curl "[http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy](http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy)"
```

**Örnek Yanıt:**
```json
{
  "basarili": true,
  "veri": {
    "il": "İSTANBUL",
    "ilce": "Bakırköy",
    "guncel": { "sicaklik": 29.4, "durum": "Açık" },
    "tahmin": [
      { "tarih": "2026-08-14", "enDusuk": 23, "enYuksek": 31 }
    ],
    "ayEvresi": "Yeni Ay"
  }
}
```

> **Not:** URL parametrelerinde Türkçe karakter (ı, İ, ş, Ş, vb.) sorun yaratıyorsa lütfen standart ASCII karakterler kullanın (Örn: `Istanbul`, `Bakirkoy`, `Kadikoy`).

## Endpoint Referansı

Tüm endpoint'lerin detaylı şeması, parametreleri ve test arayüzü **`/docs`** içinde

| HTTP Metodu | Endpoint | Açıklama |
| :--- | :--- | :--- |
| `GET` | `/hava-durumu/<il>` | İlin (ve `?ilce=`) anlık durumu ve 5 günlük tahmini. |
| `GET` | `/ara?q=...` | Hatalı/karmaşık girdi toleranslı akıllı arama. `"kadikoy/istanbul"` gibi metinleri çözer. |
| `GET` | `/uyarilar?il=...` | MGM'nin aktif sarı/turuncu/kırmızı kodlu uyarı kartları. |
| `GET` | `/konum?lat=...&lon=...` | Koordinattan veri bulma. Önce MGM, çökerse otomatik Open-Meteo. |
| `GET` | `/hava-kalitesi/<il>` | Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2). İstanbul'da İBB verisi önceliklidir, diğer illerde Open-Meteo Air Quality kullanılır. |
| `GET` | `/gun-ay-bilgisi/<il>` | Gün doğumu/batımı ve yerel formülle hesaplanmış Ay Evresi. |
| `GET` | `/polen/<il>` | Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu, zeytin, ambrosia). Open-Meteo/CAMS Avrupa, yalnızca sezonunda ve Avrupa bölgesinde veri döner. Risk seviyeleri yaklaşık sınıflandırmadır. |
| `GET` | `/deniz/<il>?lat=&lon=` | Deniz suyu sıcaklığı + dalga yüksekliği/periyodu/yönü. Sıcaklık öncelikle Piri Reis istasyon verisinden, olmazsa Open-Meteo Marine'den gelir. Dalga her zaman Open-Meteo'dan. `kaynaklar` alanı kaynağı gösterir. İkisi de kapsam dışıysa `kapsamDisi: true`. Kıyıya yakın nokta için `lat`/`lon` verilebilir. |
| `GET` | `/sondurum/en-dusuk-sicakliklar?tarih=` | Türkiye geneli gerçekleşen en düşük sıcaklıklar. `tarih` yoksa en güncel gün. |
| `GET` | `/sondurum/en-yuksek-sicakliklar?tarih=` | Aynısı, en yüksek sıcaklıklar için. |
| `GET` | `/sondurum/toplam-yagis?tarih=` | Türkiye geneli gerçekleşen toplam yağış (mm). |
| `GET` | `/sondurum/kar-kalinliklari` | Türkiye geneli anlık kar yüksekliği (cm). |
| `GET` | `/sondurum/son-gozlemler` | İl merkezlerinde anlık ölçüm (sıcaklık, nem, yağış, rüzgar, basınç, hadise). |
| `POST` | `/toplu` | Tek istekte çoklu konum sorgusu (`{"sorgular": ["istanbul", "bursa"]}`). Paralel çalışır. |
| `POST/DELETE` | `/favoriler/<liste_id>` | Favori il/ilçe ekle/sil (`{"sorgu": "kadikoy/istanbul"}`). Hesap/kimlik doğrulama yoktur, `liste_id`'yi istemci seçer. Onu bilen herkes listeyi düzenler. Liste başına en fazla `APP_FAVORI_MAX_KAYIT` kayıt. |
| `GET` | `/favoriler/<liste_id>` | Listedeki tüm favoriler için hava durumunu `/toplu` mantığıyla tek istekte döner. |
| `GET` | `/favoriler/<liste_id>/liste` | Hava durumu çekmeden kayıtlı sorguları döner (hafif). |
| `POST` | `/alerts/<liste_id>` | Webhook alert kaydı ekler (`{"tur", "il", "webhookUrl", "esik", "yon"}`). Kimlik doğrulama yok, favoriler ile aynı `liste_id` modeli. |
| `DELETE` | `/alerts/<liste_id>/<alert_id>` | Alert kaydını siler. |
| `GET` | `/alerts/<liste_id>` | Listedeki tüm alert kayıtlarını döner. |
| `POST` | `/api/v1/alerts/check` | Kayıtlı tüm alertleri değerlendirir, tetiklenenlere webhook gönderir. `Authorization: Bearer <CRON_SECRET>` gerekir. |
| `GET` | `/map/geojson` | Leaflet/Mapbox için hazır, 81 ilin anlık sıcaklığıyla birleşmiş GeoJSON. |
| `GET` | `/don-uyarisi/<il>` | Tarımsal don/kırağı riski, 5 günlük tahmine dayalı sezgisel sınıflandırma. MGM'nin resmi don uyarı ürünü değildir. |
| `GET` | `/metrics` | Prometheus formatında sistem ölçüleri |

Rate Limiting (İstek Sınırlandırması)
-----------------------------------
- Varsayılan limit: IP başına 60 istek / 60 saniye (APP_RATE_LIMIT_MAX_REQUESTS / APP_RATE_LIMIT_WINDOW_SECONDS).
- Harita limiti: /map/geojson daha sıkı, ayrı bir limite tabidir (APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS).
- Depolama: REDIS_URL varsa sayaçlar Redis'te tutulur, tüm instance'lar paylaşır. Yoksa bellekte tutulur.
- Header'lar: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset.
- Limit aşımı: HTTP 429 + Retry-After header.

Favoriler
---------
- Kimlik doğrulama yok. `liste_id`'yi istemci seçer, onu bilen herkes listeyi okur.
- Limit: Liste başına en fazla 30 kayıt (APP_FAVORI_MAX_KAYIT).
- Kalıcılık: REDIS_URL varsa Redis'te kalıcı tutulur, worker/instance'lar paylaşır.
- Toplu okuma: `GET /favoriler/<liste_id>`, tüm sorgular için `/toplu` mantığıyla hava durumunu döner.

Alert / Webhook Motoru
-----------------------
- Türler: `weather.temp_threshold`, `weather.wind_gust_exceeded`, `weather.rain_threshold` (eşik bazlı, koşul doğru olduğu her kontrolde tetiklenir), `weather.rain_started`, `weather.rain_stopped`, `weather.warning_issued` (olay bazlı, yalnızca durum değişiminde bir kez tetiklenir), `weather.frost_risk` (eşik bir don seviyesi adıdır, örn. `"Hafif Don"`).
- Kimlik doğrulama yok, favoriler ile aynı `liste_id` modeli. Limit: liste başına 30 kayıt (APP_ALERT_MAX_KAYIT).
- Sunucu içinde zamanlayıcı YOKTUR. `POST /api/v1/alerts/check`, `Authorization: Bearer <CRON_SECRET>` ile korunur ve dışarıdan periyodik çağrılmalıdır:

  **GitHub Actions** (`.github/workflows/alert-check.yml`), her 10 dakikada bir:
  ```yaml
  on:
    schedule:
      - cron: "*/10 * * * *"
  jobs:
    check:
      runs-on: ubuntu-latest
      steps:
        - run: |
            curl -X POST -H "Authorization: Bearer ${{ secrets.CRON_SECRET }}" \
              https://<render-url>/api/v1/alerts/check
  ```

  **Linux crontab**:
  ```bash
  */10 * * * * curl -X POST -H "Authorization: Bearer $CRON_SECRET" http://localhost:5000/api/v1/alerts/check
  ```

  **Test modu**: `ENABLE_INTERNAL_SCHEDULER=true` ile uygulama içinde APScheduler tabanlı bir zamanlayıcı açılır (`pip install .[zamanlayici]` gerekir), `APP_SCHEDULER_INTERVAL_DAKIKA` ile aralık ayarlanır.
- Webhook payload'ı: `{"event", "alertId", "il", "ilce", "esik", "olcum", "tetiklenmeZamani"}`.

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
