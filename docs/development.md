Hızlı başlangıç için [README](../README.md)'ye bakın. Burada Docker'ın tüm
seçenekleri, test/lint/CI ve bağımlılık yönetimi detayları var.

## Mimari: `WeatherProvider` Arayüzü (Adapter Pattern)

`app.py`, hava durumu verisine ihtiyaç duyduğu HİÇBİR yerde somut
`MGMWeather` sınıfına doğrudan bağımlı değildir — bunun yerine
`weather_provider.py`'de tanımlı soyut `WeatherProvider` arayüzüne
bağımlıdır. `app.py`'deki global `mgm` değişkeni bu arayüz tipiyle
tiplenir:

```python
mgm: WeatherProvider = MGMAdapter(_mgm_istemcisi)
```

`MGMAdapter`, `WeatherProvider`'ı var olan `MGMWeather` istemcisi
üzerinden sağlayan ince bir delege (delegation) katmanıdır — iş
mantığının hiçbiri `weather_provider.py`'de değil, `mgm_client`
paketinde yaşar.

**Neden:** Yarın MGM'nin veri kaynağı tamamen değişirse (ya da ikinci
bir sağlayıcı eklenirse), yapılması gereken tek şey `WeatherProvider`'ı
implemente eden yeni bir Adapter sınıfı yazmak ve `app.py`'deki tek bir
satırı (`mgm = MGMAdapter(...)` → `mgm = YeniAdapter(...)`)
değiştirmek. `app.py`'deki ~25 `mgm.xxx(...)` çağrı noktasının HİÇBİRİNE
dokunmaya gerek kalmaz.

Arayüzdeki metod seti, `MGMWeather`'ın TÜM yeteneklerini değil, yalnızca
`app.py`'nin gerçekten çağırdığı alt kümeyi kapsar ("ports and
adapters": arayüz tüketicinin ihtiyacına göre şekillenir, sağlayıcının
tam kapasitesine göre değil). `mgm_client`'ın arayüzde olmayan başka
public metodları da olabilir — bunlar kütüphanenin kendi API'si olarak
kalmaya devam eder ve doğrudan `mgm_client` import edilerek (script,
CLI, başka bir servis vb.) kullanılabilir.

Testlerde bu katman görünmez: `tests/test_app_integration.py`'deki
`FakeMGM`, `app_module.mgm`'e doğrudan atanır. Python'ın duck typingi sayesinde `WeatherProvider`
arayüzündeki metod isimlerini sağlayan HERHANGİ bir nesne çalışır.
`MGMAdapter`'ın kendisi `tests/test_weather_provider.py`'de ayrıca
test edilir.

## Docker Uygulaması

Uygulamayı tüm bağımlılıklarıyla birlikte ayağa kaldırmak için aşağıdaki komutları kullanabilirsiniz:

```bash
cp .env.example .env   # İsteğe bağlı olarak değişkenler düzenlenebilir
docker compose up --build
```

`MGM_REDIS_URL` otomatik olarak bundled Redis'e yönlendirilir. `.env` yoksa da
çalışır, varsayılanlarla devam eder.

Sadece uygulamayı (Redis'siz, in-memory cache ile) çalıştırmak için:

```bash
docker build -t mgm-api .
docker run -p 5000:5000 mgm-api
```

> **Not:** Docker imajı, sistemin durumunu izlemek üzere `/health` uç noktasını kullanan yapılandırılmış bir `HEALTHCHECK` barındırır. Konteynerin güncel sağlığı `docker ps` çıktısında `healthy` veya `unhealthy` olarak gözlemlenebilir.

## Test ve Lint

```bash
python -m unittest discover -s tests -v
pip install ruff && ruff check .
```

İkisi de CI'da her push/PR'da otomatik çalışır (`.github/workflows/main.yml`).

### Coverage

```bash
pip install coverage
coverage run -m unittest discover -s tests
coverage report -m
```

CI, coverage raporunu üretir ve `pyproject.toml`'daki `[tool.coverage.report] fail_under = 85` eşiğinin altına düşülürse build'i kırar (dal/branch coverage dahil, mevcut gerçek coverage ~%91). Eşik, mevcut coverage'ın biraz altında tutuluyor ki küçük, henüz test edilmemiş eklemeler CI'ı hemen kırmasın — ama bir dosyanın toptan test edilmemesi gibi büyük bir gerileme hâlâ yakalanır. Yalnızca gerçek sunucu başlatma kodu (`if __name__ == "__main__":`) `# pragma: no cover` ile bilinçli olarak hariç tutulur.

## Bağımlılık Yönetimi

Projedeki Python bağımlılıkları iki aşamalı bir yapıyla yönetilmektedir: 
* `requirements.txt` dosyası esnek sürüm aralıklarını (`>=`) barındırır.
* `requirements-lock.txt` dosyası ise bu gereksinimlerden üretilmiş, kesin olarak sabitlenmiş (*pinned*) sürümleri içerir. 

Sürekli entegrasyon (CI) süreçleri ve Docker build adımları, tekrarlanabilir yapılar elde etmek amacıyla her zaman `lock` dosyasını baz alır. Bağımlılıkların güncellenmesine dair talimatlar ilgili dosyanın üst kısmında yorum satırı olarak yer almaktadır.

Ayrıca projede yer alan Dependabot yapılandırması (`.github/dependabot.yml`), pip, Docker ve GitHub Actions bağımlılıklarını haftalık olarak tarar ve gerekli güncellemeler için otomatik PR oluşturur.

## Rate Limit

Uygulama, IP tabanlı kayan pencere (*sliding-window*) algoritması kullanarak istekleri sınırlandırmaktadır. Bu sınırlandırmalar `APP_RATE_LIMIT_WINDOW_SECONDS` ve `APP_RATE_LIMIT_MAX_REQUESTS` ortam değişkenleri ile yapılandırılabilir.

API tarafından döndürülen her HTTP yanıtı, istemciyi bilgilendirmek amacıyla aşağıdaki başlıkları (*header*) içerir:
* `X-RateLimit-Limit`: İzin verilen maksimum istek sayısı.
* `X-RateLimit-Remaining`: Kalan istek hakkı.
* `X-RateLimit-Reset`: Kayan pencere algoritması kullanıldığı için tek bir sabit sıfırlanma anı bulunmamaktadır. En eski isteğin zaman aşımına uğrayıp istemcinin en az bir yeni istek hakkı daha kazanacağı UNIX timestampini işaret eder.

> **İstisnalar:** `/health`, `/docs` ve `/openapi.yaml` uç noktaları bu sınırlandırmalardan muaftır

**Toplu İstek (Batch) Koruması:**
İstek sınırlandırması, paket içindeki öge sayısını değil toplam HTTP isteği sayısını baz alır. Tek bir istekte çok sayıda sorgu paketleyip limiti aşmayı önlemek için `POST /toplu` ayrıca `APP_TOPLU_MAX_SORGU` (varsayılan: `20`) ile sınırlandırılmıştır.

## Response Compression

`Accept-Encoding: gzip` başlığı iletildiğinde JSON, HTML ve YAML yanıtları `Flask-Compress` ile otomatik sıkıştırılır. Standart olarak aktiftir, ek yapılandırma gerekmez. `br` (Brotli) da desteklenir, `Accept-Encoding` içinde `br` geçen istemciler otomatik brotli alır.

## Gözlemlenebilirlik (Prometheus metrikleri)

`GET /metrics`, Prometheus text formatında metrik döner. Rate limitten muaf, `/health` gibi.

- `http_requests_total{method,endpoint,status}` endpoint bazlı istek sayacı. Etiket olarak ham path değil Flask'ın eşleştirdiği route adı (`request.endpoint`, ör. `guncel`) kullanılır. `/guncel/<il>` gibi path'lerde `il` değerini etikete koymak sınırsız kardinaliteye (her farklı il için ayrı zaman serisi) yol açardı.
- `http_request_duration_seconds{method,endpoint}` histogram, aynı etiketleme mantığıyla.
- `mgm_cache_result_total{sonuc}` `hit`/`stale_hit`/`miss` sayaçları (`mgm_client`, `_cached_get()` içinde artırılır).
- `mgm_circuit_breaker_state` 0=kapalı, 1=yarı-açık, 2=açık. Her scrape'te `mgm.circuit_breaker_saglik_ozeti()`'nden okunur.
- `mgm_rate_limit_rejected_total` 429 ile reddedilen istek sayısı.

## Loglama

Varsayılan olarak `INFO` seviyesinde, insan-okur (`text`) formatta loglanır. `LOG_LEVEL` ve `LOG_FORMAT` env değişkenleriyle ayarlanır:

```bash
LOG_LEVEL=WARNING   # gürültüyü azaltmak için üretimde
LOG_FORMAT=json     # Render/Datadog gibi log toplayıcılarda alan bazlı sorgulama için
```

**Request correlation ID:** Her isteğe bir kimlik atanır (`g.request_id`) ve **her log satırına** eklenir — eşzamanlı isteklerin log satırları artık birbirine karışmaz, aynı isteğe ait tüm satırlar tek bir ID ile filtrelenebilir. Yanıtta `X-Request-ID` header'ı olarak da döner. İstemci kendi `X-Request-ID` header'ını gönderirse (ör. bir upstream proxy/gateway'in ürettiği izleme ID'si) o kullanılır — ama yalnızca `^[A-Za-z0-9_-]{1,64}$` desenine uyuyorsa; uymuyorsa (log injection girişimi, aşırı uzun değer vb.) sessizce reddedilip sunucu tarafında üretilen bir ID kullanılır. Flask request context'i olmayan yerlerde (ör. `mgm_client`'ın arka plan yenileme thread'i) `request_id` `-` olarak görünür.

```bash
curl -i "http://127.0.0.1:5000/hava-durumu/Istanbul" | grep -i x-request-id
# X-Request-ID: 1d25622452ca4c2d
```

## Deploy (Render)

Repo'daki `render.yaml` Blueprint'i, web servisini ve Redis uyumlu bir Key
Value servisini tek seferde kurar. Render Dashboard'da **New +** → **Blueprint** → bu repoyu seçin.