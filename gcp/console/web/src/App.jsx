import { useCallback, useEffect, useMemo, useState } from "react";
import MapView from "./MapView.jsx";
import { IncidentPanel, IncidentRow, MachinePanel, StockView, Sev } from "./Panels.jsx";
import { api, SEVERITY, STATUS_LABEL, MODE_LABEL, fmtTime } from "./api.js";

const REFRESH_MS = 15000;

export default function App() {
  const [tab, setTab] = useState("map");
  const [machines, setMachines] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [drawer, setDrawer] = useState(null); // {kind: "machine"|"incident", id}
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
  const counts = useMemo(() => {
    const c = { operating: 0, ok: 0, observe: 0, schedule: 0, immediate: 0 };
    machines.forEach((m) => { c[m.severity || "ok"]++; if (m.status === "operating") c.operating++; });
    return c;
  }, [machines]);

  const showMachine = (id) => setDrawer({ kind: "machine", id });
  const showIncident = (id) => setDrawer({ kind: "incident", id });

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
          {[["map", "Mapa"], ["incidents", "Incidentes"], ["stock", "Stock"]].map(([k, l]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
              {l}{k === "incidents" && open.length > 0 && <em>{open.length}</em>}
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
        {tab === "incidents" && (
          <section className="page">
            <h2>Incidentes</h2>
            <table className="grid clickable">
              <thead>
                <tr><th>Incidente</th><th>Máquina</th><th>Falla</th><th>Severidad</th><th>Estado</th><th>Técnico</th><th>Abierto</th><th>Detenida</th></tr>
              </thead>
              <tbody>
                {incidents.map((i) => (
                  <tr key={i.incident_id} onClick={() => showIncident(i.incident_id)} className={drawer?.id === i.incident_id ? "active" : ""}>
                    <td className="mono small">{i.incident_id}{i.recurrence_flag && <span className="tag warn">reincidencia</span>}</td>
                    <td><b>{i.machine_id}</b></td>
                    <td>{MODE_LABEL[i.failure_mode] || i.failure_mode}</td>
                    <td><Sev s={i.severity} /></td>
                    <td>{STATUS_LABEL[i.status]}</td>
                    <td>{i.tech_id || "—"}</td>
                    <td>{fmtTime(i.opened_at)}</td>
                    <td>{i.downtime_h != null ? `${i.downtime_h} h` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
        {tab === "stock" && <StockView />}

        {drawer?.kind === "machine" && <MachinePanel id={drawer.id} onClose={() => setDrawer(null)} onOpenIncident={showIncident} />}
        {drawer?.kind === "incident" && <IncidentPanel id={drawer.id} onClose={() => setDrawer(null)} onChanged={refresh} onMachine={showMachine} />}
      </main>
    </div>
  );
}
