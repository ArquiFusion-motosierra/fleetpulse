import { useEffect, useState } from "react";
import { DEMO_ACCOUNTS } from "./accounts.js";
import { api, SEVERITY, MODEL_LABEL, REPORT_STATUS, URGENCY, PERCEIVED, ROLE_LABEL, TENANT_COLOR, TENANT_NAME, fmtTime, ago } from "./api.js";
import { loadConfig, loginCognito, loginMock } from "./auth.js";

// ---------- Login ----------
const PARK_GROUPS = [["mineria", TENANT_NAME.mineria], ["agro", TENANT_NAME.agro], ["industria", TENANT_NAME.industria], ["all", "Todos los parques"]];
const ROLE_ORDER = ["operator", "technician", "supervisor", "management", "admin", "maintenance_head"];

export function Login({ onLogged }) {
  const [cfg, setCfg] = useState(null);
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState("");
  useEffect(() => { loadConfig().then(setCfg).catch((e) => setErr(e.message)); }, []);
  // Cognito: la lista es fija (accounts.js); mock: los usuarios simulados del BFF, el jefe (sin parque) en "Todos"
  const accounts = cfg?.mode === "mock"
    ? cfg.users.map((u) => ({ tenant: u.tenant_id || "all", email: u.email, role: u.role, tech_id: u.tech_id, mock: u }))
    : DEMO_ACCOUNTS;
  async function pick(a) {
    if (a.mock) return onLogged(loginMock(a.mock));
    try { await navigator.clipboard.writeText(a.email); setCopied(a.email); } catch { /* sin portapapeles: igual se entra */ }
    loginCognito(a.email);
  }
  return (
    <div className="login">
      <div className="login-card">
        <div className="brand"><span className="logo" /><div><b>FleetPulse</b><span>Mantenimiento predictivo multi-nube (AWS + GCP) · ArquiFusion</span></div></div>
        <p>La telemetría de cada máquina se compara con su línea base y la ficha del fabricante; cuando algo se desvía, se abre un
          incidente, se asigna un técnico y se reservan repuestos. Cada rol y cada parque ve su propia consola.</p>
        {err && <p className="error">{err}</p>}
        {cfg?.mode === "cognito" && <button className="primary" onClick={() => loginCognito()}>Ingresar</button>}
        {cfg && (
          <div className="accounts">
            <div className="accounts-head">
              <b>{cfg.mode === "mock" ? "Modo de prueba: elige un usuario" : "Cuentas de evaluación"}</b>
              {cfg.mode === "cognito" && <span className="muted small">Clic en un correo: se copia y se abre el ingreso. Contraseña: en el README del repo.</span>}
            </div>
            {PARK_GROUPS.map(([t, name]) => {
              const rows = accounts.filter((a) => a.tenant === t).sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role));
              if (!rows.length) return null;
              return (
                <div key={t} className="acc-group" style={{ "--park": TENANT_COLOR[t] || "#8b9aab" }}>
                  <span className="eyebrow">{name}</span>
                  <div className="users">
                    {rows.map((a) => (
                      <button key={a.email} onClick={() => pick(a)} title={a.mock ? "Entrar con este usuario" : "Copiar correo e ingresar"}>
                        <b>{ROLE_LABEL[a.role]}{a.tech_id ? ` · ${a.tech_id}` : ""}</b><span>{copied === a.email ? "copiado ✓" : a.email}</span>
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- Operario: reportar una anormalidad ----------
export function ReportForm({ machines, onCreated }) {
  const [types, setTypes] = useState([]);
  const [form, setForm] = useState({ machine_id: "", abnormality_type: "", description: "", severity_perceived: "medium" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [created, setCreated] = useState(null);
  useEffect(() => { api.abnormalityTypes().then(setTypes).catch((e) => setErr(e.message)); }, []);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api.createReport(form);
      setCreated(r);
      onCreated?.(r);
    } catch (ex) {
      setErr(`No se pudo enviar: ${ex.message}`);
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <section className="page narrow">
        <h2>Reporte enviado</h2>
        <ReportSummary r={created} />
        <button className="toggle" onClick={() => { setCreated(null); setForm({ ...form, description: "" }); }}>Reportar otra anormalidad</button>
      </section>
    );
  }
  return (
    <section className="page narrow">
      <h2>Reportar anormalidad</h2>
      <p className="muted">Lo que observas en la máquina se cruza con su telemetría y con la ficha del fabricante para recomendar los chequeos al técnico.</p>
      <form className="form" onSubmit={submit}>
        <label htmlFor="rf-machine">Máquina</label>
        <select id="rf-machine" required value={form.machine_id} onChange={set("machine_id")}>
          <option value="">Elige la máquina</option>
          {machines.map((m) => <option key={m.machine_id} value={m.machine_id}>{m.machine_id} · {MODEL_LABEL[m.model]} · {m.zone_id}</option>)}
        </select>
        <label htmlFor="rf-type">Qué observaste</label>
        <select id="rf-type" required value={form.abnormality_type} onChange={set("abnormality_type")}>
          <option value="">Elige el tipo</option>
          {types.map((t) => <option key={t.code} value={t.code}>{t.label_es}</option>)}
        </select>
        <label htmlFor="rf-desc">Descripción</label>
        <textarea id="rf-desc" required rows={3} maxLength={1000} value={form.description} onChange={set("description")}
          placeholder="Ej. Sale vapor del capó y el indicador de temperatura está en rojo" />
        <fieldset>
          <legend>Qué tan grave te parece</legend>
          {Object.entries(PERCEIVED).map(([k, v]) => (
            <label key={k} className="radio"><input type="radio" name="sev" id={`rf-sev-${k}`} value={k} checked={form.severity_perceived === k} onChange={set("severity_perceived")} />{v}</label>
          ))}
        </fieldset>
        {err && <p className="error">{err}</p>}
        <button className="primary" disabled={busy}>{busy ? "Enviando…" : "Enviar reporte"}</button>
      </form>
    </section>
  );
}

function Urgency({ u }) {
  const c = { observe: SEVERITY.observe.color, schedule: SEVERITY.schedule.color, immediate: SEVERITY.immediate.color }[u];
  return <span className="sev" style={{ "--c": c }}><i />{URGENCY[u] || u}</span>;
}

export function Recommendation({ rec }) {
  if (!rec) return null;
  return (
    <div className="report">
      <h3>Chequeos recomendados · {rec.source === "gemini" ? "Gemini" : "plantilla"}</h3>
      <div className="muted small">Urgencia: <Urgency u={rec.urgency} /></div>
      <ol className="checks">
        {rec.checks.map((c) => (
          <li key={c.order}>
            <b>{c.step}</b>
            {(c.tool || c.expected) && <div className="small muted">{c.tool && `Herramienta: ${c.tool}. `}{c.expected && `Esperado: ${c.expected}.`}</div>}
            {c.if_fails && <div className="small">Si falla: {c.if_fails}</div>}
          </li>
        ))}
      </ol>
      {rec.probable_causes?.length > 0 && (
        <p className="small">Causas probables: {rec.probable_causes.map((p) => `${p.cause} (${Math.round(p.confidence * 100)} %)`).join(" · ")}</p>
      )}
      {rec.suggested_parts?.length > 0 && <p className="small">Repuestos sugeridos: {rec.suggested_parts.join(", ")}</p>}
      {rec.safety_notes?.map((s) => <p key={s} className="banner warn small">Seguridad: {s}</p>)}
    </div>
  );
}

function ReportSummary({ r }) {
  return (
    <>
      <div className="facts">
        <div><span>Reporte</span><b className="mono small">{r.report_id}</b></div>
        <div><span>Estado</span><b>{REPORT_STATUS[r.status]}</b></div>
        <div><span>Máquina</span><b>{r.machine_id}</b></div>
        <div><span>Técnico</span><b>{r.assigned_tech_id || "Por asignar"}</b></div>
      </div>
      {r.incident_id && (
        <p className="banner">{r.incident_link === "started" ? "Se abrió el incidente" : "Vinculado al incidente"} <span className="mono">{r.incident_id}</span></p>
      )}
      <Recommendation rec={r.recommendation} />
    </>
  );
}

// ---------- Listas de reportes (operario: mios; tecnico: asignados; supervisor y admin: todos) ----------
export function ReportsList({ title, query, onOpen, empty }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    const load = () => api.reports(query).then(setRows).catch((e) => setErr(e.message));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [query]);
  return (
    <section className="page">
      <h2>{title}</h2>
      {err && <p className="error">{err}</p>}
      {rows && rows.length === 0 && <p className="muted">{empty}</p>}
      {rows && rows.length > 0 && (
        <table className="grid clickable">
          <thead><tr><th>Reporte</th><th>Máquina</th><th>Tipo</th><th>Urgencia</th><th>Estado</th><th>Técnico</th><th>Enviado</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.report_id} onClick={() => onOpen(r.report_id)}>
                <td className="mono small">{r.report_id}</td><td><b>{r.machine_id}</b></td>
                <td>{r.abnormality_type}</td><td><Urgency u={r.recommendation?.urgency} /></td>
                <td>{REPORT_STATUS[r.status]}</td><td>{r.assigned_tech_id || "—"}</td><td>{ago(r.reported_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------- Panel del reporte: recomendacion y cierre por el tecnico ----------
export function ReportPanel({ id, user, onClose, onIncident, onChanged }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({ findings: "", confirmed_cause: "" });
  const [busy, setBusy] = useState(false);
  const load = () => api.report(id).then(setData).catch((e) => setErr(e.message));
  useEffect(() => { setData(null); setErr(""); load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  const r = data?.report;
  const canClose = r && r.status !== "closed" && ["technician", "supervisor", "admin"].includes(user.role)
    && (user.role !== "technician" || !r.assigned_tech_id || r.assigned_tech_id === user.tech_id);

  async function close(e) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const parts = (r.recommendation?.suggested_parts || []).slice(0, 1).map((p) => ({ part_id: p, zone_id: r.zone_id, qty: 1 }));
      await api.closeReport(id, { ...form, parts_used: form.useParts ? parts : [] });
      await load();
      onChanged?.();
    } catch (ex) {
      setErr(`No se pudo cerrar: ${ex.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="drawer">
      <header>
        <div><div className="eyebrow">Reporte del operario</div><h2 className="mono">{id}</h2></div>
        <button className="x" onClick={onClose} aria-label="Cerrar">×</button>
      </header>
      {err && <p className="error">{err}</p>}
      {!r && !err && <p className="muted">Cargando…</p>}
      {r && (
        <>
          <p className="report-summary">“{r.description}”</p>
          <p className="muted small">{r.reported_by} · {fmtTime(r.reported_at)} · severidad percibida {PERCEIVED[r.severity_perceived]?.toLowerCase()}</p>
          <ReportSummary r={r} />
          {r.incident_id && <button className="toggle" onClick={() => onIncident(r.incident_id)}>Ver incidente</button>}
          {r.status === "closed" && (
            <p className="banner ok">Cerrado {fmtTime(r.closed_at)} por {r.closed_by}: {r.confirmed_cause}. {r.findings}</p>
          )}
          {canClose && (
            <form className="close-box" onSubmit={close}>
              <h3>Cerrar reporte</h3>
              <label htmlFor="rc-findings">Hallazgos</label>
              <textarea id="rc-findings" required rows={3} value={form.findings} onChange={(e) => setForm({ ...form, findings: e.target.value })}
                placeholder="Qué encontraste al hacer los chequeos" />
              <label htmlFor="rc-cause">Causa confirmada</label>
              <input id="rc-cause" required value={form.confirmed_cause} onChange={(e) => setForm({ ...form, confirmed_cause: e.target.value })} placeholder="Ej. Junta de culata dañada" />
              {r.recommendation?.suggested_parts?.length > 0 && (
                <label className="radio"><input type="checkbox" id="rc-parts" checked={!!form.useParts} onChange={(e) => setForm({ ...form, useParts: e.target.checked })} />
                  Usé {r.recommendation.suggested_parts[0]} (zona {r.zone_id})</label>
              )}
              <button className="primary" disabled={busy}>{busy ? "Cerrando…" : "Cerrar reporte"}</button>
            </form>
          )}
          <h3>Línea de tiempo</h3>
          <ol className="timeline">
            {(data.timeline || []).map((t, i) => (
              <li key={i}><span className="dot" /><div><b>{t.type}</b><div className="muted small">{fmtTime(t.ts)} · {t.source}</div></div></li>
            ))}
          </ol>
        </>
      )}
    </aside>
  );
}

// ---------- Reportes vinculados a un incidente (panel del incidente) ----------
export function LinkedReports({ incidentId, onOpen }) {
  const [rows, setRows] = useState([]);
  useEffect(() => { api.reports(`incident_id=${incidentId}`).then(setRows).catch(() => setRows([])); }, [incidentId]);
  if (!rows.length) return null;
  return (
    <>
      <h3>Reportes del operario</h3>
      <ul className="linked">
        {rows.map((r) => (
          <li key={r.report_id} onClick={() => onOpen(r.report_id)}>
            <b>{r.abnormality_type}</b> · {REPORT_STATUS[r.status]}<div className="muted small">“{r.description}”</div>
          </li>
        ))}
      </ul>
    </>
  );
}

// ---------- Administracion (T35): tecnicos y tipos de anormalidad ----------
export function AdminView() {
  const [techs, setTechs] = useState([]);
  const [types, setTypes] = useState([]);
  const [err, setErr] = useState("");
  const [nt, setNt] = useState({ code: "", label_es: "" });
  const load = () => Promise.all([api.technicians(), api.abnormalityTypes()]).then(([t, a]) => { setTechs(t); setTypes(a); })
    .catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  const act = (p) => p.then(load).catch((e) => setErr(e.message));
  return (
    <section className="page">
      <h2>Administración</h2>
      {err && <p className="error">{err}</p>}
      <div className="two-col">
        <div className="card">
          <h3>Técnicos</h3>
          <table className="grid compact">
            <thead><tr><th>Técnico</th><th>Especialidad</th><th>Zona</th><th>Disponible</th></tr></thead>
            <tbody>
              {techs.map((t) => (
                <tr key={t.tech_id}>
                  <td><b>{t.tech_id}</b><div className="muted small">{t.name}</div></td>
                  <td>{t.specialty}</td>
                  <td>
                    <select aria-label={`Zona de ${t.tech_id}`} id={`zone-${t.tech_id}`} value={t.zone_id} onChange={(e) => act(api.putTechnician(t.tech_id, { zone_id: e.target.value }))}>
                      {["NORTE", "CENTRO", "SUR"].map((z) => <option key={z}>{z}</option>)}
                    </select>
                  </td>
                  <td><button className={`toggle ${t.available ? "on" : ""}`} onClick={() => act(api.putTechnician(t.tech_id, { available: !t.available }))}>{t.available ? "Disponible" : "Ocupado"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h3>Tipos de anormalidad</h3>
          <ul className="types">
            {types.map((t) => (
              <li key={t.code}><span><b>{t.label_es}</b> <span className="mono small muted">{t.code}</span></span>
                <button className="x" aria-label={`Borrar ${t.code}`} onClick={() => act(api.deleteType(t.code))}>×</button></li>
            ))}
          </ul>
          <form className="inline" onSubmit={(e) => { e.preventDefault(); act(api.addType(nt)).then(() => setNt({ code: "", label_es: "" })); }}>
            <input id="nt-code" required placeholder="codigo_nuevo" value={nt.code} onChange={(e) => setNt({ ...nt, code: e.target.value })} />
            <input id="nt-label" required placeholder="Nombre visible" value={nt.label_es} onChange={(e) => setNt({ ...nt, label_es: e.target.value })} />
            <button className="primary">Agregar</button>
          </form>
        </div>
      </div>
    </section>
  );
}
