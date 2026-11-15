// Todo pasa por el BFF (/api/*): la API key de AWS nunca llega al navegador.
async function req(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

export const api = {
  health: () => req("/health"),
  machines: () => req("/machines").then((b) => b.machines),
  machine: (id) => req(`/machines/${id}`),
  incidents: (status) => req(`/incidents?limit=100${status ? `&status=${status}` : ""}`).then((b) => b.incidents),
  incident: (id) => req(`/incidents/${id}`),
  stock: () => req("/stock").then((b) => b.stock),
  kpis: () => req("/kpis"),
  orders: () => req("/orders").then((b) => b.orders),
  reorders: () => req("/reorders").then((b) => b.reorders),
  close: (id, body) => req(`/incidents/${id}/close`, { method: "POST", body: JSON.stringify(body) }),
};

export const SEVERITY = {
  ok: { label: "Normal", color: "#22c55e" },
  observe: { label: "Observar", color: "#eab308" },
  schedule: { label: "Programar", color: "#f97316" },
  immediate: { label: "Inmediato", color: "#ef4444" },
};

export const STATUS_LABEL = { open: "Abierto", assigned: "Asignado", closed: "Cerrado" };
// incident-flow solo usa open/closed: un incidente abierto con tecnico se muestra como asignado
export const statusOf = (inc) => (inc.status === "open" && inc.tech_id ? "Asignado" : STATUS_LABEL[inc.status] || inc.status);
export const ENGINE_LABEL = { operating: "Operando", idle: "Ralentí", off: "Apagada", in_maintenance: "En mantenimiento" };
export const MODEL_LABEL = { EX: "Excavadora", CF: "Cargador frontal", VQ: "Volquete", MN: "Motoniveladora", TO: "Tractor de orugas" };
export const MODE_LABEL = {
  hydraulic_pump: "Bomba hidráulica",
  hydraulic_contamination: "Contaminación hidráulica",
  hydraulic_overheat: "Sobrecalentamiento hidráulico",
  fuel_contamination: "Combustible contaminado",
  engine_overheat: "Sobrecalentamiento de motor",
  final_drive: "Mando final",
  brake_wear: "Desgaste de frenos",
  electrical_fault: "Falla eléctrica",
  sensor_fault: "Falla de sensor",
};
export const EVENT_LABEL = {
  "incident.opened": "Incidente abierto",
  "incident.escalated": "Severidad escalada",
  "incident.assigned": "Técnico asignado",
  "incident.closed": "Incidente cerrado",
  "part.reserve.requested": "Reserva de repuestos solicitada",
  "part.reserved": "Repuestos reservados",
  "part.shortage": "Falta de stock: pedido sugerido",
};

export function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-PE", {
    timeZone: "America/Lima", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

export function ago(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "ahora";
  if (m < 60) return `hace ${m} min`;
  if (m < 1440) return `hace ${Math.round(m / 60)} h`;
  return `hace ${Math.round(m / 1440)} d`;
}
