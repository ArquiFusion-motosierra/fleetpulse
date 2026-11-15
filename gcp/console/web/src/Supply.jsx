import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtTime, TENANT_LABEL } from "./api.js";
import { groupOptions } from "./supply-options.js";

// Pestana Abastecimiento (T41, diseno 6.3): stock por parque, sugerencias con opciones comparadas, ordenes y
// transferencias con recepcion, proveedores y KPIs. Todo via /api/supply/* (el BFF reenvia el ID token).
const REFRESH_MS = 15000;
const RECEIVE_ROLES = ["admin", "technician", "maintenance_head"];
const DOC_STATUS = { suggested: "Sugerida", approved: "Aprobada", ordered: "Pedida", in_transit: "En tránsito",
  received: "Recibida", cancelled: "Cancelada", rejected: "Rechazada" };
// Errores 409/502 de aws/supply (T40) en palabras del usuario
const ERR = { SOURCE_BELOW_SAFETY: "el almacén de origen quedaría bajo su stock de seguridad",
  INVALID_STATE: "la orden ya cambió de estado; se actualiza la vista", TASK_EXPIRED: "la espera de esta orden venció",
  FLOW_UNAVAILABLE: "Step Functions no respondió; vuelve a intentar", TENANT_FORBIDDEN: "es de otro parque",
  FORBIDDEN: "tu rol no puede hacer esto" };
const STOCK_STATUS = { ok: "OK", low: "Bajo", stockout: "Quiebre" };
const EV_LABEL = { "supply.suggested": "Sugerida", "supply.approved": "Aprobada", "supply.ordered": "Orden enviada al proveedor",
  "supply.in_transit": "Despachada", "supply.received": "Recepción confirmada", "supply.cancelled": "Cancelada",
  "part.reserved": "Reservada para el incidente" };
const usd = (n) => `USD ${Math.round(n || 0).toLocaleString("es-PE")}`;
const eta = (h) => (h >= 48 ? `${Math.round(h / 24)} días` : `${h} h`);

function canDecide(user, d) {
  return d.status === "suggested" && d.approver_roles.includes(user.role) &&
    (user.role === "maintenance_head" || user.tenant_id === d.tenant_id);
}
function canReceive(user, d) {
  return ["ordered", "in_transit"].includes(d.status) && RECEIVE_ROLES.includes(user.role) &&
    (user.role === "maintenance_head" || user.tenant_id === d.tenant_id);
}
const where = (d) => (d.kind === "transfer" ? `${d.from_zone} → ${d.zone_id}` : d.zone_id);

export default function SupplyView({ user, onIncident }) {
  const [stock, setStock] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [docs, setDocs] = useState([]);
  const [kpis, setKpis] = useState(null);
  const [suppliers, setSuppliers] = useState([]);
  const [onlyIssues, setOnlyIssues] = useState(true);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");
  // Enlace directo para grabar: #po:<id> o #trf:<id> abre su linea de tiempo
  const [focus, setFocus] = useState(() => location.hash.match(/^#(?:po|trf):([A-Z0-9-]+)$/)?.[1] || "");
  const focusRef = useRef(null);
  const head = user.role === "maintenance_head";
  const seesSuppliers = ["admin", "maintenance_head"].includes(user.role);
  const seesKpis = ["admin", "maintenance_head", "management"].includes(user.role);

  const load = useCallback(async () => {
    try {
      const [s, sg, po, trf, k] = await Promise.all([
        api.supplyStock(), api.supplySuggestions(), api.supplyDocs("purchase-orders"), api.supplyDocs("transfers"),
        seesKpis ? api.supplyKpis() : Promise.resolve(null),
      ]);
      setStock(s);
      setSuggestions(sg);
      setDocs([...po, ...trf].filter((d) => d.status !== "suggested").sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
      setKpis(k);
      setErr("");
    } catch (e) {
      setErr(e.message);
    }
  }, [seesKpis]);

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => { if (seesSuppliers) api.supplySuppliers().then(setSuppliers).catch(() => {}); }, [seesSuppliers]);
  useEffect(() => { focusRef.current?.scrollIntoView({ block: "center" }); }, [focus, docs.length, suggestions.length]);

  const act = async (d, action) => {
    setBusy(d.id + action);
    setMsg("");
    try {
      const out = await api.supplyAct(d.kind === "transfer" ? "transfers" : "purchase-orders", d.id, action);
      setMsg(`${d.id}: ${DOC_STATUS[out.status] || out.status}${out.flow_pending ? " · supply-flow procesando…" : ""}`);
      setFocus(d.id);
      history.replaceState(null, "", `#${d.kind === "transfer" ? "trf" : "po"}:${d.id}`);
      await load();
      // En AWS el flujo es asincrono (flow_pending): refrescar pronto en vez de esperar los 15 s
      if (out.flow_pending) [2000, 5000, 10000].forEach((ms) => setTimeout(load, ms));
    } catch (e) {
      setMsg(`No se pudo: ${ERR[e.message] || e.message}`);
      if (e.message === "INVALID_STATE") load();
    } finally {
      setBusy("");
    }
  };

  // Matriz repuesto x almacen, columnas agrupadas por parque
  const parkOrder = Object.keys(TENANT_LABEL);
  const zones = [...new Map(stock.map((r) => [r.zone_id, r.tenant_id])).entries()]
    .sort((a, b) => parkOrder.indexOf(a[1]) - parkOrder.indexOf(b[1]));  // estable: respeta el orden de zonas
  const parks = [...new Set(zones.map(([, t]) => t))];
  const byPart = new Map();
  stock.forEach((r) => {
    if (!byPart.has(r.part_id)) byPart.set(r.part_id, { part_id: r.part_id, name: r.name, criticality: r.criticality, cells: {} });
    byPart.get(r.part_id).cells[r.zone_id] = r;
  });
  const parts = [...byPart.values()]
    .filter((p) => !onlyIssues || p.criticality === "A" || Object.values(p.cells).some((c) => c.status !== "ok" || c.in_transit || c.on_order))
    .sort((a, b) => a.part_id.localeCompare(b.part_id));

  return (
    <section className="page supply">
      <div className="page-head">
        <h2>Abastecimiento</h2>
        <span className="muted">{head ? "Todos los parques a tu cargo" : TENANT_LABEL[user.tenant_id] || user.tenant_id} · se actualiza cada 15 s</span>
      </div>
      {err && <p className="error">No se pudo leer el abastecimiento: {err}</p>}
      {msg && <div className={`banner ${msg.startsWith("No") ? "warn" : "ok"}`}>{msg}</div>}

      {kpis && (
        <div className="tiles">
          <div><span>Parada evitada</span><b>{kpis.downtime_avoided_h} h</b><span>{usd(kpis.downtime_avoided_usd)}</span></div>
          <div><span>Quiebres abiertos</span><b className={kpis.open_stockouts ? "bad-text" : ""}>{kpis.open_stockouts}</b></div>
          <div><span>Valor de inventario</span><b>{usd(Object.values(kpis.inventory_value_usd || {}).reduce((a, b) => a + b, 0))}</b>
            <span>{Object.entries(kpis.inventory_value_usd || {}).map(([t, v]) => `${TENANT_LABEL[t] || t} ${usd(v)}`).join(" · ")}</span></div>
          <div><span>Fill rate</span><b>{Math.round((kpis.fill_rate || 0) * 100)} %</b></div>
        </div>
      )}

      <div className="card">
        <h3>Sugerencias <span className="muted">({suggestions.length})</span></h3>
        {suggestions.length === 0 && <p className="muted">Sin sugerencias pendientes.</p>}
        <div className="suggestions">
          {suggestions.map((d) => (
            <article key={d.id} className={`suggestion ${focus === d.id ? "focus" : ""}`} ref={focus === d.id ? focusRef : null}>
              <header>
                <span className="eyebrow">{d.kind === "transfer" ? "Transferencia entre parques" : "Orden de compra"}</span>
                <b>{d.qty} × {d.part_id}</b> <span className="muted">a {where(d)}</span>
                {d.incident_id && <div className="small">para <span className="link" onClick={() => onIncident(d.incident_id)}>{d.incident_id}</span></div>}
              </header>
              <table className="grid compact">
                <thead><tr><th>Opción</th><th>Llega en</th><th>Costo</th><th>Costo con parada</th></tr></thead>
                <tbody>
                  {groupOptions(d.options).map((o, i) => (
                    <tr key={i} className={i === 0 ? "chosen" : ""}>
                      <td>{o.source_kind === "transfer" ? `Transferir desde ${o.zones.join(" o ")}` : `Comprar a ${o.supplier_id}`}{i === 0 && <span className="tag ok-tag">recomendada</span>}</td>
                      <td>{eta(o.eta_h)}</td><td>{usd(o.cost_usd)}</td><td>{usd(o.total_cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {d.downtime_avoided_h > 0 && <p className="avoided">Evita {d.downtime_avoided_h} h de parada ≈ {usd(d.downtime_avoided_usd)}</p>}
              <p className="muted small">{d.reason}</p>
              {canDecide(user, d) ? (
                <div className="actions">
                  <button className="primary" disabled={!!busy} onClick={() => act(d, "approve")}>{busy === d.id + "approve" ? "Aprobando…" : "Aprobar"}</button>
                  <button className="toggle" disabled={!!busy} onClick={() => act(d, "reject")}>Rechazar</button>
                </div>
              ) : <p className="muted small">Aprueba: {d.approver_roles.join(", ")}</p>}
            </article>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h3>Stock por almacén</h3>
          <label className="small muted"><input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} /> Solo críticos o con alerta</label>
        </div>
        <div className="scroll-x">
          <table className="grid compact matrix">
            <thead>
              {parks.length > 1 && <tr><th />{parks.map((t) => <th key={t} colSpan={zones.filter(([, z]) => z === t).length}>{TENANT_LABEL[t] || t}</th>)}</tr>}
              <tr><th>Repuesto</th>{zones.map(([z]) => <th key={z}>{z}</th>)}</tr>
            </thead>
            <tbody>
              {parts.map((p) => (
                <tr key={p.part_id}>
                  <td><b>{p.name}</b><div className="muted small mono">{p.part_id} · crit. {p.criticality}</div></td>
                  {zones.map(([z]) => {
                    const c = p.cells[z];
                    if (!c) return <td key={z} className="muted">—</td>;
                    return (
                      <td key={z} className={`st-${c.status}`} title={`${STOCK_STATUS[c.status]} · ROP ${c.rop} · seguridad ${c.safety_stock}`}>
                        <span className="big">{c.available}</span>
                        <span className="muted small"> · {c.days_of_coverage} d</span>
                        {c.reserved > 0 && <div className="tag">{c.reserved} reservada{c.reserved > 1 ? "s" : ""}</div>}
                        {c.in_transit > 0 && <div className="tag">+{c.in_transit} en tránsito</div>}
                        {c.on_order > 0 && <div className="tag">+{c.on_order} pedido</div>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted small">Disponible · días de cobertura. Rojo: quiebre; naranja: bajo el punto de reorden.</p>
      </div>

      <div className="card">
        <h3>Órdenes y transferencias</h3>
        {docs.length === 0 && <p className="muted">Sin órdenes en curso.</p>}
        {docs.length > 0 && (
          <table className="grid compact">
            <thead><tr><th>ID</th><th>Repuesto</th><th>Destino</th><th>Estado</th><th>ETA</th><th>Costo</th><th /></tr></thead>
            <tbody>
              {docs.map((d) => (
                <DocRow key={d.id} d={d} user={user} open={focus === d.id} busy={busy} rowRef={focus === d.id ? focusRef : null}
                  onToggle={() => setFocus(focus === d.id ? "" : d.id)} onReceive={() => act(d, "receive")} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {seesSuppliers && suppliers.length > 0 && (
        <div className="card">
          <h3>Proveedores</h3>
          <table className="grid compact">
            <thead><tr><th>Proveedor</th><th>Repuestos (precio · lead time · mínimo)</th></tr></thead>
            <tbody>
              {suppliers.map((s) => (
                <tr key={s.supplier_id}>
                  <td><b>{s.name}</b><div className="muted small mono">{s.supplier_id}</div></td>
                  <td className="small">{s.parts.map((p) => `${p.part_id} ${usd(p.unit_price_usd)} · ${p.lead_time_d} d · ${p.min_qty}`).join("  |  ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function DocRow({ d, user, open, busy, rowRef, onToggle, onReceive }) {
  return (
    <>
      <tr ref={rowRef} className={`clickable-row ${open ? "active" : ""}`} onClick={onToggle}>
        <td className="mono small">{d.id}</td>
        <td>{d.qty} × {d.part_id}</td>
        <td>{where(d)}<div className="muted small">{TENANT_LABEL[d.tenant_id] || d.tenant_id}</div></td>
        <td><span className={`tag st-tag-${d.status}`}>{DOC_STATUS[d.status] || d.status}</span></td>
        <td>{d.status === "received" || d.status === "cancelled" ? "—" : fmtTime(d.eta)}</td>
        <td>{usd(d.cost_usd)}</td>
        <td onClick={(e) => e.stopPropagation()}>
          {canReceive(user, d) && (
            <button className="primary" disabled={!!busy} onClick={onReceive}>{busy === d.id + "receive" ? "Confirmando…" : "Confirmar recepción"}</button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="detail">
          <td colSpan={7}>
            <ul className="timeline">
              {d.timeline.map((e, i) => (
                <li key={i}><span className="dot" /><div><b>{EV_LABEL[e.type] || e.type}</b>
                  <div className="muted small">{fmtTime(e.at)} · {e.by}{e.detail ? ` · ${e.detail}` : ""}</div></div></li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}
