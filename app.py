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

    GET /sondurum/toplam-yagis?tarih=YYYY-MM-DD
        -> Türkiye geneli toplam yağış ,tarih verilmezse en güncel gün.

    GET /sondurum/kar-kalinliklari
        -> Türkiye geneli anlık kar yüksekliği, tarih parametresi yok.

    GET /sondurum/son-gozlemler
        -> İl merkezlerinde anlık ölçüm (sıcaklık, nem, yağış, rüzgar,
           basınç, hadise).

    GET /polen/<il>?ilce=<ilce>
        -> Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu,
           zeytin, ambrosia). Open-Meteo Air Quality API (CAMS Avrupa)
           üzerinden. Sezon dışı/kapsam dışı türler için "Veri Yok"
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

    POST /favoriler
        -> Yeni public liste_id ile manage_token ve read_token üretir.

    POST /favoriler/<liste_id>
        -> {"sorgu": "kadikoy/istanbul"} gövdesiyle bir favori ekler.
           Authorization: Bearer <manage_token> gerekir. Liste başına en fazla
           APP_FAVORI_MAX_KAYIT (varsayılan 30) kayıt.

    DELETE /favoriler/<liste_id>
        -> {"sorgu": "kadikoy/istanbul"} gövdesiyle bir favoriyi siler.

    GET /favoriler/<liste_id>
        -> Listedeki tüm sorgular için hava durumunu tek istekte döner
           Authorization: Bearer <read_token> gerekir.
           (/toplu ile aynı akıllı çözümleyici + paralel yürütme,
           kısmi başarısızlığa toleranslı).

    GET /favoriler/<liste_id>/liste
        -> Hava durumu çekmeden, yalnızca kayıtlı sorguları döner
           (hafif, liste yönetimi arayüzleri için). read_token gerekir.

    Kalıcılık: REDIS_URL tanımlıysa favoriler Redis'te tutulur (yeniden
    başlatmalarda kalıcı, worker/instance'lar arası paylaşılır,
    APP_FAVORI_TTL_SANIYE varsayılan 90 gün hareketsizlikte düşer).
    Redis yoksa süreç-içi belleğe düşülür. Yalnızca tek worker/geliştirme
    ortamı için uygundur, süreç yeniden başladığında kaybolur.

    POST /alerts/<liste_id>
        -> {"tur": "weather.temp_threshold", "il": "İstanbul",
           "webhookUrl": "https://...", "esik": 38, "yon": "ustunde"}
           gövdesiyle bir alert kaydı ekler. manage_token gerekir.
           Desteklenen "tur" değerleri:
           weather.temp_threshold, weather.wind_gust_exceeded,
           weather.rain_threshold (eşik bazlı, koşul doğru olduğu her
           kontrolde tetiklenir), weather.rain_started,
           weather.rain_stopped, weather.warning_issued (olay bazlı,
           yalnızca durum değişiminde bir kez tetiklenir),
           weather.frost_risk (eşik bir don seviyesi adıdır, varsayılan
           "Hafif Don"). Liste başına en fazla APP_ALERT_MAX_KAYIT
           kayıt.

    DELETE /alerts/<liste_id>/<alert_id>
        -> Bir alert kaydını siler. manage_token gerekir.

    GET /alerts/<liste_id>
        -> Listedeki tüm alert kayıtlarını döner. read_token gerekir.

    POST /api/v1/alerts/check
        -> Authorization: Bearer <CRON_SECRET> header'ıyla korunur.
           Kayıtlı tüm alertleri değerlendirir, tetiklenenler için
           webhookUrl'e POST atar. Sunucu içinde zamanlayıcı YOKTUR,
           bu uç nokta dışarıdan periyodik çağrılmalıdır. Test
           amaçlı ENABLE_INTERNAL_SCHEDULER=true ile isteğe bağlı bir
           APScheduler tabanlı iç zamanlayıcı da açılabilir.

Rate limiting:
    IP başına APP_RATE_LIMIT_MAX_REQUESTS (varsayılan 60) istek /
    APP_RATE_LIMIT_WINDOW_SECONDS (varsayılan 60sn). /map/geojson soğuk
    cache'te 81 il için paralel istek attığından ayrı ve daha sıkı bir
    limite (APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS, varsayılan 10) tabidir.
    Redis yapılandırılmışsa (REDIS_URL) sayaç Redis'te tutulur ve tüm
    worker/instance'lar arasında paylaşılır. Redis yoksa/erişilemezse
    süreç-içi belleğe düşülür (tek worker'da doğru, çoklu worker'da
    worker başına ayrı sayılır).

Örnek:
    curl "http://127.0.0.1:5000/hava-durumu/Istanbul?ilce=Bakirkoy"
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import SplitResult, urlsplit

import requests
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
MAP_GEOJSON_RATE_LIMIT_MAX = int(
    os.getenv("APP_MAP_GEOJSON_RATE_LIMIT_MAX_REQUESTS", "2")
)
TOPLU_MAX_SORGU = int(os.getenv("APP_TOPLU_MAX_SORGU", "20"))
MAX_JSON_BODY_BYTES = int(os.getenv("APP_MAX_JSON_BODY_BYTES", str(64 * 1024)))
MAX_RESPONSE_BYTES = int(os.getenv("APP_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
MAX_DATE_RANGE_DAYS = int(os.getenv("APP_MAX_DATE_RANGE_DAYS", "31"))
ROUTE_RATE_LIMITS = {
    ("GET", "/hava-durumu"): ("hava-durumu", 60),
    ("POST", "/toplu"): ("toplu", 10),
    ("GET", "/toplu"): ("toplu", 10),
    ("GET", "/map/geojson"): ("map-geojson", 2),
    ("GET", "/gecmis"): ("gecmis", 10),
    ("GET", "/sondurum"): ("gecmis", 10),
    ("POST", "/alerts"): ("alerts", 10),
    ("POST", "/webhook/test"): ("webhook-test", 3),
}
app.config["MAX_CONTENT_LENGTH"] = MAX_JSON_BODY_BYTES

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
RATE_LIMIT_KILIT = threading.Lock()
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

    with RATE_LIMIT_KILIT:
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
        if random.random() < 0.01:
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
_LISTE_YETKI_BELLEK: dict[str, dict[str, str]] = {}
_LISTE_YETKI_BELLEK_KILIT = threading.Lock()


class FavoriHatasi(Exception):
    """Favoriler özelliğine özgü kullanıcı hataları (limit aşımı, geçersiz girdi vb.)."""


def _favori_liste_id_gecerli(liste_id: str) -> bool:
    return bool(FAVORI_LISTE_ID_REGEX.match(liste_id))


def _liste_id_dogrula(liste_id: str):
    """liste_id formatı geçersizse hazır bir (jsonify, 400) yanıtı döner,
    geçerliyse None. Favoriler ve alert route'larında tekrarlanan
    doğrulama bloğunu tek yerden yönetir, çağıran taraf `if hata: return
    hata` ile erken dönüş yapar."""
    if _favori_liste_id_gecerli(liste_id):
        return None
    return jsonify(
        {
            "basarili": False,
            "hata": "liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).",
        }
    ), 400


def _liste_yetki_redis_key(liste_id: str, prefix: str) -> str:
    return f"{prefix}liste-yetki:{liste_id}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _liste_yetki_olustur(liste_id: str | None = None) -> dict[str, str]:
    liste_id = liste_id or secrets.token_urlsafe(18)
    if not _favori_liste_id_gecerli(liste_id):
        raise FavoriHatasi("liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).")
    manage_token = secrets.token_urlsafe(32)
    read_token = secrets.token_urlsafe(32)
    yetkiler = {
        "manage_token_hash": _token_hash(manage_token),
        "read_token_hash": _token_hash(read_token),
    }

    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            key = _liste_yetki_redis_key(liste_id, redis_prefix)
            if redis_client.exists(key):
                raise FavoriHatasi("Bu liste_id zaten kullanılıyor.")
            redis_client.hset(key, mapping=yetkiler)
            redis_client.expire(key, max(FAVORI_TTL_SANIYE, ALERT_TTL_SANIYE))
        except redis_hata_sinifi:
            logger.warning("Liste yetkileri Redis'e yazılamadı, bellek içi depolamaya düşülüyor.")
        else:
            return {"listeId": liste_id, "manage_token": manage_token, "read_token": read_token}

    with _LISTE_YETKI_BELLEK_KILIT:
        if liste_id in _LISTE_YETKI_BELLEK:
            raise FavoriHatasi("Bu liste_id zaten kullanılıyor.")
        _LISTE_YETKI_BELLEK[liste_id] = yetkiler
    return {"listeId": liste_id, "manage_token": manage_token, "read_token": read_token}


def _liste_yetki_dogrula(liste_id: str, gereken: str):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"basarili": False, "hata": "Authorization Bearer token zorunludur."}), 401
    token = auth[7:].strip()
    if not token:
        return jsonify({"basarili": False, "hata": "Authorization Bearer token zorunludur."}), 401

    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    yetkiler = None
    if redis_musait:
        try:
            ham = redis_client.hgetall(_liste_yetki_redis_key(liste_id, redis_prefix))
            yetkiler = {
                (key.decode() if isinstance(key, bytes) else key):
                (value.decode() if isinstance(value, bytes) else value)
                for key, value in ham.items()
            }
        except redis_hata_sinifi:
            logger.warning("Liste yetkileri Redis'ten okunamadı, bellek içi depolamaya düşülüyor.")

    if yetkiler is None:
        with _LISTE_YETKI_BELLEK_KILIT:
            yetkiler = dict(_LISTE_YETKI_BELLEK.get(liste_id, {}))

    beklenen = yetkiler.get(f"{gereken}_token_hash")
    if not beklenen or not hmac.compare_digest(beklenen, _token_hash(token)):
        return jsonify({"basarili": False, "hata": "Yetkisiz."}), 401
    return None


def _json_govde_dogrula():
    """İstek gövdesi JSON değilse hazır bir (jsonify, 400) yanıtı döner,
    geçerliyse None. Favoriler ve alert route'larında tekrarlanan
    kontrolü tek yerden yönetir."""
    if request.is_json:
        return None
    return jsonify(
        {"basarili": False, "hata": "İstek gövdesi JSON olmalıdır (Content-Type: application/json)."}
    ), 400


def _favori_redis_key(liste_id: str, prefix: str) -> str:
    return f"{prefix}favoriler:{liste_id}"


def _favori_kayit_olustur(sorgu: str) -> dict:
    return {
        "sorgu": sorgu,
        "eklenmeTarihi": _dt.datetime.now(_dt.UTC)
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


# Her alert kaydı bir server-side alert_id alır ve tüm liste_id'ler
# arasında tek bir global kayıt (alerts:all) tutulur ki /alerts/check
# tüm alertleri tek taramada gezebilsin.
ALERT_TURLERI = {
    "weather.temp_threshold",
    "weather.wind_gust_exceeded",
    "weather.rain_threshold",
    "weather.rain_started",
    "weather.rain_stopped",
    "weather.frost_risk",
    "weather.warning_issued",
}
ALERT_OLAY_BAZLI_TURLER = {
    "weather.rain_started",
    "weather.rain_stopped",
    "weather.warning_issued",
}
ALERT_MAX_KAYIT = int(os.getenv("APP_ALERT_MAX_KAYIT", "30"))
ALERT_TTL_SANIYE = int(os.getenv("APP_ALERT_TTL_SANIYE", str(90 * 24 * 3600)))
ALERT_WEBHOOK_TIMEOUT = float(os.getenv("APP_ALERT_WEBHOOK_TIMEOUT_SANIYE", "5"))
ALERT_WEBHOOK_MAX_RESPONSE_BYTES = int(
    os.getenv("APP_ALERT_WEBHOOK_MAX_RESPONSE_BYTES", str(1024 * 1024))
)
ALERT_WEBHOOK_ALLOWED_PORTS = {443}
MAX_WEBHOOK_URL_LENGTH = int(os.getenv("APP_MAX_WEBHOOK_URL_LENGTH", "2048"))
ALERT_YAGISLI_HADISE_KODLARI = {
    "HY", "Y", "KY", "KKY", "HKY", "K", "KYK",
    "HSY", "SY", "KSY", "MSY", "DY", "GSY", "KGSY",
}
ALERT_DON_SEVIYE_SIRASI = {
    "Risk Yok": 0, "Bilinmiyor": 0, "Kırağı Riski": 1,
    "Hafif Don": 2, "Orta Don": 3, "Kuvvetli Don": 4, "Çok Kuvvetli Don": 5,
}

_ALERT_BELLEK: dict[str, dict] = {}
_ALERT_LISTE_INDEX: dict[str, set[str]] = defaultdict(set)
_ALERT_BELLEK_KILIT = threading.Lock()


class AlertHatasi(Exception):
    """Alert özelliğine özgü kullanıcı hataları (geçersiz tur, limit aşımı vb.)."""


def _webhook_url_ayristir(webhook_url: str) -> SplitResult:
    if len(webhook_url) > MAX_WEBHOOK_URL_LENGTH:
        raise AlertHatasi(
            f"'webhookUrl' en fazla {MAX_WEBHOOK_URL_LENGTH} karakter olabilir."
        )
    try:
        parsed = urlsplit(webhook_url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise AlertHatasi("'webhookUrl' geçerli bir HTTPS URL olmalıdır.") from exc
    if parsed.scheme.lower() != "https" or not hostname:
        raise AlertHatasi("'webhookUrl' yalnızca https URL olmalıdır.")
    if parsed.username or parsed.password:
        raise AlertHatasi("'webhookUrl' kullanıcı bilgisi içeremez.")
    if port is not None and port not in ALERT_WEBHOOK_ALLOWED_PORTS:
        raise AlertHatasi("'webhookUrl' yalnızca 443 portunu kullanabilir.")
    return parsed


def _webhook_guvenli_ipleri_getir(hostname: str, port: int) -> list[str]:
    try:
        adresler = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise AlertHatasi("Webhook hostname'i çözümlenemedi.") from exc

    try:
        ip_ler = {ipaddress.ip_address(adres[4][0]) for adres in adresler}
    except ValueError as exc:
        raise AlertHatasi("Webhook hostname'i geçerli IP adreslerine çözümlenmedi.") from exc
    if not ip_ler:
        raise AlertHatasi("Webhook hostname'i için IP adresi bulunamadı.")
    if any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
        for ip in ip_ler
    ):
        raise AlertHatasi("Webhook hedefi özel, yerel veya ayrılmış bir IP adresine çözümleniyor.")
    return [str(ip) for ip in ip_ler]


def _webhook_hedefini_dogrula(webhook_url: str) -> SplitResult:
    parsed = _webhook_url_ayristir(webhook_url)
    _webhook_guvenli_ipleri_getir(parsed.hostname, parsed.port or 443)
    return parsed


def _alert_redis_all_key(prefix: str) -> str:
    return f"{prefix}alerts:all"


def _alert_redis_liste_key(liste_id: str, prefix: str) -> str:
    return f"{prefix}alerts:liste:{liste_id}"


def _iso_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _alert_ekle(liste_id: str, govde: dict) -> dict:
    tur = govde.get("tur")
    il = govde.get("il")
    webhook_url = govde.get("webhookUrl")
    if tur not in ALERT_TURLERI:
        raise AlertHatasi(f"Geçersiz 'tur'. Seçenekler: {', '.join(sorted(ALERT_TURLERI))}")
    if not il or not isinstance(il, str):
        raise AlertHatasi("'il' zorunludur.")
    if not webhook_url or not isinstance(webhook_url, str):
        raise AlertHatasi("'webhookUrl' geçerli bir http(s) URL olmalıdır.")
    _webhook_url_ayristir(webhook_url)

    kayit = {
        "id": uuid.uuid4().hex[:12],
        "listeId": liste_id,
        "tur": tur,
        "il": il,
        "ilce": govde.get("ilce"),
        "webhookUrl": webhook_url,
        "esik": govde.get("esik"),
        "yon": govde.get("yon", "ustunde"),
        "sonDurum": None,
        "sonTetiklenme": None,
        "olusturmaTarihi": _iso_now(),
    }

    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            liste_key = _alert_redis_liste_key(liste_id, redis_prefix)
            if redis_client.scard(liste_key) >= ALERT_MAX_KAYIT:
                raise AlertHatasi(f"Bu listede en fazla {ALERT_MAX_KAYIT} alert olabilir.")
            all_key = _alert_redis_all_key(redis_prefix)
            redis_client.hset(all_key, kayit["id"], json.dumps(kayit, ensure_ascii=False))
            redis_client.sadd(liste_key, kayit["id"])
            redis_client.expire(liste_key, ALERT_TTL_SANIYE)
            return kayit
        except redis_hata_sinifi:
            logger.warning("Alert eklenemedi, bellek içi depolamaya düşülüyor.")

    with _ALERT_BELLEK_KILIT:
        if len(_ALERT_LISTE_INDEX[liste_id]) >= ALERT_MAX_KAYIT:
            raise AlertHatasi(f"Bu listede en fazla {ALERT_MAX_KAYIT} alert olabilir.")
        _ALERT_BELLEK[kayit["id"]] = kayit
        _ALERT_LISTE_INDEX[liste_id].add(kayit["id"])
    return kayit


def _alert_sil(liste_id: str, alert_id: str) -> bool:
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            liste_key = _alert_redis_liste_key(liste_id, redis_prefix)
            all_key = _alert_redis_all_key(redis_prefix)
            silindi = redis_client.srem(liste_key, alert_id)
            if silindi:
                redis_client.hdel(all_key, alert_id)
            return bool(silindi)
        except redis_hata_sinifi:
            logger.warning("Alert silinemedi, bellek içi depolamaya düşülüyor.")

    with _ALERT_BELLEK_KILIT:
        if alert_id in _ALERT_LISTE_INDEX.get(liste_id, set()):
            _ALERT_LISTE_INDEX[liste_id].discard(alert_id)
            _ALERT_BELLEK.pop(alert_id, None)
            return True
    return False


def _alert_listele(liste_id: str) -> list[dict]:
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            liste_key = _alert_redis_liste_key(liste_id, redis_prefix)
            all_key = _alert_redis_all_key(redis_prefix)
            id_ler = {i.decode() if isinstance(i, bytes) else i for i in redis_client.smembers(liste_key)}
            if not id_ler:
                return []
            ham = redis_client.hmget(all_key, list(id_ler))
            sonuc = [json.loads(d) for d in ham if d]
            return sorted(sonuc, key=lambda k: k.get("olusturmaTarihi") or "")
        except (redis_hata_sinifi, json.JSONDecodeError, TypeError):
            logger.warning("Alertler listelenemedi, bellek içi depolama okunuyor.")

    with _ALERT_BELLEK_KILIT:
        id_ler = _ALERT_LISTE_INDEX.get(liste_id, set())
        return sorted(
            (_ALERT_BELLEK[i] for i in id_ler if i in _ALERT_BELLEK),
            key=lambda k: k.get("olusturmaTarihi") or "",
        )


def _alert_tumunu_al() -> list[dict]:
    """/alerts/check taraması için tüm liste_id'lerdeki tüm alertleri döner."""
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            all_key = _alert_redis_all_key(redis_prefix)
            ham = redis_client.hgetall(all_key)
            return [json.loads(d) for d in ham.values()]
        except (redis_hata_sinifi, json.JSONDecodeError, TypeError):
            logger.warning("Alertler alınamadı, bellek içi depolama okunuyor.")

    with _ALERT_BELLEK_KILIT:
        return list(_ALERT_BELLEK.values())


def _alert_kayit_guncelle(kayit: dict) -> None:
    """Değerlendirme sonrası sonDurum/sonTetiklenme kalıcı hale getirilir."""
    redis_musait, redis_client, redis_prefix, redis_hata_sinifi = _mgm_redis_durumu()
    if redis_musait:
        try:
            all_key = _alert_redis_all_key(redis_prefix)
            redis_client.hset(all_key, kayit["id"], json.dumps(kayit, ensure_ascii=False))
            return
        except redis_hata_sinifi:
            logger.warning("Alert güncellenemedi, bellek içi depolamaya düşülüyor.")

    with _ALERT_BELLEK_KILIT:
        if kayit["id"] in _ALERT_BELLEK:
            _ALERT_BELLEK[kayit["id"]] = kayit


def _alert_degerlendir(alert: dict) -> tuple[bool, dict]:
    """(tetiklendi_mi, yeni_olcum) döner. yeni_olcum her zaman geri
    verilir, olay bazlı türlerin bir sonraki karşılaştırması için
    çağıran taraf bunu alert["sonDurum"]'a yazıp kalıcı hale getirmeli.
    """
    tur = alert["tur"]
    il = alert["il"]
    ilce = alert.get("ilce")
    onceki = alert.get("sonDurum") or {}

    if tur == "weather.warning_issued":
        uyari = mgm.uyarilar(il=il)
        ham = uyari.get("ham") if isinstance(uyari, dict) else None
        var_mi = bool(ham)
        olcum = {"aktifUyariVar": var_mi}
        onceki_var_mi = onceki.get("aktifUyariVar")
        tetiklendi = onceki_var_mi is not None and not onceki_var_mi and var_mi
        return tetiklendi, olcum

    if tur == "weather.frost_risk":
        istasyon_id = _istasyon_id_getir(il, ilce)
        risk = mgm.don_kiragi_riski(istasyon_id, il=il, ilce=ilce)
        seviye = risk.get("genelRiskSeviyesi")
        olcum = {"genelRiskSeviyesi": seviye}
        esik_seviye = alert.get("esik") or "Hafif Don"
        tetiklendi = ALERT_DON_SEVIYE_SIRASI.get(seviye, 0) >= ALERT_DON_SEVIYE_SIRASI.get(esik_seviye, 2)
        return tetiklendi, olcum

    _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
    istasyon_id = _istasyon_id_getir(il, ilce)
    guncel = mgm.guncel_durum_yedekli(istasyon_id, enlem, boylam)

    if tur == "weather.temp_threshold":
        sicaklik = guncel.get("sicaklik")
        olcum = {"sicaklik": sicaklik}
        if sicaklik is None:
            return False, olcum
        esik = alert.get("esik")
        tetiklendi = sicaklik > esik if alert.get("yon") != "altinda" else sicaklik < esik
        return tetiklendi, olcum

    if tur == "weather.wind_gust_exceeded":
        ruzgar = guncel.get("ruzgarHizi")
        olcum = {"ruzgarHizi": ruzgar}
        if ruzgar is None:
            return False, olcum
        return ruzgar > (alert.get("esik") or 0), olcum

    if tur == "weather.rain_threshold":
        yagis = guncel.get("yagis")
        olcum = {"yagis": yagis}
        if yagis is None:
            return False, olcum
        return yagis > (alert.get("esik") or 0), olcum

    if tur in ("weather.rain_started", "weather.rain_stopped"):
        simdi_yagisli = guncel.get("durumKodu") in ALERT_YAGISLI_HADISE_KODLARI
        olcum = {"durumKodu": guncel.get("durumKodu"), "yagisli": simdi_yagisli}
        once_yagisli = onceki.get("yagisli")
        tetiklendi = False
        if once_yagisli is not None:
            if tur == "weather.rain_started":
                tetiklendi = not once_yagisli and simdi_yagisli
            else:
                tetiklendi = once_yagisli and not simdi_yagisli
        return tetiklendi, olcum

    return False, {}


def _alert_webhook_gonder(alert: dict, olcum: dict) -> bool:
    payload = {
        "event": alert["tur"],
        "alertId": alert["id"],
        "il": alert["il"],
        "ilce": alert.get("ilce"),
        "esik": alert.get("esik"),
        "olcum": olcum,
        "tetiklenmeZamani": _iso_now(),
    }
    resp = None
    try:
        _webhook_hedefini_dogrula(alert["webhookUrl"])
        resp = requests.post(
            alert["webhookUrl"],
            json=payload,
            timeout=ALERT_WEBHOOK_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        content_length = resp.headers.get("Content-Length")
        if content_length is not None and int(content_length) > ALERT_WEBHOOK_MAX_RESPONSE_BYTES:
            return False
        response_size = 0
        for chunk in resp.iter_content(chunk_size=8192):
            response_size += len(chunk)
            if response_size > ALERT_WEBHOOK_MAX_RESPONSE_BYTES:
                return False
        return resp.status_code < 400
    except (AlertHatasi, requests.RequestException, ValueError) as exc:
        logger.warning("Webhook gönderilemedi (%s): %s", alert["webhookUrl"], exc)
        return False
    finally:
        if resp is not None:
            resp.close()


def _alert_kontrol_calistir() -> dict:
    """Tüm alertleri değerlendirir, tetiklenenlere webhook gönderir.
    /api/v1/alerts/check ve iç zamanlayıcı tarafından çağrılır."""
    sonuc = {"kontrolEdilen": 0, "tetiklenen": 0, "hataliWebhook": 0}
    for alert in _alert_tumunu_al():
        sonuc["kontrolEdilen"] += 1
        try:
            tetiklendi, olcum = _alert_degerlendir(alert)
        except MGMWeatherError as exc:
            logger.warning("Alert %s değerlendirilemedi: %s", alert.get("id"), exc)
            continue

        if alert["tur"] in ALERT_OLAY_BAZLI_TURLER:
            alert["sonDurum"] = olcum

        if tetiklendi:
            sonuc["tetiklenen"] += 1
            alert["sonTetiklenme"] = _iso_now()
            if not _alert_webhook_gonder(alert, olcum):
                sonuc["hataliWebhook"] += 1

        _alert_kayit_guncelle(alert)
    return sonuc


def _ic_zamanlayiciyi_baslat() -> None:
    if os.getenv("ENABLE_INTERNAL_SCHEDULER", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError as exc:
        raise RuntimeError(
            "ENABLE_INTERNAL_SCHEDULER=true ama APScheduler kurulu değil. "
            "`pip install APScheduler` veya `pip install .[zamanlayici]` çalıştırın."
        ) from exc

    dakika = int(os.getenv("APP_SCHEDULER_INTERVAL_DAKIKA", "10"))
    zamanlayici = BackgroundScheduler(daemon=True)
    zamanlayici.add_job(_alert_kontrol_calistir, "interval", minutes=dakika)
    zamanlayici.start()
    logger.info("İç zamanlayıcı başlatıldı (%d dakikada bir alert kontrolü).", dakika)


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


def _rota_rate_limit_ayari(method: str, path: str) -> tuple[str, int] | None:
    for (route_method, route_prefix), ayar in ROUTE_RATE_LIMITS.items():
        if route_method == method and (path == route_prefix or path.startswith(f"{route_prefix}/")):
            return ayar
    return None


def _tarih_araligi_dogrula():
    if request.path != "/gecmis":
        return None
    start = request.args.get("start")
    end = request.args.get("end")
    if not start and not end:
        return None
    if not start or not end:
        return jsonify({"basarili": False, "hata": "'start' ve 'end' birlikte gönderilmelidir."}), 400
    try:
        baslangic = _dt.date.fromisoformat(start)
        bitis = _dt.date.fromisoformat(end)
    except ValueError:
        return jsonify({"basarili": False, "hata": "Tarihler YYYY-MM-DD biçiminde olmalıdır."}), 400
    if bitis < baslangic:
        return jsonify({"basarili": False, "hata": "'end', 'start' tarihinden önce olamaz."}), 400
    if (bitis - baslangic).days > MAX_DATE_RANGE_DAYS:
        return jsonify(
            {
                "basarili": False,
                "hata": f"Tarih aralığı en fazla {MAX_DATE_RANGE_DAYS} gün olabilir.",
            }
        ), 400
    return None


@app.before_request
def istek_sinirlarini_kontrol_et():
    if request.is_json and request.content_length is not None and request.content_length > MAX_JSON_BODY_BYTES:
        return jsonify(
            {
                "basarili": False,
                "hata": f"JSON gövdesi en fazla {MAX_JSON_BODY_BYTES} byte olabilir.",
            }
        ), 413
    return _tarih_araligi_dogrula()


@app.before_request
def rate_limit():
    if request.method == "OPTIONS":
        return None
    if request.path in {"/health", "/docs", "/openapi.yaml", "/metrics"}:
        return None

    ip = request.remote_addr or "unknown"
    window_seconds = max(1, RATE_LIMIT_WINDOW)
    rota_ayari = _rota_rate_limit_ayari(request.method, request.path)
    if rota_ayari is not None:
        kapsam, limit = rota_ayari
        if kapsam == "map-geojson":
            limit = MAP_GEOJSON_RATE_LIMIT_MAX
    else:
        limit = RATE_LIMIT_MAX
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
    if not response.direct_passthrough and len(response.get_data()) > MAX_RESPONSE_BYTES:
        response = jsonify(
            {
                "basarili": False,
                "hata": f"Yanıt gövdesi en fazla {MAX_RESPONSE_BYTES} byte olabilir.",
            }
        )
        response.status_code = 413
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
  <title>Hava Durumu API Dokümantasyon</title>
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
    Tek istekte birden çok yer için hava durumu. Her sorgu, /ara ile
    aynı akıllı çözümleyiciyi kullanır, yani "istanbul", "kadikoy/istanbul",
    "maslak itü" gibi serbest metinler burada da geçerlidir.

    Kısmi başarısızlığa toleranslıdır: bir sorgu çözülemese bile diğerleri
    etkilenmez. Yanıt her zaman 200 döner (batch'in kendisi işlendi),
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


@app.post("/favoriler")
def favori_liste_olustur():
    body = request.get_json(silent=True) if request.is_json else {}
    if body is None or not isinstance(body, dict):
        return jsonify({"basarili": False, "hata": "Geçerli bir JSON nesnesi gönderin."}), 400
    if "sorgu" in body:
        liste_id = body.get("listeId")
        if not isinstance(liste_id, str):
            return jsonify({"basarili": False, "hata": "'listeId' alanı zorunludur."}), 400
        if hata := _liste_id_dogrula(liste_id):
            return hata
        if hata := _liste_yetki_dogrula(liste_id, "manage"):
            return hata
        if not isinstance(body.get("sorgu"), str):
            return jsonify({"basarili": False, "hata": "'sorgu' alanı zorunludur (bir metin)."}), 400
        try:
            kayit = _favori_ekle(liste_id, body["sorgu"])
        except FavoriHatasi as exc:
            return jsonify({"basarili": False, "hata": str(exc)}), 400
        return jsonify({"basarili": True, "veri": kayit})
    try:
        yetkiler = _liste_yetki_olustur(body.get("listeId"))
    except FavoriHatasi as exc:
        return jsonify({"basarili": False, "hata": str(exc)}), 400
    return jsonify({"basarili": True, "veri": yetkiler}), 201


@app.post("/favoriler/<liste_id>")
def favori_ekle(liste_id: str):
    """Bir favoriye sorgu ekler/günceller, bkz. modül docstring'i."""
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "manage"):
        return hata
    if hata := _json_govde_dogrula():
        return hata
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
    """Bir favoriden sorgu siler, bkz. modül docstring'i."""
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "manage"):
        return hata
    if hata := _json_govde_dogrula():
        return hata
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
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "read"):
        return hata
    return jsonify({"basarili": True, "veri": _favori_listele(liste_id)})


@app.get("/favoriler/<liste_id>")
def favori_hava_durumu(liste_id: str):
    """
    Listedeki tüm sorgular için hava durumunu tek istekte döner (/toplu
    ile aynı akıllı çözümleyici + paralel yürütme, kısmi başarısızlığa
    toleranslı).
    """
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "read"):
        return hata
    kayitlar = _favori_listele(liste_id)
    if not kayitlar:
        return jsonify({"basarili": True, "veri": []})

    sorgular = [k["sorgu"] for k in kayitlar]
    with ThreadPoolExecutor(max_workers=min(len(sorgular), 10)) as havuz:
        sonuclar = list(havuz.map(_hava_durumu_akilli_guvenli, sorgular))

    return jsonify({"basarili": True, "veri": sonuclar})


@app.post("/alerts/<liste_id>")
def alert_ekle(liste_id: str):
    """Bir alert kaydı ekler, bkz. modül docstring'i."""
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "manage"):
        return hata
    if hata := _json_govde_dogrula():
        return hata
    govde = request.get_json(silent=True)
    if not isinstance(govde, dict):
        return jsonify({"basarili": False, "hata": "Geçerli bir JSON nesnesi gönderin."}), 400
    try:
        kayit = _alert_ekle(liste_id, govde)
    except AlertHatasi as exc:
        return jsonify({"basarili": False, "hata": str(exc)}), 400
    return jsonify({"basarili": True, "veri": kayit})


@app.delete("/alerts/<liste_id>/<alert_id>")
def alert_sil(liste_id: str, alert_id: str):
    """Bir alert kaydını siler."""
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "manage"):
        return hata
    silindi = _alert_sil(liste_id, alert_id)
    if not silindi:
        return jsonify({"basarili": False, "hata": "Bu alert bulunamadı."}), 404
    return jsonify({"basarili": True})


@app.get("/alerts/<liste_id>")
def alert_liste_goster(liste_id: str):
    """Listedeki tüm alert kayıtlarını döner."""
    if hata := _liste_id_dogrula(liste_id):
        return hata
    if hata := _liste_yetki_dogrula(liste_id, "read"):
        return hata
    return jsonify({"basarili": True, "veri": _alert_listele(liste_id)})


@app.post("/api/v1/alerts/check")
def alert_kontrol():
    """
    Kayıtlı tüm alertleri değerlendirir, tetiklenenler için webhook
    gönderir. Authorization: Bearer <CRON_SECRET> ile korunur. Sunucu
    içinde otomatik bir zamanlayıcı YOKTUR, bu uç nokta dışarıdan
    (GitHub Actions cron, crontab vb.) periyodik çağrılmalıdır.
    """
    cron_secret = os.getenv("CRON_SECRET") or os.getenv("APP_CRON_SECRET")
    if not cron_secret:
        return jsonify(
            {"basarili": False, "hata": "CRON_SECRET tanımlı değil, bu uç nokta devre dışı."}
        ), 503
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {cron_secret}":
        return jsonify({"basarili": False, "hata": "Yetkisiz."}), 401
    sonuc = _alert_kontrol_calistir()
    return jsonify({"basarili": True, "veri": sonuc})


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
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı, "
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
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı, "
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


@app.get("/sondurum/toplam-yagis")
def sondurum_toplam_yagis():
    tarih = request.args.get("tarih")
    try:
        veri = mgm.toplam_yagislar(tarih)
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/sondurum/kar-kalinliklari")
def sondurum_kar():
    try:
        veri = mgm.kar_kalinliklari()
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/sondurum/son-gozlemler")
def sondurum_son_gozlemler():
    try:
        veri = mgm.son_gozlemler()
        return jsonify({"basarili": True, "veri": veri})
    except MGMWeatherError as exc:
        return _hata_yanit(exc, 502)


@app.get("/polen/<il>")
def polen(il: str):
    """
    Anlık polen/alerji indeksi (çimen, huş, kızılağaç, pelin otu, zeytin,
    ambrosia). Open-Meteo Air Quality API (CAMS Avrupa) üzerinden.
    yalnızca ilgili türün sezonunda ve modelin kapsadığı konumlarda veri
    döner, aksi halde `seviye: "Veri Yok"` ile işaretlenir.
    """
    ilce = request.args.get("ilce")
    try:
        _, enlem, boylam = _istasyon_ve_konum_getir(il, ilce)
        if enlem is None or boylam is None:
            raise MGMWeatherError(
                f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı, "
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
    dışı/başarısız olursa Open-Meteo Marine API'sinden. Dalga verisi
    her zaman Open-Meteo'dan gelir. Varsayılan olarak il/ilçenin
    istasyon koordinatı kullanılır. Kıyı ilçeleri için daha isabetlidir.
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
                    f"'{il}' için konum (enlem/boylam) bilgisi bulunamadı, "
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
    MGM'nin resmi bir don uyarı ürünü DEĞİLDİR. Bkz. mgm.don_kiragi_riski
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

    _ic_zamanlayiciyi_baslat()

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
