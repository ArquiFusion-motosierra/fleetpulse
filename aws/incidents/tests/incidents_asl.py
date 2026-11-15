"""Interprete minimo de ASL para las pruebas de punta a punta: corre `statemachine/incident-flow.asl.json` tal cual,
con las Lambdas reales (moto + dobles) en lugar de Step Functions.

Cubre solo lo que usa la definicion y falla ante cualquier otra cosa: Task (Lambda por el ARN de
DefinitionSubstitutions, `lambda:invoke.waitForTaskToken` y `sns:publish`), Choice (BooleanEquals, StringEquals,
IsPresent, IsNull, And, Or, Not), Pass, Parallel, Succeed y Fail; Parameters y ResultSelector con `.$` (rutas `$.`,
`$$.Task.Token`, `$$.Execution.Name` y States.Format), ResultPath, Retry (sin esperas) y Catch.

Lo que imita de Step Functions porque es donde se rompen las costuras ASL <-> Lambda:
- entrada y salida de cada Lambda pasan por JSON (un Decimal devuelto es `Runtime.MarshalError`);
- una ruta inexistente en Parameters o en una regla de Choice (salvo IsPresent) es `States.Runtime`, que ningun
  Catch atrapa y termina la ejecucion en FAILED;
- `States.ALL` atrapa todo menos `States.Runtime`; `States.TaskFailed` todo menos ademas `States.Timeout`;
- un Catch sin ResultPath reemplaza el estado por `{"Error", "Cause"}`;
- el estado no pasa de 256 KB;
- `sns:publish` valida lo que SNS rechazaria (Subject ASCII de una linea y < 100 caracteres, Message no vacio).

Nombre unico a proposito: CI corre las pruebas de todos los servicios en una sola sesion de pytest.
"""
from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
ASL_TEXT = (ROOT / "statemachine" / "incident-flow.asl.json").read_text()
REGION, ACCOUNT = "us-east-1", "000000000000"
TOPICS = {"NotifyTopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:fp-notify",
          "AlertsTopicArn": f"arn:aws:sns:{REGION}:{ACCOUNT}:fp-alerts"}
EXECUTION_PREFIX = f"arn:aws:states:{REGION}:{ACCOUNT}:execution:incident-flow"
MAX_STATE_BYTES = 256 * 1024
_FORMAT = re.compile(r"^States\.Format\('((?:[^'\\]|\\.)*)'((?:, *\$[$.\w]+)*)\)$")


def lambda_arn(key: str) -> str:
    """`ClassifyArn` -> ARN de Lambda falso; el runner lo vuelve a mapear a la funcion de prueba."""
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:fp-incidents-dev-{key[:-3]}"


def definition(lambda_keys) -> dict:
    """La ASL con DefinitionSubstitutions aplicadas, como la despliega CloudFormation."""
    subs = {**TOPICS, **{k: lambda_arn(k) for k in lambda_keys}}
    text = re.sub(r"\$\{(\w+)\}", lambda m: subs[m.group(1)], ASL_TEXT)  # KeyError = sustitucion sin valor
    return json.loads(text)


class StatesError(Exception):
    """Error de un estado tal como lo ven Retry y Catch."""

    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error, self.cause = error, cause


def _uncatchable(error: str) -> bool:
    return error in ("States.Runtime", "States.DataLimitExceeded")


def _matches(names: list[str], error: str) -> bool:
    if _uncatchable(error):
        return False
    return (error in names or "States.ALL" in names
            or ("States.TaskFailed" in names and error != "States.Timeout"))


def _path(path: str, data, ctx: dict):
    if path == "$":
        return data
    if path.startswith("$$."):
        root, rest = ctx, path[3:]
    elif path.startswith("$."):
        root, rest = data, path[2:]
    else:
        raise AssertionError(f"ruta no soportada por el runner: {path}")
    for key in rest.split("."):
        if not isinstance(root, dict) or key not in root:
            raise StatesError("States.Runtime", f"la ruta {path} no existe en la entrada")
        root = root[key]
    return root


def _format(expr: str, data, ctx: dict) -> str:
    m = _FORMAT.match(expr)
    if not m:
        raise AssertionError(f"intrinseca no soportada por el runner: {expr}")
    template = m.group(1)
    assert "\\" not in template, "el runner no interpreta escapes en States.Format"
    args = [a.strip() for a in m.group(2).split(",") if a.strip()]
    if template.count("{}") != len(args):
        raise StatesError("States.Runtime", f"States.Format con {len(args)} argumentos para "
                                            f"{template.count('{}')} marcadores")
    for arg in args:
        value = _path(arg, data, ctx)
        template = template.replace("{}", value if isinstance(value, str) else json.dumps(value), 1)
    return template


def _resolve(tmpl, data, ctx: dict):
    """Parameters / ResultSelector: las claves `x.$` se evaluan contra `data`; el resto se copia."""
    if isinstance(tmpl, dict):
        out = {}
        for k, v in tmpl.items():
            if k.endswith(".$"):
                out[k[:-2]] = _format(v, data, ctx) if v.startswith("States.") else _path(v, data, ctx)
            else:
                out[k] = _resolve(v, data, ctx)
        return out
    if isinstance(tmpl, list):
        return [_resolve(v, data, ctx) for v in tmpl]
    return tmpl


def _put(data, path: str, value):
    if path == "$":
        return value
    assert path.startswith("$."), path
    out = copy.deepcopy(data)
    cur = out
    keys = path[2:].split(".")
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
        if not isinstance(cur, dict):
            raise StatesError("States.Runtime", f"ResultPath {path} pasa por un valor que no es objeto")
    cur[keys[-1]] = value
    return out


def _rule(rule: dict, data) -> bool:
    if "And" in rule:
        return all(_rule(r, data) for r in rule["And"])
    if "Or" in rule:
        return any(_rule(r, data) for r in rule["Or"])
    if "Not" in rule:
        return not _rule(rule["Not"], data)
    var = rule["Variable"]
    if "IsPresent" in rule:
        try:
            _path(var, data, {})
        except StatesError:
            return rule["IsPresent"] is False
        return rule["IsPresent"] is True
    value = _path(var, data, {})
    if "IsNull" in rule:
        return (value is None) == rule["IsNull"]
    if "BooleanEquals" in rule:
        return isinstance(value, bool) and value == rule["BooleanEquals"]
    if "StringEquals" in rule:
        return isinstance(value, str) and value == rule["StringEquals"]
    raise AssertionError(f"operador de Choice no soportado por el runner: {rule}")


def _json(value, what: str):
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise StatesError("Runtime.MarshalError", f"{what}: {exc}") from None


class Sns:
    """`sns:publish` de la ASL y cliente SNS de WaitForApproval. Valida lo que SNS rechazaria."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on  # fail_on(call) -> True para simular una falla de SNS

    def publish(self, **kwargs):
        subject, message = kwargs.get("Subject"), kwargs.get("Message")
        assert kwargs.get("TopicArn", "").startswith("arn:aws:sns:"), kwargs
        assert isinstance(message, str) and message.strip(), "Message vacio"
        assert len(message.encode()) < 256 * 1024
        if subject is not None:
            assert subject.isascii() and "\n" not in subject and 0 < len(subject) < 100, subject
        if self.fail_on and self.fail_on(kwargs):
            raise StatesError("SNS.InternalErrorException", "falla simulada")
        self.calls.append(kwargs)
        return {"MessageId": f"msg-{len(self.calls)}"}

    def to(self, role: str) -> list[dict]:
        return [c for c in self.calls if c["MessageAttributes"]["role"]["StringValue"] == role]


class Execution:
    """Una ejecucion de incident-flow. `status`: RUNNING, WAITING (en un waitForTaskToken), SUCCEEDED, FAILED o
    ABORTED. `visited`: estados de primer nivel en orden; `invocations`: llamadas por Lambda (con reintentos)."""

    def __init__(self, machine: "StateMachine", name: str, data: dict):
        self.machine, self.name, self.arn = machine, name, f"{EXECUTION_PREFIX}:{name}"
        self.status, self.output, self.error, self.cause = "RUNNING", None, None, None
        self.visited, self.invocations = [], {}
        self.token, self._waiting = None, None  # (nombre del estado, estado, datos)
        self._data = _json(data, "entrada de la ejecucion")

    @property
    def ctx(self) -> dict:
        return {"Execution": {"Id": self.arn, "Name": self.name}}

    # --- bucle -----------------------------------------------------------------------------------------------------

    def start(self) -> "Execution":
        self._loop(self.machine.defn["StartAt"], self._data)
        return self

    def _loop(self, name: str | None, data) -> None:
        states = self.machine.defn["States"]
        try:
            while name is not None:
                self.visited.append(name)
                name, data = self._step(states, name, data, top=True)
                if self.status == "WAITING":
                    return
        except StatesError as exc:  # sin Catch que lo atrape
            self.status, self.error, self.cause = "FAILED", exc.error, exc.cause
            return
        if self.status == "RUNNING":
            self.status, self.output = "SUCCEEDED", data

    def _branch(self, branch: dict, data):
        name, states = branch["StartAt"], branch["States"]
        while name is not None:
            name, data = self._step(states, name, data, top=False)
        return data

    def _step(self, states: dict, name: str, data, *, top: bool):
        """Corre un estado. Devuelve (siguiente o None si termino, datos)."""
        st = states[name]
        if len(json.dumps(data).encode()) > MAX_STATE_BYTES:
            raise StatesError("States.DataLimitExceeded", name)
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
        if kind == "Pass":
            out = _put(data, st.get("ResultPath", "$"), copy.deepcopy(st["Result"])) if "Result" in st else data
            return self._next(st), out
        if kind not in ("Task", "Parallel"):
            raise AssertionError(f"tipo de estado no soportado por el runner: {kind}")
        try:
            result = self._attempts(name, st, data, top=top)
        except StatesError as exc:
            return self._catch(st, data, exc)
        if self.status == "WAITING":
            return None, data
        return self._next(st), self._apply_result(st, data, result)

    @staticmethod
    def _next(st: dict) -> str | None:
        if st.get("End"):
            return None
        return st["Next"]

    def _apply_result(self, st: dict, data, result):
        if "ResultSelector" in st:
            result = _resolve(st["ResultSelector"], result, self.ctx)
        return _put(data, st.get("ResultPath", "$"), result)

    def _catch(self, st: dict, data, exc: StatesError):
        for c in st.get("Catch", []):
            if _matches(c["ErrorEquals"], exc.error):
                return c["Next"], _put(data, c.get("ResultPath", "$"), {"Error": exc.error, "Cause": exc.cause})
        raise exc

    def _attempts(self, name: str, st: dict, data, *, top: bool):
        used = [0] * len(st.get("Retry", []))
        while True:
            try:
                return self._run(name, st, data, top=top)
            except StatesError as exc:
                i = next((i for i, r in enumerate(st.get("Retry", [])) if _matches(r["ErrorEquals"], exc.error)),
                         None)
                if i is None or used[i] >= st["Retry"][i].get("MaxAttempts", 3):
                    raise
                used[i] += 1

    def _invoke(self, arn: str, payload, what: str):
        key = self.machine.arns[arn]
        self.invocations[key] = self.invocations.get(key, 0) + 1
        try:
            result = self.machine.lambdas[key](_json(payload, f"entrada de {what}"))
        except StatesError:
            raise
        except Exception as exc:  # como el runtime de Lambda: errorType = clase de la excepcion
            raise StatesError(type(exc).__name__, str(exc)) from exc
        return _json(result, f"salida de {what}")

    def _run(self, name: str, st: dict, data, *, top: bool):
        if st["Type"] == "Parallel":
            return [self._branch(b, copy.deepcopy(data)) for b in st["Branches"]]
        resource = st["Resource"]
        if resource == "arn:aws:states:::sns:publish":
            params = _resolve(st["Parameters"], data, self.ctx)
            return self.machine.sns.publish(**params)
        if resource == "arn:aws:states:::lambda:invoke.waitForTaskToken":
            assert top, "waitForTaskToken dentro de un Parallel: el runner no lo soporta"
            self.token = self.machine.new_token(self)
            ctx = {**self.ctx, "Task": {"Token": self.token}}
            params = _resolve(st["Parameters"], data, ctx)
            self._invoke(params["FunctionName"], params["Payload"], name)
            self.status, self._waiting = "WAITING", (name, st, data)
            return None
        assert resource.startswith("arn:aws:lambda:"), resource
        assert "Parameters" not in st and "InputPath" not in st, "el runner pasa el estado completo a la Lambda"
        return self._invoke(resource, data, name)

    # --- callbacks de waitForTaskToken -----------------------------------------------------------------------------

    def _resume(self, outcome) -> None:
        name, st, data = self._waiting
        self.status, self._waiting, self.token = "RUNNING", None, None
        try:
            if isinstance(outcome, StatesError):
                nxt, data = self._catch(st, data, outcome)
            else:
                nxt, data = self._next(st), self._apply_result(st, data, outcome)
        except StatesError as exc:
            self.status, self.error, self.cause = "FAILED", exc.error, exc.cause
            return
        self._loop(nxt, data)

    def time_out(self) -> None:
        """Vencen los TimeoutSeconds del estado que espera (sin aprobacion en 30 min)."""
        assert self.status == "WAITING"
        self.machine.spent.add(self.token)
        self._resume(StatesError("States.Timeout", "sin respuesta del supervisor"))


class StateMachine:
    """incident-flow con las Lambdas de prueba. Tambien hace de cliente de Step Functions para `api/approvals` y
    `api/close` (`send_task_success`, `stop_execution`) con las excepciones reales de botocore."""

    def __init__(self, lambdas: dict, sns: Sns):
        self.defn = definition(lambdas)
        self.lambdas, self.sns = lambdas, sns
        self.arns = {lambda_arn(k): k for k in lambdas}
        self.executions: dict[str, Execution] = {}
        self.spent: set[str] = set()
        self.exceptions = boto3.client("stepfunctions", region_name=REGION).exceptions
        self.stops, self._tokens = [], 0

    def start(self, data: dict) -> Execution:
        """Como `score`: nombre de la ejecucion = incident_id."""
        name = data["incident_id"]
        assert name not in self.executions, "ExecutionAlreadyExists"
        ex = self.executions[name] = Execution(self, name, data)
        return ex.start()

    def new_token(self, ex: Execution) -> str:
        """Los task tokens reales son base64 y traen `+`, `/` y `=`."""
        self._tokens += 1
        return "AAAAKgAAAAIAAAAAAAAAA" + base64.b64encode(f"{ex.name}#{self._tokens}".encode()).decode() + "+/x/Yw=="

    def _raise(self, code: str, operation: str):
        raise getattr(self.exceptions, code)({"Error": {"Code": code, "Message": code}}, operation)

    def send_task_success(self, taskToken: str, output: str):  # noqa: N803 (firma de boto3)
        ex = next((e for e in self.executions.values() if e.status == "WAITING" and e.token == taskToken), None)
        if ex is None:
            self._raise("TaskTimedOut" if taskToken in self.spent else "TaskDoesNotExist", "SendTaskSuccess")
        self.spent.add(taskToken)
        ex._resume(json.loads(output))
        return {}

    def stop_execution(self, executionArn: str, error=None, cause=None):  # noqa: N803 (firma de boto3)
        self.stops.append(executionArn)
        ex = next((e for e in self.executions.values() if e.arn == executionArn), None)
        if ex is None:
            self._raise("ExecutionDoesNotExist", "StopExecution")
        if ex.status in ("RUNNING", "WAITING"):
            if ex.token:
                self.spent.add(ex.token)
            ex.status, ex.error, ex.cause, ex.token, ex._waiting = "ABORTED", error, cause, None, None
        return {"stopDate": "2026-11-15T17:08:41Z"}
