import { test } from "node:test";
import assert from "node:assert/strict";
import { explanationOf } from "../src/explanation.js";

test("string explanation (mock and older API)", () => {
  assert.deepEqual(explanationOf({ explanation: "Desgaste", explanation_src: "gemini" }), { summary: "Desgaste", source: "gemini" });
});

test("object explanation from Gemini (live since 12:15) is never rendered as an object", () => {
  const ex = explanationOf({ explanation: { summary: "Desgaste interno", source: "gemini", probable_component: "Bomba hidraulica",
    common_cause: null, suggested_parts: ["PRT-04"], report: { summary: "EX-017 presenta..." } } });
  assert.equal(ex.summary, "Desgaste interno");
  assert.equal(ex.source, "gemini");
  assert.equal(ex.report.summary, "EX-017 presenta...");
});

test("object without summary falls back to the report summary; empty is null", () => {
  assert.equal(explanationOf({ explanation: { report: { summary: "R" } } }).summary, "R");
  assert.equal(explanationOf({}), null);
});
