import { useEffect, useState } from "react";
import MapView from "./MapView.jsx";
import { Sev } from "./Panels.jsx";
import { api, MODE_LABEL, MODEL_LABEL, REPORT_STATUS, ROLE_LABEL, SEVERITY, TENANT_NAME, ago, statusOf } from "./api.js";
import { countBySeverity, lowStock, machinesWithAlert, needsAttention, parkSummary, techWork } from "./derive.js";
import { Card, Empty, ErrorBoundary, Icon, Kpi, ParkTag, Skeleton } from "./ui.jsx";

// Inicio por rol: responde "que me toca hacer ahora" con lo que la consola ya cargo (maquinas, incidentes y
// reportes vienen de App, ya acotados al parque por el BFF). Solo pide aparte stock, KPI y el detalle de un incidente.
export default function Home(props) {
  const { user, parks, park } = props;
  const where = TENANT_NAME[park || parks.tenant_id] || (parks.global ? "todos los parques" : "tu parque");
  const inWhere = `en ${TENANT_NAME[park || parks.tenant_id] ? `el ${where.toLowerCase()}` : where}`;
  const body = {
    operator: <HomeOperator {...props} />,
    technician: <HomeTechnician {...props} />,
    supervisor: <HomeSupervisor {...props} inWhere={inWhere} />,
    admin: <HomeSupervisor {...props} inWhere={inWhere} admin />,
    management: <HomeManagement {...props} inWhere={inWhere} />,
    maintenance_head: park ? <HomeSupervisor {...props} inWhere={inWhere} onBack={() => props.onPark("")} />
      : <HomeMaintenanceHead {...props} />,
  }[user.role];
  return (
    <section className="page home">
      <div className="home-head">
        <h2>{GREETING[user.role] || "Inicio"}</h2>
        <span className="muted">{ROLE_LABEL[user.role]}{user.tech_id ? ` · ${user.tech_id}` : ""} · {where}</span>
      </div>
      {body || <Empty>Tu rol no tiene un inicio propio. Usa el menú para ver el mapa.</Empty>}
    </section>
  );
}

const GREETING = {
  operator: "Hola, ¿qué pasa con tu máquina?",
  technician: "Mi trabajo de hoy",
  supervisor: "El parque de un vistazo",
  admin: "El parque de un vistazo",
  management: "Resultados del parque",
  maintenance_head: "Los tres parques",
};

function Rows({ children }) {
  return <ul className="rows">{children}</ul>;
}

function IncLine({ inc, onIncident, showPark }) {
  return (
    <li>
      <button onClick={() => onIncident(inc.incident_id)}>
        <span className="row-main">
          {showPark && <ParkTag tenant={inc.tenant_id} />}
          <b>{inc.machine_id}</b>
          <span className="muted">{MODE_LABEL[inc.failure_mode] || inc.failure_mode}</span>
        </span>
        <span className="row-side"><Sev s={inc.severity} /><span className="muted small">{statusOf(inc)} · {ago(inc.opened_at)}</span></span>
      </button>
    </li>
  );
}

function ReportLine({ r, onReport }) {
  return (
    <li>
      <button onClick={() => onReport(r.report_id)}>
        <span className="row-main"><b>{r.machine_id}</b><span className="muted">{r.abnormality_type}</span></span>
        <span className="row-side"><span>{REPORT_STATUS[r.status] || r.status}</span><span className="muted small">{ago(r.reported_at)}</span></span>
      </button>
    </li>
  );
}

// ---------- Operario ----------
function HomeOperator({ machines, reports, loaded, onTab, onMachine, onReport }) {
  const alerts = machinesWithAlert(machines);
  return (
    <>
      <button className="cta" onClick={() => onTab("report")}>
        <Icon name="alert" size={28} />
        <span><b>Reportar una falla</b><span>Lo revisa un técnico con la telemetría de la máquina y la ficha del fabricante.</span></span>
      </button>
      <div className="grid-cards">
        <Card title="Mis últimos reportes" action={reports.length ? { label: "Ver todos", onClick: () => onTab("myreports") } : null}>
          {!loaded ? <Skeleton /> : reports.length === 0
            ? <Empty action={{ label: "Reportar una falla", onClick: () => onTab("report") }}>Todavía no enviaste reportes. Cuando reportes algo, aquí ves si un técnico ya lo está revisando.</Empty>
            : <Rows>{reports.slice(0, 3).map((r) => <ReportLine key={r.report_id} r={r} onReport={onReport} />)}</Rows>}
        </Card>
        <Card title="Máquinas con alerta en tu parque">
          {!loaded ? <Skeleton /> : alerts.length === 0
            ? <Empty>Todas las máquinas de tu parque operan dentro de su línea base. Si notas algo raro, repórtalo igual.</Empty>
            : <Rows>{alerts.map((m) => (
              <li key={m.machine_id}>
                <button onClick={() => onMachine(m.machine_id)}>
                  <span className="row-main"><b>{m.machine_id}</b><span className="muted">{MODEL_LABEL[m.model] || m.model} · {m.zone_id}</span></span>
                  <span className="row-side"><Sev s={m.severity} /></span>
                </button>
              </li>
            ))}</Rows>}
        </Card>
      </div>
    </>
  );
}

// ---------- Tecnico ----------
function HomeTechnician({ user, incidents, reports, loaded, onTab, onIncident, onReport }) {
  const w = techWork(incidents, reports, user.tech_id);
  const nextId = w.next?.incident_id;
  const [detail, setDetail] = useState(null);
  useEffect(() => {
    setDetail(null);
    if (nextId) api.incident(nextId).then(setDetail).catch(() => setDetail(false));
  }, [nextId]);
  if (w.noTech) return <Empty>Tu cuenta no tiene técnico asignado. Pide al administrador que la vincule a un técnico para ver tu trabajo.</Empty>;
  if (!loaded) return <Card><Skeleton lines={4} /></Card>;
  const parts = detail ? detail.reservations?.length ? detail.reservations
    : (detail.timeline || []).filter((t) => t.type === "part.reserved").flatMap((t) => t.payload?.reservations || []) : [];
  return (
    <>
      <div className="kpi-row">
        <Kpi value={w.incidents.length} label={w.incidents.length === 1 ? "incidente asignado" : "incidentes asignados"}
          color={w.incidents.length ? SEVERITY[w.next.severity]?.color : undefined} onClick={() => onTab("myincidents")} />
        <Kpi value={w.reports.length} label={w.reports.length === 1 ? "reporte por revisar" : "reportes por revisar"} onClick={() => onTab("assigned")} />
      </div>
      <div className="grid-cards">
        <Card title="Siguiente">
          {!w.next ? <Empty>No tienes incidentes abiertos. Cuando AWS te asigne uno por cercanía y especialidad, aparece aquí.</Empty> : (
            <div className="next">
              <div className="row-main"><b>{w.next.machine_id}</b><span>{MODE_LABEL[w.next.failure_mode] || w.next.failure_mode}</span></div>
              <div className="row-side"><Sev s={w.next.severity} /><span className="muted small">abierto {ago(w.next.opened_at)}</span></div>
              <button className="primary" onClick={() => onIncident(w.next.incident_id)}>Abrir incidente</button>
            </div>
          )}
        </Card>
        {w.next && (
          <Card title="Repuestos del siguiente incidente">
            {detail === null ? <Skeleton lines={2} /> : parts.length === 0
              ? <Empty>Sin repuestos reservados para este incidente. Si el informe sugiere alguno, AWS lo reserva en la zona más cercana.</Empty>
              : <table className="mini"><tbody>{parts.map((r, i) => <tr key={r.reservation_id || i}><td><b>{r.part_id}</b></td><td>×{r.qty}</td><td>{r.zone_id}</td><td className="muted">{!r.status || r.status === "reserved" ? "reservado" : r.status}</td></tr>)}</tbody></table>}
          </Card>
        )}
        <Card title="Mis incidentes abiertos">
          {w.incidents.length === 0 ? <Empty>Nada asignado por ahora.</Empty>
            : <Rows>{w.incidents.map((i) => <IncLine key={i.incident_id} inc={i} onIncident={onIncident} />)}</Rows>}
        </Card>
        <Card title="Reportes asignados">
          {w.reports.length === 0 ? <Empty>No tienes reportes de operarios por revisar. Cuando un operario reporte una falla en tu zona, llega aquí con los chequeos recomendados.</Empty>
            : <Rows>{w.reports.map((r) => <ReportLine key={r.report_id} r={r} onReport={onReport} />)}</Rows>}
        </Card>
      </div>
    </>
  );
}

// ---------- Supervisor y admin (y el jefe con un parque elegido) ----------
function HomeSupervisor({ machines, incidents, reports, loaded, tabs, zones, park, admin, onBack, onTab, onMachine, onIncident, onReport, inWhere }) {
  const c = countBySeverity(machines);
  const att = needsAttention(incidents, reports);
  const [low, setLow] = useState([]);
  useEffect(() => { api.stock().then((s) => setLow(lowStock(s))).catch(() => setLow([])); }, [park]);
  const stockTab = tabs.includes("supply") ? "supply" : tabs.includes("stock") ? "stock" : null;
  const groups = [
    { key: "imm", title: "Inmediato sin técnico", rows: att.immediateNoTech, kind: "inc",
      note: "AWS asigna solo los inmediatos; si aparece uno aquí, no había técnico disponible." },
    { key: "apr", title: "Esperando aprobación", rows: att.awaitingApproval, kind: "inc",
      note: "Los de severidad Programar esperan que el supervisor apruebe desde el enlace del correo." },
    { key: "rep", title: "Reportes sin revisar", rows: att.unreviewedReports, kind: "rep",
      note: "Reportes de operarios que ningún técnico ha tomado todavía." },
  ];
  const pending = groups.some((g) => g.rows.length);
  return (
    <>
      {onBack && <button className="link-btn back" onClick={onBack}><Icon name="back" size={14} /> Comparar los tres parques</button>}
      <div className="kpi-row">
        <Kpi value={loaded ? c.operating : "–"} label="operando" onClick={() => onTab("map")} />
        {["immediate", "schedule", "observe"].map((s) => (
          <Kpi key={s} value={loaded ? c[s] : "–"} label={SEVERITY[s].label.toLowerCase()} color={SEVERITY[s].color} onClick={() => onTab("map")} />
        ))}
      </div>
      <div className="grid-cards wide">
        <Card title="Requiere atención">
          {!loaded ? <Skeleton lines={4} /> : !pending
            ? <Empty>Nada pendiente {inWhere}: los incidentes abiertos ya tienen técnico y no hay reportes sin revisar.</Empty>
            : groups.map((g) => (
              <div key={g.key} className="att">
                <div className="att-head"><b>{g.title}</b><span className={`count ${g.rows.length ? "on" : ""}`}>{g.rows.length}</span></div>
                {g.rows.length > 0 && <p className="muted small">{g.note}</p>}
                {g.rows.length > 0 && <Rows>{g.rows.slice(0, 3).map((x) => g.kind === "inc"
                  ? <IncLine key={x.incident_id} inc={x} onIncident={onIncident} />
                  : <ReportLine key={x.report_id} r={x} onReport={onReport} />)}</Rows>}
              </div>
            ))}
        </Card>
        <Card title="Mapa" action={{ label: "Abrir mapa", onClick: () => onTab("map") }} className="mini-map">
          <ErrorBoundary fallback={<Empty>El mapa no se pudo dibujar en este navegador (sin WebGL).</Empty>}>
            <div className="mini-map-box"><MapView machines={machines} selected={null} onSelect={onMachine} zones={zones} focusKey={park || "all"} /></div>
          </ErrorBoundary>
        </Card>
        {low.length > 0 && (
          <Card title="Stock bajo" action={stockTab ? { label: stockTab === "supply" ? "Ir a Abastecimiento" : "Ver stock", onClick: () => onTab(stockTab) } : null}>
            <Rows>{low.slice(0, 5).map((s) => (
              <li key={`${s.part_id}-${s.zone_id}`}><div className="row-static">
                <span className="row-main"><b>{s.part_id}</b><span className="muted">{s.name}</span></span>
                <span className="row-side"><span>{s.available} disp. en {s.zone_id}</span><span className="muted small">reorden en {s.reorder_point}</span></span>
              </div></li>
            ))}</Rows>
            {low.length > 5 && <p className="muted small">y {low.length - 5} más</p>}
          </Card>
        )}
        {admin && (
          <Card title="Administración" action={{ label: "Abrir", onClick: () => onTab("admin") }}>
            <p className="muted">Técnicos (zona y disponibilidad) y tipos de anormalidad que ve el operario al reportar.</p>
          </Card>
        )}
      </div>
    </>
  );
}

// ---------- Direccion ----------
const fmtDay = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString("es-PE", { day: "numeric", month: "short", timeZone: "America/Lima" });

function HomeManagement({ incidents, loaded, park, onTab, onIncident, inWhere }) {
  const [k, setK] = useState(null);
  useEffect(() => { api.kpis().then(setK).catch(() => setK(false)); }, [park]);
  const open = incidents.filter((i) => i.status !== "closed");
  const bySev = countBySeverity(open);
  const rows = k?.machines || [];
  const avg = (f) => rows.length ? rows.reduce((a, r) => a + (r[f] || 0), 0) / rows.length : 0;
  return (
    <>
      <Card title={k?.day ? `KPI del ${fmtDay(k.day)}` : "KPI del último día"} action={{ label: "Ver planificación", onClick: () => onTab("planning") }}>
        {k === null ? <Skeleton lines={2} /> : !rows.length
          ? <Empty>Todavía no hay KPI diarios {inWhere.replace(/^en /, "para ")}. La planificación los calcula a las 05:00 con el día anterior.</Empty>
          : (
            <div className="kpi-row flush">
              <Kpi value={rows.length} label="máquinas con datos" />
              <Kpi value={`${Math.round(rows.reduce((a, r) => a + (r.operating_h || 0), 0)).toLocaleString("es-PE")} h`} label="horas operadas" />
              <Kpi value={`${Math.round(avg("utilization") * 100)} %`} label="utilización media" />
              <Kpi value={`${Math.round(avg("idle_ratio") * 100)} %`} label="ralentí medio" />
              {k.downtime_h != null && <Kpi value={`${k.downtime_h} h`} label="horas detenidas" />}
            </div>
          )}
      </Card>
      <Card title="Incidentes abiertos ahora" action={{ label: "Ver incidentes", onClick: () => onTab("incidents") }}>
        {!loaded ? <Skeleton lines={2} /> : open.length === 0
          ? <Empty>Sin incidentes abiertos {inWhere}. Cuando la telemetría de una máquina sale de su línea base, aparece aquí.</Empty>
          : (
            <>
              <div className="kpi-row flush">
                {["immediate", "schedule", "observe"].map((s) => <Kpi key={s} value={bySev[s]} label={SEVERITY[s].label.toLowerCase()} color={SEVERITY[s].color} />)}
              </div>
              <Rows>{open.slice(0, 5).map((i) => <IncLine key={i.incident_id} inc={i} onIncident={onIncident} />)}</Rows>
            </>
          )}
      </Card>
    </>
  );
}

// ---------- Jefe de mantenimiento: los tres parques ----------
const SUMMARY_ROWS = [["machines", "Máquinas"], ["operating", "Operando"], ["immediate", "Inmediato"], ["schedule", "Programar"],
  ["open", "Incidentes abiertos"]];

function HomeMaintenanceHead({ machines, incidents, parks, loaded, onPark, onIncident }) {
  const tenants = parks.scope.length ? parks.scope : parks.tenants.map((t) => t.tenant_id);
  const s = parkSummary(machines, incidents, tenants);
  const open = incidents.filter((i) => i.status !== "closed");
  return (
    <>
      <Card title="Comparativo por parque">
        {!loaded ? <Skeleton lines={5} /> : (
          <div className="scroll-x">
            <table className="grid compact compare">
              <thead><tr><th />{tenants.map((t) => <th key={t}><ParkTag tenant={t} /></th>)}</tr></thead>
              <tbody>
                {SUMMARY_ROWS.map(([key, label]) => (
                  <tr key={key}>
                    <td className="muted">{label}</td>
                    {tenants.map((t) => <td key={t} className={`num ${key === "immediate" && s[t][key] ? "bad-text" : ""}`}>{s[t][key]}</td>)}
                  </tr>
                ))}
                <tr><td />{tenants.map((t) => <td key={t}><button className="toggle" onClick={() => onPark(t)}>Ver parque</button></td>)}</tr>
              </tbody>
            </table>
          </div>
        )}
        <p className="muted small">Cada parque solo ve lo suyo; el jefe de mantenimiento ve los tres. Al elegir uno, toda la consola se filtra a ese parque.</p>
      </Card>
      <Card title={`Incidentes abiertos en los tres parques (${open.length})`}>
        {!loaded ? <Skeleton /> : open.length === 0
          ? <Empty>Sin incidentes abiertos en ningún parque. Cuando la telemetría de una máquina sale de su línea base, aparece aquí.</Empty>
          : <Rows>{open.map((i) => <IncLine key={i.incident_id} inc={i} onIncident={onIncident} showPark />)}</Rows>}
      </Card>
    </>
  );
}

