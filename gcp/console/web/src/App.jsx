import { useCallback, useEffect, useMemo, useState } from "react";
import MapView from "./MapView.jsx";
import PlanningView from "./Planning.jsx";
import { IncidentPanel, IncidentRow, MachinePanel, StockView, Sev } from "./Panels.jsx";
import { AdminView, Login, ReportForm, ReportPanel, ReportsList } from "./Reports.jsx";
import { api, SEVERITY, statusOf, MODE_LABEL, ROLE_LABEL, fmtTime } from "./api.js";
import { completeLogin, loadConfig, logout, session } from "./auth.js";

const REFRESH_MS = 15000;

// Pestanas por rol (T34). La primera es la de inicio.
const LABELS = { map: "Mapa", incidents: "Incidentes", myincidents: "Mis incidentes", stock: "Stock", planning: "Planificación",
  report: "Reportar", myreports: "Mis reportes", assigned: "Reportes asignados", reports: "Reportes", admin: "Administración" };
const ROLE_TABS = {
  operator: ["report", "myreports", "map"],
  technician: ["myincidents", "assigned", "map", "stock"],
  supervisor: ["map", "incidents", "reports", "stock", "planning"],
  management: ["planning", "map", "incidents"],
  admin: ["map", "incidents", "reports", "stock", "planning", "admin"],
};
const CAN_CLOSE_INCIDENT = ["technician", "supervisor", "admin"];

export default function App() {
  const [user, setUser] = useState(() => session());
  const [ready, setReady] = useState(false);
  useEffect(() => {
    loadConfig().then(completeLogin).then((s) => setUser(s || session())).catch(() => {}).finally(() => setReady(true));
  }, []);
  if (!ready) return <div className="login"><p className="muted">Cargando…</p></div>;
  if (!user?.role) return <Login onLogged={setUser} />;
  return <Console user={user} />;
}

function Console({ user }) {
  const tabs = ROLE_TABS[user.role] || ["map"];
  // La pestana vive en la URL (#planning) para poder abrirla directo al grabar
  const fromHash = location.hash.slice(1);
  const [tab, setTabState] = useState(() => (tabs.includes(fromHash) ? fromHash : fromHash.startsWith("incident:") && tabs.includes("incidents") ? "incidents" : tabs[0]));
  const setTab = (t) => { setTabState(t); history.replaceState(null, "", `#${t}`); };
  const [machines, setMachines] = useState([]);
  const [incidents, setIncidents] = useState([]);
  // Enlace directo para grabar: #machine:VQ-022, #incident:INC-..., #report:RPT-...
  const [drawer, setDrawer] = useState(() => {
    const m = location.hash.match(/^#(machine|incident|report):([A-Z0-9-]+)$/);
    return m ? { kind: m[1], id: m[2] } : null;
  });
  const [updated, setUpdated] = useState(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [m, i] = await Promise.all([api.machines(), api.incidents()]);
      setMachines(m);
      setIncidents(i);
      setUpdated(new Date());
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    api.health().then((h) => setMode(h.mode)).catch(() => {});
    refresh();
    const t = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const open = incidents.filter((i) => i.status !== "closed");
  const mine = incidents.filter((i) => i.tech_id && i.tech_id === user.tech_id);
  const counts = useMemo(() => {
    const c = { operating: 0, ok: 0, observe: 0, schedule: 0, immediate: 0 };
    machines.forEach((m) => { c[m.severity || "ok"]++; if (m.status === "operating") c.operating++; });
    return c;
  }, [machines]);

  const showMachine = (id) => setDrawer({ kind: "machine", id });
  const showIncident = (id) => setDrawer({ kind: "incident", id });
  const showReport = (id) => setDrawer({ kind: "report", id });

  const incidentTable = (rows, title) => (
    <section className="page">
      <h2>{title}</h2>
      {rows.length === 0 && <p className="muted">Sin incidentes.</p>}
      {rows.length > 0 && (
        <table className="grid clickable">
          <thead>
            <tr><th>Incidente</th><th>Máquina</th><th>Falla</th><th>Severidad</th><th>Estado</th><th>Técnico</th><th>Abierto</th><th>Detenida</th></tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr key={i.incident_id} onClick={() => showIncident(i.incident_id)} className={drawer?.id === i.incident_id ? "active" : ""}>
                <td className="mono small">{i.incident_id}{i.recurrence_flag && <span className="tag warn">reincidencia</span>}</td>
                <td><b>{i.machine_id}</b></td>
                <td>{MODE_LABEL[i.failure_mode] || i.failure_mode}</td>
                <td><Sev s={i.severity} /></td>
                <td>{statusOf(i)}</td>
                <td>{i.tech_id || "—"}</td>
                <td>{fmtTime(i.opened_at)}</td>
                <td>{i.downtime_h != null ? `${i.downtime_h} h` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" />
          <div>
            <b>FleetPulse</b>
            <span>Operación Cerro Azul · ArquiFusion</span>
          </div>
        </div>
        <nav>
          {tabs.map((k) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
              {LABELS[k]}{k === "incidents" && open.length > 0 && <em>{open.length}</em>}
              {k === "myincidents" && mine.filter((i) => i.status !== "closed").length > 0 && <em>{mine.filter((i) => i.status !== "closed").length}</em>}
            </button>
          ))}
        </nav>
        <div className="kpis">
          <div><b>{counts.operating}</b><span>operando</span></div>
          {["immediate", "schedule", "observe"].map((s) => (
            <div key={s} style={{ "--c": SEVERITY[s].color }} className="kpi-sev"><b>{counts[s]}</b><span>{SEVERITY[s].label.toLowerCase()}</span></div>
          ))}
        </div>
        <div className={`live ${error ? "down" : ""}`}>
          <i />{error ? "sin conexión" : updated ? `actualizado ${updated.toLocaleTimeString("es-PE", { timeZone: "America/Lima" })}` : "conectando"}
          {mode === "mock" && <span className="tag">datos de prueba</span>}
        </div>
        <div className="who">
          <span><b>{ROLE_LABEL[user.role]}</b><span className="muted small">{user.email}{user.tech_id ? ` · ${user.tech_id}` : ""}</span></span>
          <button className="toggle" onClick={logout}>Salir</button>
        </div>
      </header>

      {error && <div className="alert">No se pudo leer AWS: {error}. Reintentando cada 15 s.</div>}

      <main className={`body ${tab}`}>
        {tab === "map" && (
          <>
            <MapView machines={machines} selected={drawer?.kind === "machine" ? drawer.id : null} onSelect={showMachine} />
            <aside className="side">
              <div className="legend">
                {Object.entries(SEVERITY).map(([k, v]) => <span key={k} style={{ "--c": v.color }}><i />{v.label} <b>{counts[k]}</b></span>)}
              </div>
              <h3>Incidentes activos <span className="muted">({open.length})</span></h3>
              <div className="inc-list">
                {open.length === 0 && <p className="muted">Sin incidentes activos.</p>}
                {open.map((i) => (
                  <IncidentRow key={i.incident_id} inc={i} active={drawer?.id === i.incident_id} onClick={() => showIncident(i.incident_id)} />
                ))}
              </div>
            </aside>
          </>
        )}
        {tab === "incidents" && incidentTable(incidents, "Incidentes")}
        {tab === "myincidents" && incidentTable(mine, `Mis incidentes (${user.tech_id})`)}
        {tab === "stock" && <StockView />}
        {tab === "planning" && <PlanningView onMachine={showMachine} />}
        {tab === "report" && <ReportForm machines={machines} onCreated={refresh} />}
        {tab === "myreports" && <ReportsList title="Mis reportes" query="mine=1" onOpen={showReport} empty="Todavía no enviaste reportes." />}
        {tab === "assigned" && <ReportsList title="Reportes asignados" query="mine=1" onOpen={showReport} empty="No tienes reportes asignados." />}
        {tab === "reports" && <ReportsList title="Reportes del operario" query="" onOpen={showReport} empty="Sin reportes." />}
        {tab === "admin" && <AdminView />}

        {drawer?.kind === "machine" && <MachinePanel id={drawer.id} onClose={() => setDrawer(null)} onOpenIncident={showIncident} />}
        {drawer?.kind === "incident" && <IncidentPanel id={drawer.id} onClose={() => setDrawer(null)} onChanged={refresh} onMachine={showMachine}
          onReport={showReport} canClose={CAN_CLOSE_INCIDENT.includes(user.role)} />}
        {drawer?.kind === "report" && <ReportPanel id={drawer.id} user={user} onClose={() => setDrawer(null)} onIncident={showIncident} onChanged={refresh} />}
      </main>
    </div>
  );
}
