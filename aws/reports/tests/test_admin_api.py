"""Pruebas de la Lambda `admin` (T35): tecnicos en fp-state y catalogo de tipos de anormalidad en fp-registry, solo
rol admin. Moto DynamoDB (sin red)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from reports_support import (
    ADMIN,
    CREATE_BODY,
    MANAGEMENT,
    OPERATOR,
    SUPERVISOR,
    TECH1,
    FakeHttp,
    api_event,
    body_of,
    hot_readings,
    log_lines,
    seed_machine,
    seed_readings,
    seed_tech,
)

from fplib import ids

REPO = Path(__file__).resolve().parents[3]
REPO_SEED = json.loads((REPO / "contracts/report.schema.json").read_text())["$defs"]["abnormality_type_seed"]["enum"]

TECHS = "GET /admin/technicians"
TECH = "GET /admin/technicians/{tech_id}"
PUT = "PUT /admin/technicians/{tech_id}"
TYPES = "GET /admin/abnormality-types"
ADD = "POST /admin/abnormality-types"
DEL = "DELETE /admin/abnormality-types/{code}"
NEW_TYPE = {"code": "brake_smell", "label_es": "Olor a freno quemado",
            "default_checks": ["Oler los frenos", "Medir espesor de pastillas"]}


@pytest.fixture(autouse=True)
def _env(reports_env):
    return reports_env


@pytest.fixture
def ddb(reports_ddb):
    return reports_ddb


@pytest.fixture
def adm(ddb):
    from api import admin

    return lambda event: admin.handler(event, registry=ddb.registry, state=ddb.state)


@pytest.fixture
def techs(ddb):
    seed_tech(ddb.state, "TEC-01", "NORTE", "hydraulics")
    seed_tech(ddb.state, "TEC-04", "SUR", "engine")
    return ddb


def _put(adm, body, claims=ADMIN, tech_id="TEC-01"):
    return adm(api_event(PUT, claims, body=body, path={"tech_id": tech_id}))


def _tech(ddb, tech_id="TEC-01"):
    return ddb.state.get_item(Key={"PK": f"TECH#{tech_id}", "SK": "META"}).get("Item")


# --- matriz de roles -------------------------------------------------------------------------------------------------

ROUTES = [(TECHS, None, None), (TECH, {"tech_id": "TEC-01"}, None), (PUT, {"tech_id": "TEC-01"}, {"available": False}),
          (TYPES, None, None), (ADD, None, NEW_TYPE), (DEL, {"code": "noise"}, None)]


@pytest.mark.parametrize("route,path,body", ROUTES)
@pytest.mark.parametrize("claims", [OPERATOR, TECH1, SUPERVISOR, MANAGEMENT, {"email": "sin-rol@example.com"}])
def test_only_admin_reaches_the_admin_routes(adm, techs, route, path, body, claims):
    resp = adm(api_event(route, claims, body=body, path=path))
    assert resp["statusCode"] == 403, resp["body"]
    assert _tech(techs)["available"] is True


@pytest.mark.parametrize("route,path,body", ROUTES)
def test_admin_routes_without_claims_are_401(adm, techs, route, path, body):
    assert adm(api_event(route, None, body=body, path=path))["statusCode"] == 401


@pytest.mark.parametrize("route,path,body,code", [(TECHS, None, None, 200), (TECH, {"tech_id": "TEC-01"}, None, 200),
                                                  (PUT, {"tech_id": "TEC-01"}, {"available": False}, 200),
                                                  (TYPES, None, None, 200), (ADD, None, NEW_TYPE, 201),
                                                  (DEL, {"code": "noise"}, None, 200)])
def test_admin_reaches_every_route(adm, techs, route, path, body, code):
    resp = adm(api_event(route, ADMIN, body=body, path=path))
    assert resp["statusCode"] == code, resp["body"]


def test_unknown_admin_route_is_404(adm, ddb):
    assert adm(api_event("DELETE /admin/technicians/{tech_id}", ADMIN, path={"tech_id": "TEC-01"}))["statusCode"] == 404


# --- tecnicos --------------------------------------------------------------------------------------------------------

def test_put_changes_availability_and_keeps_the_rest(adm, techs, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        resp = _put(adm, {"available": False})
    assert resp["statusCode"] == 200, resp["body"]
    item = _tech(techs)
    assert item["available"] is False
    assert (item["zone_id"], item["specialty"], item["GSI1PK"], item["name"]) == ("NORTE", "hydraulics", "ZONE#NORTE",
                                                                                 "Tecnico TEC-01")
    assert item["updated_by"] == "admin@example.com" and item["updated_at"] == "2026-11-15T14:40:12Z"
    tech = body_of(resp)["technician"]
    assert tech["available"] is False and "PK" not in tech and "GSI1PK" not in tech
    line = log_lines(caplog, "tecnico_actualizado")[0]
    assert line["tech_id"] == "TEC-01" and line["fields"] == ["available"]


def test_put_zone_moves_the_tech_in_the_zone_index_and_to_its_centroid(adm, techs):
    resp = _put(adm, {"zone_id": "SUR", "specialty": "engine"})
    assert resp["statusCode"] == 200, resp["body"]
    item = _tech(techs)
    sur = ids.zones()["SUR"]
    assert (item["zone_id"], item["GSI1PK"], item["GSI1SK"]) == ("SUR", "ZONE#SUR", "TECH#TEC-01")
    assert (float(item["lat"]), float(item["lon"])) == (sur["lat"], sur["lon"])
    assert item["specialty"] == "engine" and item["available"] is True
    by_zone = techs.state.query(IndexName="GSI1", KeyConditionExpression="GSI1PK = :z",
                                ExpressionAttributeValues={":z": "ZONE#SUR"})["Items"]
    assert sorted(i["tech_id"] for i in by_zone) == ["TEC-01", "TEC-04"]


def test_put_without_zone_does_not_touch_position(adm, techs):
    techs.state.update_item(Key={"PK": "TECH#TEC-01", "SK": "META"}, UpdateExpression="SET lat = :a, lon = :o",
                            ExpressionAttributeValues={":a": Decimal("-13.5"), ":o": Decimal("-71.1")})
    assert _put(adm, {"specialty": "electrical"})["statusCode"] == 200
    item = _tech(techs)
    assert (float(item["lat"]), float(item["lon"])) == (-13.5, -71.1)


def test_put_does_not_clobber_the_assignment_of_incident_flow(adm, techs):
    techs.state.update_item(Key={"PK": "TECH#TEC-01", "SK": "META"},
                            UpdateExpression="SET available = :f, current_incident = :i",
                            ExpressionAttributeValues={":f": False, ":i": "INC-EX-017-20261115T1430"})
    assert _put(adm, {"zone_id": "CENTRO"})["statusCode"] == 200
    item = _tech(techs)
    assert item["current_incident"] == "INC-EX-017-20261115T1430" and item["available"] is False
    resp = _put(adm, {"available": True})
    assert resp["statusCode"] == 409, resp["body"]
    assert body_of(resp)["current_incident"] == "INC-EX-017-20261115T1430"
    assert _tech(techs)["available"] is False


def test_put_availability_after_release_is_allowed(adm, techs):
    """`release` de incident-flow deja `current_incident` en null: el tecnico vuelve a ser editable."""
    techs.state.update_item(Key={"PK": "TECH#TEC-01", "SK": "META"}, UpdateExpression="SET current_incident = :n",
                            ExpressionAttributeValues={":n": None})
    assert _put(adm, {"available": False})["statusCode"] == 200
    assert _tech(techs)["available"] is False


@pytest.mark.parametrize("body", [
    {}, {"available": "false"}, {"available": 0}, {"zone_id": "OESTE"}, {"zone_id": 3}, {"specialty": "welding"},
    {"name": "Otro"}, {"available": True, "current_incident": None}, [1, 2],
])
def test_put_rejects_unknown_fields_and_bad_values(adm, techs, body):
    resp = _put(adm, body)
    assert resp["statusCode"] == 400, resp["body"]
    assert _tech(techs)["available"] is True


def test_put_unknown_or_unseeded_tech_is_404(adm, techs):
    assert _put(adm, {"available": False}, tech_id="TEC-99")["statusCode"] == 404
    assert _put(adm, {"available": False}, tech_id="TEC-05")["statusCode"] == 404  # en ids.yaml, sin item en fp-state
    assert _tech(techs, "TEC-05") is None
    assert _put(adm, {"available": False}, tech_id="x; drop")["statusCode"] == 404


def test_get_one_technician(adm, techs):
    resp = adm(api_event(TECH, ADMIN, path={"tech_id": "TEC-04"}))
    assert resp["statusCode"] == 200
    assert body_of(resp)["technician"]["zone_id"] == "SUR"
    assert adm(api_event(TECH, ADMIN, path={"tech_id": "TEC-05"}))["statusCode"] == 404


def test_list_technicians_merges_fp_state_with_ids_yaml(adm, techs):
    resp = adm(api_event(TECHS, ADMIN))
    assert resp["statusCode"] == 200
    rows = body_of(resp)["technicians"]
    assert [r["tech_id"] for r in rows] == [t["tech_id"] for t in sorted(ids.technicians(), key=lambda t: t["tech_id"])]
    by_id = {r["tech_id"]: r for r in rows}
    assert by_id["TEC-01"]["seeded"] is True and by_id["TEC-01"]["available"] is True
    assert by_id["TEC-05"]["seeded"] is False and by_id["TEC-05"]["zone_id"] == "CENTRO"
    allowed = body_of(resp)["allowed"]
    assert allowed["zone_id"] == sorted(ids.zones()) and "hydraulics" in allowed["specialty"]


# --- catalogo de tipos de anormalidad --------------------------------------------------------------------------------

def _codes(resp):
    return [t["code"] for t in body_of(resp)["abnormality_types"]]


def test_get_returns_the_seeded_catalog(adm, ddb):
    resp = adm(api_event(TYPES, ADMIN))
    assert resp["statusCode"] == 200
    assert _codes(resp) == sorted(REPO_SEED)
    assert _codes(adm(api_event(TYPES, ADMIN))) == sorted(REPO_SEED)  # semilla idempotente
    items = ddb.registry.query(KeyConditionExpression="PK = :p",
                               ExpressionAttributeValues={":p": "CATALOG#abnormality_type"})["Items"]
    assert len(items) == len(REPO_SEED) + 1  # + #seeded


def test_add_replace_and_delete_types(adm, ddb, caplog):
    with caplog.at_level("INFO", logger="fplib"):
        resp = adm(api_event(ADD, ADMIN, body=NEW_TYPE))
    assert resp["statusCode"] == 201, resp["body"]
    assert body_of(resp)["abnormality_type"]["code"] == "brake_smell"
    assert log_lines(caplog, "tipo_anormalidad_guardado")[0]["created"] is True
    resp = adm(api_event(ADD, ADMIN, body={**NEW_TYPE, "label_es": "Olor a frenos"}))
    assert resp["statusCode"] == 200
    types = {t["code"]: t for t in body_of(adm(api_event(TYPES, ADMIN)))["abnormality_types"]}
    assert types["brake_smell"]["label_es"] == "Olor a frenos" and types["brake_smell"]["updated_by"] == "admin@example.com"
    resp = adm(api_event(DEL, ADMIN, path={"code": "brake_smell"}))
    assert resp["statusCode"] == 200 and body_of(resp) == {"deleted": "brake_smell"}
    assert adm(api_event(DEL, ADMIN, path={"code": "brake_smell"}))["statusCode"] == 404
    assert "brake_smell" not in _codes(adm(api_event(TYPES, ADMIN)))


def test_deleted_seed_type_does_not_come_back(adm, ddb):
    """Borrar antes de cualquier lectura siembra primero: el tipo borrado no reaparece en la siguiente lectura."""
    assert adm(api_event(DEL, ADMIN, path={"code": "noise"}))["statusCode"] == 200
    assert "noise" not in _codes(adm(api_event(TYPES, ADMIN)))
    assert len(_codes(adm(api_event(TYPES, ADMIN)))) == len(REPO_SEED) - 1


@pytest.mark.parametrize("body", [
    {}, {"code": "Brake", "label_es": "x"}, {"code": "brake_smell"}, {"code": "brake_smell", "label_es": "  "},
    {"code": "brake_smell", "label_es": "x" * 81}, {"code": "#seeded", "label_es": "x"},
    {"code": "brake_smell", "label_es": "x", "default_checks": "uno"},
    {"code": "brake_smell", "label_es": "x", "default_checks": ["ok", ""]},
    {"code": "brake_smell", "label_es": "x", "default_checks": ["p"] * 11},
    {"code": "brake_smell", "label_es": "x", "default_checks": ["p" * 301]},
    {"code": "brake_smell", "label_es": "x", "extra": 1},
])
def test_add_validates_the_body(adm, ddb, body):
    resp = adm(api_event(ADD, ADMIN, body=body))
    assert resp["statusCode"] == 400, resp["body"]


def test_delete_rejects_a_bad_code(adm, ddb):
    assert adm(api_event(DEL, ADMIN, path={"code": "#seeded"}))["statusCode"] == 404
    assert len(_codes(adm(api_event(TYPES, ADMIN)))) == len(REPO_SEED)


def test_operator_form_and_reports_follow_the_admin_catalog(adm, reports_svc, ddb):
    """`GET /abnormality-types` y `POST /reports` (Lambda reports) leen el mismo catalogo que edita el admin."""
    seed_machine(ddb.state)
    seed_readings(ddb.state, hot_readings())
    reports_svc.http = FakeHttp(TimeoutError("timed out"))  # plantilla: usa los default_checks del tipo nuevo
    assert adm(api_event(ADD, ADMIN, body=NEW_TYPE))["statusCode"] == 201
    assert adm(api_event(DEL, ADMIN, path={"code": "noise"}))["statusCode"] == 200
    form = _codes(reports_svc.call(api_event("GET /abnormality-types", OPERATOR)))
    assert "brake_smell" in form and "noise" not in form
    resp = reports_svc.call(api_event("POST /reports", OPERATOR, body={**CREATE_BODY, "abnormality_type": "noise"}))
    assert resp["statusCode"] == 400
    resp = reports_svc.call(api_event("POST /reports", OPERATOR,
                                      body={**CREATE_BODY, "abnormality_type": "brake_smell"}))
    assert resp["statusCode"] == 201, resp["body"]
    checks = body_of(resp)["report"]["recommendation"]["checks"]
    assert [c["step"] for c in checks] == NEW_TYPE["default_checks"]


def test_serverless_routes_match_the_handlers():
    """Cada ruta de `ROUTES` esta declarada en serverless.yml para su funcion, y todas con el autorizador cognito."""
    import yaml
    from api import admin, reports

    fns = yaml.safe_load((REPO / "aws/reports/serverless.yml").read_text())["functions"]
    for name, mod in (("admin", admin), ("reports", reports)):
        events = [e["httpApi"] for e in fns[name]["events"]]
        assert {f"{e['method']} {e['path']}" for e in events} == set(mod.ROUTES)
        assert all(e["authorizer"] == "${self:custom.cognitoAuthorizer}" for e in events)
    assert fns["admin"]["handler"] == "api/admin.handler"
