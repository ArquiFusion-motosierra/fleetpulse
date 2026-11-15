"""Pruebas estaticas de `statemachine/incident-flow.asl.json` y su cableado en `serverless.yml`."""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASL = json.loads((ROOT / "statemachine" / "incident-flow.asl.json").read_text())
RETRY = [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2}]
ROLES = {"technician", "supervisor", "management", "operator"}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader que acepta las etiquetas cortas de CloudFormation (`!GetAtt`, `!Ref`, ...)."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    return {suffix: loader.construct_sequence(node)}


_CfnLoader.add_multi_constructor("!", _cfn_tag)
SLS = yaml.load((ROOT / "serverless.yml").read_text(), Loader=_CfnLoader)
FLOW = SLS["resources"]["Resources"]["IncidentFlow"]["Properties"]


def _states(defn):
    """Todos los estados, incluidos los de las ramas de un Parallel."""
    for name, st in defn["States"].items():
        yield name, st
        for branch in st.get("Branches", []):
            yield from _states(branch)


def _is_lambda(st):
    return st["Type"] == "Task" and not st["Resource"].startswith("arn:aws:states:::")


ALERT = {"ErrorEquals": ["States.ALL"], "Next": "PublishAlert"}


def _chain(states, name):
    """`name` y los estados que siguen por `Next` (se detiene en un Choice o en un estado final)."""
    out = [name]
    while "Next" in states[name]:
        name = states[name]["Next"]
        out.append(name)
    return out


def test_starts_at_classify_and_v1_order():
    s = ASL["States"]
    assert ASL["StartAt"] == "ClassifySeverity"
    assert s["ClassifySeverity"]["Next"] == "CheckExistingIncident"
    assert s["CheckExistingIncident"]["Next"] == "AlreadyOpen?"
    (rule,) = s["AlreadyOpen?"]["Choices"]
    assert rule == {"Variable": "$.existing.found", "BooleanEquals": True, "Next": "EscalatedOrIgnored"}
    assert s["EscalatedOrIgnored"]["Type"] == "Succeed"
    assert _chain(s, s["AlreadyOpen?"]["Default"]) == [
        "CorrelateZone", "CreateIncident", "ExplainCause", "RankTechnicians", "NeedsApproval"]
    (rule,) = s["NeedsApproval"]["Choices"]
    assert rule == {"Variable": "$.classified.needs_approval", "BooleanEquals": True, "Next": "WaitForApproval"}
    assert s["NeedsApproval"]["Default"] == "AssignTechnician"
    assert s["WaitForApproval"]["Next"] == "AssignTechnician"
    assert s["ApprovalTimedOut"]["Next"] == s["ApprovalUnavailable"]["Next"] == "AssignTechnician"
    # incidente cerrado desde la consola antes de asignar: AssignTechnician devuelve `skipped` y se termina
    assert _chain(s, "AssignTechnician") == ["AssignTechnician", "IncidentClosed?"]
    (rule,) = s["IncidentClosed?"]["Choices"]
    assert rule == {"Variable": "$.assignment.skipped", "IsPresent": True, "Next": "Done"}
    assert s["IncidentClosed?"]["Default"] == "RenderMessages"
    # los textos nombran al tecnico realmente asignado; la reserva de repuestos va al final
    assert _chain(s, "RenderMessages") == ["RenderMessages", "NotifyByRole", "ReserveParts", "Done"]
    assert s["Done"]["Type"] == "Succeed"
    paths = {n: s[n]["ResultPath"] for n in (
        "ClassifySeverity", "CheckExistingIncident", "CorrelateZone", "CreateIncident", "ExplainCause",
        "RankTechnicians", "WaitForApproval", "AssignTechnician", "RenderMessages", "ReserveParts")}
    assert paths == {"ClassifySeverity": "$.classified", "CheckExistingIncident": "$.existing",
                     "CorrelateZone": "$.correlation", "CreateIncident": "$.incident",
                     "ExplainCause": "$.explanation", "RankTechnicians": "$.ranking", "WaitForApproval": "$.approval",
                     "AssignTechnician": "$.assignment", "RenderMessages": "$.messages",
                     "ReserveParts": "$.reservation"}
    # el Parallel no pisa $ (ReserveParts lo necesita): su salida (lista por rama) se reduce a un valor fijo
    assert s["NotifyByRole"]["ResultSelector"] == {"published": True}
    assert s["NotifyByRole"]["ResultPath"] == "$.notified"


def test_every_lambda_task_has_retry_and_catch():
    lambdas = {n: st for n, st in _states(ASL) if _is_lambda(st)}
    assert set(lambdas) == {"ClassifySeverity", "CheckExistingIncident", "CorrelateZone", "CreateIncident",
                            "ExplainCause", "RankTechnicians", "AssignTechnician", "RenderMessages", "ReserveParts"}
    # pasos opcionales: si fallan se sigue sin su resultado. Sin $.correlation no hay grupo; sin $.explanation
    # AssignTechnician usa la plantilla (la IA nunca bloquea la asignacion)
    degrade = {"CorrelateZone": ("$.correlation_error", "CreateIncident"),
               "ExplainCause": ("$.explain_error", "RankTechnicians")}
    for name, st in lambdas.items():
        assert st["Retry"] == RETRY, name
        if name in degrade:
            path, nxt = degrade[name]
            assert st["Catch"] == [{"ErrorEquals": ["States.ALL"], "ResultPath": path, "Next": nxt}], name
        else:
            assert st["Catch"] == [ALERT], name
    assert ASL["States"]["NotifyByRole"]["Catch"] == [ALERT]


def test_asl_v1_has_wait_for_task_token_with_timeout_and_catch():
    w = ASL["States"]["WaitForApproval"]
    assert w["Type"] == "Task" and w["Resource"] == "arn:aws:states:::lambda:invoke.waitForTaskToken"
    assert w["Parameters"] == {"FunctionName": "${WaitForApprovalArn}",
                               "Payload": {"input.$": "$", "token.$": "$$.Task.Token"}}
    assert w["TimeoutSeconds"] == 1800 and w["ResultPath"] == "$.approval"
    # States.TaskFailed no incluye States.Timeout: el vencimiento no se reintenta (no se manda otro correo)
    assert w["Retry"] == [{**RETRY[0], "ErrorEquals": ["States.TaskFailed"]}]
    # cualquier otra falla (p. ej. SNS) no deja el incidente sin asignar: se asigna sin aprobacion (auto)
    assert w["Catch"] == [
        {"ErrorEquals": ["States.Timeout"], "ResultPath": "$.approval_timeout", "Next": "ApprovalTimedOut"},
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.approval_error", "Next": "ApprovalUnavailable"}]
    for name, approval in (("ApprovalTimedOut", "timeout"), ("ApprovalUnavailable", "auto")):
        t = ASL["States"][name]
        assert t["Type"] == "Pass" and t["ResultPath"] == "$.approval", name
        assert t["Result"] == {"approval": approval}, name  # sin tech_id: AssignTechnician toma el ranking


def test_definition_has_no_json_nulls():
    """Serverless v3 borra los `null` del JSON que carga con ${file()}: un `"ResultPath": null` desplegado se
    vuelve el `$` por defecto y la salida del paso pisa todo el estado. Para descartar un resultado se usa
    ResultSelector + ResultPath, nunca null."""
    def nulls(o, path="$"):
        if isinstance(o, dict):
            for k, v in o.items():
                yield from nulls(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                yield from nulls(v, f"{path}[{i}]")
        elif o is None:
            yield path
    assert list(nulls(ASL)) == []


def test_publish_alert_then_fail():
    alert = ASL["States"]["PublishAlert"]
    assert alert["Resource"] == "arn:aws:states:::sns:publish"
    params = alert["Parameters"]
    assert params["TopicArn"] == "${AlertsTopicArn}"
    assert "$.Error" in params["Message.$"] and "$.Cause" in params["Message.$"]
    assert "$$.Execution.Name" in params["Message.$"]
    assert ASL["States"][alert["Next"]]["Type"] == "Fail"


def test_notify_by_role_has_four_branches():
    par = ASL["States"]["NotifyByRole"]
    assert par["Type"] == "Parallel" and len(par["Branches"]) == 4
    publishes = [st for b in par["Branches"] for _, st in _states(b)
                 if st.get("Resource") == "arn:aws:states:::sns:publish"]
    seen = set()
    for st in publishes:
        p = st["Parameters"]
        role = p["MessageAttributes"]["role"]["StringValue"]
        assert p["TopicArn"] == "${NotifyTopicArn}" and p["Subject.$"] == "$.messages.subject"
        assert p["Message.$"] == f"$.messages.{role}" and p["MessageAttributes"]["role"]["DataType"] == "String"
        seen.add(role)
    assert seen == ROLES and len(publishes) == 4
    (mgmt,) = [b for b in par["Branches"] if b["States"][b["StartAt"]]["Type"] == "Choice"]
    choice = mgmt["States"][mgmt["StartAt"]]
    rules = json.dumps(choice["Choices"])
    assert "$.classified.severity" in rules and '"immediate"' in rules and "$.incident.group_id" in rules
    assert mgmt["States"][choice["Default"]]["Type"] == "Pass"


def test_substitutions_match_serverless_yml():
    used = set(re.findall(r"\$\{(\w+)\}", json.dumps(ASL)))
    assert used == set(FLOW["DefinitionSubstitutions"])
    assert FLOW["Definition"] == "${file(statemachine/incident-flow.asl.json)}"
    assert FLOW["StateMachineName"] == "incident-flow" and FLOW["StateMachineType"] == "STANDARD"
    assert FLOW["LoggingConfiguration"]["Level"] == "ALL"
    assert FLOW["LoggingConfiguration"]["IncludeExecutionData"] is True
    functions = {f"{name[0].upper()}{name[1:]}LambdaFunction" for name in SLS["functions"]}
    for key, value in FLOW["DefinitionSubstitutions"].items():
        if key.endswith("TopicArn"):
            assert value.startswith("arn:aws:sns:"), key
        else:
            assert value["GetAtt"].split(".")[0] in functions, key


def test_functions_and_http_routes_on_the_shared_fp_core_api():
    assert SLS["provider"]["httpApi"] == {"id": "${cf:fp-core-${sls:stage}.HttpApiId, 'offline-default-id'}"}
    assert "api/**" in SLS["package"]["patterns"]
    fns = SLS["functions"]
    routes = {name: [e["httpApi"] for e in fn.get("events", [])] for name, fn in fns.items()}
    # {token+}: los task tokens traen "/" (%2F en el enlace); la ruta greedy acepta el segmento decodificado o no
    assert routes["approvals"] == ["GET /approvals/{token+}", "POST /approvals/{token+}"]
    assert routes["close"] == ["POST /incidents/{incident_id}/close"]
    assert {n for n, r in routes.items() if r} == {"approvals", "close"}
    assert fns["explainCause"]["timeout"] == 15
    assert fns["explainCause"]["environment"] == {"INVOKER_SECRET": "fleetpulse/gcp/sa-invoker"}
    assert fns["waitForApproval"]["environment"] == {
        "NOTIFY_TOPIC_ARN": "arn:aws:sns:${aws:region}:${aws:accountId}:fp-notify",
        "API_URL_PARAM": "/fleetpulse/api/url"}
    assert fns["close"]["environment"] == {
        "EXECUTION_ARN_PREFIX": "arn:aws:states:${aws:region}:${aws:accountId}:execution:incident-flow"}
    for name in ("approvals", "close"):
        assert fns[name]["timeout"] <= 29, name  # tope de integracion de HTTP API
