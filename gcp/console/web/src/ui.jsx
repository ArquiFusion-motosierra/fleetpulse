import { Component } from "react";
import { TENANT_COLOR, TENANT_LABEL } from "./api.js";

// Piezas visuales compartidas por Inicio, la barra lateral y las vistas (sin librerias: iconos SVG de linea)
export function Card({ title, action, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <div className="card-head">
          {title && <h3>{title}</h3>}
          {action && <button className="link-btn" onClick={action.onClick}>{action.label}</button>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Kpi({ value, label, color, onClick }) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag className={`kpi ${onClick ? "clickable" : ""}`} style={color ? { "--c": color } : undefined} onClick={onClick}>
      <b>{value}</b><span>{label}</span>
    </Tag>
  );
}

export function Empty({ children, action }) {
  return (
    <div className="empty">
      <p>{children}</p>
      {action && <button className="toggle" onClick={action.onClick}>{action.label}</button>}
    </div>
  );
}

export function Skeleton({ lines = 3 }) {
  return <div className="skeleton" aria-busy="true" aria-label="Cargando">{Array.from({ length: lines }, (_, i) => <i key={i} />)}</div>;
}

export function ParkTag({ tenant }) {
  const t = tenant || "mineria";
  return <span className="park-tag" style={{ "--park": TENANT_COLOR[t] }}><i />{TENANT_LABEL[t] || t}</span>;
}

const PATHS = {
  home: "M3 11l9-7 9 7M5 10v10h5v-6h4v6h5V10",
  map: "M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14",
  alert: "M12 3l10 18H2L12 3zM12 10v5M12 18v.01",
  report: "M5 3h10l4 4v14H5V3zM14 3v5h5M8 13h8M8 17h5",
  stock: "M3 7l9-4 9 4v10l-9 4-9-4V7zM3 7l9 4 9-4M12 11v10",
  supply: "M3 6h11v10H3zM14 9h4l3 3v4h-7M7 19a2 2 0 100-4 2 2 0 000 4zM17 19a2 2 0 100-4 2 2 0 000 4z",
  plan: "M4 5h16v15H4zM4 10h16M9 3v4M15 3v4",
  admin: "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9L7 7M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  back: "M15 5l-7 7 7 7",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
};

export function Icon({ name, size = 18 }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] || PATHS.more} />
    </svg>
  );
}

// Aisla un fallo de dibujo (p. ej. MapLibre sin WebGL) para que no tumbe toda la consola
export class ErrorBoundary extends Component {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? this.props.fallback : this.props.children; }
}
