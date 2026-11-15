"""Wrapper inyectable de Gemini Flash (REST a generativelanguage, NO Vertex AI). Prompts de /explain y /recommend-checks.

La API key se lee del secreto GCP Secret Manager `gemini-api-key` (nombre en env GEMINI_SECRET) con la
identidad de Cloud Run (servidor de metadatos), con cache de 5 min. Nunca va en env, imagen ni logs.
Salida estructurada JSON, temperatura 0.2, timeout explicito. En los tests se inyecta un falso.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from app import recommend
from app.explain import build_prompt

PROJECT = os.environ.get("GCP_PROJECT_ID", "")
# ASUNCION: el secreto guarda la API key en texto plano; si fuera JSON {"api_key": ...} tambien se soporta.
SECRET = os.environ.get("GEMINI_SECRET", "gemini-api-key")
# ASUNCION: modelo Gemini Flash; configurable por si el nombre del endpoint cambia.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "7"))  # < 8 s exigido por T16
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiError(Exception):
    pass


class GeminiClient:
    def __init__(self, opener=urllib.request.urlopen, key_loader=None, clock=time.monotonic):
        self._open = opener
        self._clock = clock
        self._key: tuple[float, str] | None = None
        self._key_loader = key_loader or self._load_key_from_secret_manager

    def _load_key_from_secret_manager(self) -> str:
        import base64

        req = urllib.request.Request(METADATA, headers={"Metadata-Flavor": "Google"})
        with self._open(req, timeout=3) as r:
            token = json.loads(r.read())["access_token"]
        url = f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/{SECRET}/versions/latest:access"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with self._open(req, timeout=5) as r:
            raw = base64.b64decode(json.loads(r.read())["payload"]["data"]).decode()
        try:
            return json.loads(raw)["api_key"]
        except (ValueError, KeyError, TypeError):
            return raw.strip()

    def key(self) -> str:
        if not self._key or self._clock() - self._key[0] > 300:
            self._key = (self._clock(), self._key_loader())
        return self._key[1]

    def explain(self, req: dict) -> dict:
        """Llama a Gemini y devuelve el JSON parseado (sin validar forma). Lanza GeminiError ante cualquier fallo."""
        return self.generate(build_prompt(req))

    def recommend(self, req: dict) -> dict:
        """Igual que `explain`, con el prompt de /recommend-checks (T33)."""
        return self.generate(recommend.build_prompt(req))

    def generate(self, prompt: str) -> dict:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
        }
        try:
            key = self.key()
        except Exception as exc:  # metadatos o Secret Manager caidos: se usa la plantilla, no un 500
            raise GeminiError(f"key_{type(exc).__name__}") from exc
        url = f"{API_BASE}/{MODEL}:generateContent?key={key}"
        data = json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"content-type": "application/json"})
        try:
            with self._open(request, timeout=TIMEOUT_S) as r:
                envelope = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GeminiError(type(exc).__name__) from exc
        try:
            text = envelope["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeminiError("bad_gemini_payload") from exc
