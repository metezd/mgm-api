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
| `GET` | `/hava-durumu/<il>` | Belirtilen ilin (ve opsiyonel `?ilce=`) anlık durumu ve 5 günlük tahmini. |
| `GET` | `/ara?q=...` | Hatalı/karmaşık girdi toleranslı akıllı arama. `"kadikoy/istanbul"` veya `"maslak itü"` gibi metinleri çözümler. |
| `GET` | `/uyarilar?il=...` | MGM'nin aktif meteorolojik (sarı/turuncu/kırmızı kodlu) uyarı kartlarını getirir. |
| `GET` | `/konum?lat=...&lon=...` | Koordinattan veri bulma. Önce MGM'yi dener, sistem çökmüşse otomatik olarak Open-Meteo'ya fallback yapar. |
| `GET` | `/hava-kalitesi/<il>` | Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2). İstanbul için İBB verisini önceliklendirir, diğer illerde ücretsiz Open-Meteo Air Quality kullanır. |
| `GET` | `/gun-ay-bilgisi/<il>` | Gün doğumu/batımı ve yerel astronomik formüllerle hesaplanmış Ay Evresi bilgisini döner. |
| `POST` | `/toplu` | Tek JSON isteği (`{"sorgular": ["istanbul", "bursa"]}`) ile çoklu konum sorgulaması yapar. Paralel çalışır. |
| `GET` | `/map/geojson` | Harita kütüphaneleri (Leaflet, Mapbox) için hazır, 81 ilin anlık sıcaklık verisiyle birleştirilmiş saf GeoJSON FeatureCollection döner. |
| `GET` | `/metrics` | Prometheus formatında sistem metriklerini (cache hit/miss, HTTP süreleri, circuit breaker durumu) döner. |

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
