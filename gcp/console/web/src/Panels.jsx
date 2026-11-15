import { useEffect, useState } from "react";
import { LinkedReports } from "./Reports.jsx";
import { api, SEVERITY, statusOf, ENGINE_LABEL, MODEL_LABEL, MODE_LABEL, EVENT_LABEL, PARAM_LABEL, FINDING_LABEL, fmtTime, ago } from "./api.js";

export function Sev({ s }) {
  const v = SEVERITY[s] || SEVERITY.ok;
  return <span className="sev" style={{ "--c": v.color }}><i />{v.label}</span>;
}

export function IncidentRow({ inc, active, onClick }) {
  return (
    <button className={`inc-row ${active ? "active" : ""}`} onClick={onClick}>
      <div className="inc-top">
        <strong>{inc.machine_id}</strong>
        <Sev s={inc.severity} />
      </div>
      <div className="inc-mode">{MODE_LABEL[inc.failure_mode] || inc.failure_mode}</div>
      <div className="inc-meta">
        <span>{statusOf(inc)}</span>
        {inc.tech_id && <span>{inc.tech_id}</span>}
        {inc.group_id && inc.group_id !== "-" && <span className="tag">grupo</span>}
        {inc.recurrence_flag && <span className="tag warn">reincidencia</span>}
        <span className="muted">{ago(inc.opened_at)}</span>
      </div>
    </button>
  );
}

// Informe tecnico de anormalidad (explanation.report de incident.assigned; contracts/incident.schema.json)
export function reportOf(inc) {
  const assigned = [...(inc.timeline || [])].reverse().find((t) => t.type === "incident.assigned");
  return assigned?.payload?.explanation?.report || null;
}

const fmtNum = (v) => (typeof v === "number" ? v.toLocaleString("es-PE", { maximumFractionDigits: 1 }) : v);

export function ReportView({ report }) {
  return (
    <div className="report">
      <h3>Informe técnico</h3>
      <p className="report-summary">{report.summary}</p>
      <table className="mini findings">
        <thead><tr><th>Parámetro</th><th>Valor</th><th>Fabricante</th></tr></thead>
        <tbody>
          {report.findings.map((f, i) => (
            <tr key={i}>
              <td>{PARAM_LABEL[f.parameter] || f.parameter}
                <div className={`finding ${f.status}`}>{FINDING_LABEL[f.status]}</div></td>
              <td className="num">{fmtNum(f.value)} {f.unit}</td>
              <td className="small">{f.oem_limit}{f.note && <div className="muted">{f.note}</div>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.resonance && (
        <p className="banner warn">
          Resonancia: el {report.resonance.harmonic} ({fmtNum(report.resonance.frequency_hz)} Hz) coincide con {report.resonance.component.toLowerCase()} ({fmtNum(report.resonance.natural_frequency_hz)} Hz).
        </p>
      )}
      <dl className="report-dl">
        <dt>Causa probable</dt><dd>{report.probable_cause}</dd>
        <dt>Acción</dt><dd>{report.recommended_action}</dd>
        <dt>Repuestos</dt><dd>{report.parts.length ? report.parts.map((p) => `${p.part_id} ×${p.qty}`).join(", ") : "—"}</dd>
      </dl>
      <p className="muted small">{report.oem_reference}. Valores del fabricante ilustrativos.</p>
    </div>
  );
}

// Plan del fabricante: horas motor contra cada intervalo de servicio de la ficha (oem_specs.yaml)
export function OemPlan({ model, hours }) {
  const [spec, setSpec] = useState(null);
  useEffect(() => { setSpec(null); if (model) api.oem(model).then(setSpec).catch(() => setSpec(false)); }, [model]);
  if (spec === false) return null;
  if (!spec) return <p className="muted">Cargando plan del fabricante…</p>;
  const h = Number(hours) || 0;
  const rows = spec.service_schedule.map((s) => {
    const next = Math.ceil((h + 0.01) / s.every_h) * s.every_h;
    return { ...s, next, left: next - h };
  }).sort((a, b) => a.left - b.left);
  return (
    <>
      <h3>Plan del fabricante</h3>
      <table className="mini plan">
        <thead><tr><th>Servicio</th><th>Cada</th><th>Próximo</th><th>Faltan</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.every_h} className={r.left <= 50 ? "due" : ""}>
              <td>{r.task}<div className="muted small">{r.parts.map((p) => `${p.part_id} ×${p.qty}`).join(", ") || "sin repuestos"}</div></td>
              <td className="num">{r.every_h.toLocaleString("es-PE")} h</td>
              <td className="num">{r.next.toLocaleString("es-PE")} h</td>
              <td className="num"><b>{Math.round(r.left).toLocaleString("es-PE")} h</b></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">Horas motor: {Math.round(h).toLocaleString("es-PE")}. Ficha {spec.name} (ilustrativa).</p>
    </>
  );
}

export function MachinePanel({ id, onClose, onOpenIncident }) {
  const [m, setM] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    setM(null);
    setErr("");
    api.machine(id).then(setM).catch((e) => setErr(e.message));
  }, [id]);
  return (
    <aside className="drawer">
      <header>
        <div>
          <div className="eyebrow">Ficha de máquina</div>
          <h2>{id}</h2>
        </div>
        <button className="x" onClick={onClose} aria-label="Cerrar">×</button>
      </header>
      {err && <p className="error">{err}</p>}
      {!m && !err && <p className="muted">Cargando…</p>}
      {m && (
        <>
          <div className="facts">
            <div><span>Modelo</span><b>{MODEL_LABEL[m.model] || m.model}</b></div>
            <div><span>Zona</span><b>{m.zone_id}</b></div>
            <div><span>Estado</span><b>{ENGINE_LABEL[m.state?.status || m.status] || "—"}</b></div>
            <div><span>Severidad</span><Sev s={m.severity} /></div>
            <div><span>Horas</span><b>{Math.round(m.hours_total || 0).toLocaleString("es-PE")}</b></div>
            <div><span>Reincidencias</span><b className={m.recurrences ? "warn-text" : ""}>{m.recurrences}</b></div>
          </div>
          <OemPlan model={m.model} hours={m.state?.hours_total ?? m.hours_total} />
          <h3>Historial</h3>
          {m.incidents.length === 0 && <p className="muted">Sin incidentes registrados.</p>}
          <ol className="history">
            {m.incidents.map((i) => (
              <li key={i.incident_id} onClick={() => onOpenIncident(i.incident_id)}>
                <div className="inc-top"><b>{MODE_LABEL[i.failure_mode] || i.failure_mode}</b><Sev s={i.severity} /></div>
                <div className="inc-meta">
                  <span>{fmtTime(i.opened_at)}</span>
                  <span>{statusOf(i)}</span>
                  {i.downtime_h != null && <span>{i.downtime_h} h detenida</span>}
                  {i.recurrence_flag && <span className="tag warn">reincidencia</span>}
                </div>
                {i.parts_used?.length > 0 && (
                  <div className="muted small">Repuestos: {i.parts_used.map((p) => `${p.part_id} ×${p.qty}`).join(", ")}</div>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </aside>
  );
}

export function IncidentPanel({ id, onClose, onChanged, onMachine, onReport, canClose = true }) {
  const [inc, setInc] = useState(null);
  const [err, setErr] = useState("");
  const [closing, setClosing] = useState(false);
  const [form, setForm] = useState({ closed_by: "", cause_summary: "" });

  const load = () => api.incident(id).then((d) => { setInc(d); setForm((f) => ({ ...f, closed_by: f.closed_by || d.tech_id || "" })); }).catch((e) => setErr(e.message));
  useEffect(() => { setInc(null); setErr(""); load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function close() {
    setClosing(true);
    setErr("");
    try {
      const parts = (inc.reservations || []).filter((r) => r.status === "reserved")
        .map((r) => ({ part_id: r.part_id, zone_id: r.zone_id, qty: r.qty }));
      await api.close(id, { ...form, parts_used: parts });
      await load();
      onChanged();
    } catch (e) {
      setErr(`No se pudo cerrar: ${e.message}`);
    } finally {
      setClosing(false);
    }
  }

  return (
    <aside className="drawer">
      <header>
        <div>
          <div className="eyebrow">Incidente</div>
          <h2 className="mono">{id}</h2>
        </div>
        <button className="x" onClick={onClose} aria-label="Cerrar">×</button>
      </header>
      {err && <p className="error">{err}</p>}
      {!inc && !err && <p className="muted">Cargando…</p>}
      {inc && (
        <>
          <div className="facts">
            <div><span>Máquina</span><b className="link" onClick={() => onMachine(inc.machine_id)}>{inc.machine_id}</b></div>
            <div><span>Severidad</span><Sev s={inc.severity} /></div>
            <div><span>Estado</span><b>{statusOf(inc)}</b></div>
            <div><span>Falla</span><b>{MODE_LABEL[inc.failure_mode] || inc.failure_mode}</b></div>
            <div><span>Técnico</span><b>{inc.tech_id || "—"}</b></div>
            <div><span>Score</span><b>{inc.score?.toFixed?.(2) ?? "—"}</b></div>
          </div>
          {inc.recurrence_flag && <p className="banner warn">Reincidencia: misma falla en esta máquina en los últimos 7 días.</p>}
          {inc.group_id && inc.group_id !== "-" && <p className="banner">Agrupado por zona: <span className="mono">{inc.group_id}</span></p>}
          {inc.explanation && (
            <div className="explain">
              <div className="eyebrow">Causa probable · {inc.explanation_src === "gemini" ? "Gemini" : "plantilla"}</div>
              <p>{inc.explanation}</p>
            </div>
          )}
          {inc.reservations?.length > 0 && (
            <>
              <h3>Repuestos</h3>
              <table className="mini">
                <tbody>
                  {inc.reservations.map((r) => (
                    <tr key={r.reservation_id}><td>{r.part_id}</td><td>×{r.qty}</td><td>{r.zone_id}</td><td className="muted">{r.status}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {reportOf(inc) && <ReportView report={reportOf(inc)} />}
          {(() => {
            const a = [...(inc.timeline || [])].reverse().find((t) => t.type === "incident.assigned")?.payload;
            return a?.tech_id ? <p className="muted small">Técnico asignado: <b>{a.tech_id}</b>{a.distance_km != null ? ` a ${fmtNum(a.distance_km)} km` : ""}{a.approval ? ` · ${a.approval === "auto" ? "asignación automática" : a.approval === "approved" ? "aprobado por el supervisor" : "sin respuesta en 30 min"}` : ""}</p> : null;
          })()}
          {onReport && <LinkedReports incidentId={id} onOpen={onReport} />}
          <h3>Línea de tiempo</h3>
          <ol className="timeline">
            {inc.timeline.map((t, i) => (
              <li key={i}>
                <span className="dot" />
                <div><b>{EVENT_LABEL[t.type] || t.type}</b><div className="muted small">{fmtTime(t.ts)} · {t.source}</div></div>
              </li>
            ))}
          </ol>
          {inc.status !== "closed" && !canClose ? null : inc.status !== "closed" ? (
            <div className="close-box">
              <h3>Cerrar incidente</h3>
              <label>Cerrado por<input value={form.closed_by} onChange={(e) => setForm({ ...form, closed_by: e.target.value })} placeholder="TEC-01" /></label>
              <label>Intervención<textarea rows={2} value={form.cause_summary} onChange={(e) => setForm({ ...form, cause_summary: e.target.value })} placeholder="Qué se hizo" /></label>
              <button className="primary" disabled={closing} onClick={close}>{closing ? "Cerrando…" : "Cerrar en AWS"}</button>
              <p className="muted small">AWS calcula el tiempo detenido, libera al técnico, descuenta los repuestos y emite incident.closed.</p>
            </div>
          ) : (
            <p className="banner ok">Cerrado {fmtTime(inc.closed_at)} · {inc.downtime_h} h detenida</p>
          )}
        </>
      )}
    </aside>
  );
}

export function StockView() {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  useEffect(() => {
    const load = () => api.stock().then(setRows).catch((e) => setErr(e.message));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);
  const parts = [...new Map(rows.map((r) => [r.part_id, r])).values()].sort((a, b) => a.part_id.localeCompare(b.part_id));
  const zones = ["NORTE", "CENTRO", "SUR"];
  const cell = (p, z) => rows.find((r) => r.part_id === p && r.zone_id === z);
  return (
    <section className="page">
      <h2>Stock por zona</h2>
      {err && <p className="error">{err}</p>}
      <table className="grid">
        <thead>
          <tr><th>Repuesto</th>{zones.map((z) => <th key={z}>{z}</th>)}</tr>
        </thead>
        <tbody>
          {parts.map((p) => (
            <tr key={p.part_id}>
              <td><b>{p.name}</b><div className="muted small mono">{p.part_id} · {p.sku}</div></td>
              {zones.map((z) => {
                const c = cell(p.part_id, z);
                if (!c) return <td key={z}>—</td>;
                return (
                  <td key={z} className={c.below_reorder ? "low" : ""}>
                    <span className="big">{c.available}</span>
                    <span className="muted small"> disp. · {c.on_hand} en mano{c.reserved ? ` · ${c.reserved} reserv.` : ""}</span>
                    {c.below_reorder && <div className="tag warn">bajo punto de reorden ({c.reorder_point})</div>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
