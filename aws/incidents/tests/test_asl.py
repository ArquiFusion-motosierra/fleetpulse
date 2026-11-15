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


def test_starts_at_classify_and_v0_order():
    s = ASL["States"]
    assert ASL["StartAt"] == "ClassifySeverity"
    assert s["ClassifySeverity"]["Next"] == "CheckExistingIncident"
    assert s["CheckExistingIncident"]["Next"] == "AlreadyOpen?"
    (rule,) = s["AlreadyOpen?"]["Choices"]
    assert rule == {"Variable": "$.existing.found", "BooleanEquals": True, "Next": "EscalatedOrIgnored"}
    assert s["EscalatedOrIgnored"]["Type"] == "Succeed"
    chain, name = [], s["AlreadyOpen?"]["Default"]
    while "Next" in s[name]:
        chain.append(name)
        name = s[name]["Next"]
    # los textos nombran al tecnico realmente asignado; T14 inserta NeedsApproval entre Rank y Assign
    assert chain == ["CreateIncident", "RankTechnicians", "AssignTechnician", "RenderMessages", "NotifyByRole"]
    assert s[name]["Type"] == "Succeed"
    paths = {n: s[n]["ResultPath"] for n in ("ClassifySeverity", "CheckExistingIncident", "CreateIncident",
                                              "RankTechnicians", "RenderMessages", "AssignTechnician")}
    assert paths == {"ClassifySeverity": "$.classified", "CheckExistingIncident": "$.existing",
                     "CreateIncident": "$.incident", "RankTechnicians": "$.ranking",
                     "RenderMessages": "$.messages", "AssignTechnician": "$.assignment"}
    assert s["NotifyByRole"]["ResultPath"] is None  # el Parallel no pisa $ (ReserveParts lo necesita en T14)


def test_every_lambda_task_has_retry_and_catch():
    lambdas = {n: st for n, st in _states(ASL) if _is_lambda(st)}
    assert len(lambdas) == 6
    for name, st in lambdas.items():
        assert st["Retry"] == RETRY, name
        assert st["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "PublishAlert"}], name
    assert ASL["States"]["NotifyByRole"]["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "PublishAlert"}]


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
