"""Cliente ORDS con OAuth2 client credentials.

Secreto fleetpulse/oci/ords con cache de 5 min; token cacheado 50 min (ORDS lo emite por 60).
Timeout explicito en cada llamada. Nunca loguea el secreto ni el token.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_ID = os.environ.get("ORDS_SECRET", "fleetpulse/oci/ords")
TIMEOUT_S = float(os.environ.get("ORDS_TIMEOUT_S", "8"))
SECRET_TTL_S = 300
TOKEN_TTL_S = 50 * 60


class OrdsError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"ORDS {status}: {body[:300]}")
        self.status = status
        self.body = body


class OrdsClient:
    def __init__(self, secrets=None, opener=urllib.request.urlopen, clock=time.monotonic):
        self._secrets = secrets
        self._open = opener
        self._clock = clock
        self._secret: tuple[float, dict] | None = None
        self._token: tuple[float, str] | None = None

    def _secrets_client(self):
        if self._secrets is None:
            import boto3
            self._secrets = boto3.client("secretsmanager")
        return self._secrets

    def secret(self) -> dict:
        if not self._secret or self._clock() - self._secret[0] > SECRET_TTL_S:
            raw = self._secrets_client().get_secret_value(SecretId=SECRET_ID)["SecretString"]
            self._secret = (self._clock(), json.loads(raw))
        return self._secret[1]

    def base_url(self) -> str:
        url = self.secret()["base_url"]
        return url if url.endswith("/") else url + "/"

    def token(self, force: bool = False) -> str:
        if force or not self._token or self._clock() - self._token[0] > TOKEN_TTL_S:
            s = self.secret()
            basic = base64.b64encode(f"{s['client_id']}:{s['client_secret']}".encode()).decode()
            req = urllib.request.Request(
                self.base_url() + "oauth/token", method="POST",
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
            status, body = self._send(req)
            if status != 200:
                raise OrdsError(status, body)
            self._token = (self._clock(), json.loads(body)["access_token"])
        return self._token[1]

    def _send(self, req) -> tuple[int, str]:
        try:
            with self._open(req, timeout=TIMEOUT_S) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    def post(self, path: str, body: dict) -> dict:
        """POST JSON a <base_url>v1/<path>. Un 401 renueva el token una vez (expiro o se roto el cliente)."""
        data = json.dumps(body, separators=(",", ":")).encode()
        for attempt in (0, 1):
            req = urllib.request.Request(
                self.base_url() + "v1/" + path, method="POST", data=data,
                headers={"Authorization": f"Bearer {self.token(force=attempt == 1)}",
                         "Content-Type": "application/json"})
            status, text = self._send(req)
            if status == 401 and attempt == 0:
                self._secret = None  # el secreto pudo cambiar
                continue
            if status != 200:
                raise OrdsError(status, text)
            return json.loads(text) if text else {}
        raise OrdsError(401, "unauthorized")
