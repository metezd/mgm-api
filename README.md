<h1 align="center">MGM Unofficial Weather API</h1>

<p align="center">
  <a href="https://github.com/metezd/mgm-api/actions/workflows/main.yml">
    <img src="https://github.com/metezd/mgm-api/actions/workflows/main.yml/badge.svg" alt="CI Status" />
  </a>
  <a href="https://test.pypi.org/project/mgm-tr/">
    <img src="https://img.shields.io/badge/testpypi-mgm--tr-blue?logo=pypi&logoColor=white" alt="TestPyPI" />
  </a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License" />
</p>

<p align="center">
  <i>Gayriresmi REST API servisidir. Meteorolojik veriler <a href="https://www.mgm.gov.tr">mgm.gov.tr</a> adresinden çekilir.</i>
</p>

---

## Başlatma

```bash
docker compose up --build
```

Docker olmadan çalıştırmak veya buluta deploy etmek için detaylı yönergeleri [docs/development.md](docs/development.md) dosyasında bulabilirsiniz.

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

Redis ve tam sunucu bağımlılıkları isteğe bağlıdır:

```bash
pip install mgm-tr[redis]     # cache ve rate limit için Redis desteği
pip install mgm-tr[sunucu]    # app.py'yi çalıştırmak için tam sunucu bağımlılıkları
pip install mgm-tr[zamanlayici]  # opsiyonel iç alert zamanlayıcısı (APScheduler)
```

Hızlı test için:

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

> **Not:** URL parametrelerinde Türkçe karakter (ı, İ, ş, Ş, vb.) sorun yaratıyorsa standart ASCII karakterler kullanın (Örn: `Istanbul`, `Bakirkoy`, `Kadikoy`).

## Endpoint Referansı

Tüm endpoint'lerin detaylı şeması, parametreleri ve test arayüzü **`/docs`** içinde

| HTTP Metodu | Endpoint | Açıklama |
| :--- | :--- | :--- |
| `GET` | `/hava-durumu/<il>` | İlin (ve `?ilce=`) anlık durumu ve 5 günlük tahmini. |
| `GET` | `/ara?q=...` | Hatalı/karmaşık girdi toleranslı akıllı arama. `"kadikoy/istanbul"` gibi metinleri çözer. |
| `GET` | `/uyarilar?il=...` | MGM'nin aktif sarı/turuncu/kırmızı kodlu uyarı kartları. |
| `GET` | `/konum?lat=...&lon=...` | Enlem ve boylam (GPS) koordinatlarına göre anlık veri. |
| `GET` | `/hava-kalitesi/<il>` | Anlık hava kalitesi (PM10, PM2.5, NO2) ve UV indeksi. |
| `GET` | `/gun-ay-bilgisi/<il>` | Gün doğumu/batımı ve yerel formülle hesaplanmış Ay Evresi. |
| `GET` | `/polen/<il>` | Sezonluk polen ve alerji riski indeksi (çimen, zeytin, huş vb.). |
| `GET` | `/deniz/<il>?lat=&lon=` | Deniz suyu sıcaklığı, dalga yüksekliği, periyodu ve yönü. |
| `GET` | `/sondurum/en-dusuk-sicakliklar?tarih=` | Türkiye geneli gerçekleşen en düşük sıcaklıklar. |
| `GET` | `/sondurum/en-yuksek-sicakliklar?tarih=` | Türkiye geneli gerçekleşen en yüksek sıcaklıklar. |
| `GET` | `/sondurum/toplam-yagis?tarih=` | Türkiye geneli gerçekleşen toplam yağış (mm). |
| `GET` | `/sondurum/kar-kalinliklari` | Türkiye geneli anlık kar yüksekliği (cm). |
| `GET` | `/sondurum/son-gozlemler` | İl merkezlerinde anlık ölçüm (sıcaklık, nem, yağış, rüzgar, basınç, hadise). |
| `POST` | `/toplu` | Tek istekte çoklu konum sorgusu (`{"sorgular": ["istanbul", "bursa"]}`). Paralel çalışır. |
| `POST/DELETE` | `/favoriler/<liste_id>` | Favori listesine konum ekleme veya silme. |
| `GET` | `/favoriler/<liste_id>` | Listedeki tüm favoriler için hava durumunu `/toplu` mantığıyla tek istekte döner. |
| `GET` | `/favoriler/<liste_id>/liste` | Hava durumunu çekmeden kayıtlı sorguları döner |
| `POST` | `/alerts/<liste_id>` | Webhook bildirim kaydı ekler (`{"tur", "il", "webhookUrl", "esik", "yon"}`). Kimlik doğrulama yok, favoriler ile aynı `liste_id` modeli. |
| `DELETE` | `/alerts/<liste_id>/<alert_id>` | Kayıtlı bildirim kuralını silme. |
| `GET` | `/alerts/<liste_id>` | Kayıtlı tüm bildirim kurallarını listeleme. |
| `POST` | `/api/v1/alerts/check` | Kayıtlı tüm bildirimleri değerlendirir, tetiklenenlere webhook gönderir. `Authorization: Bearer <CRON_SECRET>` gerekir. |
| `GET` | `/map/geojson` | Harita kütüphaneleri (Leaflet/Mapbox) için 81 ilin sıcaklık verili GeoJSON çıktısı |
| `GET` | `/don-uyarisi/<il>` | Tarımsal don ve kırağı riski değerlendirmesi |
| `GET` | `/metrics` | Sistem performans takibi için Prometheus ölçümleri |

Rate Limit
-----------------------------------
- Genel API: IP başına dakikada 60 istek.
- Harita Verisi (/map/geojson): Sistem kaynağı tüketimi nedeniyle daha sıkı özel limitlere tabidir.
- Depolama: REDIS_URL varsa sayaçlar Redis'te tutulur, tüm instance'lar paylaşır.

Favoriler
---------
- Hızlı ve Şifresiz: Kendi belirleyeceğiniz (istemci tarafında üretilmiş) bir liste_id ile listelerinizi kolayca yönetin.
- Limit: Liste başına en fazla 30 kayıt (APP_FAVORI_MAX_KAYIT).
- Toplu okuma: `GET /favoriler/<liste_id>`, tüm sorgular için `/toplu` mantığıyla hava durumunu döner.

## Uyarılar ve Bildirim Motoru (Webhook)

Belirli hava durumu olayları gerçekleştiğinde (yağmur başlangıcı, ani sıcaklık düşüşü, fırtına veya resmi MGM uyarıları) kendi uygulamalarınıza otomatik Webhook bildirimleri gönderebilirsiniz.

* **Esnek Kurallar:** İster eşik bazlı (örn. rüzgar hızı 50km/s'yi geçerse sürekli bildir), ister olay bazlı (örn. yağmur başladığında veya durduğunda bir kez bildir) kurallar oluşturabilirsiniz.
* **Üyeliksiz ve Pratik:** Favori sistemindeki gibi kendi belirlediğiniz `liste_id` ile çalışır. Her liste için 30 farklı bildirim kuralı ekleyebilirsiniz.
* **Anında İletim:** Koşullar sağlandığında, belirlediğiniz URL adresine konum, ölçüm ve zaman bilgilerini içeren net bir JSON paketi postalanır.

### Bildirimleri Tetikleme
Sunucuyu yormamak adına uygulamanın içinde sürekli çalışan bir arka plan görevi yoktur. Bildirimleri kontrol edip göndermesi için sisteminizi dışarıdan düzenli olarak tetiklemeniz gerekir (Örn: Her 10 dakikada bir).

**Linux Crontab** veya **GitHub Actions** gibi ücretsiz servislerle otomatik yapılabilir:

Örnek GitHub Actions Görevi (`.github/workflows/alert-check.yml`):
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
            https://<sunucu-adresiniz>/api/v1/alerts/check
```

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
