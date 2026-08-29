MGM'nin sitesinin resmi bir garantisi yoktur. Servis zaman zaman yavaşlayabilir veya kesilebilir. Bu proje, kullanıcıya bunu hissettirmemek için üç katman kullanıyor: cache,
stale-while-revalidate ve circuit breaker. Bu belge üçünün nasıl
çalıştığını ve ilgili tüm ortam değişkenlerini anlatır. Hızlı başlangıç ve
endpoint listesi için [README](../README.md)'ye, Docker/test/CI detayları
için [development.md](development.md)'ye bakınız.

## Önbellek Altyapısı (In-Memory & Redis)

Sistem varsayılan olarak **In-Memory** önbellekleme ile çalışır. İsteğe bağlı olarak **Redis** veya [Redis Stack](https://redis.io/docs/latest/operate/oss_and_stack/install/install-stack/) kullanılabilir.

* **Bağımsız Çalıştırma:** Redis sunucusunu Docker üzerinden başlatmak için:
  `docker run -d --rm -p 6379:6379 redis:7-alpine`
* **Yapılandırma:** `MGM_REDIS_URL` ortam değişkeni tanımlandığında, Redis birincil önbellek katmanı olur ve in-memory katmanını devre dışı bırakır:
  `MGM_REDIS_URL="redis://localhost:6379/0" python app.py`
* **Hata Yönetimi:** Redis'e bağlanılamaması durumunda uygulama başlatılamaz ve hata fırlatır. Redis kullanılmayacaksa bu değişken tanımlanmamalıdır. (`docker compose up` komutu kullanıldığında Redis servisi ve URL değişkeni otomatik olarak yapılandırılır).
* **Zaman Aşımı (Timeout):** Redis istemcisinde maksimum **2 saniyelik** socket ve bağlantı zaman aşımı uygulanır. Bu sayede olası Redis kesintileri veya yavaşlamaları ana istek akışını bloke etmez.

## Stale-While-Revalidate ve Stampede Koruması

Eşzamanlı yüksek trafik altında sistem performansını korumak için önbellekteki verinin yaşam döngüsü iki aşamalı yönetilir:

1. **Taze Dönem (`MGM_CACHE_TTL`):** Veri doğrudan önbellekten anında sunulur.
2. **Bayat Dönem (`MGM_STALE_WHILE_REVALIDATE`):** TTL süresi dolduktan sonra gelen ilk istekte, kullanıcıya mevcut (bayat) veri bekletilmeden dönülür. Eşzamanlı olarak arka planda MGM'ye istek atılarak önbellek güncellenir. Kullanıcı MGM'nin olası yavaşlığından etkilenmez.
3. **Süresi Dolmuş Dönem:** Her iki süre de dolduysa, istek MGM'ye engelleyici (blocking) olarak atılır ve taze veri beklenir.

**Cache Stampede Koruması:** Aynı anahtar için tazeleme gerektiğinde, sistem yalnızca tek bir isteğin arka planda yenileme yapmasına izin verir. SWR mekanizmasını devre dışı bırakmak için `MGM_STALE_WHILE_REVALIDATE=0` ayarlanmalıdır.

## Circuit Breaker

MGM servisinin art arda hata döndürdüğü durumlarda sistemi korumak için Devre Kesici devreye girer.

* **Durumlar:** Eşik değere ulaşıldığında devre **AÇIK** konuma geçer ve MGM'ye yeni istek atılmaz, doğrudan hata dönülür. Bekleme süresi dolduğunda devre **YARI AÇIK** duruma geçer ve tek bir test isteği atar. İstek başarılı olursa devre **KAPALI** (normal) duruma döner, başarısız olursa tekrar açılır.
* **Önbellek Etkileşimi (Önemli):** Devre kesici **yalnızca ağ isteklerini engeller**, önbellek katmanının önüne geçmez. Devre açıkken önbellekte SWR kapsamında bayat veri varsa, bu veri istemciye sunulmaya devam eder. Arka plandaki gereksiz MGM istekleri kesilmiş olur. Önbellekte veri yoksa istek bekletilmeden reddedilir.
* **İzleme:** Sistem durumu `GET /health` uç noktasındaki `circuit_breaker` alanından (`kapali` | `acik` | `yari-acik`) takip edilebilir.

## Dinamik TTL Yapılandırması (`guncel_durum`, Deneysel)

MGM istasyon ölçümleri, gözlemsel olarak genellikle her saat başını birkaç dakika geçe (örn. 14:08, 15:07) güncellenmektedir. Bu döngüyü yakalamak için yalnızca `guncel_durum()` uç noktasında zamana duyarlı dinamik TTL uygulanır (İl listesi, tahmin ve geocoding verileri statik `MGM_CACHE_TTL` kullanmaya devam eder).

* **Sıcak Pencere (`MGM_GUNCEL_SICAK_TTL_SANIYE` - Varsayılan: 120):** Saat başlarındaki veri değişimlerini hızlıca yakalamak için TTL kısa tutulur.
* **Soğuk Pencere (`MGM_GUNCEL_SOGUK_TTL_SANIYE` - Varsayılan: 1800):** MGM'nin veri yayınlamadığı saat ortalarında gereksiz ağ isteklerini ve revalidation işlemlerini azaltmak için TTL 30 dakikaya uzatılır. 
* **Zaman Bazlı Hesaplama:** TTL, verinin yazıldığı an değil, okunduğu anki saate göre (`Europe/Istanbul` saat diliminde) hesaplanır. Yeni bir saate geçildiğinde kayıt otomatik olarak bayat kabul edilir ve revalidation tetiklenir. Bu işlem için standart Python kütüphanesi `zoneinfo` ve `tzdata` bağımlılığı kullanılır.
* **Kapatma:** `MGM_CACHE_TTL=0` veya `MGM_GUNCEL_DINAMIK_TTL=0` ayarlanarak dinamik TTL devre dışı bırakılabilir, bu durumda statik TTL geçerli olur.

## Yedek Veri Kaynağı (Open-Meteo Fallback)

MGM API'sinde yaşanabilecek olası kesintilerde, sistemin tamamen hizmet dışı kalmasını önlemek amacıyla yedek olarak ücretsiz [Open-Meteo](https://open-meteo.com) servisi kullanılmaktadır.

### Kapsam ve Davranış

* **Sadece Anlık Veri:** Yedek sistem yalnızca `GET /guncel` uç noktası ve anlık durum verileri için devreye girer. Saatlik veya 5 günlük tahminleri kapsamaz. 
* **HTTP 200 Yanıtı:** Kesinti anında `GET /hava-durumu` uç noktası hata fırlatmaz ve başarılı yanıt (`200 OK`) dönmeye devam eder. Bu senaryoda anlık durum verisi (`guncel`) Open-Meteo'dan sağlanırken, tahmin verisi (`tahmin`) boş bir liste `[]` olarak döner.

### Veri Kaynağını Tespit Etme

API yanıtlarındaki `kaynak` alanı, verinin hangi servisten sağlandığını açıkça belirtir:

```json
{ "kaynak": "mgm" }         // Sistem normal çalışıyor
{ "kaynak": "open-meteo" }  // MGM'ye ulaşılamadı, yedek sistem devrede
```

## İl ve İlçe Sorgulama Davranışı

MGM altyapısı gereği, il ve ilçe sorgularında belirli kısıtlamalar bulunmaktadır:

* **Sadece İl ile Sorgulama:** `GET /istasyonlar/<il>` gibi sadece il parametresi içeren istekler, o ilin tüm ilçelerini **listelemez**. Bunun yerine MGM'nin o il için belirlediği **varsayılan istasyonun** (örneğin İstanbul için sadece Bakırköy) verilerini döner.
* **İlçe Sorgulama:** Belirli bir ilçenin verisine ulaşmak için ilçe adının açıkça (parametre olarak) belirtilmesi zorunludur. 
* **İlçe Listeleme:** Sistemin "bir ildeki tüm ilçeleri listele" gibi bir uç noktası bulunmamaktadır, çünkü MGM tarafında toplu ilçe listesi sunan bir servis yoktur.

---

## Akıllı Arama (`/ara`)

`/ara?q=...` esnek metin girdilerini kabul eden üç aşamalı bir arama motoru kullanır

**Desteklenen Sorgu Tipleri:**
1. **İl Adı:** Sadece il adı girildiğinde (ör. `q=ankara`) toleranslı arama yapılarak sonuç anında döndürülür.
2. **İl/İlçe Formatı:** Sorgu içinde `/`, `,` veya boşluk kullanılarak lokasyon belirtilebilir (ör. `kadikoy/istanbul`). Sistem bu parçaları analiz edip doğru il ve ilçeyi eşleştirir.
3. **Serbest Metin (Geocoding):** Doğrudan bir bölge veya semt adı girildiğinde (ör. `maslak` veya `maslak itü`), sistem Open-Meteo API'sini kullanarak lokasyonu tespit eder.

**Dönüş Davranışları:**
* Bulunan lokasyonun MGM'de karşılığı varsa standart hava durumu verisi döner.
* MGM'de karşılığı yoksa, o bölgenin anlık durumu Open-Meteo üzerinden sağlanır (`tahmin` listesi boş döner).
* Eğer aranan kelime çok genel bir ifadeyse ve farklı illerde birden fazla karşılığı varsa, sistem hatalı tahmin yapmak yerine seçenekleri listeleyerek `durum: "belirsiz"` yanıtı döner.

## Konum Bazlı Arama (`/konum`)

Verilen enlem ve boylam koordinatları üzerinden hava durumu bilgisini getirir. MGM doğrudan koordinat tabanlı arama desteklemediği için sistem arka planda ters coğrafi kodlama (reverse geocoding) kullanır.

### Çalışma Mantığı

1. Girilen koordinatlar [Nominatim](https://nominatim.org) kullanılarak il ve ilçe bilgisine dönüştürülür.
2. Tespit edilen il/ilçe verisiyle MGM üzerinden hava durumu sorgulanır ve detaylı veri döner
3. **Yedek Sistem:** Eğer verilen koordinatlar Türkiye sınırları dışında veya deniz üzerindeyse veya MGM'de karşılığı bulunmayan bir lokasyonsa, anlık hava durumu verisi otomatik olarak **Open-Meteo** servisinden sağlanır

> **Rate Limiting hakkında:**
> Bu uç nokta, arka planda ücretsiz Nominatim sunucularını kullandığı için saniyede 1 istek limitiyle çalışır. Projenin yerleşik Cache ve SWR yapısı tekrarlayan istekleri önleyerek bu limiti korur. Yüksek trafikli bir ortama dağıtım yapacaksanız kendi Nominatim sunucunuzu kurmanız veya alternatif bir servis kullanmanız önerilir

## Meteorolojik Uyarılar (`/uyarilar`) - Deneysel

Sistem, doğrulanmamış veri yapıları üzerinde hatalı dönüşümler (mapping) yapmamak adına, MGM'den dönen uyarı verilerini hiçbir alan adını değiştirmeden doğrudan istemciye iletir (passthrough). 

MGM'nin resmi MeteoUYARI sistemine (mgm.gov.tr/meteouyari) göre beklenen kavramsal şema şu şekildedir:

* **Şiddet (Renk Kodları):** Yeşil (Tehlike Yok) → Sarı (Potansiyel Tehlike) → Turuncu (Tehlikeli) → Kırmızı (Çok Tehlikeli)
* **Hadise Tipi:** Soğuk, Sıcak, Sis, Zirai Don, Buzlanma ve Don, Toz Taşınımı, Kar Erimesi, Çığ, Kar, Gökgürültülü Sağanak Yağış, Rüzgar, Yağmur
* **Kapsam:** Bugün ve Yarın (İl/İlçe bazlı)

> **Not:** Geliştirme sürecinde aktif bir meteorolojik uyarı bulunmadığından, MGM'nin JSON yanıtındaki kesin alan (field) isimleri henüz tam olarak doğrulanmamış ve haritalanmamıştır. Bu nedenle veri olduğu gibi aktarılır.

### İl Bazlı Filtreleme Davranışı

Sorgu içinde gönderilen `il` parametresi MGM sunucularına doğrudan iletilir. Ancak aktif uyarı eksikliği nedeniyle MGM'nin uç noktasında bu filtrelemenin çalışıp çalışmadığı henüz doğrulanamamıştır. MGM'nin bu parametreyi yoksayması senaryosunda sistem hata fırlatmaz. En kötü senaryoda herhangi bir filtre uygulanmamış tam uyarı listesini döndürür.

## Environment Variables

Sistemin ağ istekleri, önbellek stratejileri, güvenlik politikaları ve sunucu ayarları ortam değişkenleri üzerinden yapılandırılmaktadır. Tüm değişkenlerin varsayılan değerleriyle birlikte listelendiği örnek bir şablon için [`.env.example`](../.env.example) dosyasına göz atınız.

### 1. MGM İstemcisi (Timeout ve Retry)

MGM uç noktalarına yapılan isteklerin zaman aşımı ve hata durumundaki yeniden deneme davranışlarını kontrol eder.

| Değişken | Varsayılan Değer |
|---|---|
| `MGM_TIMEOUT` | `10` |
| `MGM_RETRY_TOTAL` | `3` |
| `MGM_RETRY_BACKOFF` | `0.3` |

### 2. Önbellek ve SWR Katmanı

In-memory veya Redis önbellek altyapısının TTL sürelerini ve kapasite sınırlarını belirler

| Değişken | Varsayılan Değer |
|---|---|
| `MGM_CACHE_TTL` | `60` |
| `MGM_STALE_WHILE_REVALIDATE` | `300` |
| `MGM_CACHE_MAX_ENTRIES` | `512` |
| `MGM_REDIS_URL` | *(Tanımsız - Redis Kapalı)* |
| `MGM_REDIS_PREFIX` | `mgm-cache` |

### 3. Circuit Breaker

Hata oranları yükseldiğinde MGM sunucularına giden yükü kesmek için kullanılan eşik ve bekleme değerlerini içerir.

| Değişken | Varsayılan Değer |
|---|---|
| `MGM_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` |
| `MGM_CIRCUIT_BREAKER_WINDOW_SECONDS` | `30` |
| `MGM_CIRCUIT_BREAKER_OPEN_SECONDS` | `60` |

### 4. CORS ve Güvenlik

| Değişken | Varsayılan Değer |
|---|---|
| `APP_CORS_ALLOW_ORIGIN` | `*` |

> **Headers:** Sunucu, tüm API yanıtlarına otomatik olarak `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` ve `Content-Security-Policy` güvenlik başlıklarını ekler.

### 5. Rate Limit

| Değişken | Varsayılan Değer |
|---|---|
| `APP_RATE_LIMIT_WINDOW_SECONDS` | `60` |
| `APP_RATE_LIMIT_MAX_REQUESTS` | `60` |

> **Limit Aşımı:** Aynı IP adresi üzerinden belirlenen zaman penceresi içinde (window) maksimum limit aşıldığında sistem `429 Too Many Requests` hatası döndürür. (Not: `/health`, `/docs` ve `/openapi.yaml` uç noktaları bu sınırlamadan muaftır).

### 6. Sunucu Yapılandırması

Uygulamanın ağ üzerinde nasıl ayağa kalkacağını ve hangi sunucu motorunu kullanacağını belirler.

| Değişken | Varsayılan Değer |
|---|---|
| `APP_HOST` | `0.0.0.0` (Docker) / `127.0.0.1` (Yerel) |
| `APP_PORT` | `5000` |
| `APP_SERVER` | `waitress` |
| `FLASK_DEBUG` | *(Sadece `APP_SERVER=flask` iken aktiftir)* |