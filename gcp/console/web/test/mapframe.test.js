import { test } from "node:test";
import assert from "node:assert/strict";
import { framePoints } from "../src/mapframe.js";

const agro = [{ id: "FUNDO-A", lat: -12.1, lon: -76.9, tenant: "agro" }, { id: "PACKING", lat: -12.2, lon: -76.8, tenant: "agro" }];

test("frames the park zones even when no machine has a position", () => {
  assert.equal(framePoints(agro, [{ machine_id: "TA-001", tenant_id: "agro" }]).length, 2);
});

test("ignores machines of the previous park still on screen right after switching", () => {
  const stale = [{ machine_id: "EX-017", lat: -9.5, lon: -77.5, tenant_id: "mineria" }];
  const pts = framePoints(agro, stale);
  assert.deepEqual(pts.map((p) => p.id), ["FUNDO-A", "PACKING"]);
});

test("adds positioned machines of the shown park; machines without tenant count as mineria", () => {
  const mine = [{ id: "NORTE", lat: -9.4, lon: -77.4, tenant: "mineria" }];
  assert.equal(framePoints(mine, [{ machine_id: "EX-017", lat: -9.5, lon: -77.5 }]).length, 2);
});
