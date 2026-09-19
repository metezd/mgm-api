# Changelog

Bu proje [Semantik Sürümleme](https://semver.org/lang/tr/) kullanır.
`0.x` sürümlerde API henüz kararlı kabul edilmez, küçük sürümler
(0.1 → 0.2) arasında bile geriye dönük uyumsuz değişiklik olabilir.

## [Yayınlanmadı]

- **Güvenlik — CORS whitelist:** `APP_CORS_ALLOW_ORIGIN`, `"*"`
  (varsayılan, değişmedi) yerine virgülle ayrılmış bir origin
  whitelist'i (`https://a.com,https://b.com`) kabul edecek şekilde
  genişletildi. Whitelist modunda yalnızca eşleşen `Origin` yansıtılır
  (+ `Vary: Origin`), eşleşmeyen/olmayan origin'de CORS header'ı hiç
  eklenmez. 4 yeni test (`TestCorsWhitelist`).

- **Güvenlik — liste_id üretimi:** `POST /favoriler`'da `listeId`
  verilmezse sunucu artık `secrets.token_urlsafe(18)` yerine standart
  bir **UUID v4** üretiyor. Not: erişim kontrolü zaten `listeId`'ye
  değil, ayrıca üretilip yalnızca hash'i saklanan (SHA-256, HMAC-safe
  karşılaştırma) `manage_token`/`read_token`'a dayanıyordu — bu
  değişiklik savunma derinliği amaçlıdır, client'ların kendi
  `listeId`'sini seçebilmesi (geriye dönük uyumluluk için) korundu.
  3 yeni test.

- Redis Pipeline: `_redis_cift_yaz()` eklendi — her başarılı fetch'te
  normal cache + LKG yazımı (ikisi de aktifken) artık iki ayrı Redis
  ağ round-trip'i yerine `redis-py`'nin `pipeline(transaction=False)`
  ile TEK round-trip'te gönderiliyor. `_yukle_singleton` ve
  `_arka_planda_yenile`'nin başarı yollarında (en sık tetiklenen Redis
  trafiği) kullanılıyor; LKG kapalıysa ya da yalnızca biri aktifse
  pipeline'a gerek kalmadan tek `SETEX`'e düşülüyor — davranış aynı,
  sadece ağ gidiş-dönüşü azaltılıyor. 5 yeni test
  (`TestRedisPipelineYazimi`, sahte bir Redis istemcisiyle gerçek
  `pipeline()` çağrı sayısını doğruluyor). 200/200 test geçiyor.
  Detaylar: `docs/resilience.md`.

- Son Bilinen İyi Değer (LKG) katmanı eklendi: normal önbellekten
  (TTL + SWR, varsayılan toplam 6 dakika) tamamen ayrı, çok daha uzun
  ömürlü (`MGM_LKG_TTL_SANIYE`, varsayılan 10800sn = 3 saat — `tahmin_ttl_saniye`
  ile aynı) bir son çare katmanı. Yalnızca hem taze hem bayat dönem
  tükenip gerçek MGM isteği de başarısız olduğunda devreye girer;
  hata fırlatmak yerine son başarılı yanıt sunulur (uygulama uzun
  kesintilerde bile tamamen "çevrimdışı" görünmez). Hem bellek içi
  hem Redis'te (yapılandırılmışsa, konteyner restart'ına dayanıklı
  şekilde) tutulur; `MGM_LKG_AKTIF=0` ile kapatılabilir.
  `mgm_cache_result_total{sonuc="lkg_fallback"}` Prometheus sayacı
  eklendi (mevcut Grafana dashboard'una otomatik yansır). 4 yeni test
  (`TestSonBilinenIyiDeger`). 195/195 test geçiyor, mevcut TTL/SWR/
  circuit-breaker davranışında değişiklik yok. Detaylar:
  `docs/resilience.md`.

- Adapter Pattern: `weather_provider.py` eklendi — soyut `WeatherProvider`
  arayüzü (`abc.ABC`, app.py'nin gerçekten çağırdığı 23 metod) ve bunu
  `MGMWeather` üzerinden sağlayan ince delege katmanı `MGMAdapter`.
  `app.py`'deki global `mgm` artık `WeatherProvider` tipiyle tiplenir
  (`mgm: WeatherProvider = MGMAdapter(_mgm_istemcisi)`); mevcut ~25
  `mgm.xxx(...)` çağrı noktasının hiçbiri değişmedi (MGMAdapter aynı
  isim/imzaları expose ediyor). Amaç: MGM'nin veri kaynağı tamamen
  değişirse ya da ikinci bir sağlayıcı eklenirse tek değişecek yer
  `app.py`'deki tek bir kurulum satırı olsun. 7 yeni test
  (`tests/test_weather_provider.py`, gerçek `MGMWeather` ile uyumluluk
  dahil). 191/191 test geçiyor, davranış değişikliği yok. Detaylar:
  `docs/development.md`.

- `/toplu` ve `/favoriler/<liste_id>` artık her istekte yeni bir
  `ThreadPoolExecutor` açıp kapatmak yerine, uygulama ömrü boyunca
  yaşayan **tek ve sabit boyutlu** paylaşılan bir havuz kullanıyor
  (`APP_TOPLU_MAX_WORKERS`, varsayılan 20). Bu hem thread oluşturma
  overhead'ini azaltır hem de eşzamanlı batch istekleri altında toplam
  thread sayısının sınırsız büyümesini engeller (öngörülebilir kaynak
  tavanı). `requests.Session`'daki HTTP connection pool boyutu da
  (`MGM_HTTP_POOL_MAXSIZE`, varsayılan 20) worker sayısını karşılayacak
  şekilde büyütüldü, aksi halde paralel worker'lar pool seviyesinde
  sıraya girip paralelliğin faydasını azaltıyordu. Not: tam asenkron
  (asyncio/httpx) bir yeniden yazım — `mgm_client`'ın tüm HTTP
  katmanını ve sync public API'sini değiştireceği, 180+ testi
  etkileyeceği için — kapsam dışı bırakıldı; bu, aynı faydayı çok daha
  düşük riskle sağlayan ölçülü bir iyileştirmedir. 2 yeni test.
  Detaylar: `docs/resilience.md`.

- ETag / 304 Not Modified desteği eklendi: başarılı (200), akışsız
  `GET` yanıtlarına `ETag` (Werkzeug `add_etag()`/`make_conditional()`)
  ve `Cache-Control: no-cache` header'ları eklenir; istemci aynı
  `ETag`'i `If-None-Match` ile gönderirse gövde tekrar üretilip
  gönderilmeden `304 Not Modified` döner. `/health` ve `/metrics`
  kapsam dışı. Prometheus metrikleri de 304'ü doğru sayar (hook,
  `metrik_kaydet`'ten önce çalışacak şekilde sıralandı). 8 yeni test
  (`TestEtagConditionalGet`). Detaylar: `docs/resilience.md`.

- Girdi doğrulama/sanitization: `<il>` path parametresi, `ilce`/`il`/`q`
  query parametreleri artık route'a girmeden Pydantic v2 katmanında
  sıkı regex + uzunluk validasyonundan geçiyor (`KonumSorguModel`,
  `SerbestAramaModel`). Yer adları yalnızca harf/boşluk/tire/kesme
  işareti (1-80 karakter), serbest arama harf/rakam/boşluk/virgül/`/`/
  `-`/`.` (1-150 karakter) — XSS/enjeksiyon karakterlerine (`< > " ' ;
  { } | & `` $` vb.) izin verilmiyor, geçersiz girdi 400 ile reddedilir.
  Aynı desenler mevcut POST gövdesi modellerine de uygulandı
  (`AlertGovdeModel.il/ilce`, `FavoriGovdeModel.sorgu`,
  `TopluGovdeModel.sorgular`). 14 yeni test eklendi
  (`TestKonumVeAramaDogrulama`).

- Prometheus + Grafana izleme örneği eklendi: `docker-compose.yml`'e
  `monitoring` profili altında (varsayılan `docker compose up`'ı
  etkilemez) `prometheus` ve `grafana` servisleri eklendi.
  `monitoring/prometheus.yml` `/metrics`'i scrape eder; Grafana veri
  kaynağı ve hazır "mgm-api" dashboard'u (istek hızı, 5xx oranı, p95
  gecikme, cache isabet oranı, circuit breaker durumu, rate-limit
  reddi) `monitoring/grafana/provisioning/` ile otomatik yüklenir.
  Detaylar: `docs/monitoring.md`. Etkinleştirmek için:
  `docker compose --profile monitoring up -d`.

- Dockerfile `python:3.14-slim`'den `python:3.13-slim`'e geri alındı.
  Dependabot'un Docker imaj bump'ı CI'da (main.yml yalnızca 3.13 ile
  lint/test çalıştırıyor, Docker build etmiyor) yakalanamayan bir hataya
  yol açtı. `.github/dependabot.yml`'a Python imajı için minör/majör
  sürüm güncellemelerini durduran bir `ignore` kuralı eklendi; patch/
  digest (güvenlik) güncellemeleri hâlâ otomatik önerilir. 3.14'e geçiş
  manuel doğrulamadan sonra tekrar denenmeli.

- Akıllı Özetleme (NLP) eklendi (`akilli_ozet()` / `GET /akilli-ozet/<il>`):
  güncel durum ve 5 günlük tahmini kural tabanlı (rule-based) doğal dil
  üretimiyle tek bir Türkçe özet paragrafına, öne çıkan noktalara
  (`anahtarNoktalar`), uyarılara (`uyarilar`: sıcak/soğuk/rüzgar eşiği
  aşımı) ve basit bir sıcaklık trendine (`trend`) çevirir. Bir dil
  modeli/ML kullanmaz; MGM'nin resmi bir metin ürünü değildir.

## [0.1.0] - İlk PyPI sürümü

`mgm_client.py`, `mgm-tr` adıyla bağımsız bir Python paketi olarak
PyPI'a yayınlandı. HTTP sunucu (`app.py`/Flask) olmadan, sadece
`requests` + `tzdata` ile çalışır.

İçerdiği başlıca özellikler (hepsi bu sürümden önce, `app.py` REST
API'sinin arka planında geliştirildi):

- Güncel durum, günlük/saatlik tahmin, akıllı il/ilçe arama
- Hava kalitesi, polen indeksi, deniz suyu sıcaklığı/dalga durumu
- Gün doğumu/batımı, ay evresi, tarımsal don/kırağı riski
- Türkiye geneli son durum uç noktaları
- Circuit breaker, Redis destekli cache/stale-while-revalidate

Not: Kullanılan uç noktaların büyük kısmı MGM'nin resmi
dokümante edilmiş bir API'si değil, mgm.gov.tr'nin kendi iç
servisleridir. MGM bunları değiştirirse istemcinin güncellenmesi
gerekir.
