"""scripts/smoke_planning.py (T18) con un Lab falso: el recorrido completo sin AWS ni GCP."""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import smoke_planning  # noqa: E402

DAY = "2026-11-15"
DUE = [{"machine_id": "EX-019", "order_id": "ORD-EX-019-20261117", "due_at": "2026-11-17T13:00:00Z", "days_to_due": 2.1},
       {"machine_id": "TA-001", "order_id": "ORD-TA-001-20261115", "due_at": "2026-11-15T13:00:00Z", "days_to_due": 0}]
REORDER = {"part_id": "PRT-01", "zone_id": "NORTE", "qty": 9}
TENANT = {"EX-019": "mineria", "TA-001": "agro", "NORTE": "mineria"}


class FakeLab:
    """fp-outbox, fp-inbox, fp-registry, la API, la consola y BigQuery en memoria; registry y gcpEvents al instante."""

    def __init__(self, earlier_writer=False):
        self.orders, self.reorders, self.outbox, self.claims, self.bq = {}, [], [], {}, []
        self.runs, self.status, self.stopped, self.history = [], {}, [], []
        self.drop_order, self.rewrite = None, False
        if earlier_writer:  # otra ejecucion de hoy ya escribio todo
            self.history.append(("manual-20261115T150000", "2026-11-15T15:00:00Z", self._write("manual-20261115T150000")))

    def tenants(self):
        return ["mineria", "agro", "industria"]

    def tenant_of(self, machine_id):
        return TENANT[machine_id]

    def tenant_of_zone(self, zone_id):
        return TENANT[zone_id]

    def rule_state(self):
        return "DISABLED"

    def _base(self, dry):
        return {"day": DAY, "horizon_days": 14, "dry_run": dry, "machines": 124, "due": DUE, "skipped_open": [],
                "already_planned": [], "reorders_already_planned": []}

    def _write(self, name):
        for e in DUE:
            if e["machine_id"] == self.drop_order:
                continue
            self.outbox.append((name, e["order_id"]))
            self.bq.append((name, e["order_id"]))
            self.orders[e["machine_id"]] = [{"PK": f"ORDER#{e['machine_id']}", "SK": e["due_at"], "order_id": e["order_id"],
                                            "status": "suggested", "kind": "preventive", "reason": "r",
                                            "hours_since_service": 421, "GSI1PK": "ORDERS", "machine_id": e["machine_id"]}]
            self.claims[f"ORDER#{e['machine_id']}"] = {"consumer": "planning"}
        self.claims["REORDER#PRT-01#NORTE"] = {"consumer": "planning"}
        self.reorders.append({"PK": "REORDER#PRT-01#NORTE", "SK": "2026-11-15T15:00:05Z",
                              "created_at": "2026-11-15T15:00:05Z", **REORDER, "source": "planning", "day": DAY,
                              "available": 6, "on_order": 0, "forecast_14d": 11, "reorder_point": 4, "GSI1PK": "REORDERS"})
        return {**self._base(False), "orders": [e["order_id"] for e in DUE], "reorders": [REORDER]}

    def start(self, payload, name):
        self.runs.append((payload, name))
        arn = f"arn:aws:states:us-east-1:1:execution:daily-planning:{name}"
        if payload.get("dry_run"):
            out = {**self._base(True), "orders": [e["order_id"] for e in DUE], "reorders": [REORDER]}
        elif self.orders and not self.rewrite:  # ya escrito: las maquinas tienen orden suggested
            out = {**self._base(False), "orders": [], "reorders": [],
                   "skipped_open": [e["machine_id"] for e in DUE]}
        else:
            out = self._write(name)
        self.status[arn] = {"status": "SUCCEEDED", "output": json.dumps(out)}
        return arn

    def describe(self, arn):
        return self.status[arn]

    def stop(self, arn):
        self.stopped.append(arn)

    def visited(self, arn):
        return ["PlanPreventive"]

    def writers_today(self, day):
        return list(self.history)

    def orders_of(self, machine_id):
        return self.orders.get(machine_id, [])

    def planning_reorders(self, day):
        return [r for r in self.reorders if r["day"] == day]

    def kpi_rows(self, day):
        return 0

    def outbox_orders(self, trace_id):
        return [a for t, a in self.outbox if t == trace_id]

    def claim(self, day, key):
        return self.claims.get(key)

    def recompute(self, machine_id, today, horizon):
        return next(e for e in DUE if e["machine_id"] == machine_id)

    def api_get(self, path, query=""):
        if path == "/orders":
            return 200, {"orders": [r for rows in self.orders.values() for r in rows]}
        return 200, {"reorders": self.reorders}

    def console_get(self, path, query="", token=None):
        if path == "/api/kpis":
            return 200, {"day": None, "machines": []}
        if path == "/api/orders":
            rows = [r for rows in self.orders.values() for r in rows]
            return 200, {"orders": rows if token else [r for r in rows if TENANT[r["machine_id"]] == "mineria"]}
        return 200, {"reorders": self.reorders}

    def head_token(self):
        return "token"

    def bq_orders(self, trace_id, since_iso):
        return [a for t, a in self.bq if t == trace_id]


def story(lab, **kw):
    lines = []
    s = smoke_planning.Story(lab, out=lines.append, sleep=lambda s: None, **kw)
    return s, s.run(), lines


def levels(s):
    return {name: level for level, name, _, _ in s.steps}


def test_full_story_is_green():
    lab = FakeLab()
    s, rc, lines = story(lab)
    assert rc == 0, "\n".join(lines)
    assert set(levels(s).values()) == {"OK"} and len(s.steps) == 14
    assert [n for _, n in lab.runs] == [f"{s.name}-dry", s.name, f"{s.name}-again"]
    assert s.writer[0] == s.name and not lab.stopped


def test_verifies_the_earlier_writer_when_this_run_writes_nothing():
    lab = FakeLab(earlier_writer=True)
    s, rc, lines = story(lab)
    assert rc == 0, "\n".join(lines)
    assert s.writer[0] == "manual-20261115T150000"


def test_fails_when_nothing_was_written_today():
    lab = FakeLab()
    lab.orders = {"EX-019": [], "TA-001": []}  # ordenes viejas abiertas: todo skipped_open, nadie escribio hoy
    s, rc, _ = story(lab)
    assert rc == 1 and levels(s)["4 corrida real"] == "FAIL"


def test_missing_order_in_registry_fails(monkeypatch):
    monkeypatch.setattr(smoke_planning, "REGISTRY_POLL_S", 0)
    lab = FakeLab()
    real = lab._write

    def write_without_registry(name):
        out = real(name)
        lab.orders.pop("TA-001", None)
        return out
    lab._write = write_without_registry
    s, rc, _ = story(lab)
    assert rc == 1 and levels(s)["7 ORDER# en fp-registry"] == "FAIL"


def test_rerun_that_writes_again_fails():
    lab = FakeLab()
    lab.rewrite = True
    s, rc, _ = story(lab)
    assert rc == 1 and levels(s)["13 segunda corrida"] == "FAIL"
    assert levels(s)["8 REORDER# en fp-registry"] == "OK"


def test_no_rerun_is_not_green():
    s, rc, _ = story(FakeLab(), rerun=False)
    assert rc == 2 and levels(s)["13 segunda corrida"] == "WARN"


def test_running_execution_is_stopped_in_finally(monkeypatch):
    monkeypatch.setattr(smoke_planning, "EXEC_TIMEOUT_S", 0)
    lab = FakeLab()
    real = lab.start

    def start_stuck(payload, name):
        arn = real(payload, name)
        lab.status[arn] = {"status": "RUNNING"}
        return arn
    lab.start = start_stuck
    s, rc, _ = story(lab)
    assert rc == 1 and levels(s)["2 dry-run"] == "FAIL"
    assert lab.stopped == [f"arn:aws:states:us-east-1:1:execution:daily-planning:{s.name}-dry"]


def test_interrupt_still_cleans_up():
    lab = FakeLab()
    real = lab.start

    def start_stuck(payload, name):
        arn = real(payload, name)
        lab.status[arn] = {"status": "RUNNING"}
        return arn

    def ctrl_c(seconds):
        raise KeyboardInterrupt
    lab.start = start_stuck
    s = smoke_planning.Story(lab, out=lambda line: None, sleep=ctrl_c)
    try:
        s.run()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt no deberia tragarse")
    assert lab.stopped == [f"arn:aws:states:us-east-1:1:execution:daily-planning:{s.name}-dry"]
