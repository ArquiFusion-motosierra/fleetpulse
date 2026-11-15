import { test } from "node:test";
import assert from "node:assert/strict";
import { groupOptions } from "../src/supply-options.js";

test("groups identical transfer options from the same park into one row", () => {
  const t = (z) => ({ source_kind: "transfer", from_zone: z, eta_h: 16, cost_usd: 180, total_cost_usd: 7380 });
  const buy = { source_kind: "purchase", supplier_id: "SUP-HIDRA", eta_h: 168, cost_usd: 6800, total_cost_usd: 82400 };
  const rows = groupOptions([t("FUNDO-A"), t("FUNDO-B"), t("PACKING"), buy]);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].zones, ["FUNDO-A", "FUNDO-B", "PACKING"]);
  assert.equal(rows[1].supplier_id, "SUP-HIDRA");
});

test("keeps transfers with different cost apart", () => {
  const rows = groupOptions([
    { source_kind: "transfer", from_zone: "FUNDO-A", eta_h: 16, cost_usd: 180, total_cost_usd: 7380 },
    { source_kind: "transfer", from_zone: "PLANTA-1", eta_h: 20, cost_usd: 220, total_cost_usd: 9220 },
  ]);
  assert.equal(rows.length, 2);
});
