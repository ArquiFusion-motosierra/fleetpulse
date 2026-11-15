# Pitch de FleetPulse (video, 4:30)

Borrador para el ensayo (T22), para que el equipo lo ajuste. Adapta `docs/demo/guion-video.md` (Benjamin) de recorrido de
pantallas a **pitch**: gancho → problema con nombre y cifra → idea en una frase → demo como prueba → pasada por la
rúbrica → cierre que vuelve al gancho. La logística (checklist de 15 min, cuentas, plan B por bloque y apagado) sigue
siendo la de `docs/demo/guion-video.md` secciones 4 a 6: aquí no se repite.

- **Límite:** 5:00 descalifica. Objetivo **4:30**; si el ensayo pasa de 4:45, se corta la lámina de arquitectura, nunca la demo.
- **Ritmo:** ~2,3 palabras por segundo. Cada bloque trae su presupuesto de palabras: si alguien agrega texto, quita otro tanto.
- **Rúbrica (20 pts, documento de diseño):** problemas resueltos 3 · persistencia y HA 2 · Step Functions 2 · 2+ nubes 3 ·
  arquitectura 3 · pitch 2 · serverless 2 · IA 3. Ninguna lámina se titula con el criterio: el título es un mensaje.
- **Criterio del pitch, textual:** *"Pitch de 5 minutos vendiendo y explicando rápidamente su idea"*. Dos exigencias:
  **vender** (valor en dinero y en horas, por qué ArquiFusion lo pagaría) y **explicar rápido** (la idea entendida en
  el primer minuto, antes de cualquier pantalla técnica). Prueba: al minuto 1:00, alguien que no vio nada más debe
  poder decir qué es FleetPulse y cuánto ahorra.
- **Regla del equipo:** nada de pantallas que el sistema no produjo (ni `MOCK=1`). Datos simulados, sistema real: se dice.

---

## 1. La historia en una frase

> **"FleetPulse detecta la falla antes de que pare la máquina, elige al técnico y consigue el repuesto aunque esté en
> otro parque: una sola transferencia de hoy evitó 152 horas de parada, unos 68 mil dólares."**

Si alguien del jurado solo recuerda una cosa, es esa cifra. Aparece en el gancho, en la demo y en el cierre.

**Protagonista:** el jefe de mantenimiento de ArquiFusion (minería, agro e industria). No el equipo, no la nube: él.

---

## 2. Estructura y tiempos

| # | Bloque | Tiempo | Voz | Rúbrica que deja marcada |
|---|---|---|---|---|
| 1 | Gancho | 0:00-0:20 | Benjamin | Pitch |
| 2 | Problema | 0:20-0:50 | Benjamin | Problemas (1, 3, 4, 13) |
| 3 | Idea + equipo | 0:50-1:10 | Fabricio | Arquitectura, 2+ nubes |
| 4 | Demo: detectar y decidir | 1:10-2:05 | Alex → Benjamin | Problemas 1, 4, 5, 9 · Step Functions · IA |
| 5 | Demo: el repuesto que estaba en otro parque | 2:05-3:10 | Alex (inserto Benjamin) | Problema 3 · Step Functions · multi-tenant |
| 6 | Pasada por la rúbrica (4 láminas-mensaje) | 3:10-4:05 | Fabricio → Benjamin | 2+ nubes · persistencia · serverless · IA · problemas |
| 7 | Por qué ArquiFusion lo compraría (venta) + límite honesto | 4:05-4:20 | Alex | Pitch (vender) · arquitectura |
| 8 | Cierre con frase de venta | 4:20-4:30 | Benjamin | Pitch |

Demo (bloques 4 y 5): 2:00 de 4:30 = 44 %. Es lo que más pesa y lo único que no se recorta.

---

## 3. Bloque por bloque

### 1 · Gancho (0:00-0:20) · Benjamin · ~40 palabras

**Pantalla:** negro → una sola cifra grande: **"152 horas"**. Corte al mapa del jefe con las 124 máquinas moviéndose
(con S1, S8 y S9 activos hay severidad en minería, agro e industria; ver checklist §4.4 de `guion-video.md`).

**Voz:** "Una excavadora parada cuesta 450 dólares por hora. Esta semana, una bomba hidráulica de 6.800 dólares podía
dejar una excavadora 152 horas detenida… y la bomba estaba en la empresa, en otro parque. Somos FleetPulse."

> Gancho memorable: la ironía de tener el repuesto y no saberlo. Se cierra con la misma cifra (bloque 8).

### 2 · Problema (0:20-0:50) · Benjamin · ~65 palabras

**Lámina-mensaje:** "Cada parque ve solo lo suyo, y nadie ve la falla venir".
Tres íconos: 124 máquinas · 3 parques (minería 100, agro 12, industria 12) · repuestos repartidos.

**Voz:** "ArquiFusion opera 124 máquinas en minería, agro e industria. Hoy las fallas se ven cuando la máquina ya paró;
el técnico se elige por teléfono; y el almacén de mina no sabe que agro tiene tres bombas en estante. Cada hora parada
es producción perdida. Queríamos que el jefe de mantenimiento lo viera venir, y decidiera con datos."

### 3 · Idea + equipo (0:50-1:10) · Fabricio · ~45 palabras

**Pantalla:** diagrama `docs/architecture.png` con dos zonas resaltadas: AWS "detecta, orquesta y registra" · GCP
"aprende, explica y muestra". Caras/nombres pequeños en la esquina: Benjamin (AWS) · Alex (consola y simulador) · Fabricio (GCP).

**Voz:** "FleetPulse hace tres cosas: avisa antes de que la máquina pare, manda al técnico correcto y consigue el
repuesto donde esté, aunque sea en otro parque. Para eso une dos nubes: AWS detecta y decide; Google Cloud aprende y
explica con Gemini. Todo serverless. Les mostramos un caso real."

> Esta es la frase de "explicar rápido": **avisa, manda, consigue**. Al minuto 1:10 la idea ya está vendida; la demo
> solo la prueba.

### 4 · Demo: detectar y decidir (1:10-2:05) · Alex → Benjamin · ~120 palabras

Bloques B2 y B3 de `guion-video.md`, recortados. `incident_id` de EX-017 visible siempre.

| Tiempo | Pantalla | Voz |
|---|---|---|
| 1:10-1:30 | (Alex) Mapa: EX-017 pasa de amarillo a rojo. Ficha: presión hidráulica bajando, aceite y vibración subiendo | "Cada máquina manda telemetría cada minuto. Ninguna variable sola alarma; la combinación contra la propia historia de la máquina, sí. EX-017 acaba de cruzar a inmediato." |
| 1:30-1:50 | (Benjamin) Grafo `incident-flow` avanzando; correo de aprobación y clic (incidente pendiente CF-032, nota 3 de `guion-video.md`); TEC-01 asignado | "Se abre el incidente y una máquina de estado lo lleva: elige al técnico más cercano, de la especialidad correcta y libre. Si no es urgente, el supervisor aprueba con un clic desde el correo." |
| 1:50-2:05 | (Alex) Ficha del incidente: "Causa probable · Gemini · Bomba hidráulica", hallazgos contra la ficha del fabricante; "PRT-03 reservado · PRT-04 en falta" | "Gemini compara las lecturas con la ficha del fabricante y entrega un informe que el técnico puede seguir. Los sellos se reservan solos. La bomba… no hay ninguna en mina." |

> Transición en voz (Alex): "Y aquí es donde normalmente se pierde una semana."
>
> Si Gemini tarda en la toma: la ficha sale con la explicación por plantilla (es parte del diseño). Decir en voz "si
> Gemini tarda, el incidente sale con explicación de respaldo y nunca se bloquea", y seguir.
>
> Inserto opcional (+10 s, solo si el ensayo queda bajo 4:20): `operador@` reporta una falla y aparecen los chequeos de
> Gemini en segundos (B4 de `guion-video.md`). Voz: "El operario no espera al sensor: reporta lo que ve y Gemini le
> dice qué revisar." Suma a IA y al problema 9.

### 5 · Demo: el repuesto que estaba en otro parque (2:05-3:10) · Alex (inserto Benjamin) · ~130 palabras

Bloques B5a a B5d en una toma continua. **Se graba una sola vez** (aprobar consume la historia).

| Tiempo | Pantalla | Voz |
|---|---|---|
| 2:05-2:25 | `jefe.mantenimiento` · Abastecimiento: PRT-04 en rojo en los 3 almacenes de mina, verde en agro. Tarjeta: "Transferir desde FUNDO-A o FUNDO-B o PACKING: 16 h, USD 180" contra "Comprar a HidraAndes: 7 días, USD 6.800". **"Evita 152 h ≈ USD 68.400"** | "El sistema busca en los tres parques. Transferir desde agro llega en 16 horas por 180 dólares; comprar tarda una semana. Evita 152 horas de parada." |
| 2:25-2:45 | Clic **Aprobar** → (inserto Benjamin) `supply-flow` sale de `WaitForApproval` a `Ship`; en la consola "+1 en tránsito" (corte de ~8 s) | "No compramos lo que ya tenemos. Cada orden es otra máquina de estado, con un humano que aprueba." |
| 2:45-3:00 | `admin@` (minería) · `#trf:<id>` · **Confirmar recepción** → "Recibida" → "Reservada para el incidente" (inserto: `Receive` → `AllocateBackorders`) | "Al recibirla, se reserva sola para la máquina que la esperaba, y el técnico recibe el aviso." |
| 3:00-3:10 | `admin.agro@`: solo sus 3 almacenes y la transferencia que sale, sin botón de recepción; pide minería → 403 | "Y cada parque ve solo lo suyo. El jefe ve los tres." |

### 6 · Pasada por la rúbrica: 4 láminas-mensaje (3:10-4:05) · Fabricio → Benjamin · ~125 palabras

Cada lámina tiene una frase grande, una evidencia (captura real) y la voz. ~14 s cada una.

| Tiempo | Lámina (título = mensaje) | Evidencia en pantalla | Voz | Criterio |
|---|---|---|---|---|
| 3:10-3:25 | **"El mismo incidente vive en dos nubes"** | Pantalla dividida: ejecución de Step Functions (AWS) y BigQuery `domain_events` con el mismo `incident_id`, de `incident.opened` a `supply.received` | (Fabricio) "Este es el mismo incidente en AWS y en BigQuery, en menos de dos minutos, cargado por lotes, sin streaming." | 2+ nubes · persistencia |
| 3:25-3:40 | **"La IA decide, no solo redacta"** | Gemini (informe y chequeos del operario) + BigQuery ML (pares por clúster, tendencia de combustible, pronóstico de repuestos) | (Fabricio) "Gemini explica y recomienda chequeos; BigQuery ML decide quiénes son pares de verdad, qué aumento es tendencia y cuántos repuestos harán falta." | IA |
| 3:40-3:55 | **"Nada encendido si no hay datos"** | Íconos: Lambda, Step Functions, DynamoDB, SQS/SNS, Cloud Run, BigQuery; "0 servidores"; abajo: capa gratuita · `maximum_bytes_billed` en cada consulta · alerta de presupuesto | (Benjamin) "Cero servidores y costo cero en la hackatón. Si la flota crece a miles de máquinas, crece la cuenta por lote, no la infraestructura." | Serverless · problema 13 · costos |
| 3:55-4:05 | **"No se pierde un dato"** | PITR en DynamoDB · crudo versionado en S3 · outbox con reproceso · inbox idempotente · secretos en Secrets Manager | (Benjamin) "Respaldo continuo, archivo crudo y un buzón que reenvía todo si una nube cae. Las llaves de la otra nube viven en Secrets Manager." | Persistencia y HA · regla obligatoria |

> Lámina opcional si sobra tiempo (o en la descripción de Canvas): **"13 problemas, dónde se ve cada uno"** (tabla del
> documento de diseño, adaptada a la consola). Antes de mostrarla, confirmar en vivo los que no salen en la demo
> (2 preventivo, 6 combustible, 7 productividad, 12 histórico): solo se listan los que se pueden abrir en la consola.

### 7 · Por qué ArquiFusion lo compraría (4:05-4:20) · Alex · ~38 palabras

**Lámina-mensaje:** **"180 dólares de flete contra 68.400 de parada"**. Debajo, tres líneas: "detecta antes" ·
"el técnico correcto a la primera" · "el repuesto que ya tienen, en horas".

**Voz:** "Una sola transferencia: 180 dólares de flete en vez de 68 mil de máquina parada. Eso, en cada falla de una flota
de 124 máquinas. Y sin servidores que mantener. Lo único pendiente para producción: datos reales y un rol por función."

> Vender = traducir a dinero. La limitación (datos simulados, LabRole) va en media frase al final: el jurado la iba a
> notar igual y decirla suma en arquitectura.

### 8 · Cierre (4:20-4:30) · Benjamin · ~20 palabras

**Pantalla:** vuelve la cifra grande: **"152 horas → 16 horas"** y el logo.

**Voz:** "Ninguna máquina debería esperar un repuesto que la empresa ya tiene. Eso es FleetPulse. Gracias."

---

## 4. Plan de la demo y de respaldo

Logística completa: `docs/demo/guion-video.md` §4 (checklist de 15 min) y §5 (plan B por bloque). Lo específico del pitch:

- **Orden de grabación:** primero las láminas y el gancho (no dependen de AWS); después bloque 4; después bloque 5 **una sola vez**; al final el bloque 6 (BigQuery necesita los eventos de la toma del bloque 5, < 2 min).
- **Pre-armado:** pestañas ya logueadas (supervisor, operador, técnico, admin, admin.agro, jefe), ficha de EX-017 abierta una vez para calentar Gemini, consulta de BigQuery escrita con `maximum_bytes_billed`, `seed-demo.py pending` 5-25 min antes.
- **Esperas:** cortar las esperas reales con un corte visible; nunca acelerar la pantalla ni inventarla. Medidas en vivo:
  deriva de S1 hasta inmediato (5-11 min), explicación de Gemini (~11 s en frío), sugerencia de transferencia (~25 s
  después del faltante), "en tránsito" (~8 s), recepción (~60 s), eventos en BigQuery (< 2 min).
- **Durante la grabación no se pushea a `aws/`, `simulator/` ni `contracts/`:** un deploy deja el simulador apagado.
- **Cifra de la alerta de presupuesto:** `docs/architecture.md` dice S/ 50 al mes y se comentó S/ 900. Confirmar en Billing
  antes de decirla; si no hay certeza, decir solo "alerta de presupuesto".

### Quién graba cada bloque (T23)

| Bloque | Voz y pantalla | Cuenta o pestaña | Inserto que graba otro |
|---|---|---|---|
| 1 Gancho · 2 Problema | Benjamin | Láminas; `jefe.mantenimiento@` (mapa) | Ninguno |
| 3 Idea + equipo | Fabricio | Diagrama `docs/architecture.png` | Ninguno |
| 4 Detectar y decidir | Alex (mapa, ficha) y Benjamin (grafo y correo) | `supervisor@`; Step Functions `incident-flow`; correo de aprobación | — |
| 5 Repuesto en otro parque | Alex | `jefe.mantenimiento@`, `admin@`, `admin.agro@` | Benjamin: `supply-flow` en `Ship`, `Receive` y `AllocateBackorders` |
| 6 Láminas de la rúbrica | Fabricio (2 primeras) y Benjamin (2 últimas) | BigQuery `domain_events` con el `incident_id` de la toma 5 | Benjamin: la ejecución en Step Functions |
| 7 Venta · 8 Cierre | Alex · Benjamin | Láminas | Ninguno |

Fabricio edita y mide la duración en el archivo exportado.
- **Si falla una toma:** quien graba ese bloque decide en el momento; si en 2 intentos no sale, se usa la captura real de hoy y se dice en voz ("así se vio a las [hora]").
- **Si se consume la historia de PRT-04:** rearmar según `guion-video.md` ("Si se consumió la historia") y regrabar 4, 5 y 6 juntos (cambia el `incident_id`).

---

## 5. Checklist del ensayo (T22, 17:00-18:00)

- [ ] Lectura completa con cronómetro, en voz alta, al ritmo real (10 % más lento de lo que parece natural). Objetivo 4:20-4:35.
- [ ] Si un bloque se pasa dos veces, **se corta texto**, no se acelera.
- [ ] Cada voz anuncia el relevo con la primera palabra de su bloque (sin "ahora le paso a…").
- [ ] Confirmar las cifras en voz: 450 USD/h (costo de parada de EX en `supply.yaml`), 152 h, USD 68.400, 16 h / USD 180, 7 días / USD 6.800, 124 máquinas (100/12/12).
- [ ] Confirmar con Benjamin la nota 3 de `guion-video.md` (el clic de aprobación se hace sobre CF-032, no sobre EX-017).
- [ ] Revisar que ninguna lámina diga el nombre de un criterio ni "informe" (entregar un informe descalifica: es un pitch, no un documento).
- [ ] **Prueba del minuto 1:** detener el video en 1:10 y preguntar a alguien que no lo conoce "¿qué es y cuánto ahorra?". Si no puede responder "avisa, manda al técnico y consigue el repuesto; evitó 68 mil dólares", reescribir los bloques 1-3.
- [ ] **Prueba de venta:** la cifra en dinero (USD 68.400 contra USD 180) aparece al menos 3 veces: gancho, demo y bloque 7.
- [ ] Sin música de fondo, sin pantallas de login, sin notificaciones.
- [ ] Medir el archivo final exportado, no la línea de tiempo del editor.

---

## 6. Preguntas difíciles (si hay ronda con el jurado)

| Pregunta probable | Quién | Respuesta corta |
|---|---|---|
| "¿En qué se diferencia de un CMMS como SAP PM o Maximo?" | Benjamin | "Un CMMS registra lo que ya pasó. FleetPulse detecta antes, decide técnico y repuesto, y aprende entre parques; podría alimentar un CMMS." |
| "¿Qué es real y qué es simulado?" | Alex | "Los datos de la flota son simulados, con escenarios de falla; el sistema es real: todo lo del video corre en AWS y GCP, nada es maqueta." |
| "¿Cómo escala y cuánto cuesta?" | Benjamin | "Escala a cero, ingesta por lotes y telemetría fuera de la base transaccional: el costo crece por lote, no por servidor. En la hackatón, capa gratuita." |
| "¿Por qué la elección del técnico no es IA?" | Alex | "Porque tiene que ser explicable y auditable: distancia, especialidad y disponibilidad. La IA está donde suma: causa raíz, chequeos, pares y demanda." |

---

## 7. Diferencias con `docs/demo/guion-video.md`

| Cambio | Por qué |
|---|---|
| Gancho con cifra (152 h) y protagonista (jefe de mantenimiento) en vez de "una máquina detenida frena la operación" | El jurado recuerda una historia con número, no un tour |
| Bloque de idea + equipo de 20 s antes de la demo | El pitch pide que se entienda la solución antes de verla |
| Demo de 2:00 en dos actos (detectar/decidir y el repuesto en otro parque); se cae B4 (reporte del operario) y B5e (KPIs) | Mantiene un solo hilo; el reporte del operario y los KPIs pasan a la lámina de IA o a la descripción |
| Pasada por la rúbrica con 4 láminas-mensaje y evidencia real | Que cada criterio quede marcado sin nombrarlo |
| Limitaciones dichas por nosotros (LabRole, datos simulados) | Honestidad = puntos en arquitectura |
| Cierre que vuelve a la cifra del gancho | Memorabilidad |

**Tomado del guion de Benjamin:** checklist de 15 minutos y apagado (§4 y §6, se usan tal cual), quién graba cada
bloque, esperas medidas, S8/S9 para severidad en los 3 parques, respaldo por plantilla si Gemini tarda, costos
(capa gratuita, `maximum_bytes_billed`, alerta de presupuesto), no pushear durante la grabación, y el reporte del
operario como inserto opcional.
