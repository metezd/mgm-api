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

Docker olmadan çalıştırmak veya buluta deploy etmek için detaylı yönergeler [docs/development.md](docs/development.md) dosyasında bulunur.

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

> **Not:** URL parametrelerinde Türkçe karakter (ı, İ, Ğ, vb.) sorun yaratıyorsa standart ASCII karakterler kullanın (Örn: `Istanbul`, `Bakirkoy`, `Kadikoy`).

## API Referansı

| Endpoint | Açıklama | HTTP Metodu |
| :--- | :--- | :---: |
| `/hava` | Belirli bir konum için anlık hava durumu sorgular. | `GET` |
| `/toplu` | Birden fazla konum için aynı anda sorgulama yapar. | `GET` |
| `/favoriler` | Kayıtlı favori konumları yönetir ve sorgular. | `GET/POST/DELETE` |
| `/alerts` | Hava durumu uyarılarını takip eder ve webhook tetikler. | `GET/POST` |
| `/akilli-ozet/<il>` | Güncel durum + tahmini kural tabanlı NLG ile Türkçe özet paragrafına çevirir. | `GET` |
| `/metrics` | API performans ve cache istatistiklerini döner (Prometheus formatı). | `GET` |

## Daha fazlası

- [docs/development.md](docs/development.md)
- [docs/resilience.md](docs/resilience.md)
- [docs/monitoring.md](docs/monitoring.md) hazır Prometheus + Grafana dashboard (`docker compose --profile monitoring up -d`)
- [docs/endpoint.md](docs/endpoint.md)