import { useCallback, useEffect, useMemo, useState } from "react";
import Home from "./Home.jsx";
import MapView from "./MapView.jsx";
import PlanningView from "./Planning.jsx";
import SupplyView from "./Supply.jsx";
import { IncidentPanel, IncidentRow, MachinePanel, StockView, Sev } from "./Panels.jsx";
import { AdminView, Login, ReportForm, ReportPanel, ReportsList } from "./Reports.jsx";
import { BottomNav, Sidebar, Topbar } from "./Shell.jsx";
import { api, SEVERITY, statusOf, MODE_LABEL, TENANT_COLOR, TENANT_NAME, TENANT_OPERATION, fmtTime, setTenant } from "./api.js";
import { completeLogin, loadConfig, logout, session } from "./auth.js";
import { countBySeverity, needsAttention } from "./derive.js";
import { Empty, ParkTag } from "./ui.jsx";

const REFRESH_MS = 15000;

// Pestanas por rol (T34). Inicio es la primera para todos.
const LABELS = { home: "Inicio", map: "Mapa", incidents: "Incidentes", myincidents: "Mis incidentes", stock: "Stock", supply: "Abastecimiento", planning: "Planificación",
  report: "Reportar", myreports: "Mis reportes", assigned: "Reportes asignados", reports: "Reportes", admin: "Administración" };
const ICONS = { home: "home", map: "map", incidents: "alert", myincidents: "alert", stock: "stock", supply: "supply", planning: "plan",
  report: "report", myreports: "report", assigned: "report", reports: "report", admin: "admin" };
// Rotulos cortos para la barra inferior del celular
const SHORT = { myincidents: "Incidentes", assigned: "Reportes", myreports: "Mis reportes", planning: "Plan", supply: "Abastec." };
const ROLE_TABS = {
  operator: ["home", "report", "myreports", "map"],
  technician: ["home", "myincidents", "assigned", "map", "stock"],
  // T41: Abastecimiento reemplaza a Stock para supervisor, admin y jefe; el tecnico sigue con Stock de su parque
  supervisor: ["home", "map", "incidents", "reports", "supply", "planning"],
  management: ["home", "planning", "map", "incidents"],
  admin: ["home", "map", "incidents", "reports", "supply", "planning", "admin"],
  maintenance_head: ["home", "map", "incidents", "reports", "supply", "planning"],
};
// Reportes que carga la consola: los propios (operario y tecnico) o todos los del alcance; direccion no los ve
const REPORTS_QUERY = { operator: "mine=1", technician: "mine=1", supervisor: "", admin: "", maintenance_head: "" };
const CAN_CLOSE_INCIDENT = ["technician", "supervisor", "admin", "maintenance_head"];

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
  const tabs = ROLE_TABS[user.role] || ["home", "map"];
  // La pestana vive en la URL (#planning) para poder abrirla directo al grabar
  const fromHash = location.hash.slice(1);
  const [tab, setTabState] = useState(() => (tabs.includes(fromHash) ? fromHash : fromHash.startsWith("incident:") && tabs.includes("incidents") ? "incidents"
    : /^(po|trf):/.test(fromHash) && tabs.includes("supply") ? "supply" : tabs[0]));
  const setTab = (t) => { setTabState(t); history.replaceState(null, "", `#${t}`); };
  const [machines, setMachines] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [reports, setReports] = useState([]);
  const [loaded, setLoaded] = useState(false);
  // Enlace directo para grabar: #machine:VQ-022, #incident:INC-..., #report:RPT-...
  const [drawer, setDrawer] = useState(() => {
    const m = location.hash.match(/^#(machine|incident|report):([A-Z0-9-]+)$/);
    return m ? { kind: m[1], id: m[2] } : null;
  });
  const [updated, setUpdated] = useState(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState("");
  // Multi-tenancy: parques del alcance del usuario (el BFF lo saca del token) y el elegido por el jefe de mantenimiento
  const [parks, setParks] = useState({ tenants: [], zones: [], scope: [], global: false, tenant_id: null });
  const [park, setPark] = useState("");
  const choosePark = (t) => { setTenant(t); setPark(t); setDrawer(null); };

  const refresh = useCallback(async () => {
    try {
      const rq = REPORTS_QUERY[user.role];
      const [m, i, r] = await Promise.all([api.machines(), api.incidents(), rq == null ? [] : api.reports(rq).catch(() => [])]);
      setMachines(m);
      setIncidents(i);
      setReports(r);
      setUpdated(new Date());
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoaded(true);
    }
  }, [user.role]);

  useEffect(() => {
    api.health().then((h) => setMode(h.mode)).catch(() => {});
    api.tenants().then(setParks).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(t);
  }, [refresh, park]);

  const current = park || parks.tenant_id;
  const title = current ? TENANT_NAME[current] || current : parks.global ? "Todos los parques" : "FleetPulse";
  const zonesShown = parks.zones.filter((z) => !park || z.tenant_id === park);

  const open = incidents.filter((i) => i.status !== "closed");
  const mine = incidents.filter((i) => i.tech_id && i.tech_id === user.tech_id);
  const counts = useMemo(() => countBySeverity(machines), [machines]);
  const badges = {
    incidents: open.length,
    myincidents: mine.filter((i) => i.status !== "closed").length,
    reports: needsAttention([], reports).unreviewedReports.length,
  };
  const navTabs = tabs.map((k) => ({ key: k, label: LABELS[k], short: SHORT[k], icon: ICONS[k] }));

  const showMachine = (id) => setDrawer({ kind: "machine", id });
  const showIncident = (id) => setDrawer({ kind: "incident", id });
  const showReport = (id) => setDrawer({ kind: "report", id });

  const incidentTable = (rows, title) => (
    <section className="page">
      <h2>{title}</h2>
      {rows.length === 0 && <Empty>Sin incidentes {parks.global && !park ? "en ningún parque" : `en el ${(TENANT_NAME[current] || "parque").toLowerCase()}`}. Cuando la telemetría de una máquina sale de su línea base, aparece aquí.</Empty>}
      {rows.length > 0 && (
        <div className="scroll-x">
          <table className="grid clickable">
            <thead>
              <tr><th>Incidente</th>{parks.global && <th>Parque</th>}<th>Máquina</th><th>Falla</th><th>Severidad</th><th>Estado</th><th>Técnico</th><th>Abierto</th><th>Detenida</th></tr>
            </thead>
            <tbody>
              {rows.map((i) => (
                <tr key={i.incident_id} onClick={() => showIncident(i.incident_id)} className={drawer?.id === i.incident_id ? "active" : ""}>
                  <td className="mono small">{i.incident_id}{i.recurrence_flag && <span className="tag warn">reincidencia</span>}</td>
                  {parks.global && <td><ParkTag tenant={i.tenant_id} /></td>}
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
        </div>
      )}
    </section>
  );

  return (
    <div className="app" style={{ "--park": TENANT_COLOR[current] || TENANT_COLOR.mineria }}>
      <Sidebar tabs={navTabs} tab={tab} onTab={setTab} badges={badges} parks={parks} park={park} onPark={choosePark} user={user} onLogout={logout} />
      <div className="main-col">
        <Topbar title={title} detail={TENANT_OPERATION[current]} subtitle={tab === "home" ? null : LABELS[tab]} live={{ error, updated, mock: mode === "mock" }}
          parks={parks} park={park} onPark={choosePark} />
        {error && <div className="alert">No se pudo leer AWS: {error}. Reintentando cada 15 s.</div>}

        <main className={`body ${tab}`}>
          {tab === "home" && <Home user={user} machines={machines} incidents={incidents} reports={reports} loaded={loaded} tabs={tabs}
            parks={parks} park={park} zones={zonesShown} onPark={choosePark} onTab={setTab} onMachine={showMachine}
            onIncident={showIncident} onReport={showReport} />}
          {tab === "map" && (
            <>
              <MapView machines={machines} selected={drawer?.kind === "machine" ? drawer.id : null} onSelect={showMachine}
                zones={zonesShown} focusKey={park || "all"} />
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
          {tab === "myincidents" && incidentTable(mine, user.tech_id ? `Mis incidentes (${user.tech_id})` : "Mis incidentes")}
          {tab === "stock" && <StockView key={park} />}
          {tab === "supply" && <SupplyView key={park} user={{ ...user, tenant_id: parks.tenant_id }} onIncident={showIncident} />}
          {tab === "planning" && <PlanningView key={park} onMachine={showMachine} />}
          {tab === "report" && <ReportForm machines={machines} onCreated={refresh} />}
          {tab === "myreports" && <ReportsList title="Mis reportes" query="mine=1" onOpen={showReport} empty="Todavía no enviaste reportes." />}
          {tab === "assigned" && <ReportsList title="Reportes asignados" query="mine=1" onOpen={showReport} empty="No tienes reportes asignados." />}
          {tab === "reports" && <ReportsList key={park} title="Reportes del operario" query="" onOpen={showReport} empty="Sin reportes." />}
          {tab === "admin" && <AdminView />}

          {drawer?.kind === "machine" && <MachinePanel id={drawer.id} onClose={() => setDrawer(null)} onOpenIncident={showIncident} />}
          {drawer?.kind === "incident" && <IncidentPanel id={drawer.id} onClose={() => setDrawer(null)} onChanged={refresh} onMachine={showMachine}
            onReport={showReport} canClose={CAN_CLOSE_INCIDENT.includes(user.role)} user={user} />}
          {drawer?.kind === "report" && <ReportPanel id={drawer.id} user={user} onClose={() => setDrawer(null)} onIncident={showIncident} onChanged={refresh} />}
          {drawer && <button className="drawer-back" onClick={() => setDrawer(null)}>← Volver</button>}
        </main>
      </div>
      <BottomNav tabs={navTabs} tab={tab} onTab={setTab} badges={badges} user={user} onLogout={logout} />
    </div>
  );
}
