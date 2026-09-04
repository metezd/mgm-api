from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from urllib.parse import SplitResult, urlsplit

import requests
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AlertWebhookError(Exception):
    pass


def parse_webhook_url(webhook_url: str, max_length: int, allowed_ports: set[int]) -> SplitResult:
    if len(webhook_url) > max_length:
        raise AlertWebhookError(f"'webhookUrl' en fazla {max_length} karakter olabilir.")
    try:
        parsed = urlsplit(webhook_url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise AlertWebhookError("'webhookUrl' geçerli bir HTTPS URL olmalıdır.") from exc
    if parsed.scheme.lower() != "https" or not hostname:
        raise AlertWebhookError("'webhookUrl' yalnızca https URL olmalıdır.")
    if parsed.username or parsed.password:
        raise AlertWebhookError("'webhookUrl' kullanıcı bilgisi içeremez.")
    if port is not None and port not in allowed_ports:
        raise AlertWebhookError("'webhookUrl' yalnızca 443 portunu kullanabilir.")
    return parsed


def resolve_safe_ips(hostname: str, port: int) -> list[str]:
    try:
        addresses = socket.getaddrinfo(hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AlertWebhookError("Webhook hostname'i çözümlenemedi.") from exc
    try:
        ips = {ipaddress.ip_address(address[4][0]) for address in addresses}
    except ValueError as exc:
        raise AlertWebhookError("Webhook hostname'i geçerli IP adreslerine çözümlenmedi.") from exc
    if not ips:
        raise AlertWebhookError("Webhook hostname'i için IP adresi bulunamadı.")
    if any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified for ip in ips
    ):
        raise AlertWebhookError("Webhook hedefi özel, yerel veya ayrılmış bir IP adresine çözümleniyor.")
    return [str(ip) for ip in ips]


def validate_webhook_target(webhook_url: str, max_length: int, allowed_ports: set[int]) -> SplitResult:
    parsed = parse_webhook_url(webhook_url, max_length, allowed_ports)
    resolve_safe_ips(parsed.hostname, parsed.port or 443)
    return parsed


def event_id(alert: dict, measurement: dict) -> str:
    canonical = json.dumps(
        {"tur": alert["tur"], "olcum": measurement}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{alert['id']}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"


def send_webhook(
    alert: dict,
    measurement: dict,
    payload_model: type[BaseModel],
    max_url_length: int,
    allowed_ports: set[int],
    timeout: float,
    retry_max: int,
    retry_backoff: float,
    signing_secret: str,
    max_response_bytes: int,
    target_validator=None,
) -> bool:
    current_event_id = event_id(alert, measurement)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = payload_model(
        event=alert["tur"],
        eventId=current_event_id,
        alertId=alert["id"],
        il=alert["il"],
        ilce=alert.get("ilce"),
        esik=alert.get("esik"),
        olcum=measurement,
        tetiklenmeZamani=timestamp,
    ).model_dump()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": current_event_id,
        "X-MGM-Alert-Id": alert["id"],
        "X-MGM-Alert-Timestamp": timestamp,
    }
    if signing_secret:
        signature = hmac.new(
            signing_secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        headers["X-MGM-Alert-Signature"] = f"sha256={signature}"
    try:
        if target_validator is None:
            validate_webhook_target(alert["webhookUrl"], max_url_length, allowed_ports)
        else:
            target_validator(alert["webhookUrl"])
        for attempt in range(1, retry_max + 1):
            response = None
            try:
                response = requests.post(
                    alert["webhookUrl"],
                    data=body,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > max_response_bytes:
                    return False
                response_size = 0
                for chunk in response.iter_content(chunk_size=8192):
                    response_size += len(chunk)
                    if response_size > max_response_bytes:
                        return False
                if response.status_code < 400:
                    return True
                retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
            except requests.RequestException as exc:
                retryable = True
                logger.warning("Webhook denemesi başarısız (%s, deneme %d): %s", alert["webhookUrl"], attempt, exc)
            finally:
                if response is not None:
                    response.close()
            if not retryable or attempt == retry_max:
                return False
            time.sleep(retry_backoff * (2 ** (attempt - 1)))
    except (AlertWebhookError, ValueError) as exc:
        logger.warning("Webhook gönderilemedi (%s): %s", alert["webhookUrl"], exc)
        return False
    return False
