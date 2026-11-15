"""Cliente minimo de GCS y BigQuery via REST, autenticado con la SA fp-exporter.

Usa google-auth (viene en la layer fp-shared) solo para el token; las llamadas van por
urllib para no depender de las librerias google-cloud-*. En los tests se inyecta un falso,
asi que este modulo no se importa sin red.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpClient:
    def __init__(self, sa_key: dict):
        self._sa = sa_key
        self._cred = None

    def _token(self) -> str:
        import google.auth.transport.requests as tr
        from google.oauth2 import service_account

        if self._cred is None:
            self._cred = service_account.Credentials.from_service_account_info(
                self._sa, scopes=[_SCOPE]
            )
        if not self._cred.valid:
            self._cred.refresh(tr.Request())
        return self._cred.token

    def _post(self, url: str, data: bytes, content_type: str) -> dict:
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    def upload(self, bucket: str, name: str, data: bytes) -> None:
        url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
            f"?uploadType=media&name={urllib.parse.quote(name, safe='')}"
        )
        self._post(url, data, "application/gzip")

    def load(self, project: str, dataset: str, table: str, uris: list[str]) -> None:
        job = {
            "configuration": {
                "load": {
                    "sourceUris": uris,
                    "destinationTable": {
                        "projectId": project,
                        "datasetId": dataset,
                        "tableId": table,
                    },
                    "sourceFormat": "NEWLINE_DELIMITED_JSON",
                    "writeDisposition": "WRITE_APPEND",
                    "ignoreUnknownValues": True,
                }
            }
        }
        url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/jobs"
        resp = self._post(url, json.dumps(job).encode(), "application/json")
        self._wait(project, resp["jobReference"]["jobId"])

    def _wait(self, project: str, job_id: str, timeout_s: int = 120) -> None:
        url = (
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/jobs/"
            f"{urllib.parse.quote(job_id)}?fields=status"
        )
        deadline = time.monotonic() + timeout_s
        while True:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token()}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = json.loads(resp.read()).get("status", {})
            if status.get("state") == "DONE":
                if status.get("errorResult"):
                    raise RuntimeError(f"load job {job_id}: {status['errorResult']}")
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"load job {job_id} no termino en {timeout_s}s")
            time.sleep(2)
