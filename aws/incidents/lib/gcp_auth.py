"""ID token de Google para invocar Cloud Run `insights` desde AWS con la llave de la service account `fp-invoker`.

`google.oauth2.service_account.IDTokenCredentials` (google-auth, en la layer) firma una asercion con la llave y la
cambia por un ID token firmado por Google con `target_audience` = URL base del servicio (POST al token_uri, timeout
`TOKEN_TIMEOUT_S`; google-auth solo reintenta respuestas 5xx). El token vale 1 h: se cachea `CACHE` por
(service account, audiencia), asi que en caliente ExplainCause no hace esta llamada.
Nunca se registra la llave ni el token.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fplib import clock

CACHE = timedelta(minutes=50)
TOKEN_TIMEOUT_S = 3
_cache: dict[tuple[str, str], tuple[datetime, str]] = {}


def _fetch(sa_key: dict, audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.IDTokenCredentials.from_service_account_info(sa_key, target_audience=audience)
    transport = Request()

    def request(url, method="GET", body=None, headers=None, **kwargs):  # google-auth no pasa timeout
        kwargs["timeout"] = TOKEN_TIMEOUT_S
        return transport(url, method=method, body=body, headers=headers, **kwargs)

    creds.refresh(request)
    return creds.token


def id_token(sa_key: dict, audience: str, now: datetime | None = None) -> str:
    now = now or clock.now()
    key = (sa_key.get("client_email", ""), audience)
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE:
        return hit[1]
    token = _fetch(sa_key, audience)
    _cache[key] = (now, token)
    return token


def clear_cache() -> None:
    _cache.clear()
