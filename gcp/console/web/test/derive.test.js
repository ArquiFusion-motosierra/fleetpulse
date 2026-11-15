import { test } from "node:test";
import assert from "node:assert/strict";
import { countBySeverity, lowStock, machinesWithAlert, needsAttention, parkSummary, techWork } from "../src/derive.js";

test("parkSummary cuenta por parque y lleva a mineria lo que no tiene tenant", () => {
  const m = [{ tenant_id: "agro", severity: "schedule", status: "operating" }, { severity: "immediate", status: "idle" }];
  const i = [{ tenant_id: "agro", status: "open" }, { status: "closed" }, { status: "open" }];
  const s = parkSummary(m, i, ["mineria", "agro", "industria"]);
  assert.deepEqual(s.agro, { machines: 1, operating: 1, immediate: 0, schedule: 1, observe: 0, open: 1 });
  assert.deepEqual(s.mineria, { machines: 1, operating: 0, immediate: 1, schedule: 0, observe: 0, open: 1 });
  assert.deepEqual(s.industria, { machines: 0, operating: 0, immediate: 0, schedule: 0, observe: 0, open: 0 });
});
test("needsAttention separa inmediatos sin técnico, aprobaciones y reportes abiertos", () => {
  const inc = [{ incident_id: "A", status: "open", severity: "immediate" }, { incident_id: "B", status: "open", severity: "schedule" },
    { incident_id: "C", status: "open", severity: "schedule", tech_id: "TEC-01" }, { incident_id: "D", status: "closed", severity: "immediate" }];
  const r = needsAttention(inc, [{ report_id: "R1", status: "open" }, { report_id: "R2", status: "closed" }]);
  assert.deepEqual(r.immediateNoTech.map((x) => x.incident_id), ["A"]);
  assert.deepEqual(r.awaitingApproval.map((x) => x.incident_id), ["B"]);
  assert.deepEqual(r.unreviewedReports.map((x) => x.report_id), ["R1"]);
  assert.deepEqual(needsAttention([], []), { immediateNoTech: [], awaitingApproval: [], unreviewedReports: [] });
});
test("techWork ordena por severidad y fecha y tolera técnico ausente", () => {
  const inc = [{ incident_id: "X", status: "open", tech_id: "TEC-11", severity: "schedule", opened_at: "2026-11-15T10:00:00Z" },
    { incident_id: "Y", status: "open", tech_id: "TEC-11", severity: "immediate", opened_at: "2026-11-15T12:00:00Z" },
    { incident_id: "Z", status: "open", tech_id: "TEC-01", severity: "immediate", opened_at: "2026-11-15T09:00:00Z" }];
  const w = techWork(inc, [{ status: "in_review" }, { status: "closed" }], "TEC-11");
  assert.deepEqual(w.incidents.map((x) => x.incident_id), ["Y", "X"]);
  assert.equal(w.next.incident_id, "Y");
  assert.equal(w.reports.length, 1);
  assert.deepEqual(techWork(inc, [], null), { incidents: [], reports: [], next: null, noTech: true });
});
test("countBySeverity, lowStock y machinesWithAlert", () => {
  assert.deepEqual(countBySeverity([{ status: "operating" }, { severity: "observe" }]),
    { operating: 1, ok: 1, observe: 1, schedule: 0, immediate: 0 });
  assert.deepEqual(lowStock([{ part_id: "P1", below_reorder: true }, { part_id: "P2", below_reorder: false }]).map((s) => s.part_id), ["P1"]);
  const ms = [{ machine_id: "a", severity: "observe" }, { machine_id: "b", severity: "immediate" }, { machine_id: "c" }];
  assert.deepEqual(machinesWithAlert(ms).map((m) => m.machine_id), ["b", "a"]);
});
