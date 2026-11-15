"""Pruebas de fplib: sin red ni credenciales (moto para DynamoDB, fakes para Secrets Manager)."""
import json
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from fplib import auth, clock, envelope, geohash, ids, inbox, outbox, schema, secrets

REPO = Path(__file__).resolve().parents[3]
INC = "INC-EX-017-20261115T1430"


class FakeSecrets:
    def __init__(self, values):
        self.values, self.calls = values, 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if SecretId not in self.values:
            raise KeyError(SecretId)
        return {"SecretString": self.values[SecretId]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    secrets.clear_cache()
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _incident_env(payload=None):
    if payload is None:
        payload = json.loads((REPO / "contracts/examples/envelope/incident-opened.json").read_text())["payload"]
    return envelope.build("incident.opened", "aws.incident-flow", "incident", INC, payload,
                          incident_id=INC, machine_id="EX-017")


def test_window15_and_incident_id():
    ts = clock.parse("2026-11-15T14:38:05Z")
    assert clock.window15(ts) == "20261115T1430"
    assert ids.incident_id("EX-017", ts) == INC
    assert clock.iso(ts) == "2026-11-15T14:38:05Z"


def test_group_id():
    ts = clock.parse("2026-11-15T14:38:05Z")
    assert ids.group_id("6ma7ju", ts) == "GRP-6ma7ju-20261115T14"


def test_geohash_known_point():
    assert geohash.encode(57.64911, 10.40744, 6) == "u4pruy"
    assert len(geohash.encode(-13.9003, -71.5611, 6)) == 6


def test_envelope_validates_incident_opened():
    env = _incident_env()
    assert env["schema_version"] == 1 and uuid.UUID(env["event_id"]).version == 4


def test_envelope_rejects_bad_payload():
    with pytest.raises(schema.ContractError) as e:
        _incident_env({})
    assert isinstance(e.value.path, list)


def test_envelope_rejects_unknown_event_type():
    with pytest.raises(schema.ContractError):
        envelope.build("nope.x", "aws.incident-flow", "incident", INC, {})


def _tables():
    ddb = boto3.client("dynamodb")
    for name, key in (("fp-inbox", "message_id"), ("fp-outbox", "event_id")):
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}])
    return ddb


@mock_aws
def test_inbox_claim_once_and_outbox_conditional():
    ddb = _tables()
    assert inbox.claim("fp-inbox", "batch:B-1", "ingest") is True
    assert inbox.claim("fp-inbox", "batch:B-1", "ingest") is False
    env = _incident_env()
    outbox.put_event("fp-outbox", env)
    with pytest.raises(ClientError):
        outbox.put_event("fp-outbox", env)
    item = ddb.get_item(TableName="fp-outbox", Key={"event_id": {"S": env["event_id"]}})["Item"]
    assert json.loads(item["payload"]["S"]) == env
    assert item["type"]["S"] == "incident.opened" and item["incident_id"]["S"] == INC and "ttl" in item


@mock_aws
def test_outbox_transact_items():
    ddb = _tables()
    env = _incident_env()
    ddb.transact_write_items(TransactItems=[outbox.transact_items(env)])
    assert "Item" in ddb.get_item(TableName="fp-outbox", Key={"event_id": {"S": env["event_id"]}})


def test_bom_and_specialty():
    assert ids.bom_for("EX", "hydraulic_pump") == [{"part_id": "PRT-04", "qty": 1}, {"part_id": "PRT-03", "qty": 2}]
    assert ids.specialty_for("hydraulic_pump") == "hydraulics"
    assert ids.machines()["EX-017"]["zone_id"] == "NORTE"
    assert len(ids.technicians("mineria")) == 6 and set(ids.zones("mineria")) == {"NORTE", "CENTRO", "SUR"}


def test_check_api_key_constant_time_and_case_insensitive():
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    assert auth.check_api_key({"X-Api-Key": "k"}, "fleetpulse/api/ingest-key", client=fake) is True
    assert auth.check_api_key({"x-api-key": "nope"}, "fleetpulse/api/ingest-key", client=fake) is False
    assert auth.check_api_key({}, "fleetpulse/api/ingest-key", client=fake) is False
    assert auth.check_api_key({"x-api-key": "k"}, "missing", client=fake) is False


def test_check_api_key_logs_the_rejection_reason_without_values(caplog):
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k-secret"}', "raw": "not-json", "nokey": '{"x":1}'})
    with caplog.at_level("INFO", logger="fplib"):
        assert auth.check_api_key({}, "fleetpulse/api/ingest-key", client=fake) is False
        assert auth.check_api_key({"x-api-key": "given-value"}, "missing", client=fake) is False
        assert auth.check_api_key({"x-api-key": "given-value"}, "fleetpulse/api/ingest-key", client=fake) is False
        assert auth.check_api_key({"x-api-key": "given-value"}, "raw", client=fake) is False
        assert auth.check_api_key({"x-api-key": "given-value"}, "nokey", client=fake) is False
        assert auth.check_api_key({"x-api-key": "k-secret"}, "fleetpulse/api/ingest-key", client=fake) is True
    lines = [json.loads(r.getMessage()) for r in caplog.records]
    assert [(x["level"], x["msg"], x["reason"], x["secret"]) for x in lines] == [
        ("ERROR", "api_key_rechazada", "missing_header", "fleetpulse/api/ingest-key"),
        ("ERROR", "api_key_rechazada", "secret_unavailable:KeyError", "missing"),
        ("ERROR", "api_key_rechazada", "mismatch", "fleetpulse/api/ingest-key"),
        ("ERROR", "api_key_rechazada", "secret_unavailable:JSONDecodeError", "raw"),
        ("ERROR", "api_key_rechazada", "secret_unavailable:MissingApiKey", "nokey"),
    ]  # aceptar no loguea nada
    assert "given-value" not in caplog.text and "k-secret" not in caplog.text


def test_rejection_returns_the_reason_and_does_not_log(caplog):
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    with caplog.at_level("INFO", logger="fplib"):
        assert auth.rejection({"X-API-KEY": "k"}, "fleetpulse/api/ingest-key", client=fake) is None
        assert auth.rejection({"x-api-key": "nope"}, "fleetpulse/api/ingest-key", client=fake) == "mismatch"
        assert auth.rejection({"x-api-key": ""}, "fleetpulse/api/ingest-key", client=fake) == "missing_header"
    assert caplog.records == []


def test_secrets_cached():
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k"
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k" and fake.calls == 1


@mock_aws
def test_secrets_default_client_has_explicit_timeouts():
    cfg = secrets._client().meta.config
    assert cfg.connect_timeout == 3 and cfg.read_timeout == 5
    assert cfg.retries["total_max_attempts"] == 3  # botocore guarda max_attempts=2 reintentos como 3 intentos


def test_log_event_is_one_json_line(caplog):
    from fplib import log
    with caplog.at_level("INFO", logger="fplib"):
        log.event("hola", machine_id="EX-017", n=1)
    assert json.loads(caplog.records[0].getMessage()) == {"level": "INFO", "msg": "hola", "machine_id": "EX-017", "n": 1}


@pytest.mark.parametrize("bad", ["garbage", "2026-11-15T14:38:05"])
def test_envelope_rejects_malformed_occurred_at(bad):
    payload = json.loads((REPO / "contracts/examples/envelope/incident-opened.json").read_text())["payload"]
    env = envelope.build("incident.opened", "aws.incident-flow", "incident", INC, payload,
                         incident_id=INC, machine_id="EX-017")
    env["occurred_at"] = bad
    with pytest.raises(schema.ContractError):
        schema.validate("envelope", env)


# --- ids.oem_specs (ficha del fabricante, T26) ---------------------------------------------------------------------

OEM_YAML = """
illustrative: true
version: 1
models:
  EX:
    limits:
      coolant_temp_c: {normal: [65, 96], warning: 102, critical: 108}
    service_schedule:
      - {every_h: 500, task: "Filtros", parts: [{part_id: PRT-01, qty: 1}]}
    typical_failures:
      - {failure_mode: head_gasket, name: "Junta de culata"}
"""


@pytest.fixture
def oem_dirs(tmp_path, monkeypatch):
    """Paquete y contracts/ aislados en tmp: `pkg/data/oem_specs.json` (layer) y `contracts/oem_specs.yaml` (dev)."""
    pkg, contracts = tmp_path / "pkg", tmp_path / "contracts"
    (pkg / "data").mkdir(parents=True)
    contracts.mkdir()
    monkeypatch.setattr(ids, "HERE", pkg)
    monkeypatch.setattr(schema, "contracts_dir", lambda: contracts)
    ids.oem_catalog.cache_clear()
    yield pkg, contracts
    ids.oem_catalog.cache_clear()


def test_oem_specs_empty_when_the_sheet_does_not_exist(oem_dirs):
    assert ids.oem_specs("EX") == {} and ids.oem_catalog() == {}


def test_oem_specs_reads_the_yaml_in_development(oem_dirs):
    _, contracts = oem_dirs
    (contracts / "oem_specs.yaml").write_text(OEM_YAML)
    ex = ids.oem_specs("EX")
    assert ex["limits"]["coolant_temp_c"]["normal"] == [65, 96]
    assert ex["typical_failures"][0]["failure_mode"] == "head_gasket"
    assert ids.oem_specs("VQ") == {}
    ex["limits"].clear()  # copia: no ensucia la cache
    assert ids.oem_specs("EX")["limits"]


def test_oem_specs_prefers_the_bundled_json_of_the_layer(oem_dirs):
    pkg, contracts = oem_dirs
    (contracts / "oem_specs.yaml").write_text(OEM_YAML)
    (pkg / "data" / "oem_specs.json").write_text(json.dumps({"models": {"EX": {"limits": {}, "from": "layer"}}}))
    assert ids.oem_specs("EX") == {"limits": {}, "from": "layer"}


def test_oem_specs_without_contracts_dir(monkeypatch, tmp_path):
    def missing():
        raise FileNotFoundError("contracts/")
    monkeypatch.setattr(ids, "HERE", tmp_path)
    monkeypatch.setattr(schema, "contracts_dir", missing)
    ids.oem_catalog.cache_clear()
    try:
        assert ids.oem_specs("EX") == {}
    finally:
        ids.oem_catalog.cache_clear()


# --- schema.validate(ref=...) y eventos report.* -------------------------------------------------------------------

def _report_example(name):
    return json.loads((REPO / "contracts/examples/report" / name).read_text())


def test_schema_validate_ref_targets_one_def():
    schema.validate("report", _report_example("create.json"), ref="create")
    schema.validate("report", _report_example("close.json"), ref="close")
    with pytest.raises(schema.ContractError) as e:
        schema.validate("report", _report_example("close.json"), ref="create")
    assert str(e.value).startswith("report#create:")
    with pytest.raises(schema.ContractError) as e:
        schema.validate("report", {**_report_example("create.json"), "description": ""}, ref="create")
    assert e.value.path == ["description"]
    with pytest.raises(KeyError):
        schema.validate("report", {}, ref="nope")


def test_envelope_builds_report_events_against_their_contract():
    for name, event_type in (("created-event.json", "report.created"), ("closed-event.json", "report.closed")):
        ex = _report_example(name)
        env = envelope.build(event_type, "aws.reports-api", "report", ex["aggregate_id"], ex["payload"],
                             incident_id=ex["incident_id"], machine_id="EX-017")
        assert env["aggregate_type"] == "report" and env["payload"] == ex["payload"]
    bad = {k: v for k, v in _report_example("created-event.json")["payload"].items() if k != "urgency"}
    with pytest.raises(schema.ContractError):
        envelope.build("report.created", "aws.reports-api", "report", bad["report_id"], bad, machine_id="EX-017")


# ---------- catalogo multi-tenant ----------

def test_catalog_every_entity_belongs_to_exactly_one_tenant():
    cat = ids.load()
    assert set(ids.tenants()) == {"mineria", "agro", "industria"} and ids.default_tenant() == "mineria"
    for key, pk in (("machines", "machine_id"), ("zones", "zone_id"), ("technicians", "tech_id"),
                    ("fuel_stations", "station_id")):
        rows = cat[key]
        assert len({r[pk] for r in rows}) == len(rows), f"{key}: ids repetidos entre parques"
        assert all(r["tenant_id"] in ids.tenants() for r in rows)
    for m in cat["machines"]:  # la zona de cada maquina es de su mismo parque
        assert ids.tenant_of_zone(m["zone_id"]) == m["tenant_id"]
    for t in cat["technicians"]:
        assert ids.tenant_of_zone(t["zone_id"]) == t["tenant_id"]
    parts = {p["part_id"] for p in cat["parts"]}
    assert {b["part_id"] for b in cat["bom"]} <= parts and {s["part_id"] for s in cat["part_stock"]} <= parts
    assert set(cat["specialty_by_failure_mode"]) >= {b["failure_mode"] for b in cat["bom"]}


def test_catalog_per_tenant_views():
    assert len(ids.machines("mineria")) == 100 and len(ids.machines("agro")) == 12
    assert set(ids.zones("industria")) == {"PLANTA-1", "PLANTA-2", "ALMACEN"}
    assert {t["tech_id"] for t in ids.technicians("agro")} == {"TEC-11", "TEC-12", "TEC-13", "TEC-14"}
    assert ids.tenant_of_machine("CS-003") == "agro" and ids.tenant_of_machine("GE-001") == "industria"
    assert ids.tenant_of_machine("EX-017") == "mineria" and ids.tenant_of_machine("XX-999") == "mineria"
    assert ids.tenant_of_item({"zone_id": "PACKING"}) == "agro"
    assert ids.tenant_of_item({"tenant_id": "industria", "machine_id": "EX-001"}) == "industria"
    assert ids.bom_for("CS", "final_drive") == [{"part_id": "PRT-17", "qty": 2}, {"part_id": "PRT-16", "qty": 1}]


def test_envelope_always_carries_the_tenant():
    env = envelope.build("fuel.anomaly", "aws.daily-planning", "machine", "CS-003", {}, machine_id="CS-003")
    assert env["tenant_id"] == "agro"
    assert envelope.build("fuel.anomaly", "aws.daily-planning", "machine", "CS-003", {}, machine_id="CS-003",
                          tenant_id="industria")["tenant_id"] == "industria"
    assert envelope.build("kpi.daily", "aws.daily-planning", "fleet", "KPI-2026-11-15",
                          {"day": "2026-11-15"})["tenant_id"] == "mineria"
