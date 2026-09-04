from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from typing import Any

from flask import jsonify, request


class ListeYetkiService:
    def __init__(self, redis_state, liste_id_validator, favori_ttl: int, alert_ttl: int):
        self.redis_state = redis_state
        self.liste_id_validator = liste_id_validator
        self.ttl = max(favori_ttl, alert_ttl)
        self.memory: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def redis_key(liste_id: str, prefix: str) -> str:
        return f"{prefix}liste-yetki:{liste_id}"

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, liste_id: str | None = None) -> dict[str, str]:
        liste_id = liste_id or secrets.token_urlsafe(18)
        if not self.liste_id_validator(liste_id):
            raise ValueError("liste_id yalnızca harf, rakam, '-' ve '_' içerebilir (3-64 karakter).")
        manage_token = secrets.token_urlsafe(32)
        read_token = secrets.token_urlsafe(32)
        hashes = {
            "manage_token_hash": self.token_hash(manage_token),
            "read_token_hash": self.token_hash(read_token),
        }
        available, client, prefix, error_class = self.redis_state()
        if available:
            try:
                key = self.redis_key(liste_id, prefix)
                if client.exists(key):
                    raise ValueError("Bu liste_id zaten kullanılıyor.")
                client.hset(key, mapping=hashes)
                client.expire(key, self.ttl)
                return {"listeId": liste_id, "manage_token": manage_token, "read_token": read_token}
            except error_class:
                pass
        with self.lock:
            if liste_id in self.memory:
                raise ValueError("Bu liste_id zaten kullanılıyor.")
            self.memory[liste_id] = hashes
        return {"listeId": liste_id, "manage_token": manage_token, "read_token": read_token}

    def authorize(self, liste_id: str, required: str):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            return jsonify({"basarili": False, "hata": "Authorization Bearer token zorunludur."}), 401
        token = authorization[7:].strip()
        available, client, prefix, error_class = self.redis_state()
        permissions: dict[str, Any] | None = None
        if available:
            try:
                raw = client.hgetall(self.redis_key(liste_id, prefix))
                permissions = {
                    (key.decode() if isinstance(key, bytes) else key):
                    (value.decode() if isinstance(value, bytes) else value)
                    for key, value in raw.items()
                }
            except error_class:
                permissions = None
        if permissions is None:
            with self.lock:
                permissions = dict(self.memory.get(liste_id, {}))
        expected = permissions.get(f"{required}_token_hash")
        if not expected or not hmac.compare_digest(expected, self.token_hash(token)):
            return jsonify({"basarili": False, "hata": "Yetkisiz."}), 401
        return None
