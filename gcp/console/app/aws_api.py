"""Cliente de las APIs de AWS para el BFF de la consola.

La API key (fleetpulse/api/console-key) vive en GCP Secret Manager (`aws-console-api-key`) y se lee en tiempo de
ejecucion con la identidad de Cloud Run (servidor de metadatos), con cache de 5 minutos. Nunca va en variables
de entorno, en la imagen ni al navegador. Timeout explicito en cada llamada.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

REGISTRY_URL = os.environ.get("AWS_REGISTRY_URL", "").rstrip("/")   # API de lectura (aws/registry)
INCIDENTS_URL = os.environ.get("AWS_INCIDENTS_URL", "").rstrip("/")  # HTTP API de Benjamin (close)
PROJECT = os.environ.get("GCP_PROJECT_ID", "fleetpulse-g19-2026")
SECRET = os.environ.get("AWS_KEY_SECRET", "aws-console-api-key")
TIMEOUT_S = float(os.environ.get("AWS_TIMEOUT_S", "6"))
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


class UpstreamError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"AWS {status}")
        self.status = status
        self.body = body


class AwsApi:
    def __init__(self, opener=urllib.request.urlopen, key_loader=None, clock=time.monotonic):
        self._open = opener
        self._clock = clock
        self._key: tuple[float, str] | None = None
        self._key_loader = key_loader or self._load_key_from_secret_manager

    def _load_key_from_secret_manager(self) -> str:
        req = urllib.request.Request(METADATA, headers={"Metadata-Flavor": "Google"})
        with self._open(req, timeout=3) as r:
            token = json.loads(r.read())["access_token"]
        url = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{SECRET}/versions/latest:access"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with self._open(req, timeout=5) as r:
            raw = base64.b64decode(json.loads(r.read())["payload"]["data"]).decode()
        try:
            return json.loads(raw)["api_key"]  # mismo JSON que en AWS Secrets Manager
        except (ValueError, KeyError, TypeError):
            return raw.strip()

    def key(self) -> str:
        if not self._key or self._clock() - self._key[0] > 300:
            self._key = (self._clock(), self._key_loader())
        return self._key[1]

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"x-api-key": self.key(), "content-type": "application/json"})
        try:
            with self._open(req, timeout=TIMEOUT_S) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            raise UpstreamError(exc.code, exc.read().decode(errors="replace")[:500]) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamError(504, type(exc).__name__) from exc

    def get(self, path: str, query: str = "") -> dict:
        return self._call("GET", f"{REGISTRY_URL}{path}{'?' + query if query else ''}")

    def close_incident(self, incident_id: str, body: dict) -> dict:
        return self._call("POST", f"{INCIDENTS_URL}/incidents/{incident_id}/close", body)
