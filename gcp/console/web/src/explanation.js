// La explicacion del incidente llega como texto (mock y API anterior: `explanation` + `explanation_src`) o, desde que
// incident-flow guarda la salida de Gemini, como objeto {summary, source, probable_component, report, suggested_parts}.
// Siempre devuelve {summary, source, probable_component?, report?} o null: React no puede pintar un objeto (error #31).
export function explanationOf(inc) {
  const ex = inc?.explanation;
  if (!ex) return null;
  if (typeof ex === "string") return { summary: ex, source: inc.explanation_src || "template" };
  if (typeof ex !== "object") return null;
  const summary = typeof ex.summary === "string" ? ex.summary : typeof ex.report?.summary === "string" ? ex.report.summary : "";
  return { ...ex, summary, source: ex.source || inc.explanation_src || "template" };
}
