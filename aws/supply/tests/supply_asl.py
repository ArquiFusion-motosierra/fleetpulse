"""Interprete minimo de ASL para las pruebas de punta a punta de `statemachine/supply-flow.asl.json`, con las Lambdas
reales (moto + dobles) en lugar de Step Functions. Misma idea que aws/incidents/tests/incidents_asl.py, adaptado a lo
que usa supply-flow: Task de Lambda con Parameters, `lambda:invoke.waitForTaskToken` (TimeoutSeconds o
TimeoutSecondsPath), `sns:publish`, Choice (StringEquals), Parallel, Succeed y Fail; ResultPath (tambien null), Retry
(sin esperas, MaxAttempts 0 incluido) y Catch.

Imita de Step Functions lo que rompe las costuras ASL <-> Lambda: entrada y salida de cada Lambda pasan por JSON;
una ruta inexistente es `States.Runtime` (ningun Catch la atrapa); `States.ALL` atrapa todo menos `States.Runtime`;
un Catch sin ResultPath reemplaza el estado por `{"Error", "Cause"}`; `sns:publish` valida lo que SNS rechazaria.
Tambien hace de cliente de Step Functions para la API (`send_task_success`, `send_task_failure`).

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
ASL_TEXT = (ROOT / "statemachine" / "supply-flow.asl.json").read_text()
REGION, ACCOUNT = "us-east-1", "000000000000"
TOPICS = {"NotifyTopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:fp-notify",
          "AlertsTopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:fp-alerts"}
_FORMAT = re.compile(r"^States\.Format\('((?:[^'\\]|\\.)*)'((?:, *\$[$.\w]+)*)\)$")


def lambda_arn(key: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:fp-supply-dev-{key[:-3]}"


def definition(lambda_keys) -> dict:
    subs = {**TOPICS, **{k: lambda_arn(k) for k in lambda_keys}}
    return json.loads(re.sub(r"\$\{(\w+)\}", lambda m: subs[m.group(1)], ASL_TEXT))  # KeyError = sin valor


class StatesError(Exception):
    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error, self.cause = error, cause


def _matches(names: list[str], error: str) -> bool:
    if error in ("States.Runtime", "States.DataLimitExceeded"):
        return False
    return error in names or "States.ALL" in names or ("States.TaskFailed" in names and error != "States.Timeout")


def _path(path: str, data, ctx: dict):
    if path == "$":
        return data
    root, rest = (ctx, path[3:]) if path.startswith("$$.") else (data, path[2:])
    assert path.startswith("$"), path
    for k in rest.split("."):
        if not isinstance(root, dict) or k not in root:
            raise StatesError("States.Runtime", f"la ruta {path} no existe en la entrada")
        root = root[k]
    return root


def _format(expr: str, data, ctx: dict) -> str:
    m = _FORMAT.match(expr)
    assert m, f"intrinseca no soportada: {expr}"
    template, args = m.group(1), [a.strip() for a in m.group(2).split(",") if a.strip()]
    if template.count("{}") != len(args):
        raise StatesError("States.Runtime", "States.Format con argumentos de mas o de menos")
    for arg in args:
        v = _path(arg, data, ctx)
        template = template.replace("{}", v if isinstance(v, str) else json.dumps(v), 1)
    return template


def _resolve(tmpl, data, ctx: dict):
    if isinstance(tmpl, dict):
        return {(k[:-2] if k.endswith(".$") else k):
                ((_format(v, data, ctx) if v.startswith("States.") else _path(v, data, ctx)) if k.endswith(".$")
                 else _resolve(v, data, ctx)) for k, v in tmpl.items()}
    if isinstance(tmpl, list):
        return [_resolve(v, data, ctx) for v in tmpl]
    return tmpl


def _put(data, path, value):
    if path is None:  # ResultPath null: descarta el resultado
        return data
    if path == "$":
        return value
    out = copy.deepcopy(data)
    cur = out
    keys = path[2:].split(".")
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    return out


def _rule(rule: dict, data) -> bool:
    value = _path(rule["Variable"], data, {})
    if "StringEquals" in rule:
        return isinstance(value, str) and value == rule["StringEquals"]
    raise AssertionError(f"operador de Choice no soportado: {rule}")


def _json(value, what: str):
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise StatesError("Runtime.MarshalError", f"{what}: {exc}") from None


class Sns:
    """`sns:publish` de la ASL y cliente SNS de las Lambdas. Valida lo que SNS rechazaria."""

    def __init__(self):
        self.calls = []

    def publish(self, **kw):
        subject, message = kw.get("Subject"), kw.get("Message")
        assert kw.get("TopicArn", "").startswith("arn:aws:sns:"), kw
        assert isinstance(message, str) and message.strip(), "Message vacio"
        if subject is not None:
            assert subject.isascii() and "\n" not in subject and 0 < len(subject) < 100, subject
        for attr in (kw.get("MessageAttributes") or {}).values():
            assert attr["DataType"] == "String" and attr["StringValue"], kw
        self.calls.append(kw)
        return {"MessageId": f"msg-{len(self.calls)}"}

    def to(self, role: str) -> list[dict]:
        return [c for c in self.calls if c.get("MessageAttributes", {}).get("role", {}).get("StringValue") == role]


class Execution:
    def __init__(self, machine: "StateMachine", name: str, data: dict):
        self.machine, self.name = machine, name
        self.status, self.output, self.error, self.cause = "RUNNING", None, None, None
        self.visited, self.token, self.timeout_s, self._waiting = [], None, None, None
        self._data = _json(data, "entrada")

    @property
    def ctx(self) -> dict:
        return {"Execution": {"Name": self.name}}

    def start(self):
        self._loop(self.machine.defn["StartAt"], self._data)
        return self

    def _loop(self, name, data):
        states = self.machine.defn["States"]
        try:
            while name is not None:
                self.visited.append(name)
                name, data = self._step(states, name, data, top=True)
                if self.status == "WAITING":
                    return
        except StatesError as exc:
            self.status, self.error, self.cause = "FAILED", exc.error, exc.cause
            return
        if self.status == "RUNNING":
            self.status, self.output = "SUCCEEDED", data

    def _step(self, states, name, data, *, top):
        st = states[name]
        kind = st["Type"]
        if kind == "Succeed":
            return None, data
        if kind == "Fail":
            raise StatesError(st["Error"], st.get("Cause", ""))
        if kind == "Choice":
            nxt = next((r["Next"] for r in st["Choices"] if _rule(r, data)), st.get("Default"))
            if nxt is None:
                raise StatesError("States.NoChoiceMatched", name)
            return nxt, data
        assert kind in ("Task", "Parallel"), kind
        try:
            result = self._attempts(name, st, data, top=top)
        except StatesError as exc:
            return self._catch(st, data, exc)
        if self.status == "WAITING":
            return None, data
        return self._next(st), _put(data, st.get("ResultPath", "$"), result)

    @staticmethod
    def _next(st):
        return None if st.get("End") else st["Next"]

    def _catch(self, st, data, exc):
        for c in st.get("Catch", []):
            if _matches(c["ErrorEquals"], exc.error):
                return c["Next"], _put(data, c.get("ResultPath", "$"), {"Error": exc.error, "Cause": exc.cause})
        raise exc

    def _attempts(self, name, st, data, *, top):
        used = [0] * len(st.get("Retry", []))
        while True:
            try:
                return self._run(name, st, data, top=top)
            except StatesError as exc:
                i = next((i for i, r in enumerate(st.get("Retry", [])) if _matches(r["ErrorEquals"], exc.error)), None)
                if i is None or used[i] >= st["Retry"][i].get("MaxAttempts", 3):
                    raise
                used[i] += 1

    def _invoke(self, arn, payload, what):
        key = self.machine.arns[arn]
        self.machine.invocations[key] = self.machine.invocations.get(key, 0) + 1
        try:
            result = self.machine.lambdas[key](_json(payload, f"entrada de {what}"))
        except Exception as exc:  # como el runtime de Lambda: errorType = clase de la excepcion
            raise StatesError(type(exc).__name__, str(exc)) from exc
        return _json(result, f"salida de {what}")

    def _run(self, name, st, data, *, top):
        if st["Type"] == "Parallel":
            out = []
            for b in st["Branches"]:
                n, d = b["StartAt"], copy.deepcopy(data)
                while n is not None:
                    n, d = self._step(b["States"], n, d, top=False)
                out.append(d)
            return out
        res = st["Resource"]
        if res == "arn:aws:states:::sns:publish":
            return self.machine.sns.publish(**_resolve(st["Parameters"], data, self.ctx))
        if res == "arn:aws:states:::lambda:invoke.waitForTaskToken":
            assert top
            self.token = self.machine.new_token(self)
            params = _resolve(st["Parameters"], data, {**self.ctx, "Task": {"Token": self.token}})
            if "TimeoutSecondsPath" in st:
                self.timeout_s = _path(st["TimeoutSecondsPath"], data, self.ctx)
                assert isinstance(self.timeout_s, int) and self.timeout_s > 0, self.timeout_s
            else:
                self.timeout_s = st["TimeoutSeconds"]
            self._invoke(params["FunctionName"], params["Payload"], name)
            self.status, self._waiting = "WAITING", (name, st, data)
            return None
        assert res.startswith("arn:aws:lambda:"), res
        return self._invoke(res, _resolve(st["Parameters"], data, self.ctx), name)

    def _resume(self, outcome):
        name, st, data = self._waiting
        self.status, self._waiting, self.token = "RUNNING", None, None
        try:
            if isinstance(outcome, StatesError):
                nxt, data = self._catch(st, data, outcome)
            else:
                nxt, data = self._next(st), _put(data, st.get("ResultPath", "$"), outcome)
        except StatesError as exc:
            self.status, self.error, self.cause = "FAILED", exc.error, exc.cause
            return
        self._loop(nxt, data)

    @property
    def waiting_in(self) -> str | None:
        return self._waiting[0] if self._waiting else None

    def time_out(self):
        assert self.status == "WAITING"
        self.machine.spent.add(self.token)
        self._resume(StatesError("States.Timeout", ""))


class StateMachine:
    """supply-flow con las Lambdas de prueba; tambien es el cliente `stepfunctions` de supply-sourcing y de la API."""

    def __init__(self, lambdas: dict, sns: Sns):
        self.defn = definition(lambdas)
        self.lambdas, self.sns = lambdas, sns
        self.arns = {lambda_arn(k): k for k in lambdas}
        self.executions: dict[str, Execution] = {}
        self.invocations: dict[str, int] = {}
        self.spent: set[str] = set()
        self.exceptions = boto3.client("stepfunctions", region_name=REGION).exceptions
        self._tokens = 0

    def _raise(self, code, op):
        raise getattr(self.exceptions, code)({"Error": {"Code": code, "Message": code}}, op)

    def start_execution(self, stateMachineArn, name, input):  # noqa: A002,N803 (firma de boto3)
        assert stateMachineArn.endswith(":stateMachine:supply-flow"), stateMachineArn
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name), name
        if name in self.executions:
            self._raise("ExecutionAlreadyExists", "StartExecution")
        self.executions[name] = Execution(self, name, json.loads(input)).start()
        return {"executionArn": f"arn:aws:states:{REGION}:{ACCOUNT}:execution:supply-flow:{name}"}

    def new_token(self, ex):
        self._tokens += 1
        return f"AAAAKgAAAAIAAAAA{ex.name}{self._tokens}+/x/Yw=="

    def _waiting(self, token, op):
        ex = next((e for e in self.executions.values() if e.status == "WAITING" and e.token == token), None)
        if ex is None:
            self._raise("TaskTimedOut" if token in self.spent else "TaskDoesNotExist", op)
        self.spent.add(token)
        return ex

    def send_task_success(self, taskToken, output):  # noqa: N803
        self._waiting(taskToken, "SendTaskSuccess")._resume(json.loads(output))
        return {}

    def send_task_failure(self, taskToken, error=None, cause=None):  # noqa: N803
        self._waiting(taskToken, "SendTaskFailure")._resume(StatesError(error or "", cause or ""))
        return {}
