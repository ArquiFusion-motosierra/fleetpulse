import os

from fastapi import FastAPI

app = FastAPI(title="FleetPulse insights")


@app.get("/health")
def health():
    return {"status": "ok", "project": os.environ.get("GCP_PROJECT_ID", "")}
