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

Tüm endpoint'lerin detaylı şeması, parametreleri ve test arayüzü **`/docs`** (Swagger UI) adresinde mevcuttur.

| HTTP Metodu | Endpoint | Açıklama |
| :--- | :--- | :--- |
| `GET` | `/hava-durumu/<il>` | Belirtilen ilin (ve `?ilce=`) anlık durumu ve 5 günlük tahmini. |
| `GET` | `/ara?q=...` | Hatalı/karmaşık girdi toleranslı akıllı arama. `"kadikoy/istanbul"` veya `"maslak itü"` gibi metinleri çözümler. |
| `GET` | `/uyarilar?il=...` | MGM'nin aktif meteorolojik (sarı/turuncu/kırmızı kodlu) uyarı kartlarını getirir. |
| `GET` | `/konum?lat=...&lon=...` | Koordinattan veri bulma. Önce MGM'yi dener, sistem çökmüşse otomatik olarak Open-Meteo'ya fallback yapar. |
| `GET` | `/hava-kalitesi/<il>` | Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2). İstanbul için İBB verisini önceliklendirir, diğer illerde ücretsiz Open-Meteo Air Quality kullanır. |
| `GET` | `/gun-ay-bilgisi/<il>` | Gün doğumu/batımı ve yerel astronomik formüllerle hesaplanmış Ay Evresi bilgisini döner. |
| `GET` | `/polen/<il>` | Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu, zeytin, ambrosia) Open-Meteo/CAMS Avrupa, yalnızca ilgili türün sezonunda ve Avrupa bölgesinde veri döner. Risk seviyeleri yaklaşık sınıflandırmadır |
| `GET` | `/deniz/<il>?lat=&lon=` | Anlık deniz suyu sıcaklığı + dalga yüksekliği/periyodu/yönü. Sıcaklık **MGM'nin Piri Reis istasyon verisinden**, başarısız olursa Open-Meteo Marine API'ye düşer. Dalga verisi her zaman Open-Meteo'dan gelir. `kaynaklar` alanı hangi verinin nereden geldiğini gösterir. İkisi de kapsam dışıysa `kapsamDisi: true` ile null döner. Kıyıya daha isabetli bir nokta için `lat`/`lon` verilebilir. |
| `POST` | `/toplu` | Tek JSON isteği (`{"sorgular": ["istanbul", "bursa"]}`) ile çoklu konum sorgulaması yapar. Paralel çalışır. |
| `POST/DELETE` | `/favoriler/<liste_id>` | Favori il/ilçe ekle/sil (`{"sorgu": "kadikoy/istanbul"}`). Hesap/kimlik doğrulama **yoktur** — `liste_id`'yi istemci kendi seçer/üretir. Onu bilen herkes listeyi düzenler. Liste başına en fazla `APP_FAVORI_MAX_KAYIT` kayıt. |
| `GET` | `/favoriler/<liste_id>` | Listedeki tüm favoriler için hava durumunu `/toplu` ile aynı mantıkla (paralel, kısmi başarısızlığa toleranslı) tek istekte döner. |
| `GET` | `/favoriler/<liste_id>/liste` | Hava durumu çekmeden yalnızca kayıtlı sorguları döner (hafif). |
| `GET` | `/map/geojson` | Harita kütüphaneleri (Leaflet, Mapbox) için hazır, 81 ilin anlık sıcaklık verisiyle birleştirilmiş saf GeoJSON FeatureCollection döner. |
| `GET` | `/don-uyarisi/<il>` | Tarımsal don/kırağı riski: 5 günlük tahminin en düşük sıcaklığına dayalı sezgisel risk sınıflandırması. **MGM'nin resmi don uyarı ürünü değildir**
| `GET` | `/metrics` | Prometheus formatında sistem metriklerini (cache hit/miss, HTTP süreleri, circuit breaker durumu) döner. |

Rate Limiting (İstek Sınırlandırması)
-----------------------------------
- Varsayılan Limit: IP başına 60 istek / 60 saniye (APP_RATE_LIMIT_MAX_REQUESTS / APP_RATE_LIMIT_WINDOW_SECONDS).
- Harita Limiti: /map/geojson daha sıkı ve ayrı bir limite tabidir (APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS).
- Depolama & Fallback: REDIS_URL varsa sayaçlar Redis'te tutulur ve tüm instance'lar arasında paylaşılır. Redis yoksa in-memory belleğe düşer.
- Header'lar: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset.
- Limit Aşımı: HTTP 429 Too Many Requests + Retry-After header

Favoriler
---------
- Kimlik Doğrulama: Yok. `liste_id`'yi istemci kendi seçer, onu bilen herkes o listeyi okur
- Limit: Liste başına en fazla 30 kayıt (APP_FAVORI_MAX_KAYIT).
- Kalıcılık: REDIS_URL varsa favoriler Redis'te tutulur, kalıcıdır ve tüm worker/instance'lar arasında paylaşılır.
- Toplu okuma: `GET /favoriler/<liste_id>`, listedeki tüm sorgular için `/toplu` ile aynı mantıkla hava durumunu döner.

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
