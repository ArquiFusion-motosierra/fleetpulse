// Logica de Inicio por rol en funciones puras: solo ordena y cuenta lo que el BFF ya devolvio (nunca filtra por
// parque: el aislamiento lo hace el BFF). Sin React ni api.js para poder probarla con `node --test`.
export const SEV_RANK = { immediate: 3, schedule: 2, observe: 1, ok: 0 };

// Filas anteriores al backfill de multi-tenancy no traen tenant_id: el BFF las trata como del parque por defecto
export const tenantOf = (row) => row.tenant_id || "mineria";

const isOpen = (i) => i.status !== "closed";
const rank = (x) => SEV_RANK[x.severity || "ok"] ?? 0;

export function countBySeverity(machines) {
  const c = { operating: 0, ok: 0, observe: 0, schedule: 0, immediate: 0 };
  machines.forEach((m) => { c[m.severity || "ok"]++; if (m.status === "operating") c.operating++; });
  return c;
}

export function parkSummary(machines, incidents, tenants) {
  const out = {};
  const row = (t) => (out[t] ||= { machines: 0, operating: 0, immediate: 0, schedule: 0, observe: 0, open: 0 });
  tenants.forEach(row);
  machines.forEach((m) => {
    const r = row(tenantOf(m));
    r.machines++;
    if (m.status === "operating") r.operating++;
    if (m.severity && m.severity !== "ok") r[m.severity]++;
  });
  incidents.filter(isOpen).forEach((i) => { row(tenantOf(i)).open++; });
  return out;
}

export function needsAttention(incidents, reports) {
  const open = incidents.filter((i) => isOpen(i) && !i.tech_id);
  return {
    immediateNoTech: open.filter((i) => i.severity === "immediate"),
    // incident-flow deja los "programar" sin tecnico hasta que el supervisor aprueba desde el correo
    awaitingApproval: open.filter((i) => i.severity === "schedule"),
    unreviewedReports: reports.filter((r) => r.status === "open"),
  };
}

export function techWork(incidents, reports, techId) {
  if (!techId) return { incidents: [], reports: [], next: null, noTech: true };
  const mine = incidents.filter((i) => isOpen(i) && i.tech_id === techId)
    .sort((a, b) => rank(b) - rank(a) || String(a.opened_at).localeCompare(String(b.opened_at)));
  return { incidents: mine, reports: reports.filter((r) => r.status !== "closed"), next: mine[0] || null, noTech: false };
}

export const lowStock = (stock) => stock.filter((s) => s.below_reorder === true);

export const machinesWithAlert = (machines, n = 5) =>
  machines.filter((m) => (m.severity || "ok") !== "ok").sort((a, b) => rank(b) - rank(a)).slice(0, n);
