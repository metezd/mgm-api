"""
app.py
------
mgm_client.MGMWeather sınıfını basit bir REST API olarak dışarı açan
Flask uygulaması.

Çalıştırma:
    pip install -r requirements.txt
    python app.py
    # varsayılan olarak http://127.0.0.1:5000 üzerinde çalışır

Uç noktalar:
    GET /istasyonlar/<il>
        -> İldeki tüm istasyonları listeler

    GET /guncel/<il>?ilce=<ilce>
        -> Anlık hava durumu

    GET /tahmin/<il>?ilce=<ilce>
        -> 5 günlük tahmin

    GET /hava-durumu/<il>?ilce=<ilce>
        -> Güncel durum + tahmin + gün doğumu ve batımı

    GET /hava-kalitesi/<il>?ilce=<ilce>
        -> Anlık UV indeksi ve hava kalitesi (PM10, PM2.5, NO2).
           İstanbul için öncelik İBB'nin resmi ölçüm ağındadır,
           PM2.5/UV ve İBB'nin kapsamadığı yerler Open-Meteo'dan gelir.

    GET /gun-ay-bilgisi/<il>?ilce=<ilce>
        -> Verilen il/ilçe (koordinat) için gün doğumu, gün batımı
           (sunrise-sunset.org) ve ay evresi. /hava-durumu yanıtına da
           "ayEvresi" alanı olarak otomatik eklenir.

    GET /sondurum/en-dusuk-sicakliklar?tarih=YYYY-MM-DD
    GET /sondurum/en-yuksek-sicakliklar?tarih=YYYY-MM-DD
        -> Türkiye geneli, tüm istasyonlar için gerçekleşen en düşük/
           en yüksek sıcaklıklar. tarih verilmezse en güncel gün.

    GET /polen/<il>?ilce=<ilce>
        -> Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu,
           zeytin, ambrosia). Open-Meteo Air Quality API (CAMS Avrupa)
           üzerinden; sezon dışı/kapsam dışı türler için "Veri Yok"
           döner. Seviyeler (Düşük/Orta/Yüksek/Çok Yüksek) yaklaşık
           sınıflandırmadır, kesin klinik eşik değildir.

    GET /deniz/<il>?ilce=<ilce>&lat=&lon=
        -> Deniz suyu sıcaklığı ÖNCELİKLE MGM'nin Piri Reis istasyon verisinden, 
           başarısız olursa Open-Meteo Marine API'sinden gelir 
           dalga verisi her zaman Open-Meteo'dan gelir (Piri Reis kaynağında yok). 
           "kaynaklar" alanı hangi verinin nereden geldiğini gösterir. 
           Kıyıya daha yakın bir koordinat için isteğe bağlı ?lat=&lon= ile override
           edilebilir. İkisi de kapsam dışıysa tüm alanlar null döner
           "kapsamDisi": true ile işaretlenir

    GET /map/geojson
        -> Türkiye il sınırları (GeoJSON) + MGM son durum sıcaklıkları
           birleştirilmiş, doğrudan Leaflet/Mapbox'a beslenebilecek
           tek bir FeatureCollection. Yanıt "basarili" sarmalayıcısı
           OLMADAN saf GeoJSON olarak döner

    GET /don-uyarisi/<il>?ilce=<ilce>
        -> Tarımsal don/kırağı riski: 5 günlük tahminin en düşük
           sıcaklığına dayalı sezgisel risk sınıflandırması (Kırağı
           Riski / Hafif-Orta-Kuvvetli-Çok Kuvvetli Don) + düşük
           rüzgar/yüksek nem koşullarında kırağı uygunluk işareti.
           MGM'nin resmi bir don uyarı ürünü DEĞİLDİR, türetilmiş bir
           göstergedir.

    POST /favoriler/<liste_id>
        -> {"sorgu": "kadikoy/istanbul"} gövdesiyle bir favori ekler.
           <liste_id> istemcinin kendi seçtiği/ürettiği bir kimliktir
           (hesap/kimlik doğrulama YOKTUR — liste_id'yi bilen herkes
           listeyi okur/düzenler, tıpkı paylaşılmayan bir Pastebin
           linki gibi ele alınmalıdır). Liste başına en fazla
           APP_FAVORI_MAX_KAYIT (varsayılan 30) kayıt.

    DELETE /favoriler/<liste_id>
        -> {"sorgu": "kadikoy/istanbul"} gövdesiyle bir favoriyi siler.

    GET /favoriler/<liste_id>
        -> Listedeki tüm sorgular için hava durumunu tek istekte döner
           (/toplu ile aynı akıllı çözümleyici + paralel yürütme,
           kısmi başarısızlığa toleranslı).

    GET /favoriler/<liste_id>/liste
        -> Hava durumu çekmeden, yalnızca kayıtlı sorguları döner
           (hafif; liste yönetimi arayüzleri için).

    Kalıcılık: REDIS_URL tanımlıysa favoriler Redis'te tutulur (yeniden
    başlatmalarda kalıcı, worker/instance'lar arası paylaşılır,
    APP_FAVORI_TTL_SANIYE varsayılan 90 gün hareketsizlikte düşer).
    Redis yoksa süreç-içi belleğe düşülür — yalnızca tek worker/geliştirme
    ortamı için uygundur, süreç yeniden başladığında kaybolur.

Rate limiting:
    IP başına APP_RATE_LIMIT_MAX_REQUESTS (varsayılan 60) istek /
    APP_RATE_LIMIT_WINDOW_SECONDS (varsayılan 60sn). /map/geojson soğuk
    cache'te 81 il için paralel istek attığından ayrı ve daha sıkı bir
    limite (APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS, varsayılan 10) tabidir.
    Redis yapılandırılmışsa (REDIS_URL) sayaç Redis'te tutulur ve tüm
    worker/instance'lar arasında paylaşılır; Redis yoksa/erişilemezse
    süreç-içi belleğe düşülür (tek worker'da doğru, çoklu worker'da
    worker başına ayrı sayılır).

Örnek:
    curl "http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy"
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, g, jsonify, request
from flask_compress import Compress
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from mgm_client import MGMWeather, MGMWeatherError, turkiye_illeri

app = Flask(__name__)
logger = logging.getLogger(__name__)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# gzip/br response compression.
app.config["COMPRESS_MIMETYPES"] = [
    "application/json",
    "application/yaml",
    "text/html",
]
Compress(app)
mgm = MGMWeather(
    timeout=int(os.getenv("MGM_TIMEOUT", "10")),
    retry_total=int(os.getenv("MGM_RETRY_TOTAL", "3")),
    retry_backoff=float(os.getenv("MGM_RETRY_BACKOFF", "0.3")),
    cache_ttl_seconds=int(os.getenv("MGM_CACHE_TTL", "60")),
    cache_max_entries=int(os.getenv("MGM_CACHE_MAX_ENTRIES", "512")),
    stale_while_revalidate_seconds=int(
        os.getenv("MGM_STALE_WHILE_REVALIDATE", "300")
    ),
    circuit_breaker_failure_threshold=int(
        os.getenv("MGM_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
    ),
    circuit_breaker_window_seconds=float(
        os.getenv("MGM_CIRCUIT_BREAKER_WINDOW_SECONDS", "30")
    ),
    circuit_breaker_open_seconds=float(
        os.getenv("MGM_CIRCUIT_BREAKER_OPEN_SECONDS", "60")
    ),
    guncel_dinamik_ttl_aktif=os.getenv("MGM_GUNCEL_DINAMIK_TTL", "1") not in {"0", "false", "False"},
    guncel_sicak_pencere_baslangic_dk=int(
        os.getenv("MGM_GUNCEL_SICAK_PENCERE_BASLANGIC_DK", "5")
    ),
    guncel_sicak_pencere_bitis_dk=int(
        os.getenv("MGM_GUNCEL_SICAK_PENCERE_BITIS_DK", "15")
    ),
    guncel_sicak_ttl_saniye=int(os.getenv("MGM_GUNCEL_SICAK_TTL_SANIYE", "120")),
    guncel_soguk_ttl_saniye=int(os.getenv("MGM_GUNCEL_SOGUK_TTL_SANIYE", "1800")),
    guncel_gece_baslangic_saat=int(os.getenv("MGM_GUNCEL_GECE_BASLANGIC_SAAT", "0")),
    guncel_gece_bitis_saat=int(os.getenv("MGM_GUNCEL_GECE_BITIS_SAAT", "6")),
    guncel_gece_ttl_saniye=int(os.getenv("MGM_GUNCEL_GECE_TTL_SANIYE", "3600")),
    tahmin_ttl_saniye=int(os.getenv("MGM_TAHMIN_TTL_SANIYE", "10800")),
    hava_kalitesi_ttl_saniye=int(os.getenv("MGM_HAVA_KALITESI_TTL_SANIYE", "600")),
    ibb_istasyon_ttl_saniye=int(os.getenv("MGM_IBB_ISTASYON_TTL_SANIYE", "21600")),
    ibb_max_mesafe_km=float(os.getenv("MGM_IBB_MAX_MESAFE_KM", "40")),
    geojson_sinir_ttl_saniye=int(
        os.getenv("MGM_GEOJSON_SINIR_TTL_SANIYE", str(30 * 24 * 3600))
    ),
    harita_sicaklik_ttl_saniye=int(
        os.getenv("MGM_HARITA_SICAKLIK_TTL_SANIYE", "600")
    ),
    deniz_ttl_saniye=int(os.getenv("MGM_DENIZ_TTL_SANIYE", "1800")),
    sondurum_ttl_saniye=int(os.getenv("MGM_SONDURUM_TTL_SANIYE", "1800")),
    piri_reis_ttl_saniye=int(os.getenv("MGM_PIRI_REIS_TTL_SANIYE", "1800")),
    piri_reis_max_mesafe_km=float(os.getenv("MGM_PIRI_REIS_MAX_MESAFE_KM", "60")),
    redis_url=os.getenv("REDIS_URL") or os.getenv("MGM_REDIS_URL") or None,
    redis_prefix=os.getenv("MGM_REDIS_PREFIX", "mgm-cache:"),
)
CORS_ALLOW_ORIGIN = os.getenv("APP_CORS_ALLOW_ORIGIN", "*")
RATE_LIMIT_WINDOW = int(os.getenv("APP_RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_MAX = int(os.getenv("APP_RATE_LIMIT_MAX_REQUESTS", "60"))
# /map/geojson soğuk cache'te 81 il için paralel istek attığından
# (bkz. mgm.harita_geojson) genel limitten ayrı ve daha sıkı bir limite tabidir
MAP_GEOJSON_RATE_LIMIT_MAX = int(
    os.getenv("APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS", "10")
)
TOPLU_MAX_SORGU = int(os.getenv("APP_TOPLU_MAX_SORGU", "20"))

HTTP_ISTEK_SAYAC = Counter(
    "http_requests_total", "Toplam HTTP isteği", ["method", "endpoint", "status"]
)
HTTP_ISTEK_SURESI = Histogram(
    "http_request_duration_seconds", "İstek süresi (saniye)", ["method", "endpoint"]
)
RATE_LIMIT_RED_SAYAC = Counter(
    "mgm_rate_limit_rejected_total", "429 ile reddedilen istek sayısı"
)
CIRCUIT_BREAKER_DURUM_GAUGE = Gauge(
    "mgm_circuit_breaker_state", "0=kapali 1=yari-acik 2=acik (scrape anında okunur)"
)


RATE_LIMIT_BUCKETS: dict[str, deque] = defaultdict(deque)
RATE_LIMIT_BUCKETS_MAX_IZLENEN = int(
    os.getenv("APP_RATE_LIMIT_MAX_TRACKED_IPS", "50000")
)


def _mgm_redis_durumu():
    musait = bool(getattr(mgm, "_redis_available", False))
    client = getattr(mgm, "redis_client", None)
    prefix = getattr(mgm, "redis_prefix", "mgm-cache:")
    hata_sinifi = getattr(mgm, "_redis_error_cls", None) or Exception
    return (musait and client is not None), client, prefix, hata_sinifi


def _rate_limit_bellek_temizle() -> None:
    """Boşalmış (pencere dışına çıkmış) bucket'ları dict'ten siler.
    Her istekte değil, dict büyüdüğünde ve düşük olasılıkla çağrılır."""
    if len(RATE_LIMIT_BUCKETS) <= RATE_LIMIT_BUCKETS_MAX_IZLENEN:
        return
    bos_anahtarlar = [k for k, v in RATE_LIMIT_BUCKETS.items() if not v]
    for k in bos_anahtarlar:
        del RATE_LIMIT_BUCKETS[k]


def _rate_limit_kontrol(
    ip: str, kapsam: str, limit: int, window_seconds: int
) -> tuple[bool, int, int]:
    """
    (izin_verildi_mi, kalan_hak, reset_epoch) döner.

    Redis yapılandırılmış ve erişilebilirse INCR+EXPIRE tabanlı sabit
    pencere sayaç kullanılır (mgm.redis_client ile paylaşılan bağlantı)
    bu sayede birden fazla instance aynı limiti paylaşır. Redis
    yoksa veya o an erişilemezse, isteği reddetmek yerine 
    kaydırmalı pencereye (deque) düşülür.
    """
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            pencere = int(time.time() // window_seconds)
            redis_key = f"{redis_prefix}ratelimit:{kapsam}:{ip}:{pencere}"
            pipe = redis_client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds)
            sayac, _ = pipe.execute()
            reset_epoch = (pencere + 1) * window_seconds
            kalan = max(0, limit - int(sayac))
            return int(sayac) <= limit, kalan, reset_epoch
        except redis_hata_sinifi:
            logger.warning(
                "Rate limit için Redis'e erişilemedi, süreç-içi belleğe düşülüyor."
            )
            # aşağıya düş

    now = time.monotonic()
    bucket_anahtari = f"{kapsam}:{ip}"
    bucket = RATE_LIMIT_BUCKETS[bucket_anahtari]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()

    kalan_sure = max(0.0, window_seconds - (now - bucket[0])) if bucket else 0.0
    reset_epoch = int(time.time() + kalan_sure)

    if len(bucket) >= limit:
        return False, 0, reset_epoch

    bucket.append(now)
    if random.random() < 0.01:  # ~her 100 istekte bir fırsatçı temizlik
        _rate_limit_bellek_temizle()
    return True, max(0, limit - len(bucket)), reset_epoch


# Hesap/kimlik doğrulama yok: liste_id istemcinin kendi seçtiği bir
# kimliktir cihazda üretilip saklanan bir UUID. liste_id'yi bilen
# herkes o listeyi okur haberiniz olsun
FAVORI_MAX_KAYIT = int(os.getenv("APP_FAVORI_MAX_KAYIT", "30"))
FAVORI_TTL_SANIYE = int(os.getenv("APP_FAVORI_TTL_SANIYE", str(90 * 24 * 3600)))
FAVORI_LISTE_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
FAVORI_SORGU_MAX_UZUNLUK = 100


_FAVORI_BELLEK: dict[str, dict[str, dict]] = defaultdict(dict)
_FAVORI_BELLEK_KILIT = threading.Lock()


class FavoriHatasi(Exception):
    """Favoriler özelliğine özgü kullanıcı hataları (limit aşımı, geçersiz girdi vb.)."""


def _favori_liste_id_gecerli(liste_id: str) -> bool:
    return bool(FAVORI_LISTE_ID_REGEX.match(liste_id))


def _favori_redis_key(liste_id: str, prefix: str) -> str:
    return f"{prefix}favoriler:{liste_id}"


def _favori_kayit_olustur(sorgu: str) -> dict:
    return {
        "sorgu": sorgu,
        "eklenmeTarihi": _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def _favori_ekle(liste_id: str, sorgu: str) -> dict:
    sorgu = sorgu.strip()
    if not sorgu:
        raise FavoriHatasi("'sorgu' boş olamaz.")
    if len(sorgu) > FAVORI_SORGU_MAX_UZUNLUK:
        raise FavoriHatasi(
            f"'sorgu' en fazla {FAVORI_SORGU_MAX_UZUNLUK} karakter olabilir."
        )
    slug = sorgu.lower()
    kayit = _favori_kayit_olustur(sorgu)

    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            key = _favori_redis_key(liste_id, redis_prefix)
            mevcut_alanlar = {
                a.decode() if isinstance(a, bytes) else a
                for a in redis_client.hkeys(key)
            }
            if slug not in mevcut_alanlar and len(mevcut_alanlar) >= FAVORI_MAX_KAYIT:
                raise FavoriHatasi(
                    f"Bu listede en fazla {FAVORI_MAX_KAYIT} kayıt olabilir."
                )
            redis_client.hset(key, slug, json.dumps(kayit, ensure_ascii=False))
            redis_client.expire(key, FAVORI_TTL_SANIYE)
            return kayit
        except redis_hata_sinifi:
            logger.warning(
                "Favori eklenemedi, bellek içi depolamaya düşülüyor."
            )

    with _FAVORI_BELLEK_KILIT:
        liste = _FAVORI_BELLEK[liste_id]
        if slug not in liste and len(liste) >= FAVORI_MAX_KAYIT:
            raise FavoriHatasi(f"Bu listede en fazla {FAVORI_MAX_KAYIT} kayıt olabilir.")
        liste[slug] = kayit
    return kayit


def _favori_sil(liste_id: str, sorgu: str) -> bool:
    slug = sorgu.strip().lower()
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            key = _favori_redis_key(liste_id, redis_prefix)
            silindi = redis_client.hdel(key, slug)
            return bool(silindi)
        except redis_hata_sinifi:
            logger.warning(
                "Favori silinemedi, bellek içi depolamaya düşülüyor."
            )

    with _FAVORI_BELLEK_KILIT:
        liste = _FAVORI_BELLEK.get(liste_id, {})
        if slug in liste:
            del liste[slug]
            return True
    return False


def _favori_listele(liste_id: str) -> list[dict]:
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            key = _favori_redis_key(liste_id, redis_prefix)
            ham = redis_client.hgetall(key)
            sonuc = []
            for deger in ham.values():
                try:
                    sonuc.append(json.loads(deger))
                except (json.JSONDecodeError, TypeError):
                    continue
            return sorted(sonuc, key=lambda k: k.get("eklenmeTarihi") or "")
        except redis_hata_sinifi:
            logger.warning(
                "Favoriler listelenemedi, bellek içi depolama okunuyor."
            )

    with _FAVORI_BELLEK_KILIT:
        liste = _FAVORI_BELLEK.get(liste_id, {})
        return sorted(liste.values(), key=lambda k: k.get("eklenmeTarihi") or "")


def _hata_yanit(exc: Exception, kod: int = 502):
    return jsonify({"basarili": False, "hata": str(exc)}), kod


def _hava_durumu_akilli_guvenli(sorgu: str) -> dict:
    """/toplu ve /favoriler/<liste_id> tarafından paylaşılır: serbest
    metin sorguyu çözüp hava durumunu döner, hata durumunda kısmi
    başarısızlığa tolerans için exception'ı yakalayıp sonuç sözlüğüne
    gömer (üst çağrı 200 dönmeye devam eder)."""
    sorgu = sorgu.strip()
    try:
        veri = mgm.hava_durumu_akilli(sorgu)
        return {"sorgu": sorgu, "basarili": True, "veri": veri}
    except MGMWeatherError as exc:
        return {"sorgu": sorgu, "basarili": False, "hata": str(exc)}


def _istasyon_id_getir(il: str, ilce: str | None) -> int | str:
    istasyon = mgm.ilce_istasyonu(il, ilce)
    istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
    if istasyon_id is None:
        raise MGMWeatherError(f"'{il}' için geçerli istasyon kimliği bulunamadı.")
    return istasyon_id


def _istasyon_ve_konum_getir(
    il: str, ilce: str | None
) -> tuple[int | str, float | None, float | None]:
    """
    _istasyon_id_getir'e ek olarak enlem/boylam da döner
    fallback'i (guncel_durum_yedekli) için gerekli.
    """
    istasyon = mgm.ilce_istasyonu(il, ilce)
    istasyon_id = istasyon.get("istasyonId") or istasyon.get("merkezId")
    if istasyon_id is None:
        raise MGMWeatherError(f"'{il}' için geçerli istasyon kimliği bulunamadı.")
    enlem = istasyon.get("enlem") or istasyon.get("lat")
    boylam = istasyon.get("boylam") or istasyon.get("lon")
    return istasyon_id, enlem, boylam


@app.before_request
def istek_zamanlayici():
    g.metrik_baslangic = time.monotonic()


@app.before_request
def rate_limit():
    if request.method == "OPTIONS":
        return None
    if request.path in {"/health", "/docs", "/openapi.yaml", "/metrics"}:
        return None

    ip = request.remote_addr or "unknown"
    window_seconds = max(1, RATE_LIMIT_WINDOW)
    if request.path == "/map/geojson":
        limit = max(1, MAP_GEOJSON_RATE_LIMIT_MAX)
        kapsam = "map-geojson"
    else:
        limit = max(1, RATE_LIMIT_MAX)
        kapsam = "genel"

    izin_verildi, kalan, reset_epoch = _rate_limit_kontrol(
        ip, kapsam, limit, window_seconds
    )
    g.rl_limit = limit
    g.rl_remaining = kalan
    g.rl_reset_epoch = reset_epoch

    if not izin_verildi:
        RATE_LIMIT_RED_SAYAC.inc()
        retry_after = max(1, reset_epoch - int(time.time()))
        response = jsonify({
            "basarili": False,
            "hata": "Çok fazla istek gönderdiniz. Lütfen birkaç saniye sonra tekrar deneyin.",
        })
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response


@app.after_request
def guvenlik_ve_cors_headerlari(response):
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none';"

    if hasattr(g, "rl_limit"):
        response.headers["X-RateLimit-Limit"] = str(g.rl_limit)
        response.headers["X-RateLimit-Remaining"] = str(g.rl_remaining)
        response.headers["X-RateLimit-Reset"] = str(g.rl_reset_epoch)
    return response


@app.after_request
def metrik_kaydet(response):
    endpoint = request.endpoint or "eslesmedi"  # 404 gibi durumlarda route yok
    HTTP_ISTEK_SAYAC.labels(
        method=request.method, endpoint=endpoint, status=response.status_code
    ).inc()
    baslangic = getattr(g, "metrik_baslangic", None)
    if baslangic is not None:
        HTTP_ISTEK_SURESI.labels(method=request.method, endpoint=endpoint).observe(
            time.monotonic() - baslangic
        )
    return response


@app.get("/metrics")
def metrics():
    CIRCUIT_BREAKER_DURUM_GAUGE.set(
        {"kapali": 0, "yari-acik": 1, "acik": 2}.get(
            mgm.circuit_breaker_saglik_ozeti()["durum"], -1
        )
    )
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.get("/openapi.yaml")
def openapi_spec():
    with open(os.path.join(_BASE_DIR, "openapi.yaml"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/yaml")


@app.get("/docs")
def docs():
    # Swagger UI'ı CDN'den yükleyen minimal bir HTML sayfası. Yeni bir
    # Python bağımlılığı (flask-smorest, apispec vb.) eklemeden, elle
    # yazılmış openapi.yaml'i görselleştirir.
    html = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <title>Hava Durumu API — Dokümantasyon</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css" />
  <style>body { margin: 0; }</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: "/openapi.yaml",
        dom_id: "#swagger-ui",
        presets: [SwaggerUIBundle.presets.apis],
      });
    };
  </script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/iller")
def iller():
    return jsonify({"basarili": True, "veri": turkiye_illeri()})


@app.get("/ara")
def ara():
    sorgu = request.args.get("q", "").strip()
    if not sorgu:
        return jsonify({"basarili": False, "hata": "'q' parametresi zorunludur."}), 400
    try:
        veri = mgm.hava_durumu_akilli(sorgu)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/uyarilar")
def uyarilar():
    il = request.args.get("il")
    try:
        return jsonify({"basarili": True, "veri": mgm.uyarilar(il)})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/konum")
def konum():
    lat_str = request.args.get("lat")
    lon_str = request.args.get("lon")
    if not lat_str or not lon_str:
        return jsonify(
            {"basarili": False, "hata": "'lat' ve 'lon' parametreleri zorunludur."}
        ), 400
    try:
        enlem = float(lat_str)
        boylam = float(lon_str)
    except ValueError:
        return jsonify(
            {"basarili": False, "hata": "'lat' ve 'lon' geçerli birer sayı olmalıdır."}
        ), 400
    if not (-90 <= enlem <= 90) or not (-180 <= boylam <= 180):
        return jsonify(
            {"basarili": False, "hata": "'lat' -90..90, 'lon' -180..180 aralığında olmalıdır."}
        ), 400
    try:
        veri = mgm.hava_durumu_konum(enlem, boylam)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.post("/toplu")
def toplu():
    """
    Tek istekte birden çok yer için hava durumu — her sorgu, /ara ile
    aynı akıllı çözümleyiciyi kullanır, yani "istanbul", "kadikoy/istanbul", 
    "maslak itü" gibi serbest metinler burada da geçerlidir.

    Kısmi başarısızlığa toleranslıdır: bir sorgu çözülemese bile diğerleri
    etkilenmez — yanıt her zaman 200 döner (batch'in kendisi işlendi),
    her öğe kendi basarili/hata durumunu taşır. Sonuç dizisi, istek
    sırasıyla birebir aynı sırada döner

    Batch boyutu TOPLU_MAX_SORGU ile sınırlıdır: aksi halde tek bir
    isteğe çok sayıda sorgu sıkıştırmak, rate limit'i fiilen bypass
    etmenin bir yolu olurdu
    """
    if not request.is_json:
        return jsonify(
            {"basarili": False, "hata": "İstek gövdesi JSON olmalıdır (Content-Type: application/json)."}
        ), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "sorgular" not in body:
        return jsonify(
            {"basarili": False, "hata": "'sorgular' alanı zorunludur (bir dizi metin sorgusu)."}
        ), 400

    sorgular = body["sorgular"]
    if not isinstance(sorgular, list) or not sorgular:
        return jsonify(
            {"basarili": False, "hata": "'sorgular' boş olmayan bir dizi olmalıdır."}
        ), 400
    if len(sorgular) > TOPLU_MAX_SORGU:
        return jsonify(
            {
                "basarili": False,
                "hata": f"En fazla {TOPLU_MAX_SORGU} sorgu gönderebilirsiniz "
                f"(gönderilen: {len(sorgular)}).",
            }
        ), 400
    if not all(isinstance(s, str) and s.strip() for s in sorgular):
        return jsonify(
            {"basarili": False, "hata": "'sorgular' listesindeki her öğe boş olmayan bir metin olmalıdır."}
        ), 400

    # MGMWeather client thread safe yapıdadır
    # Paralel yürütme sayesinde N adet sorgunun toplam gecikmesi, 
    # sıralı toplam yerine en yavaş tekil sorgu süresine indirgenir.
    with ThreadPoolExecutor(max_workers=min(len(sorgular), 10)) as havuz:
        sonuclar = list(havuz.map(_hava_durumu_akilli_guvenli, sorgular))

    return jsonify({"basarili": True, "veri": sonuclar})


@app.post("/favoriler/<liste_id>")
def favori_ekle(liste_id: str):
    """Bir favoriye sorgu ekler/günceller — bkz. modül docstring'i."""
    if not _favori_liste_id_gecerli(liste_id):
        return jsonify(
            {
                "basarili": False,
                "hata": "liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).",
            }
        ), 400
    if not request.is_json:
        return jsonify(
            {"basarili": False, "hata": "İstek gövdesi JSON olmalıdır (Content-Type: application/json)."}
        ), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("sorgu"), str):
        return jsonify(
            {"basarili": False, "hata": "'sorgu' alanı zorunludur (bir metin)."}
        ), 400
    try:
        kayit = _favori_ekle(liste_id, body["sorgu"])
    except FavoriHatasi as exc:
        return jsonify({"basarili": False, "hata": str(exc)}), 400
    return jsonify({"basarili": True, "veri": kayit})


@app.delete("/favoriler/<liste_id>")
def favori_sil(liste_id: str):
    """Bir favoriden sorgu siler — bkz. modül docstring'i."""
    if not _favori_liste_id_gecerli(liste_id):
        return jsonify(
            {
                "basarili": False,
                "hata": "liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).",
            }
        ), 400
    if not request.is_json:
        return jsonify(
            {"basarili": False, "hata": "İstek gövdesi JSON olmalıdır (Content-Type: application/json)."}
        ), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("sorgu"), str):
        return jsonify(
            {"basarili": False, "hata": "'sorgu' alanı zorunludur (bir metin)."}
        ), 400
    silindi = _favori_sil(liste_id, body["sorgu"])
    if not silindi:
        return jsonify(
            {"basarili": False, "hata": "Bu sorgu listede bulunamadı."}
        ), 404
    return jsonify({"basarili": True})


@app.get("/favoriler/<liste_id>/liste")
def favori_liste_goster(liste_id: str):
    """Hava durumu çekmeden yalnızca kayıtlı sorguları döner (hafif)."""
    if not _favori_liste_id_gecerli(liste_id):
        return jsonify(
            {
                "basarili": False,
                "hata": "liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).",
            }
        ), 400
    return jsonify({"basarili": True, "veri": _favori_listele(liste_id)})


@app.get("/favoriler/<liste_id>")
def favori_hava_durumu(liste_id: str):
    """
    Listedeki tüm sorgular için hava durumunu tek istekte döner (/toplu
    ile aynı akıllı çözümleyici + paralel yürütme, kısmi başarısızlığa
    toleranslı).
    """
    if not _favori_liste_id_gecerli(liste_id):
        return jsonify(
            {
                "basarili": False,
                "hata": "liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).",
            }
        ), 400
    kayitlar = _favori_listele(liste_id)
    if not kayitlar:
        return jsonify({"basarili": True, "veri": []})

    sorgular = [k["sorgu"] for k in kayitlar]
    with ThreadPoolExecutor(max_workers=min(len(sorgular), 10)) as havuz:
        sonuclar = list(havuz.map(_hava_durumu_akilli_guvenli, sorgular))

    return jsonify({"basarili": True, "veri": sonuclar})


@app.get("/istasyonlar/<il>")
def istasyonlar(il: str):
    try:
        return jsonify({"basarili": True, "veri": mgm.il_istasyonlari(il)})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/health")
def health():
    devre_durumu = mgm.circuit_breaker_saglik_ozeti()["durum"]

    deep = request.args.get("deep", "").strip().lower() in {"1", "true", "yes", "on"}
    if not deep:
        return jsonify(
            {
                "basarili": True,
                "durum": "ok",
                "servis": "hava-durumu",
                "mgm": "skip",
                "redis": "skip",
                "circuit_breaker": devre_durumu,
            }
        )

    redis_durum = mgm.redis_saglik_ozeti()
    if redis_durum["durum"] == "hata":
        return (
            jsonify(
                {
                    "basarili": False,
                    "durum": "degraded",
                    "servis": "hava-durumu",
                    "mgm": "skip",
                    "redis": "hata",
                    "circuit_breaker": devre_durumu,
                    "hata": redis_durum["hata"],
                }
            ),
            503,
        )

    try:
        mgm.il_istasyonlari("Ankara")
        return jsonify(
            {
                "basarili": True,
                "durum": "ok",
                "servis": "hava-durumu",
                "mgm": "ok",
                "redis": redis_durum["durum"],
                "circuit_breaker": mgm.circuit_breaker_saglik_ozeti()["durum"],
            }
        )
    except MGMWeatherError as exc:
        return (
            jsonify(
                {
                    "basarili": False,
                    "durum": "degraded",
                    "servis": "hava-durumu",
                    "mgm": "hata",
                    "redis": redis_durum["durum"],
                    "circuit_breaker": mgm.circuit_breaker_saglik_ozeti()["durum"],
                    "hata": str(exc),
                }
            ),
            503,
        )


@app.get("/guncel/<il>")
def guncel(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        veri = mgm.guncel_durum_yedekli(istasyon_id, enlem, boylam)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/tahmin/<il>")
def tahmin(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id = _istasyon_id_getir(il, ilce)
        veri = mgm.gunluk_tahmin(istasyon_id)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/saatlik/<il>")
def saatlik(il: str):
    ilce = request.args.get("ilce")
    try:
        istasyon_id = _istasyon_id_getir(il, ilce)
        veri = mgm.saatlik_tahmin(istasyon_id)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/hava-durumu/<il>")
def hava_durumu(il: str):
    ilce = request.args.get("ilce")
    try:
        veri = mgm.hava_durumu(il, ilce)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/hava-kalitesi/<il>")
def hava_kalitesi(il: str):
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                "hava kalitesi koordinat gerektirir."
            )
        veri = mgm.hava_kalitesi(float(enlem), float(boylam))
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/gun-ay-bilgisi/<il>")
def gun_ay_bilgisi(il: str):
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                "gün doğumu/batımı koordinat gerektirir."
            )
        veri: dict = {}
        veri.update(mgm.gun_dogumu_batimi(float(enlem), float(boylam)))
        veri["ayEvresi"] = mgm.ay_evresi()
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/sondurum/en-dusuk-sicakliklar")
def sondurum_en_dusuk():
    tarih = request.args.get("tarih")
    try:
        veri = mgm.en_dusuk_sicakliklar(tarih)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/sondurum/en-yuksek-sicakliklar")
def sondurum_en_yuksek():
    tarih = request.args.get("tarih")
    try:
        veri = mgm.en_yuksek_sicakliklar(tarih)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/polen/<il>")
def polen(il: str):
    """
    Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu, zeytin,
    ambrosia). Open-Meteo Air Quality API (CAMS Avrupa) üzerinden;
    yalnızca ilgili türün sezonunda ve modelin kapsadığı konumlarda veri
    döner, aksi halde `seviye: "Veri Yok"` ile işaretlenir.
    """
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                "polen indeksi koordinat gerektirir."
            )
        veri = mgm.polen_indeksi(float(enlem), float(boylam))
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/deniz/<il>")
def deniz(il: str):
    """
    Anlık deniz suyu sıcaklığı + dalga durumu (yükseklik, periyot, yön).
    Sıcaklık öncelikle MGM Piri Reis istasyon verisinden, kapsam
    dışı/başarısız olursa Open-Meteo Marine API'sinden; dalga verisi
    her zaman Open-Meteo'dan gelir. Varsayılan olarak il/ilçenin
    istasyon koordinatı kullanılır — kıyı ilçeleri için daha isabetlidir.
    Kıyıya daha yakın özel bir koordinat vermek için ?lat=&lon= geçilebilir
    (karasal bir il merkezi yerine, örn. bir plaj/koy koordinatı).
    """
    ilce = request.args.get("ilce")
    lat_str = request.args.get("lat")
    lon_str = request.args.get("lon")
    try:
        if lat_str and lon_str:
            try:
                enlem = float(lat_str)
                boylam = float(lon_str)
            except ValueError:
                return jsonify(
                    {"basarili": False, "hata": "'lat' ve 'lon' geçerli birer sayı olmalıdır."}
                ), 400
            if not (-90 <= enlem <= 90) or not (-180 <= boylam <= 180):
                return jsonify(
                    {"basarili": False, "hata": "'lat' -90..90, 'lon' -180..180 aralığında olmalıdır."}
                ), 400
        else:
            _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
            if enlem is None or boylam is None:
                raise MGMWeatherError(
                    f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı; "
                    "deniz durumu koordinat gerektirir."
                )
        veri = mgm.deniz_durumu(float(enlem), float(boylam))
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.get("/map/geojson")
def map_geojson():
    """
    İl sınırları GeoJSON'u + MGM son durum sıcaklıklarını tek bir
    FeatureCollection'da birleştirip döner. Doğrudan Leaflet/Mapbox gibi
    harita kütüphanelerine beslenebilir
    """
    try:
        veri = mgm.harita_geojson()
        return jsonify(veri)
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/don-uyarisi/<il>")
def don_uyarisi(il: str):
    """
    Tarımsal don/kırağı riski (5 günlük tahmin, sezgisel sınıflandırma).
    MGM'nin resmi bir don uyarı ürünü DEĞİLDİR — bkz. mgm.don_kiragi_riski
    docstring'i.
    """
    ilce = request.args.get("ilce")
    try:
        istasyon_id = _istasyon_id_getir(il, ilce)
        veri = mgm.don_kiragi_riski(istasyon_id, il=il, ilce=ilce)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 404)


@app.errorhandler(404)
def not_found(_exc):
    return jsonify({"basarili": False, "hata": "Uç nokta bulunamadı."}), 404


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "5000"))
    server = os.getenv("APP_SERVER", "waitress").strip().lower()

    if server == "waitress":
        try:
            from waitress import serve
        except ImportError as exc:
            raise RuntimeError(
                "Waitress kurulu değil. `pip install -r requirements.txt` çalıştırın "
                "veya geçici olarak `APP_SERVER=flask` ile başlatın."
            ) from exc
        serve(app, host=host, port=port)
    elif server == "flask":
        debug = os.getenv("FLASK_DEBUG", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        app.run(host=host, port=port, debug=debug)
    else:
        raise RuntimeError(
            f"Geçersiz APP_SERVER değeri: '{server}'. Geçerli değerler: waitress, flask."
        )
