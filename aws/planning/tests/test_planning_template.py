"""Pruebas estaticas de `statemachine/daily-planning.asl.json`, su `serverless.yml` y su lugar en el compose."""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASL = json.loads((ROOT / "statemachine" / "daily-planning.asl.json").read_text())
RETRY = [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2}]


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader que acepta las etiquetas cortas de CloudFormation (`!GetAtt`, `!Ref`, ...)."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    return {suffix: loader.construct_sequence(node)}


_CfnLoader.add_multi_constructor("!", _cfn_tag)
SLS = yaml.load((ROOT / "serverless.yml").read_text(), Loader=_CfnLoader)
RES = SLS["resources"]["Resources"]


def test_plan_task_retries_and_alerts_on_failure():
    s = ASL["States"]
    assert ASL["StartAt"] == "PlanPreventive"
    task = s["PlanPreventive"]
    assert task["Resource"] == "${PlanArn}"
    assert task["Parameters"] == {"input.$": "$", "execution.$": "$$.Execution.Name"}
    assert task["Retry"] == RETRY
    assert task["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "PublishAlert"}]
    assert task["End"] is True
    alert = s["PublishAlert"]
    assert alert["Resource"] == "arn:aws:states:::sns:publish"
    assert alert["Parameters"]["TopicArn"] == "${AlertsTopicArn}"
    assert alert["Next"] == "Failed" and s["Failed"]["Type"] == "Fail"


def test_state_machine_is_standard_daily_planning_with_logs():
    p = RES["DailyPlanning"]["Properties"]
    assert p["StateMachineName"] == "daily-planning" and p["StateMachineType"] == "STANDARD"
    assert p["RoleArn"].endswith(":role/LabRole")
    subs = p["DefinitionSubstitutions"]
    assert subs["PlanArn"] == {"GetAtt": "PlanLambdaFunction.Arn"}
    assert subs["AlertsTopicArn"].endswith(":fp-alerts")
    assert p["LoggingConfiguration"]["IncludeExecutionData"] is True
    assert set(re.findall(r"\$\{(\w+)\}", json.dumps(ASL))) == set(subs)


def test_daily_schedule_is_disabled_by_default():
    rule = RES["DailyPlanningSchedule"]["Properties"]
    assert rule["ScheduleExpression"] == "cron(0 10 * * ? *)"  # 05:00 de Lima
    assert rule["State"] == "${param:schedule}"
    assert SLS["params"]["default"]["schedule"] == "DISABLED"
    (target,) = rule["Targets"]
    assert target["Arn"] == {"Ref": "DailyPlanning"} and target["RoleArn"].endswith(":role/LabRole")


def test_plan_lambda_env_and_package():
    fn = SLS["functions"]["plan"]
    assert fn["handler"] == "planning/handler.handler"
    env = SLS["provider"]["environment"]
    assert env == {"STATE_TABLE": "fp-state", "REGISTRY_TABLE": "fp-registry", "OUTBOX_TABLE": "fp-outbox",
                   "INBOX_TABLE": "fp-inbox", "HORIZON_DAYS": "14"}
    assert SLS["provider"]["iam"]["role"].endswith(":role/LabRole")
    assert SLS["provider"]["runtime"] == "python3.12" and SLS["provider"]["architecture"] == "arm64"
    assert "planning/**" in SLS["package"]["patterns"]


def test_compose_deploys_planning_after_core():
    compose = yaml.safe_load((ROOT.parent / "serverless-compose.yml").read_text())
    assert compose["services"]["planning"] == {"path": "planning", "dependsOn": ["shared", "core"]}
