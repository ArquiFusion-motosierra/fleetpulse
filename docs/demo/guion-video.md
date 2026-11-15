# Guion del video de la demo (T43)

Borrador para que lo lean y ajusten Benjamin (AWS), Alex (consola y simulador) y Fabricio (GCP). Cada bloque se graba por separado (T23, Dom 18:00-20:00) y el video final debe durar entre 4:15 y 4:45 (issues #20 a #23 del backlog). Las frases son literales pero cortas: se leen a unas 2,3 palabras por segundo, así que no les agreguen texto sin quitar otro tanto.

Fuentes: `docs/superpowers/plans/2026-11-15-repuestos-abastecimiento.md` (sección 2), `docs/architecture.md`, `README.md` (Cómo evaluar), `scripts/seed-demo.py`, `scripts/smoke_supply.py`, `scripts/smoke_incidents.py` y el documento técnico de diseño. Las contraseñas no van aquí: están en el README.

## 1. Resumen

Una flota de 124 máquinas en 3 parques se degrada, FleetPulse detecta, aprueba, explica y repone el repuesto que falta, y el mismo incidente vive en AWS y en GCP. Duración objetivo: **4:30 ± 0:15**. La tabla suma 4:30 exactos.

Criterio de producción (del documento de diseño): un solo recorrido, sin láminas que se titulen con el nombre de un criterio. Cada título en pantalla es un mensaje, no el nombre de un criterio ("Una bomba que no teníamos, en otro parque", no "Multi-tenancy").

## 2. Tabla de bloques

| Tiempo | Qué se ve en pantalla (rol, pestaña, acción) | Qué se dice | Rúbrica |
|---|---|---|---|
| **B1** 0:00-0:25 | Consola como `supervisor@example.com`, **Inicio**: mapa con 124 máquinas, tres parques (minería 100, agro 12, industria 12), colores por severidad. Zoom lento a EX-017 | "Una máquina detenida frena toda la operación. Tenemos 124 máquinas en tres parques. ¿Cuál falla primero, y tenemos el repuesto cuando falla?" | Problema (1, 3, 4, 13) |
| **B2** 0:25-0:50 | Misma consola, mapa en vivo: la telemetría llega cada minuto y EX-017 sube de amarillo a rojo. Ficha de EX-017 con las variables desviadas (presión hidráulica, temperatura, vibración) | "Cada máquina manda su telemetría cada minuto. Ninguna variable sola alarma; la combinación y la tendencia sí. Gravedad en tres niveles." | Problemas 1 y 4, serverless, IA (score) |
| **B3** 0:50-1:30 | `supervisor@`, **Incidentes**: S1 en EX-017 (bomba hidráulica) ya escalado a inmediato. Corte a Step Functions `incident-flow` con el recorrido completo. Correo del supervisor con enlace de aprobación y clic (en un incidente pendiente, ver nota 3 de la sección 5). Vuelta a la consola: técnico TEC-01 asignado, explicación de Gemini con informe técnico, reserva de PRT-03 y PRT-04 en falta | "Si la falla es inmediata, se asigna sola; si no, el supervisor aprueba con un clic. Elegimos al técnico por distancia y especialidad. Gemini explica la causa. Reservamos los sellos, pero la bomba no está." | Step Functions, problemas 5 y 9, IA (Gemini) |
| **B4** 1:30-1:55 | `operador@example.com`, **Inicio**: botón "Reportar falla" en una máquina. Aparecen 3 o 4 chequeos recomendados por Gemini. Corte a `tecnico@example.com`: ve los chequeos y cierra el reporte | "El operario no espera al sensor: reporta lo que ve. Gemini recomienda los chequeos en segundos, se abre el incidente y el técnico lo cierra." | Problema 9, IA, roles |
| **B5a** 1:55-2:15 | `jefe.mantenimiento@example.com`, **Abastecimiento**: mapa de calor repuesto por almacén de los 3 parques, PRT-04 en rojo en minería y verde en agro (3 en FUNDO-A). Tarjeta de sugerencia con dos opciones: transferir 1 desde FUNDO-A, o comprar a proveedor, y "evita 152 h de parada, USD 68.400" | "No compramos lo que ya tenemos en otro parque. Transferir la bomba desde agro, o comprarla: el sistema compara las dos opciones y calcula la parada evitada." | Problema 3, multi-tenant |
| **B5b** 2:15-2:40 | Mismo rol: clic en **Aprobar**. Corte a Step Functions `supply-flow`: estaba detenido en `WaitForApproval`, avanza a `Ship`; el stock de agro baja y `in_transit` sube | "Cada orden es una máquina de estado con aprobación humana. Aprobamos y la bomba sale de agro." | Step Functions, persistencia |
| **B5c** 2:40-3:05 | `admin@example.com` (minería): **Abastecimiento**, "Confirmar recepción" (la espera real de unos 60 s se corta en edición). `supply-flow` pasa por `Receive` y `AllocateBackorders`; PRT-04 queda reservado para EX-017; correo "tu repuesto llegó" al técnico | "La recepción reserva sola el repuesto para quien lo esperaba. El técnico recibe el aviso y EX-017 vuelve a operar." | Problemas 3 y 9 |
| **B5d** 3:05-3:25 | `admin.agro@example.com`: ve 12 máquinas y solo su stock y la transferencia saliente; al pedir minería, 403. Luego `jefe.mantenimiento@`: tabla comparando los 3 parques | "Cada parque administra lo suyo. El jefe de mantenimiento ve los tres." | Seguridad, multi-tenant |
| **B5e** 3:25-3:45 | `jefe.mantenimiento@`, **Abastecimiento**, panel de KPIs: quiebres de stock por día, días de cobertura y parada evitada acumulada (152 h, unos USD 68.400 por esta transferencia) | "Una sola transferencia evitó 152 horas de parada, unos 68 mil dólares." | Problema 3, persistencia |
| **B6** 3:45-4:10 | Pantalla dividida: ejecución de Step Functions (AWS) con el `incident_id` de EX-017 y, al lado, BigQuery `domain_events` con el mismo `incident_id` (consulta ya preparada, sin escanear de más). Luego la respuesta de insights `/supply/analytics` | "El mismo incidente vive en dos nubes. AWS decide y orquesta; GCP guarda la historia, entrena y explica. Llega a BigQuery en menos de dos minutos, por lotes, sin streaming." | 2 nubes (momento clave), persistencia, IA |
| **B7** 4:10-4:30 | Una lámina o la tabla del README: roles (operario, técnico, supervisor, dirección, admin, jefe), costo (Free Trial de GCP, Learner Lab, todo serverless, `maximum_bytes_billed` en cada consulta) y dos líneas de lo que no cumplimos | "Cuatro roles, tres parques, todo serverless y a costo cero en la hackatón. Lo que no hicimos: mínimo privilegio en AWS, porque el Learner Lab solo ofrece LabRole." | Costos, buenas prácticas, honestidad |

Suma: 25 + 25 + 40 + 25 + 20 + 25 + 25 + 20 + 20 + 25 + 20 = 270 s = **4:30**. Bloque B5 completo: 1:50 (condensa los 2:30 del diseño; se cae la variante de `daily-planning` y las líneas de tiempo repetidas, porque B3 ya mostró `part.reserved` y `part.shortage`).

Reglas de edición:
- El `incident_id` de EX-017 es visible en cada toma de B3, B5 y B6.
- Los cortes se hacen sobre esperas reales, nunca sobre pantallas inventadas. La recepción (~60 s) se corta en B5c; la aprobación (~8 s) y la sugerencia (~25 s después del faltante) se muestran enteras o con un corte de reloj visible.
- Se mide la duración en el archivo final, no en el editor.

## 3. Quién graba cada bloque

T23 graba por bloques. Cada quien graba pantalla y voz de los suyos; Fabricio edita.

| Bloque | Voz y pantalla | Cuenta o pestaña | Insertos que graba otro |
|---|---|---|---|
| B1 gancho y flota | Benjamin | `supervisor@example.com`, Inicio | Ninguno |
| B2 detección | Alex | `supervisor@example.com`, mapa y ficha de EX-017 | Ninguno |
| B3 incidente y Gemini | Benjamin | `supervisor@example.com`, Incidentes; consola de Step Functions en AWS; correo de aprobación | Alex: la ficha del incidente en la consola |
| B4 reporte del operario | Alex | `operador@example.com` y luego `tecnico@example.com` | Ninguno |
| B5a-B5b sugerencia y aprobación | Alex (voz y consola), Benjamin (inserto de `supply-flow`) | `jefe.mantenimiento@example.com`, Abastecimiento | Benjamin: grafo de Step Functions |
| B5c recepción | Alex | `admin@example.com` (minería) | Benjamin: grafo en `Receive` y `AllocateBackorders` |
| B5d-B5e multi-tenant y KPIs | Alex | `admin.agro@example.com`, `jefe.mantenimiento@example.com` | Ninguno |
| B6 dos nubes | Fabricio | BigQuery, insights, y el `incident_id` en AWS que le pase Benjamin | Benjamin: la ejecución en Step Functions |
| B7 roles, costos y cierre | Fabricio | Lámina o README | Ninguno |

Los bloques B5a a B5c son una sola toma continua y **se graban una sola vez**: aprobar la transferencia consume la historia (ver sección 4).

## 4. Checklist de 15 minutos antes de grabar

Todo en `/Users/btoro/Documents/UTEC/Cloud/proyectos/fleetpulse`, no desde un worktree de trabajo. No se despliega desde la Mac: el CI lo hace con los secretos del Learner Lab. Durante la grabación **no se pushea a `aws/`, `simulator/` ni `contracts/`**, porque un deploy deja el simulador apagado.

1. **Learner Lab activo (Benjamin).** Sesión abierta y credenciales cargadas:
   ```bash
   scripts/lab-creds.sh
   aws sts get-caller-identity --region us-east-1
   ```
2. **Disparadores encendidos (Benjamin).** Al vencer la sesión del lab quedan en Disabled. Deben estar Enabled los 6: tres colas (`registry-q`, `gcp-events-q`, `supply-q`) y tres streams (`fp-state` hacia `archiver`, `fp-state` hacia `score`, `fp-outbox` hacia `outbox-publisher`):
   ```bash
   aws lambda list-event-source-mappings --region us-east-1 \
     --query 'EventSourceMappings[].[FunctionName,EventSourceArn,State]' --output table
   ```
3. **Consola e insights en la revisión correcta (Alex y Fabricio).** Compararla con la que quedó tras el último merge a `main` (anotarla en el chat antes de empezar):
   ```bash
   gcloud run services describe console  --project fleetpulse-g19-2026 --region us-east1 --format='value(status.latestReadyRevisionName)'
   gcloud run services describe insights --project fleetpulse-g19-2026 --region us-east1 --format='value(status.latestReadyRevisionName)'
   curl -s https://console-qgbfzxdq2a-ue.a.run.app/health
   ```
   La consola grabada **no** debe ser la versión `MOCK=1`: la regla del equipo es no fabricar pantallas que el sistema no produjo.
4. **Simulador y escenarios.** Simulador encendido y escenarios S1, S8 y S9 activos (S8 y S9 dan severidad en vivo a agro e industria):
   ```bash
   scripts/simulator.sh on
   scripts/simulator.sh status
   scripts/drift.py status
   ```
   Si falta alguno: `scripts/drift.py on S1` (y S8, S9). No encender S2 ni otros: ensucian el mapa de B1 y B2.
5. **Datos de la demo.** La contraseña común entra solo por el entorno, nunca por la línea de comandos ni en archivos:
   ```bash
   read -rs FP_TEST_PASSWORD && export FP_TEST_PASSWORD
   AWS_CONFIG_FILE=/dev/null AWS_REGION=us-east-1 .venv/bin/python scripts/seed-demo.py verify
   ```
   `verify` debe terminar en `seed-demo: VERDE`, con la TRF de PRT-04 sugerida (`TRF-FUNDO-A-NORTE-PRT-04-20261115T2136`), PRT-04 en 0 en NORTE, CENTRO y SUR y el BACKORDER# de EX-017 esperando. Si avisa que no hay TRF sugerida, la historia está consumida: ver "Si se consumió la historia" más abajo.
6. **Incidentes pendientes de aprobación, entre 5 y 25 minutos antes de B3.** Deja 3 incidentes esperando aprobación en CF-032 (minería), CS-003 (agro) y GE-003 (industria). **Vencen a los 30 minutos**: ejecutar de nuevo si pasó más tiempo.
   ```bash
   AWS_CONFIG_FILE=/dev/null AWS_REGION=us-east-1 .venv/bin/python scripts/seed-demo.py pending
   ```
7. **Flota viva.** En la salida de `verify`, las tres líneas de "flota en operación" deben mostrar todas las máquinas con lectura de los últimos 3 minutos. El mapa de B1 debe mostrar 124 máquinas (100, 12 y 12).
8. **Cuentas.** Una pestaña o ventana de navegador por cuenta, ya logueada, con la contraseña del README: `supervisor@`, `operador@`, `tecnico@`, `admin@`, `admin.agro@` y `jefe.mantenimiento@` (todas `@example.com`). Cambiar de rol es "Cambiar de rol / Salir" en la barra lateral.
9. **Correo de aprobación.** Confirmar que el supervisor recibió el enlace del incidente pendiente y que abre la página de confirmación sin aprobar todavía.
10. **Prueba en seco, solo lectura.** Para mirar el flujo sin gastar la historia:
    ```bash
    scripts/smoke_supply.py --base https://console-qgbfzxdq2a-ue.a.run.app --read-only
    ```
    (necesita los ID tokens en `FP_TOKEN_HEAD`, `FP_TOKEN_ADMIN` y `FP_TOKEN_ADMIN_AGRO`, copiados de `sessionStorage` `fp.session`).
11. **Grabación.** OBS a 1080p, notificaciones del sistema apagadas, `incident_id` visible en pantalla.

**No ensayar la aprobación de la TRF ni la recepción.** Eso consume la historia. El ensayo cronometrado (T22) se hace con la lectura y el guion, y la aprobación real se hace una sola vez, grabando.

**Si se consumió la historia** (alguien aprobó la TRF): se rearma con `scripts/seed-registry.py --demo-supply` (PRT-04 en 0 en minería) y un incidente nuevo en EX-017 (`S1`, o `scripts/smoke_incidents.py s1 --keep-open`), y luego `verify` otra vez. El `incident_id` de las tomas anteriores cambia: regrabar B3, B5 y B6 juntos.

## 5. Plan B por bloque

Regla general: cada pantalla se captura **hoy**, apenas funciona, y se guarda fuera del repo (`recordings/`). Si algo falla a la hora de grabar, se usa la toma real anterior del mismo flujo. Nunca se fabrica una pantalla que el sistema no produjo, y no se usa la consola `MOCK=1`.

| Bloque | Captura de respaldo para tomar hoy | Qué decir si algo falla en vivo |
|---|---|---|
| B1 | Pantalla del mapa con las 124 máquinas y las tres severidades | "Esta es una captura de la flota en operación" (se dice en voz, no se oculta) |
| B2 | Clip de EX-017 pasando de amarillo a rojo con S1 activo (tarda unos 5-11 min en cruzar) | Si el mapa no cambia a tiempo, usar el clip y decir "así se vio cuando EX-017 cruzó el umbral" |
| B3 | Ejecución completa de `incident-flow` de S1, el correo de aprobación con su enlace y la explicación de Gemini (~11 s). Si Gemini no responde, la plantilla de respaldo es parte del diseño: mostrarla y decir "si Gemini tarda, el incidente sale con explicación por plantilla y nunca se bloquea" | Mismo texto que arriba |
| B4 | Clip del reporte, de los chequeos de Gemini (2 a 5 s) y del cierre por el técnico | Si Gemini falla, el reporte usa la plantilla del catálogo: "los chequeos salen del catálogo cuando Gemini no responde" |
| B5a-B5c | Clip continuo de la aprobación, `supply-flow` en `Ship`, la recepción y `AllocateBackorders`; capturas del mapa de calor y de la tarjeta de sugerencia **antes** de aprobar | Si la transferencia no se puede aprobar en vivo, usar el clip del smoke y decir "esta es la misma historia corrida hoy a las [hora]" |
| B5d | Capturas de `admin.agro` con 12 máquinas y del 403 al pedir minería; tabla del jefe con los tres parques | Si el 403 no aparece, mostrar el test o la captura |
| B5e | Captura de los KPIs (152 h, unos USD 68.400) | Los números salen de `/supply/analytics` (2,7 s): se puede mostrar la respuesta cruda |
| B6 | Captura del mismo `incident_id` en Step Functions y en BigQuery `domain_events`, y de `/supply/analytics` | Si BigQuery tarda más de 2 minutos: "llega por lotes, no por streaming" y usar la captura. Si no hay filas, usar la toma anterior; nunca insertar datos a mano |
| B7 | Lámina de costos y alerta de presupuesto (ver nota 2) | Sin cambios: es una lámina |

Notas:
1. Si el Learner Lab expira durante la grabación, rehacer los pasos 1 y 2 del checklist y verificar que el simulador sigue encendido antes de seguir.
2. Cifra de la alerta de presupuesto: `docs/architecture.md` dice S/ 50 al mes y el equipo comentó S/ 900. Confirmar cuál está configurada en Billing antes de decirla en voz; si no hay certeza, decir solo "alerta de presupuesto" y `maximum_bytes_billed` en cada consulta.
3. El incidente de EX-017 de la historia ya está armado y no se vuelve a disparar. El clic de aprobación de B3 se hace sobre uno de los tres incidentes pendientes (por ejemplo CF-032), porque S1 en EX-017 escala a inmediato y no espera aprobación. Confirmar con Benjamin que así se quiere contar.

## 6. Después de grabar

Apagar todo para que el consumo quede en cero. La regla del simulador es que ni el simulador ni los escenarios queden encendidos fuera de las pruebas.

```bash
scripts/drift.py off all
scripts/drift.py status
scripts/simulator.sh off
scripts/simulator.sh status
```

Además:
- Confirmar que la regla de `daily-planning` sigue deshabilitada (la checklist de T25 lo exige junto con el simulador apagado).
- No ejecutar `seed-demo.py teardown` hasta tener todos los bloques aprobados: cierra lo que creó la corrida y no se puede repetir una toma sin rearmar la historia.
- Guardar las tomas originales y el `incident_id` usado en `recordings/` (fuera del repo), por si hay que re-editar.
- Avisar en el issue #40 que el simulador quedó apagado y a qué hora.
