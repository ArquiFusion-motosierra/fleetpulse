"""Pruebas estaticas de `statemachine/supply-flow.asl.json`, `serverless.yml`, `functions-flow.yml` y el lugar de
fp-supply en el compose y en CI."""
import importlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASL = json.loads((ROOT / "statemachine" / "supply-flow.asl.json").read_text())


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader que acepta las etiquetas cortas de CloudFormation (`!GetAtt`, `!Ref`, ...)."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    return {suffix: loader.construct_sequence(node)}


_CfnLoader.add_multi_constructor("!", _cfn_tag)
SLS = yaml.load((ROOT / "serverless.yml").read_text(), Loader=_CfnLoader)
RES = SLS["resources"]["Resources"]
FLOW_FNS = yaml.load((ROOT / "functions-flow.yml").read_text(), Loader=_CfnLoader)


def _states(states):
    for name, st in states.items():
        yield name, st
        for b in st.get("Branches", []):
            yield from _states(b["States"])


def _targets(st):
    out = [st.get("Next"), st.get("Default")] + [c["Next"] for c in st.get("Catch", []) + st.get("Choices", [])]
    return [t for t in out if t]


def test_every_transition_exists_and_every_state_is_reachable():
    states = ASL["States"]
    seen, todo = set(), [ASL["StartAt"]]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        assert n in states, n
        todo += _targets(states[n])
    assert seen == set(states)
    for b in states["NotifyReceived"]["Branches"]:
        for _, st in _states(b["States"]):
            assert st.get("End") is True and "Catch" not in st


def test_waits_store_the_token_and_reject_or_timeout_cancel():
    s = ASL["States"]
    w = s["WaitForApproval"]
    assert w["Resource"] == "arn:aws:states:::lambda:invoke.waitForTaskToken" and w["TimeoutSeconds"] == 86400
    assert w["Parameters"]["Payload"] == {"id.$": "$.id", "token.$": "$$.Task.Token"}
    assert w["Catch"][0] == {"ErrorEquals": ["SupplyRejected", "Rejected", "States.Timeout"], "ResultPath": "$.cancel",
                             "Next": "Cancel"}
    # una espera no reintenta el rechazo (States.ALL / TaskFailed lo atraparian y volverian a pedir aprobacion)
    for name in ("WaitForApproval", "WaitForReceipt", "WaitForReceiptLate"):
        for r in s[name]["Retry"]:
            assert not {"States.ALL", "States.TaskFailed", "SupplyRejected", "Rejected"} & set(r["ErrorEquals"]), name
    assert s["WaitForReceipt"]["TimeoutSecondsPath"] == "$.shipment.receipt_timeout_s"
    assert s["WaitForReceipt"]["Catch"][0]["Next"] == "WaitForReceiptLate"
    assert s["WaitForReceiptLate"]["Parameters"]["Payload"]["late"] is True
    assert s["Ship"]["Catch"][0] == {"ErrorEquals": ["InsufficientStock"], "ResultPath": "$.cancel", "Next": "Cancel"}


def test_lambda_tasks_retry_transient_errors_but_not_domain_ones():
    for name, st in _states(ASL["States"]):
        if st["Type"] == "Task" and st["Resource"].startswith("${"):
            assert st["Retry"][0] == {"ErrorEquals": ["InvalidState", "InsufficientStock"], "MaxAttempts": 0}, name
            assert st["Catch"][-1] == {"ErrorEquals": ["States.ALL"], "Next": "PublishAlert"}, name


def test_state_machine_resource_matches_lambdas_and_substitutions():
    p = RES["SupplyFlow"]["Properties"]
    assert p["StateMachineName"] == "supply-flow" and p["StateMachineType"] == "STANDARD"
    assert p["RoleArn"].endswith(":role/LabRole") and p["Definition"] == "${file(statemachine/supply-flow.asl.json)}"
    subs = p["DefinitionSubstitutions"]
    assert set(re.findall(r"\$\{(\w+)\}", json.dumps(ASL))) == set(subs)
    logical = {f"{k[0].upper()}{k[1:]}LambdaFunction" for k in FLOW_FNS}
    for k, v in subs.items():
        if k.endswith("TopicArn"):
            continue
        assert v["GetAtt"].removesuffix(".Arn") in logical, k
    assert p["LoggingConfiguration"]["IncludeExecutionData"] is True


def test_handlers_exist():
    for name, fn in FLOW_FNS.items():
        module, func = fn["handler"].rsplit(".", 1)
        assert callable(getattr(importlib.import_module(module.replace("/", ".")), func)), name


def test_supply_queue_only_gets_shortages_with_dlq_and_alarm():
    sub = RES["SupplySubscription"]["Properties"]
    assert sub["FilterPolicy"] == {"event_type": ["part.shortage"]} and sub["RawMessageDelivery"] is True
    q = RES["SupplyQueue"]["Properties"]
    assert q["QueueName"] == "supply-q" and q["RedrivePolicy"]["deadLetterTargetArn"] == {"GetAtt": "SupplyDlq.Arn"}
    assert q["VisibilityTimeout"] >= 6 * FLOW_FNS["sourcing"]["timeout"]
    (ev,) = FLOW_FNS["sourcing"]["events"]
    assert ev["sqs"]["arn"] == {"Fn::GetAtt": ["SupplyQueue", "Arn"]}
    assert ev["sqs"]["functionResponseType"] == "ReportBatchItemFailures"
    assert RES["SupplyDlqAlarm"]["Properties"]["Dimensions"] == [{"Name": "QueueName", "Value": "supply-dlq"}]


def test_flow_arn_is_exported_and_in_the_environment_for_t40():
    env = SLS["provider"]["environment"]
    assert env["SUPPLY_FLOW_ARN"].endswith(":stateMachine:supply-flow")
    assert env["REGISTRY_TABLE"] == "fp-registry" and env["OUTBOX_TABLE"] == "fp-outbox"
    assert SLS["resources"]["Outputs"]["SupplyFlowArn"] == {"Value": {"Ref": "SupplyFlow"},
                                                            "Export": {"Name": "${self:service}-SupplyFlowArn"}}
    assert RES["SupplyFlowArnParam"]["Properties"]["Name"] == "/fleetpulse/supply/flow-arn"
    assert SLS["functions"] == ["${file(./functions-flow.yml)}", "${file(./functions-api.yml)}"]
    assert isinstance(yaml.load((ROOT / "functions-api.yml").read_text(), Loader=_CfnLoader), dict)
    assert SLS["provider"]["iam"]["role"].endswith(":role/LabRole") and SLS["provider"]["architecture"] == "arm64"
    assert SLS["custom"]["cognitoAuthorizer"]["id"].startswith("${cf:fp-core-${sls:stage}.HttpApiAuthorizerCognitoId")


def test_compose_and_ci_include_supply():
    compose = yaml.safe_load((ROOT.parent / "serverless-compose.yml").read_text())
    assert compose["services"]["supply"] == {"path": "supply", "dependsOn": ["shared", "core", "auth"]}
    ci = (ROOT.parents[1] / ".github" / "workflows" / "ci.yml").read_text()
    assert " aws/supply " in ci


def test_asl_has_no_null_values_because_serverless_drops_them():
    """`${file(...asl.json)}` pasa por Serverless, que borra las claves en null: un `"ResultPath": null` desaparece en
    el deploy y la respuesta de SNS reemplaza el estado (WaitForReceipt perdia `$.id`, 15 nov). Usar una ruta."""
    nulls = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if v is None:
                    nulls.append(f"{path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(ASL, "$")
    assert nulls == []
