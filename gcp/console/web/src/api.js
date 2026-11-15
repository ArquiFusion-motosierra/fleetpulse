import { session } from "./auth.js";

// Todo pasa por el BFF (/api/*): la API key de AWS nunca llega al navegador. El ID token del usuario si viaja:
// el BFF lo reenvia a las rutas con Cognito (/reports, /admin).
async function req(path, opts = {}) {
  const token = session()?.token;
  const r = await fetch(`/api${path}`, {
    ...opts,
    headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
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
  oem: (model) => req(`/oem/${model}`),
  reports: (q = "") => req(`/reports${q ? `?${q}` : ""}`).then((b) => b.reports),
  report: (id) => req(`/reports/${id}`),
  createReport: (body) => req("/reports", { method: "POST", body: JSON.stringify(body) }).then((b) => b.report),
  closeReport: (id, body) => req(`/reports/${id}/close`, { method: "POST", body: JSON.stringify(body) }),
  abnormalityTypes: () => req("/abnormality-types").then((b) => b.abnormality_types),
  technicians: () => req("/admin/technicians").then((b) => b.technicians),
  putTechnician: (id, body) => req(`/admin/technicians/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  addType: (body) => req("/admin/abnormality-types", { method: "POST", body: JSON.stringify(body) }),
  deleteType: (code) => req(`/admin/abnormality-types/${code}`, { method: "DELETE" }),
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
export const PARAM_LABEL = {
  coolant_temp_c: "Refrigerante", hydraulic_oil_temp_c: "Aceite hidráulico", hydraulic_pressure_bar: "Presión hidráulica",
  vibration_mm_s: "Vibración RMS", engine_rpm: "Velocidad del motor", fuel_rate_lph: "Consumo", speed_kmh: "Velocidad",
  vibration_spectrum: "Espectro de vibración",
};
export const ROLE_LABEL = { operator: "Operario", technician: "Técnico", supervisor: "Supervisor", management: "Dirección", admin: "Administrador" };
export const REPORT_STATUS = { open: "Abierto", in_review: "En revisión", closed: "Cerrado" };
export const URGENCY = { observe: "Observar", schedule: "Programar", immediate: "Inmediato" };
export const PERCEIVED = { low: "Baja", medium: "Media", high: "Alta" };
export const FINDING_LABEL = { normal: "Normal", warning: "Advertencia", critical: "Crítico" };
MODE_LABEL.head_gasket = "Junta de culata";
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
