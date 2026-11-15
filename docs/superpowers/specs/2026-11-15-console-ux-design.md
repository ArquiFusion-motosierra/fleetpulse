# Consola FleetPulse: esqueleto nuevo e inicio por rol (diseño)

Fecha: 2026-11-15 · Rama: `feat/console-ux` · Alcance: `gcp/console/web` (frontend). Dueño de la consola: Alex; lo implementa Benjamin.

## Por qué

El jurado tiene acceso a los repositorios y a los servicios en la nube y va a **entrar solo, sin guía**, a la consola
desplegada (https://console-qgbfzxdq2a-ue.a.run.app). Hoy la consola funciona, pero no se explica sola:

- una sola barra superior mezcla marca, parque, pestañas, KPIs, estado y usuario; con 5-6 pestañas se desborda;
- los roles solo cambian qué pestañas se ven (`ROLE_TABS`): el operador cae en un formulario y el técnico en una tabla;
- el multi-tenancy (lo que más queremos que se note) es un `<select>` para el jefe y una etiqueta chica para el resto;
- no hay diseño para celular, aunque operador y técnico son los usuarios de terreno.

**Éxito:** alguien que entra por primera vez entiende en segundos qué es FleetPulse, qué rol tiene, en qué parque
está y qué puede hacer; puede cambiar de rol/parque para ver la diferencia; y ve datos vivos.

## Objetivos

1. Esqueleto nuevo: barra lateral de navegación + cabecera delgada con el contexto (parque, usuario, estado en vivo).
2. Una pantalla **Inicio** distinta por rol, que responde "¿qué me toca hacer ahora?".
3. El parque, protagonista: color e identidad por parque; el jefe de mantenimiento compara los tres.
4. Login que explique los roles y parques de prueba, para que el jurado cambie de rol fácilmente.
5. Usable en celular (≥ 360 px) para operador y técnico; escritorio para el resto.

## No objetivos

- No se cambia el BFF (`gcp/console/app`) ni AWS: todo lo nuevo se calcula en el navegador con las rutas existentes
  (`/api/machines`, `/api/incidents`, `/api/reports`, `/api/stock`, `/api/kpis`, `/api/tenants`, ...).
- No se reescriben los paneles de detalle (`MachinePanel`, `IncidentPanel`, `ReportPanel`), el mapa (`MapView`), el
  formulario de reporte, Planificación ni Administración: se reutilizan y solo se ajusta su estilo.
- No se agrega librería de componentes ni CSS framework (se descartó la opción C).
- No se implementa la pestaña Abastecimiento (T41); solo se deja su lugar en la navegación.

## Anti-objetivos (sería un fracaso aunque funcione)

- Romper un flujo ya probado: login Cognito, reporte del operario, chequeos del técnico, cierre con ID token,
  aislamiento por parque, enlaces directos `#machine:…`, `#incident:…`, `#report:…`, `#<pestaña>`.
- Mostrar datos de otro parque a un usuario de parque (la consola solo pinta lo que el BFF devuelve; no se agrega
  ningún filtro del lado del cliente que se pueda saltar).
- Pantallas vacías o con "Cargando…" eterno: cada vista tiene estado vacío con una explicación y un siguiente paso.

## Restricciones

- Mismo stack: React 18 + Vite 5 + MapLibre 4 + `oidc-client-ts`; CSS propio en `styles.css` con tokens en `:root`.
- El build (`npm run build`) y los 22 tests de la consola (`gcp/console/tests`) deben seguir pasando; el CI corre ambos.
- Despliegue igual que hoy: `gcp/console/connect_aws.py` (Cloud Run, Free Trial; nada nuevo en GCP).
- Tiempo: ~3-4 h. Se entrega en un PR que Alex pueda revisar.

## Enfoques considerados

| | Enfoque | A favor | En contra |
|---|---|---|---|
| A | Pulido incremental (CSS, cabecera, responsive) | 1-2 h, sin riesgo | los roles se siguen sintiendo iguales |
| **B** | **Esqueleto nuevo + Inicio por rol** (elegido) | cuenta la historia de roles y parques sin narrador; reutiliza todo lo que funciona | 3-4 h; toca `App.jsx` y estilos |
| C | Reescritura con librería de componentes | lo más pulido | reescribe 1.500 líneas probadas a horas de la entrega |

Elegido **B** por el usuario.

## Diseño

### Esqueleto

```
┌──────────────┬──────────────────────────────────────────────────────────┐
│ ◉ FleetPulse │ Parque agrícola · Fundo Valle Sur        ● en vivo 13:42  │
│              ├──────────────────────────────────────────────────────────┤
│ [Agro ▾]     │                                                          │
│  (color del  │                     contenido de la vista                │
│   parque)    │                                                          │
│ ⌂ Inicio     │                                                          │
│ ◎ Mapa       │                                                          │
│ ⚠ Incidentes │                                                          │
│ ✎ Reportes   │                                                          │
│ ▦ Stock      │                                                          │
│ ◷ Planific.  │                                                          │
│ ⚙ Admin      │                                                          │
│ ─────────────│                                                          │
│ Administrador│                                                          │
│ admin.agro@… │                                                          │
│ [Salir]      │                                                          │
└──────────────┴──────────────────────────────────────────────────────────┘
```

- **Barra lateral** (240 px; colapsa a íconos entre 768 y 1100 px; en celular pasa a barra inferior con las 4
  primeras entradas + "Más"). Arriba la marca y el **bloque de parque**: para un usuario de parque, una tarjeta fija
  con el nombre del parque y su color; para el jefe de mantenimiento, un selector "Todos / Minería / Agro / Industria".
  Abajo, rol + correo + Salir. Contadores (incidentes abiertos, reportes sin revisar) como insignias en cada entrada.
- **Cabecera** delgada: operación y parque actual, estado en vivo (punto verde + hora de la última lectura; rojo
  "sin conexión" si falla), etiqueta "datos de prueba" en modo mock. Los KPIs salen de la cabecera y pasan a Inicio.
- **Color por parque** (token `--park`): Minería ámbar `#f59e0b` (el acento actual), Agro verde `#22c55e`, Industria
  azul `#3b82f6`. Se usa en el bloque de parque, en el borde superior de la cabecera y en las etiquetas de parque de
  tablas/listas del jefe. La severidad mantiene sus colores (`SEVERITY`); el parque nunca usa rojo.
- **Paneles de detalle**: siguen siendo un panel lateral derecho en escritorio; en celular ocupan la pantalla
  completa con botón "Volver".

### Navegación por rol

`Inicio` es la primera entrada para todos. El resto mantiene las vistas de hoy:

| Rol | Entradas |
|---|---|
| Operador | Inicio, Reportar, Mis reportes, Mapa |
| Técnico | Inicio, Mis incidentes, Reportes asignados, Mapa, Stock |
| Supervisor | Inicio, Mapa, Incidentes, Reportes, Stock, Planificación |
| Dirección | Inicio, Planificación, Mapa, Incidentes |
| Admin | Inicio, Mapa, Incidentes, Reportes, Stock, Planificación, Administración |
| Jefe de mantenimiento | Inicio, Mapa, Incidentes, Reportes, Stock, Planificación |

Una entrada "Abastecimiento" queda declarada pero oculta hasta T41 (`LABELS`/`ROLE_TABS` con un flag).
La URL sigue usando `#<pestaña>`; se agrega `#home`.

### Inicio por rol

Cada Inicio = saludo con rol y parque + 1 acción principal + 2-4 tarjetas. Todo con datos que la consola ya carga.

**Operador** (pensado para celular)
```
Hola, Operario · Parque agrícola
┌──────────────────────────────────────┐
│  ⚠  Reportar una falla               │  ← botón grande, abre Reportar
│     Lo revisa un técnico con la      │
│     telemetría y la ficha del        │
│     fabricante                        │
└──────────────────────────────────────┘
Mis últimos reportes                      (3 últimos de /reports?mine=1, con estado)
Máquinas con alerta en mi parque          (machines con severidad ≠ ok, máx. 5)
```

**Técnico** (celular y escritorio)
```
Mi trabajo de hoy · TEC-11
[2 incidentes asignados] [1 reporte por revisar]
Siguiente: INC-CS-003-… Cosechadora · Bomba hidráulica · Inmediato   [Abrir]
Lista: incidentes asignados abiertos (por severidad) y reportes asignados abiertos
Repuestos del siguiente incidente  (solo del "Siguiente": 1 llamada a /api/incidents/{id}, que trae reservas y timeline)
```

**Supervisor / Admin** — "El parque de un vistazo"
```
[Operando 96] [Inmediato 1] [Programar 3] [Observar 5]          ← tarjetas KPI clicables (filtran el mapa)
┌ Requiere atención ───────────────┐ ┌ Mapa mini (MapView) ──────────┐
│ • Inmediato sin técnico (0)      │ │                               │
│ • Esperando aprobación (2)       │ │                               │
│   = abiertos, severidad schedule,│ │                               │
│     sin tech_id                  │ └───────────────────────────────┘
│ • Reportes sin revisar (3)       │ Stock bajo: PRT-04 en FUNDO-A (1)
└──────────────────────────────────┘
```
"Esperando aprobación" se deriva en el cliente: incidente `open`, `severity === "schedule"`, sin `tech_id`. La
aprobación sigue siendo por el enlace del correo; la tarjeta explica eso ("el supervisor aprueba desde el correo").
Admin agrega un acceso a Administración.

**Dirección**: los KPI del último día con datos (`/api/kpis`, normalmente ayer; se muestra la fecha) + incidentes
abiertos ahora por severidad. Las horas detenidas salen del payload de KPI si lo trae; no se suman en el cliente
(la lista está limitada a 100). *(decisión del implementador)*

**Jefe de mantenimiento** — "Los tres parques"
```
            Minería        Agro          Industria
Máquinas      100            12              12
Operando       96            11              10
Inmediato       1             0               0
Programar       3             1               0
Abiertos        4             1               1
            [Ver parque]  [Ver parque]   [Ver parque]   ← fija el selector en ese parque
```
Al elegir un parque ("Ver parque" o el selector), Inicio muestra el Inicio de supervisor de ese parque con un enlace
"← Comparar los tres parques"; con "Todos", la tabla comparativa. Las demás vistas ya se filtran por el selector.
*(decisión del implementador)*

Se calcula con una sola carga sin filtro (el BFF ya devuelve los tres parques al jefe) agrupando por `tenant_id`.
Debajo, la lista de incidentes abiertos de todos los parques con la etiqueta de color del parque.

### Login

La pantalla de login explica el producto en una línea y muestra **quién puede entrar y qué ve**:

```
FleetPulse — mantenimiento predictivo multi-nube (AWS + GCP)
[ Ingresar ]   → Hosted UI de Cognito (sin cambios)

Cuentas de evaluación (contraseña: en el README del repo)
Parque minero     operador@  tecnico@  supervisor@  direccion@  admin@
Parque agrícola   operador.agro@  tecnico.agro@  …
Parque industrial operador.industria@ …
Todos los parques jefe.mantenimiento@
```
En modo Cognito la lista se escribe en el frontend (no hay ruta que la entregue) y es exactamente la que sembró
`scripts/cognito-seed.sh` en el pool en vivo el 2026-11-15 con el dominio por defecto `example.com` (los 17 existen;
verificado): `operador@ tecnico@ supervisor@ direccion@ admin@ admin.mineria@` (minería), `{operador,tecnico,supervisor,
direccion,admin}.agro@`, `{…}.industria@` y `jefe.mantenimiento@`. Se define en una constante junto a `cognito-seed.sh`
como fuente (comentario que diga que deben coincidir).

Cada correo es un botón que lo copia al portapapeles y abre el Hosted UI (si Cognito acepta `login_hint`, se lo pasa;
si no, solo copia). **La contraseña no se muestra en la página pública** (ver "Pendiente de decidir").
"Cambiar de rol" en la barra lateral = Salir + volver a este login.

En modo mock la lista ya existe (`cfg.users`); se agrupa por parque con el mismo diseño.

### Estados vacíos y carga

- Esqueletos grises (no spinners) en las tarjetas mientras llega la primera respuesta.
- Vacío con explicación: "Sin incidentes abiertos en el Parque agrícola. Cuando la telemetría de una máquina sale de
  su línea base, aparece aquí." / "Todavía no enviaste reportes. [Reportar una falla]".
- Error de red: la franja roja actual, con el texto "Reintentando cada 15 s".

### Estilo

- Se mantiene el tema oscuro y los tokens actuales; se agregan `--park`, `--space-*` (4/8/12/16/24/32) y
  `--shadow-1`, y tipografía: títulos 20/16 px, cuerpo 14 px, números tabulares.
- Tarjeta base `.card` (panel, borde, radio 12 px, padding 16 px) reutilizada en Inicio, Stock y Planificación.
- Foco visible en todo lo navegable con teclado; contraste AA en texto sobre `--panel`.

### Archivos

| Archivo | Cambio |
|---|---|
| `web/src/App.jsx` | esqueleto (barra lateral + cabecera), `ROLE_TABS` con `home`, estado de parque, ruteo por hash |
| `web/src/Shell.jsx` (nuevo) | `Sidebar`, `Topbar`, `ParkBlock`, `BottomNav` |
| `web/src/Home.jsx` (nuevo) | `HomeOperator`, `HomeTechnician`, `HomeSupervisor` (admin), `HomeManagement`, `HomeMaintenanceHead` |
| `web/src/ui.jsx` (nuevo) | `Card`, `Kpi`, `Empty`, `Skeleton`, `ParkTag` |
| `web/src/Reports.jsx` | solo `Login` (nuevo diseño); el resto sin cambios de lógica |
| `web/src/api.js` | `TENANT_COLOR`; sin cambios en las llamadas |
| `web/src/styles.css` | tokens nuevos, esqueleto, responsive, tarjetas |
| `README.md` (raíz) o `docs/` | sección "Cómo evaluar" con URL, cuentas y contraseña |

### Verificación

- `npm run build` y `gcp/console/tests` (22) en verde.
- Modo mock (`MOCK=1`): recorrer los 6 roles + `admin_agro`, en 1440 px y 390 px, con capturas.
- En vivo tras el deploy: `admin.agro`, `supervisor`, `jefe.mantenimiento` — mismo conteo de máquinas que hoy
  (12 / 100 / 124) y el comparativo del jefe cuadra con esos números.

## Lo que queda al implementador

- Íconos: SVG inline propios (sin dependencia); estilo de línea simple.
- Umbral de "stock bajo" en Inicio: `qty_available <= reorder_point` si el dato existe en `/api/stock`; si no, se omite
  la tarjeta.
- Orden exacto de tarjetas y textos de estados vacíos.
- Si `login_hint` no funciona con el Hosted UI, los botones solo copian el correo.

## Decidido por el usuario

- La contraseña de las cuentas de evaluación va **solo en el README** (sección "Cómo evaluar"); la pantalla de login
  dice "contraseña en el README del repo" y nunca la muestra.
