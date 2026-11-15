# Consola: esqueleto nuevo e inicio por rol — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que el jurado, entrando solo a la consola, entienda en segundos su rol, su parque y qué hacer, con un Inicio por rol y el parque como protagonista.

**Architecture:** solo frontend (`gcp/console/web`). La lógica de Inicio vive en funciones puras (`derive.js`) probadas con `node --test`; los componentes nuevos (`ui.jsx`, `Shell.jsx`, `Home.jsx`) solo pintan. `App.jsx` cambia la barra superior por barra lateral + cabecera y agrega la pestaña `home`. BFF y AWS no cambian.

**Tech Stack:** React 18.3.1, Vite 5.4.11, MapLibre 4.7.1, oidc-client-ts 3.1.0, CSS propio; tests con el runner nativo de Node (`node --test`, sin dependencias nuevas).

**Spec:** `docs/superpowers/specs/2026-11-15-console-ux-design.md`

**Ajuste tras el PR #34 (T41):** la pestaña Abastecimiento (`Supply.jsx`) ya existe y reemplaza a Stock para
supervisor, admin y jefe. No se oculta (sin `SHOW_SUPPLY`): se encaja en la barra lateral con su ícono y en el estilo
de tarjetas, sin reescribir `Supply.jsx`.

## Global Constraints

- Sin dependencias nuevas en `package.json` (ni librerías de componentes ni de íconos; íconos SVG inline).
- Sin cambios en `gcp/console/app/**` (BFF) ni en `aws/**`.
- Colores de parque: Minería `#f59e0b`, Agro `#22c55e`, Industria `#3b82f6`; severidad mantiene `SEVERITY` de `api.js`; el parque nunca usa rojo.
- Tokens nuevos en `:root`: `--park`, `--space-1..6` = 4/8/12/16/24/32 px, `--shadow-1`; tarjeta `.card` radio 12 px, padding 16 px; títulos 20/16 px, cuerpo 14 px.
- Barra lateral 240 px; íconos solos entre 768 y 1100 px; < 768 px barra inferior con 4 entradas + "Más"; usable desde 360 px.
- Enlaces directos que deben seguir funcionando: `#<pestaña>`, `#home`, `#machine:<id>`, `#incident:<id>`, `#report:<id>`.
- La contraseña de evaluación nunca aparece en la web; el login dice "contraseña en el README del repo".
- Texto de la interfaz en español con tildes, como hoy.

## Review Focus

1. Usuario de parque con 0 incidentes, 0 reportes y stock sin alertas (agro/industria hoy): cada tarjeta de Inicio muestra su estado vacío con explicación, nunca una tarjeta en blanco → test `needsAttention` y `techWork` con listas vacías (Task 1).
2. Incidentes sin `tenant_id` (anteriores al backfill) en la vista del jefe: se cuentan en `mineria` (el parque por defecto del BFF), no desaparecen → test en `parkSummary` (Task 1).
3. Técnico sin `tech_id` en el token (cuenta mal sembrada): Inicio del técnico no revienta y muestra "Tu cuenta no tiene técnico asignado" → test `techWork(..., null)` (Task 1).
4. Celular 390 px con un panel de detalle abierto: el panel ocupa la pantalla y "Volver" lo cierra; no queda scroll horizontal → verificación visual en Task 6.
5. Enlace directo `#incident:<id>` para un rol sin pestaña Incidentes (operador): abre el panel sobre Inicio sin romper → verificación en Task 3.

---

### Task 1: Lógica de Inicio en funciones puras + tests + job de CI

**Files:**
- Create: `gcp/console/web/src/derive.js`
- Create: `gcp/console/web/test/derive.test.js`
- Modify: `gcp/console/web/package.json` (script `"test": "node --test test/"`)
- Modify: `.github/workflows/ci.yml` (job nuevo `console-web`: `actions/setup-node@v4` node 20, `working-directory: gcp/console/web`, `npm ci`, `npm test`, `npm run build`)

**Interfaces:**
- Produces (todos `export` de `derive.js`):
  - `SEV_RANK = { immediate: 3, schedule: 2, observe: 1, ok: 0 }`
  - `tenantOf(row) -> string` — `row.tenant_id || "mineria"`
  - `countBySeverity(machines) -> { operating, ok, observe, schedule, immediate }` (misma regla que el `useMemo` actual de `App.jsx`: severidad ausente = `ok`; `operating` cuenta `status === "operating"`)
  - `parkSummary(machines, incidents, tenants: string[]) -> { [tenant]: { machines, operating, immediate, schedule, observe, open } }` — `open` = incidentes con `status !== "closed"`; incluye cada tenant de `tenants` aunque tenga 0
  - `needsAttention(incidents, reports) -> { immediateNoTech: Inc[], awaitingApproval: Inc[], unreviewedReports: Rpt[] }` — abierto + `immediate` sin `tech_id`; abierto + `schedule` sin `tech_id`; reportes `status === "open"`
  - `techWork(incidents, reports, techId) -> { incidents: Inc[], reports: Rpt[], next: Inc | null, noTech: boolean }` — incidentes abiertos con `tech_id === techId` ordenados por `SEV_RANK` desc y luego `opened_at` asc; reportes no cerrados; `next` = primero; `noTech = !techId`
  - `lowStock(stock) -> Row[]` — filas con `below_reorder === true`
  - `machinesWithAlert(machines, n = 5) -> Machine[]` — severidad ≠ `ok`, por `SEV_RANK` desc, máx. `n`

- [ ] **Step 1: Write the failing tests** en `test/derive.test.js` (`import { test } from "node:test"; import assert from "node:assert/strict";`):

```js
test("parkSummary cuenta por parque y lleva a mineria lo que no tiene tenant", () => {
  const m = [{ tenant_id: "agro", severity: "schedule", status: "operating" }, { severity: "immediate", status: "idle" }];
  const i = [{ tenant_id: "agro", status: "open" }, { status: "closed" }, { status: "open" }];
  const s = parkSummary(m, i, ["mineria", "agro", "industria"]);
  assert.deepEqual(s.agro, { machines: 1, operating: 1, immediate: 0, schedule: 1, observe: 0, open: 1 });
  assert.deepEqual(s.mineria, { machines: 1, operating: 0, immediate: 1, schedule: 0, observe: 0, open: 1 });
  assert.deepEqual(s.industria, { machines: 0, operating: 0, immediate: 0, schedule: 0, observe: 0, open: 0 });
});
test("needsAttention separa inmediatos sin técnico, aprobaciones y reportes abiertos", () => {
  const inc = [{ incident_id: "A", status: "open", severity: "immediate" }, { incident_id: "B", status: "open", severity: "schedule" },
    { incident_id: "C", status: "open", severity: "schedule", tech_id: "TEC-01" }, { incident_id: "D", status: "closed", severity: "immediate" }];
  const r = needsAttention(inc, [{ report_id: "R1", status: "open" }, { report_id: "R2", status: "closed" }]);
  assert.deepEqual(r.immediateNoTech.map((x) => x.incident_id), ["A"]);
  assert.deepEqual(r.awaitingApproval.map((x) => x.incident_id), ["B"]);
  assert.deepEqual(r.unreviewedReports.map((x) => x.report_id), ["R1"]);
  assert.deepEqual(needsAttention([], []), { immediateNoTech: [], awaitingApproval: [], unreviewedReports: [] });
});
test("techWork ordena por severidad y fecha y tolera técnico ausente", () => {
  const inc = [{ incident_id: "X", status: "open", tech_id: "TEC-11", severity: "schedule", opened_at: "2026-11-15T10:00:00Z" },
    { incident_id: "Y", status: "open", tech_id: "TEC-11", severity: "immediate", opened_at: "2026-11-15T12:00:00Z" },
    { incident_id: "Z", status: "open", tech_id: "TEC-01", severity: "immediate", opened_at: "2026-11-15T09:00:00Z" }];
  const w = techWork(inc, [{ status: "in_review" }, { status: "closed" }], "TEC-11");
  assert.deepEqual(w.incidents.map((x) => x.incident_id), ["Y", "X"]);
  assert.equal(w.next.incident_id, "Y");
  assert.equal(w.reports.length, 1);
  assert.deepEqual(techWork(inc, [], null), { incidents: [], reports: [], next: null, noTech: true });
});
test("countBySeverity, lowStock y machinesWithAlert", () => {
  assert.deepEqual(countBySeverity([{ status: "operating" }, { severity: "observe" }]),
    { operating: 1, ok: 1, observe: 1, schedule: 0, immediate: 0 });
  assert.deepEqual(lowStock([{ part_id: "P1", below_reorder: true }, { part_id: "P2", below_reorder: false }]).map((s) => s.part_id), ["P1"]);
  const ms = [{ machine_id: "a", severity: "observe" }, { machine_id: "b", severity: "immediate" }, { machine_id: "c" }];
  assert.deepEqual(machinesWithAlert(ms).map((m) => m.machine_id), ["b", "a"]);
});
```

- [ ] **Step 2:** `cd gcp/console/web && npm test` → FAIL (`Cannot find module '../src/derive.js'`).
- [ ] **Step 3:** Implementar `derive.js` con las firmas de arriba (sin importar React ni `api.js`, para que corra en Node).
- [ ] **Step 4:** `npm test` → 4 pass; `npm run build` → `✓ built`.
- [ ] **Step 5:** Agregar el script `test` y el job `console-web` en `ci.yml`; commit `feat(console): logica de inicio por rol en funciones puras con tests (node --test) y job de CI`.

### Task 2: Base visual — tokens, tarjetas y color de parque

**Files:**
- Create: `gcp/console/web/src/ui.jsx`
- Modify: `gcp/console/web/src/api.js` (agregar `TENANT_COLOR` y `TENANT_NAME`)
- Modify: `gcp/console/web/src/styles.css` (tokens y clases nuevas al inicio; no borrar clases que usan `Panels.jsx`, `Reports.jsx`, `Planning.jsx`)

**Interfaces:**
- Produces:
  - `api.js`: `TENANT_COLOR = { mineria: "#f59e0b", agro: "#22c55e", industria: "#3b82f6" }`; `TENANT_NAME = { mineria: "Parque minero", agro: "Parque agrícola", industria: "Parque industrial" }`
  - `ui.jsx`: `Card({ title, action?, children, className? })`, `Kpi({ value, label, color?, onClick? })`, `Empty({ children, action? })` (texto explicativo + botón opcional `{ label, onClick }`), `Skeleton({ lines = 3 })`, `ParkTag({ tenant })` (punto + nombre corto con `TENANT_COLOR`), `Icon({ name, size = 18 })` con `name` ∈ `home, map, alert, report, stock, supply, plan, admin, more, back, logout`
  - CSS: `.card`, `.card-head`, `.kpi`, `.empty`, `.skeleton`, `.park-tag`, `.grid-cards` (auto-fit, min 280 px)

- [ ] **Step 1:** Agregar `TENANT_COLOR`/`TENANT_NAME` y `ui.jsx` con las firmas de arriba; tokens en `:root` según Global Constraints.
- [ ] **Step 2:** `npm run build` → `✓ built`; `npm test` sigue en verde.
- [ ] **Step 3:** Commit `feat(console): tokens, tarjetas, iconos y color por parque`.

### Task 3: Esqueleto — barra lateral, cabecera, barra inferior y pestaña Inicio

**Files:**
- Create: `gcp/console/web/src/Shell.jsx`
- Modify: `gcp/console/web/src/App.jsx` (reemplazar `<header className="top">…</header>` y el ruteo de pestañas)
- Modify: `gcp/console/web/src/styles.css` (`.app` pasa a grid `240px 1fr`; media queries 1100/768)

**Interfaces:**
- Consumes: `Icon`, `ParkTag` (Task 2); `TENANT_COLOR`, `TENANT_NAME`, `ROLE_LABEL` (`api.js`)
- Produces:
  - `Shell.jsx`: `Sidebar({ tabs, tab, onTab, badges, parks, park, onPark, user, onLogout })`, `Topbar({ title, subtitle, live: { error, updated, mock } })`, `BottomNav({ tabs, tab, onTab, badges })` (4 primeras + "Más" que abre la lista completa)
  - `App.jsx`: `ROLE_TABS` con `"home"` primero en todos los roles (tabla de la spec, sección "Navegación por rol"); `LABELS.home = "Inicio"`; `"supply"` (Abastecimiento, PR #34) se mantiene visible en los roles que #34 ya le dio (supervisor, admin, jefe) con ícono `supply`; `#po:`/`#trf:` siguen abriendo Abastecimiento
  - `badges = { incidents: abiertos, myincidents: míos abiertos, reports: needsAttention(...).unreviewedReports.length }`
  - El contenedor raíz lleva `style={{ "--park": TENANT_COLOR[park || parks.tenant_id] || TENANT_COLOR.mineria }}`

- [ ] **Step 1:** Implementar `Shell.jsx`. Bloque de parque: si `parks.global`, selector Todos/Minería/Agro/Industria (llama `onPark`, que hoy es `choosePark`); si no, tarjeta fija con `TENANT_NAME` y franja `--park`. Pie: rol, correo, `tech_id`, botón "Cambiar de rol / Salir" → `logout`.
- [ ] **Step 2:** En `App.jsx` montar `Sidebar` + `Topbar` (+ `BottomNav` visible solo < 768 px por CSS), pestaña inicial `home` si el hash no es una pestaña válida; los KPIs salen de la cabecera. Mientras no exista Task 4, `home` pinta `<p>Inicio</p>`.
- [ ] **Step 3:** Verificar con `MOCK=1` (ver "Cómo correr en local" en Task 6): los 7 usuarios mock entran, la pestaña `home` es la primera, `#planning`, `#machine:EX-017` y `#incident:<id>` con el operador abren lo esperado (Review Focus 5); `npm run build` en verde.
- [ ] **Step 4:** Commit `feat(console): barra lateral con parque, cabecera en vivo y barra inferior en celular`.

### Task 4: Inicio por rol

**Files:**
- Create: `gcp/console/web/src/Home.jsx`
- Modify: `gcp/console/web/src/App.jsx` (renderizar `<Home …/>` en `tab === "home"`)

**Interfaces:**
- Consumes: todo `derive.js` (Task 1); `Card`, `Kpi`, `Empty`, `Skeleton`, `ParkTag` (Task 2); `MapView` (existente, props `machines, selected, onSelect, zones, focusKey`); `api.reports(q)`, `api.stock()`, `api.kpis()`, `api.incident(id)`
- Produces: `Home({ user, machines, incidents, parks, park, onPark, onTab, onMachine, onIncident, onReport, loaded })` que despacha a `HomeOperator`, `HomeTechnician`, `HomeSupervisor` (supervisor y admin; admin agrega acceso a Administración), `HomeManagement`, `HomeMaintenanceHead`. `loaded=false` → `Skeleton`.

Contenido por rol (copiar los bocetos de la spec, sección "Inicio por rol"):
- **Operador:** botón grande "Reportar una falla" → `onTab("report")`; "Mis últimos reportes" = 3 primeros de `api.reports("mine=1")`; "Máquinas con alerta en mi parque" = `machinesWithAlert(machines)`. Vacío de reportes: "Todavía no enviaste reportes." con acción "Reportar una falla".
- **Técnico:** `techWork(incidents, api.reports("mine=1"), user.tech_id)`; si `noTech` → `Empty` "Tu cuenta no tiene técnico asignado. Pide al administrador que la vincule." (Review Focus 3); KPIs "N incidentes asignados" / "N reportes por revisar"; tarjeta "Siguiente" con botón Abrir → `onIncident(next.incident_id)`; "Repuestos del siguiente incidente" desde `api.incident(next.incident_id)` (`reservations`, o eventos `part.reserved` del `timeline`), una sola llamada por cambio de `next`.
- **Supervisor/Admin:** fila de `Kpi` de `countBySeverity` (clic en una severidad → `onTab("map")`); tarjeta "Requiere atención" con los tres grupos de `needsAttention` (reportes de `api.reports("")`) y la nota "La aprobación llega al correo del supervisor"; mapa mini (`MapView` en una `.card` de 320 px de alto); "Stock bajo" = `lowStock(api.stock())`, tarjeta omitida si la lista viene vacía; su acción abre Abastecimiento si el rol lo tiene (`onTab("supply")`), si no Stock.
- **Dirección:** `api.kpis()` con su `day` visible ("KPI del 14 nov"); incidentes abiertos por severidad; sin suma de `downtime_h`.
- **Jefe de mantenimiento:** si `park === ""`, tabla `parkSummary(machines, incidents, parks.scope)` (filas Máquinas/Operando/Inmediato/Programar/Abiertos; columnas con `ParkTag`; botón "Ver parque" → `onPark(t)`) + lista de incidentes abiertos con `ParkTag`; si hay `park`, `HomeSupervisor` de ese parque con enlace "← Comparar los tres parques" → `onPark("")`.
- Cada tarjeta con lista vacía usa `Empty` con un texto que explique cuándo se llena (Review Focus 1), p. ej. "Sin incidentes abiertos en el Parque agrícola. Cuando la telemetría de una máquina sale de su línea base, aparece aquí."

- [ ] **Step 1:** Implementar `Home.jsx` y conectarlo en `App.jsx`.
- [ ] **Step 2:** Verificar en mock: operador, técnico (TEC-01), supervisor, dirección, admin, `admin_agro` (sin incidentes → vacíos explicados) y jefe (tabla con 100/12/12 máquinas, "Ver parque" y volver).
- [ ] **Step 3:** `npm test` y `npm run build` en verde; commit `feat(console): inicio por rol (operador, tecnico, supervisor, direccion, jefe de mantenimiento)`.

### Task 5: Login de evaluación y README "Cómo evaluar"

**Files:**
- Create: `gcp/console/web/src/accounts.js`
- Modify: `gcp/console/web/src/Reports.jsx` (solo la función `Login`)
- Modify: `gcp/console/web/src/auth.js` (`loginCognito(hint?)` pasa `extraQueryParams: { login_hint: hint }` si viene)
- Modify: `README.md` (raíz): sección "Cómo evaluar"

**Interfaces:**
- Produces: `accounts.js`: `DEMO_ACCOUNTS: { tenant: "mineria"|"agro"|"industria"|"all", email: string, role: string }[]` — los 17 de `scripts/cognito-seed.sh` con `@example.com`, comentario `// Debe coincidir con scripts/cognito-seed.sh`.

- [ ] **Step 1:** `Login` nuevo: línea "FleetPulse — mantenimiento predictivo multi-nube (AWS + GCP)"; botón Ingresar (Hosted UI, igual que hoy); en modo Cognito, cuentas agrupadas por parque (`TENANT_NAME`, "Todos los parques" para `all`) con `ROLE_LABEL`; clic en una cuenta → `navigator.clipboard.writeText(email)` (en try/catch) y `loginCognito(email)`; texto "Contraseña: en el README del repo". En modo mock, la misma agrupación con `cfg.users` (por `tenant_id`, el jefe en "Todos") y clic → `loginMock(u)` como hoy.
- [ ] **Step 2:** README: URL de la consola, tabla de las 17 cuentas por parque y rol, contraseña `LaMotosierra123`, qué ve cada rol en una línea, y que el jefe de mantenimiento compara los tres parques.
- [ ] **Step 3:** Verificar en mock que cada botón entra con su rol; `npm run build` en verde. Commit `feat(console): login de evaluacion con cuentas por parque y README Como evaluar`.

### Task 6: Celular, paneles, revisión visual y PR

**Files:**
- Modify: `gcp/console/web/src/styles.css`
- Modify: `gcp/console/web/src/Panels.jsx` y `Reports.jsx` solo si hace falta un botón "Volver" en los paneles (mismo `onClose`)

- [ ] **Step 1:** < 768 px: `.drawer`/paneles a pantalla completa con "Volver" (= `onClose`); tablas `.grid` con scroll horizontal propio dentro de su tarjeta; formulario de reporte a ancho completo; sin scroll horizontal de página a 360 px (Review Focus 4).
- [ ] **Step 2:** Cómo correr en local: `cd gcp/console && MOCK=1 STATIC_DIR=web/dist uvicorn app.main:app --port 8080` tras `npm run build` (o `npm run dev` con el proxy de `vite.config.js`). Recorrer los 7 usuarios mock a 1440 y 390 px y guardar capturas.
- [ ] **Step 3:** `gcp/console/tests` (22) con el venv de la consola, `npm test`, `npm run build`: todo verde.
- [ ] **Step 4:** Push de `feat/console-ux`, PR con capturas y pedido de revisión a Alex (dueño de la consola). Tras el merge: `gcp/console/connect_aws.py` y comprobar en vivo `admin.agro` 12, `supervisor` 100, `jefe.mantenimiento` 124 máquinas y que la tabla del jefe cuadra.
