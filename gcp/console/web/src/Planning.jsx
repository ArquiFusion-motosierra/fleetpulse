import { useEffect, useMemo, useState } from "react";
import { api, MODEL_LABEL, fmtTime } from "./api.js";

// Paleta validada (dataviz validate_palette.js, modo oscuro sobre #111821): azul = maquina, naranja = destacada
const DOT = "#3987e5";
const HIGHLIGHT = "#d95926";
// Escenarios del simulador que esta pantalla debe contar (S3 ralenti, S4 desviacion vs pares, S6 uso desbalanceado)
const NOTABLE = { "CF-042": "CF-042 · ralentí 45 %", "MN-005": "MN-005", "MN-003": "MN-003 · 20 h/día", "VQ-031": "VQ-031…035 · 2 h/día" };
const NOTABLE_SET = new Set(["CF-042", "MN-005", "MN-003", "VQ-031", "VQ-032", "VQ-033", "VQ-034", "VQ-035"]);
const USE_SPLIT = 0.35; // utilizacion que separa uso alto / bajo
const COST_SPLIT = 5;   // % de combustible sobre la mediana del cluster

const QUADRANT = {
  star: "Eficiente y productiva",
  workhorse: "Productiva pero cara",
  underused: "Subutilizada",
  inefficient: "Poco uso y cara",
};

const W = 760, H = 420, M = { t: 18, r: 20, b: 46, l: 56 };

function Scatter({ rows }) {
  const [hover, setHover] = useState(null);
  const xMax = Math.max(0.9, ...rows.map((r) => r.utilization));
  const yVals = rows.map((r) => r.fuel_vs_peers_pct ?? 0);
  const yMin = Math.min(-15, Math.floor(Math.min(...yVals) / 5) * 5);
  const yMax = Math.max(25, Math.ceil(Math.max(...yVals) / 5) * 5);
  const x = (v) => M.l + (v / xMax) * (W - M.l - M.r);
  const y = (v) => M.t + ((yMax - v) / (yMax - yMin)) * (H - M.t - M.b);
  const xTicks = [0, 0.2, 0.4, 0.6, 0.8].filter((t) => t <= xMax);
  const yTicks = [];
  for (let t = yMin; t <= yMax; t += 10) yTicks.push(t);
  const sorted = [...rows].sort((a, b) => NOTABLE_SET.has(a.machine_id) - NOTABLE_SET.has(b.machine_id));

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="scatter" role="img" aria-label="Utilización contra costo de combustible frente a pares, por máquina">
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} className={t === 0 ? "axis-zero" : "grid"} />
            <text x={M.l - 8} y={y(t) + 4} className="tick" textAnchor="end">{t > 0 ? `+${t}` : t} %</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={`x${t}`} x={x(t)} y={H - M.b + 18} className="tick" textAnchor="middle">{Math.round(t * 100)} %</text>
        ))}
        <line x1={x(USE_SPLIT)} x2={x(USE_SPLIT)} y1={M.t} y2={H - M.b} className="split" />
        <line x1={M.l} x2={W - M.r} y1={y(COST_SPLIT)} y2={y(COST_SPLIT)} className="split" />
        <text x={M.l + 8} y={M.t + 14} className="quad">{QUADRANT.inefficient}</text>
        <text x={W - M.r - 8} y={M.t + 14} className="quad" textAnchor="end">{QUADRANT.workhorse}</text>
        <text x={M.l + 8} y={H - M.b - 10} className="quad">{QUADRANT.underused}</text>
        <text x={W - M.r - 8} y={H - M.b - 10} className="quad" textAnchor="end">{QUADRANT.star}</text>
        <text x={(M.l + W - M.r) / 2} y={H - 8} className="axis-title" textAnchor="middle">Utilización del día (horas operando / 24)</text>
        <text transform={`translate(14 ${(M.t + H - M.b) / 2}) rotate(-90)`} className="axis-title" textAnchor="middle">Combustible frente a sus pares</text>
        {sorted.map((r) => {
          const hot = NOTABLE_SET.has(r.machine_id);
          const cx = x(r.utilization), cy = y(r.fuel_vs_peers_pct ?? 0);
          return (
            <g key={r.machine_id} onMouseEnter={() => setHover({ r, cx, cy })} onMouseLeave={() => setHover(null)}>
              <circle cx={cx} cy={cy} r={12} fill="transparent" />
              <circle cx={cx} cy={cy} r={hover?.r.machine_id === r.machine_id ? 6 : 4.5} fill={hot ? HIGHLIGHT : DOT} className="dot" />
              {NOTABLE[r.machine_id] && <text x={cx + 9} y={cy + 4} className="dlabel">{NOTABLE[r.machine_id]}</text>}
            </g>
          );
        })}
      </svg>
      {hover && (
        <div className="tip" style={{ left: `${(hover.cx / W) * 100}%`, top: `${(hover.cy / H) * 100}%` }}>
          <b>{hover.r.machine_id}</b>
          <div>Utilización <span>{Math.round(hover.r.utilization * 100)} %</span></div>
          <div>Ralentí <span>{Math.round(hover.r.idle_ratio * 100)} %</span></div>
          <div>Combustible vs pares <span>{hover.r.fuel_vs_peers_pct > 0 ? "+" : ""}{hover.r.fuel_vs_peers_pct} %</span></div>
          <div>Costo por hora <span>USD {hover.r.cost_per_h}</span></div>
          <div className="muted">{QUADRANT[hover.r.quadrant]}</div>
        </div>
      )}
      <div className="legend-row">
        <span><i style={{ background: DOT }} />Máquina</span>
        <span><i style={{ background: HIGHLIGHT }} />Escenario a revisar (S3, S4, S6)</span>
        <span className="muted">Líneas punteadas: {Math.round(USE_SPLIT * 100)} % de uso y +{COST_SPLIT} % sobre la mediana de su clúster</span>
      </div>
    </div>
  );
}

export default function PlanningView({ onMachine }) {
  const [kpi, setKpi] = useState(null);
  const [orders, setOrders] = useState([]);
  const [reorders, setReorders] = useState([]);
  const [asTable, setAsTable] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    Promise.all([api.kpis(), api.orders(), api.reorders()])
      .then(([k, o, r]) => { setKpi(k); setOrders(o); setReorders(r); })
      .catch((e) => setErr(e.message));
  }, []);

  const rows = kpi?.machines || [];
  const stats = useMemo(() => {
    if (!rows.length) return null;
    const avg = (f) => rows.reduce((s, r) => s + f(r), 0) / rows.length;
    return {
      util: avg((r) => r.utilization), idle: avg((r) => r.idle_ratio), cost: avg((r) => r.cost_per_h),
      flagged: rows.filter((r) => r.quadrant === "inefficient" || r.quadrant === "underused").length,
    };
  }, [rows]);

  return (
    <section className="page planning">
      <div className="page-head">
        <h2>Planificación</h2>
        <span className="muted">{kpi?.day ? `KPIs del ${kpi.day} · daily-planning` : "Sin KPIs todavía"}</span>
      </div>
      {err && <p className="error">{err}</p>}
      {stats && (
        <div className="tiles">
          <div><span>Utilización media</span><b>{Math.round(stats.util * 100)} %</b></div>
          <div><span>Ralentí medio</span><b>{Math.round(stats.idle * 100)} %</b></div>
          <div><span>Costo medio por hora</span><b>USD {stats.cost.toFixed(1)}</b></div>
          <div><span>Máquinas a revisar</span><b>{stats.flagged}</b></div>
        </div>
      )}
      {rows.length > 0 && (
        <div className="card">
          <div className="card-head">
            <h3>Productividad: uso contra costo</h3>
            <button className="toggle" onClick={() => setAsTable(!asTable)}>{asTable ? "Ver gráfico" : "Ver tabla"}</button>
          </div>
          {asTable ? (
            <table className="grid compact clickable">
              <thead><tr><th>Máquina</th><th>Modelo</th><th>Utilización</th><th>Ralentí</th><th>Combustible vs pares</th><th>Costo/h</th><th>Cuadrante</th></tr></thead>
              <tbody>
                {[...rows].sort((a, b) => b.fuel_vs_peers_pct - a.fuel_vs_peers_pct).map((r) => (
                  <tr key={r.machine_id} onClick={() => onMachine(r.machine_id)}>
                    <td><b>{r.machine_id}</b></td><td>{MODEL_LABEL[r.machine_id.slice(0, 2)]}</td>
                    <td>{Math.round(r.utilization * 100)} %</td><td>{Math.round(r.idle_ratio * 100)} %</td>
                    <td>{r.fuel_vs_peers_pct > 0 ? "+" : ""}{r.fuel_vs_peers_pct} %</td><td>USD {r.cost_per_h}</td>
                    <td>{QUADRANT[r.quadrant]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Scatter rows={rows} />}
        </div>
      )}
      <div className="two-col">
        <div className="card">
          <h3>Órdenes preventivas sugeridas <span className="muted">({orders.length})</span></h3>
          {orders.length === 0 ? <p className="muted">Sin órdenes sugeridas.</p> : (
            <table className="grid compact clickable">
              <thead><tr><th>Máquina</th><th>Vence</th><th>Motivo</th></tr></thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.order_id || `${o.machine_id}${o.due_at}`} onClick={() => onMachine(o.machine_id)}>
                    <td><b>{o.machine_id}</b><div className="muted small">{o.kind === "preventive" ? "Preventivo" : "Correctivo"}</div></td>
                    <td>{fmtTime(o.due_at)}</td>
                    <td className="small">{o.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="card">
          <h3>Pedidos sugeridos de repuestos <span className="muted">({reorders.length})</span></h3>
          {reorders.length === 0 ? <p className="muted">Sin pedidos sugeridos.</p> : (
            <table className="grid compact">
              <thead><tr><th>Repuesto</th><th>Zona</th><th>Cantidad</th><th>Origen</th></tr></thead>
              <tbody>
                {reorders.map((r, i) => (
                  <tr key={`${r.part_id}${r.zone_id}${r.created_at}${i}`}>
                    <td className="mono">{r.part_id}</td><td>{r.zone_id}</td><td><b>{r.qty}</b></td>
                    <td className="small">{r.source === "shortage" ? `Falta de stock${r.incident_id ? ` · ${r.incident_id}` : ""}` : `Pronóstico 14 d: ${r.forecast_14d ?? "—"}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </section>
  );
}
