#!/usr/bin/env python3
"""Conecta la consola de Cloud Run con AWS (T15): un solo comando cuando haya credenciales del Learner Lab.

    python gcp/console/connect_aws.py            # copia la key, da acceso y redespliega con datos reales
    python gcp/console/connect_aws.py --check    # solo verifica: identidad AWS, URLs y GET /machines

Pasos: lee fleetpulse/api/console-key de AWS Secrets Manager (nunca la imprime) -> la guarda como nueva version del
secreto aws-console-api-key de GCP -> da secretAccessor a fp-console -> lee las URLs de SSM -> gcloud run deploy.
Credenciales: las de AWS en ~/.aws/credentials (el bloque "AWS CLI" del Learner Lab); las de GCP, gcloud auth login.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

PROJECT = os.environ.get("GCP_PROJECT_ID", "fleetpulse-g19-2026")
REGION = os.environ.get("GCP_REGION", "us-east1")
SA = f"fp-console@{PROJECT}.iam.gserviceaccount.com"
GCP_SECRET = "aws-console-api-key"
HERE = os.path.dirname(os.path.abspath(__file__))


def gcloud() -> str:
    found = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if found:
        return found
    win = os.path.expandvars(r"%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd")
    if os.path.isfile(win):
        return win
    sys.exit("No encuentro gcloud. Instala Google Cloud CLI y corre: gcloud auth login")


def run(args: list[str], stdin: str | None = None) -> str:
    r = subprocess.run([gcloud(), *args, "--project", PROJECT, "--quiet"], input=stdin, capture_output=True,
                       text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"gcloud {' '.join(args[:3])} fallo: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def aws():
    import boto3
    s = boto3.Session(region_name="us-east-1")
    who = s.client("sts").get_caller_identity()
    print(f"AWS: cuenta {who['Account']}")
    ssm = s.client("ssm")
    urls = {name: ssm.get_parameter(Name=f"/fleetpulse/{name}/url")["Parameter"]["Value"].rstrip("/")
            for name in ("registry", "api")}
    key = json.loads(s.client("secretsmanager").get_secret_value(SecretId="fleetpulse/api/console-key")
                     ["SecretString"])["api_key"]
    return urls, key


def check_registry(url: str, key: str) -> None:
    req = urllib.request.Request(f"{url}/machines", headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        n = len(json.loads(r.read())["machines"])
    print(f"GET {url}/machines -> {n} maquinas")
    if n < 100:
        print("  Aviso: faltan maquinas. Corre antes: python scripts/seed-registry.py")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="solo verificar, sin tocar GCP")
    a = ap.parse_args()
    urls, key = aws()
    print(f"registry: {urls['registry']}\napi:      {urls['api']}")
    check_registry(urls["registry"], key)
    if a.check:
        return
    try:
        run(["secrets", "describe", GCP_SECRET])
    except RuntimeError:
        run(["secrets", "create", GCP_SECRET, "--replication-policy=automatic"])
    run(["secrets", "versions", "add", GCP_SECRET, "--data-file=-"], stdin=json.dumps({"api_key": key}))
    run(["secrets", "add-iam-policy-binding", GCP_SECRET, f"--member=serviceAccount:{SA}",
         "--role=roles/secretmanager.secretAccessor"])
    print(f"GCP: nueva version de {GCP_SECRET}; {SA} puede leerla")
    env = (f"MOCK=0,GCP_PROJECT_ID={PROJECT},AWS_KEY_SECRET={GCP_SECRET},"
           f"AWS_REGISTRY_URL={urls['registry']},AWS_INCIDENTS_URL={urls['api']}")
    os.chdir(HERE)
    out = run(["run", "deploy", "console", "--region", REGION, "--source", ".", "--service-account", SA,
               "--min-instances", "0", "--max-instances", "2", "--memory", "512Mi", "--cpu", "1",
               "--concurrency", "80", "--timeout", "30", "--cpu-throttling", "--allow-unauthenticated",
               "--set-env-vars", env, "--format", "value(status.url)"])
    print(f"Consola con datos reales: {out or 'desplegada'}")


if __name__ == "__main__":
    main()
