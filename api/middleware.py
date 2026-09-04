from __future__ import annotations

import datetime as _dt
import ipaddress
import random
import threading
import time
from collections import defaultdict, deque

from flask import jsonify, request


class RateLimiter:
    def __init__(
        self,
        redis_state,
        trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
        max_tracked_keys: int = 50000,
    ):
        self.redis_state = redis_state
        self.trusted_proxy_networks = trusted_proxy_networks
        self.max_tracked_keys = max_tracked_keys
        self.buckets: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def client_ip(self) -> str:
        remote_addr = request.remote_addr or "unknown"
        try:
            remote_ip = ipaddress.ip_address(remote_addr)
        except ValueError:
            return remote_addr
        if not any(remote_ip in network for network in self.trusted_proxy_networks):
            return remote_addr
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        addresses = []
        for candidate in forwarded_for.split(","):
            try:
                addresses.append(ipaddress.ip_address(candidate.strip()))
            except ValueError:
                continue
        for address in reversed(addresses):
            if not any(address in network for network in self.trusted_proxy_networks):
                return str(address)
        return remote_addr

    def cleanup(self) -> None:
        if len(self.buckets) <= self.max_tracked_keys:
            return
        empty_keys = [key for key, bucket in self.buckets.items() if not bucket]
        for key in empty_keys:
            del self.buckets[key]

    def check(self, ip: str, scope: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        redis_available, redis_client, redis_prefix, redis_error = self.redis_state()
        if redis_available:
            try:
                window = int(time.time() // window_seconds)
                redis_key = f"{redis_prefix}ratelimit:{scope}:{ip}:{window}"
                count = redis_client.eval(
                    """
                    local count = redis.call('INCR', KEYS[1])
                    if count == 1 then
                      redis.call('EXPIRE', KEYS[1], ARGV[1])
                    end
                    return count
                    """,
                    1,
                    redis_key,
                    window_seconds,
                )
                reset_epoch = (window + 1) * window_seconds
                remaining = max(0, limit - int(count))
                return int(count) <= limit, remaining, reset_epoch
            except redis_error:
                pass

        with self.lock:
            now = time.monotonic()
            bucket_key = f"{scope}:{ip}"
            bucket = self.buckets[bucket_key]
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            remaining_seconds = max(0.0, window_seconds - (now - bucket[0])) if bucket else 0.0
            reset_epoch = int(time.time() + remaining_seconds)
            if len(bucket) >= limit:
                return False, 0, reset_epoch
            bucket.append(now)
            if random.random() < 0.01:
                self.cleanup()
            return True, max(0, limit - len(bucket)), reset_epoch


def validate_json_body(max_bytes: int):
    if request.is_json and request.content_length is not None and request.content_length > max_bytes:
        return jsonify({"basarili": False, "hata": f"JSON gövdesi en fazla {max_bytes} byte olabilir."}), 413
    return None


def validate_date_range(max_days: int):
    if request.path != "/gecmis":
        return None
    start = request.args.get("start")
    end = request.args.get("end")
    if not start and not end:
        return None
    if not start or not end:
        return jsonify({"basarili": False, "hata": "'start' ve 'end' birlikte gönderilmelidir."}), 400
    try:
        begin_date = _dt.date.fromisoformat(start)
        end_date = _dt.date.fromisoformat(end)
    except ValueError:
        return jsonify({"basarili": False, "hata": "Tarihler YYYY-MM-DD biçiminde olmalıdır."}), 400
    if end_date < begin_date:
        return jsonify({"basarili": False, "hata": "'end', 'start' tarihinden önce olamaz."}), 400
    if (end_date - begin_date).days > max_days:
        return jsonify({"basarili": False, "hata": f"Tarih aralığı en fazla {max_days} gün olabilir."}), 400
    return None


def route_limit_setting(
    method: str,
    path: str,
    settings: dict[tuple[str, str], tuple[str, int]],
) -> tuple[str, int] | None:
    for (route_method, route_prefix), setting in settings.items():
        if route_method == method and (path == route_prefix or path.startswith(f"{route_prefix}/")):
            return setting
    return None
