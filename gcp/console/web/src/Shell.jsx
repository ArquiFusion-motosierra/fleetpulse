import { useState } from "react";
import { ROLE_LABEL, TENANT_LABEL, TENANT_NAME, TENANT_OPERATION } from "./api.js";
import { Icon } from "./ui.jsx";

// Esqueleto de la consola: barra lateral (escritorio), cabecera con el contexto y barra inferior (celular).
// `tabs` = [{ key, label, icon }]; `badges` = { [key]: numero }.

function Badge({ n }) {
  return n > 0 ? <em className="badge">{n}</em> : null;
}

// Parque del usuario (tarjeta fija) o selector del jefe de mantenimiento ("" = todos)
export function ParkBlock({ parks, park, onPark, compact = false }) {
  const current = park || parks.tenant_id;
  if (parks.global) {
    return (
      <label className={`park-block ${compact ? "compact" : ""}`}>
        {!compact && <span className="eyebrow">Parque</span>}
        <select className="park" value={park} onChange={(e) => onPark(e.target.value)} aria-label="Parque">
          <option value="">Todos los parques</option>
          {parks.tenants.map((t) => <option key={t.tenant_id} value={t.tenant_id}>{TENANT_LABEL[t.tenant_id] || t.name}</option>)}
        </select>
      </label>
    );
  }
  if (!current) return null;
  return (
    <div className={`park-block fixed ${compact ? "compact" : ""}`}>
      {!compact && <span className="eyebrow">Tu parque</span>}
      <b>{TENANT_NAME[current] || current}</b>
      {!compact && <span className="muted small">{TENANT_OPERATION[current]}</span>}
    </div>
  );
}

function NavButton({ t, on, badge, onTab }) {
  return (
    <button className={on ? "on" : ""} onClick={() => onTab(t.key)} title={t.label} aria-current={on ? "page" : undefined}>
      <Icon name={t.icon} /><span className="nav-label">{t.label}</span><Badge n={badge} />
    </button>
  );
}

function Who({ user, onLogout }) {
  return (
    <div className="who">
      <span>
        <b>{ROLE_LABEL[user.role] || user.role}</b>
        <span className="muted small">{user.email}{user.tech_id ? ` · ${user.tech_id}` : ""}</span>
      </span>
      <button className="toggle" onClick={onLogout} title="Cambiar de rol o salir"><Icon name="logout" size={16} /><span className="nav-label">Cambiar de rol / Salir</span></button>
    </div>
  );
}

export function Sidebar({ tabs, tab, onTab, badges, parks, park, onPark, user, onLogout }) {
  return (
    <aside className="sidebar">
      <div className="sb-brand"><span className="logo" /><b className="nav-label">FleetPulse</b></div>
      <div className="sb-park"><ParkBlock parks={parks} park={park} onPark={onPark} /></div>
      <nav className="sb-nav" aria-label="Secciones">
        {tabs.map((t) => <NavButton key={t.key} t={t} on={tab === t.key} badge={badges[t.key]} onTab={onTab} />)}
      </nav>
      <div className="sb-foot"><Who user={user} onLogout={onLogout} /></div>
    </aside>
  );
}

export function Topbar({ title, detail, subtitle, live: { error, updated, mock }, parks, park, onPark }) {
  return (
    <header className="topbar">
      <div className="tb-title">
        <span className="logo tb-logo" />
        <div><b>{title}{detail && <span className="tb-detail"> · {detail}</span>}</b>{subtitle && <span className="muted small">{subtitle}</span>}</div>
      </div>
      {/* sin barra lateral completa (< 1100 px) el selector del jefe sube a la cabecera */}
      {parks.global && <div className="tb-park"><ParkBlock parks={parks} park={park} onPark={onPark} compact /></div>}
      <div className={`live ${error ? "down" : ""}`} role="status">
        <i />{error ? "sin conexión" : updated ? `en vivo · ${updated.toLocaleTimeString("es-PE", { timeZone: "America/Lima", hour: "2-digit", minute: "2-digit" })}` : "conectando…"}
        {mock && <span className="tag">datos de prueba</span>}
      </div>
    </header>
  );
}

// Celular: 4 primeras entradas + "Más" (el resto, el usuario y Salir)
export function BottomNav({ tabs, tab, onTab, badges, user, onLogout }) {
  const [more, setMore] = useState(false);
  const first = tabs.slice(0, 4);
  const rest = tabs.slice(4);
  const go = (k) => { setMore(false); onTab(k); };
  return (
    <>
      {more && (
        <div className="sheet-bg" onClick={() => setMore(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Más opciones">
            {rest.length > 0 && <nav className="sheet-nav">{rest.map((t) => <NavButton key={t.key} t={t} on={tab === t.key} badge={badges[t.key]} onTab={go} />)}</nav>}
            <Who user={user} onLogout={onLogout} />
          </div>
        </div>
      )}
      <nav className="bottom-nav" aria-label="Secciones">
        {first.map((t) => <NavButton key={t.key} t={t} on={tab === t.key} badge={badges[t.key]} onTab={go} />)}
        <button className={more || rest.some((t) => t.key === tab) ? "on" : ""} onClick={() => setMore(!more)} aria-expanded={more}>
          <Icon name="more" /><span className="nav-label">Más</span>
        </button>
      </nav>
    </>
  );
}
