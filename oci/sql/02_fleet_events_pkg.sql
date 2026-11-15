-- FleetPulse OCI - 02_fleet_events_pkg.sql (T10). Ejecutar como FLEET.
--   sql -cloudconfig wallet.zip fleet@fpdb_low @oci/sql/02_fleet_events_pkg.sql
-- FLEET_EVENTS.apply aplica un evento de dominio en una transaccion. Primero inserta en PROCESSED_EVENTS:
-- si el event_id ya existe devuelve "duplicate" sin tocar nada (inbox idempotente; reintentar es seguro).
-- No hace COMMIT: ORDS confirma al terminar bien el handler y deshace si hay excepcion.

CREATE OR REPLACE PACKAGE fleet_events AS
  FUNCTION apply(p_event IN CLOB) RETURN CLOB;
  FUNCTION apply_snapshot(p_body IN CLOB) RETURN CLOB;
  FUNCTION apply_reorders(p_body IN CLOB) RETURN CLOB;
  FUNCTION apply_kpis(p_payload IN CLOB) RETURN CLOB;
  PROCEDURE print_json(p_json IN CLOB);
  FUNCTION to_ts(p_iso IN VARCHAR2) RETURN TIMESTAMP WITH TIME ZONE;
END fleet_events;
/

CREATE OR REPLACE PACKAGE BODY fleet_events AS

  FUNCTION to_ts(p_iso IN VARCHAR2) RETURN TIMESTAMP WITH TIME ZONE IS
    s VARCHAR2(64) := REPLACE(TRIM(p_iso), 'Z', '+00:00');
  BEGIN
    IF p_iso IS NULL THEN
      RETURN NULL;
    END IF;
    IF INSTR(s, '.') > 0 THEN
      RETURN TO_TIMESTAMP_TZ(s, 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM');
    END IF;
    RETURN TO_TIMESTAMP_TZ(s, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM');
  END to_ts;

  PROCEDURE print_json(p_json IN CLOB) IS
    l_len PLS_INTEGER := DBMS_LOB.GETLENGTH(p_json);
    l_pos PLS_INTEGER := 1;
  BEGIN
    OWA_UTIL.MIME_HEADER('application/json', TRUE);
    WHILE l_pos <= NVL(l_len, 0) LOOP
      HTP.PRN(DBMS_LOB.SUBSTR(p_json, 8000, l_pos));
      l_pos := l_pos + 8000;
    END LOOP;
  END print_json;

  FUNCTION yn(p_bool IN BOOLEAN) RETURN CHAR IS
  BEGIN
    RETURN CASE WHEN p_bool THEN 'Y' ELSE 'N' END;
  END yn;

  PROCEDURE timeline(p_event_id VARCHAR2, p_incident_id VARCHAR2, p_type VARCHAR2, p_actor VARCHAR2,
                     p_payload CLOB, p_ts TIMESTAMP WITH TIME ZONE) IS
  BEGIN
    INSERT INTO incident_events (event_id, incident_id, type, actor, payload, ts)
    VALUES (p_event_id, p_incident_id, p_type, p_actor, p_payload, p_ts);
  END timeline;

  -- Fila minima del incidente si un evento posterior llega antes que incident.opened (SQS no ordena)
  PROCEDURE ensure_incident(p_incident_id VARCHAR2, p_machine_id VARCHAR2, p_ts TIMESTAMP WITH TIME ZONE) IS
  BEGIN
    INSERT INTO incidents (incident_id, machine_id, severity, status, opened_at)
    SELECT p_incident_id, p_machine_id, 'schedule', 'open', p_ts FROM dual
     WHERE NOT EXISTS (SELECT 1 FROM incidents WHERE incident_id = p_incident_id);
  END ensure_incident;

  PROCEDURE on_opened(p_inc VARCHAR2, p_mid VARCHAR2, p JSON_OBJECT_T) IS
    l_opened TIMESTAMP WITH TIME ZONE := to_ts(p.get_string('opened_at'));
    l_zone   VARCHAR2(16) := p.get_string('zone_id');
    l_group  VARCHAR2(40) := p.get_string('group_id');
    l_sev    VARCHAR2(16) := p.get_string('severity');
    l_score  NUMBER := p.get_number('score');
    l_mode   VARCHAR2(32) := p.get_string('failure_mode');
    l_rec    CHAR(1) := yn(p.get_boolean('recurrence_flag'));
  BEGIN
    MERGE INTO incidents i
    USING (SELECT p_inc AS incident_id FROM dual) s ON (i.incident_id = s.incident_id)
    WHEN MATCHED THEN UPDATE SET
      i.zone_id = l_zone, i.group_id = l_group, i.severity = l_sev, i.score = l_score,
      i.failure_mode = l_mode, i.opened_at = l_opened, i.recurrence_flag = l_rec
    WHEN NOT MATCHED THEN INSERT
      (incident_id, machine_id, zone_id, group_id, severity, status, score, failure_mode, opened_at, recurrence_flag)
      VALUES (p_inc, p_mid, l_zone, l_group, l_sev, 'open', l_score, l_mode, l_opened, l_rec);
    UPDATE machine_status
       SET severity = l_sev, score = l_score, open_incident_id = p_inc
     WHERE machine_id = p_mid AND (open_incident_id IS NULL OR open_incident_id = p_inc);
  END on_opened;

  PROCEDURE on_escalated(p_inc VARCHAR2, p_mid VARCHAR2, p JSON_OBJECT_T, p_ts TIMESTAMP WITH TIME ZONE) IS
    l_sev   VARCHAR2(16) := p.get_string('to_severity');
    l_score NUMBER := p.get_number('score');
  BEGIN
    ensure_incident(p_inc, p_mid, p_ts);
    UPDATE incidents SET severity = l_sev, score = l_score
     WHERE incident_id = p_inc AND status <> 'closed';
    UPDATE machine_status SET severity = l_sev, score = l_score
     WHERE machine_id = p_mid AND open_incident_id = p_inc;
  END on_escalated;

  PROCEDURE on_assigned(p_inc VARCHAR2, p_mid VARCHAR2, p JSON_OBJECT_T, p_ts TIMESTAMP WITH TIME ZONE) IS
    l_expl     JSON_OBJECT_T := p.get_object('explanation');
    l_tech     VARCHAR2(8) := p.get_string('tech_id');
    l_assigned TIMESTAMP WITH TIME ZONE := to_ts(p.get_string('assigned_at'));
    l_approval VARCHAR2(16) := p.get_string('approval');
    l_summary  VARCHAR2(1000) := SUBSTR(l_expl.get_string('summary'), 1, 1000);
    l_src      VARCHAR2(16) := l_expl.get_string('source');
  BEGIN
    ensure_incident(p_inc, p_mid, p_ts);
    UPDATE incidents
       SET tech_id = l_tech, assigned_at = l_assigned, approval = l_approval,
           status = CASE WHEN status = 'closed' THEN status ELSE 'assigned' END,
           explanation = l_summary, explanation_src = l_src
     WHERE incident_id = p_inc;
    UPDATE technicians SET available = 'N'
     WHERE tech_id = l_tech
       AND NOT EXISTS (SELECT 1 FROM incidents WHERE incident_id = p_inc AND status = 'closed');
  END on_assigned;

  PROCEDURE on_closed(p_inc VARCHAR2, p_mid VARCHAR2, p JSON_OBJECT_T, p_ts TIMESTAMP WITH TIME ZONE) IS
    l_parts JSON_ARRAY_T := p.get_array('parts_used');
    l_line  JSON_OBJECT_T;
    l_part  VARCHAR2(8);
    l_zone  VARCHAR2(16);
    l_qty   NUMBER;
    l_res   NUMBER;
    l_tech  VARCHAR2(8);
    l_closed   TIMESTAMP WITH TIME ZONE := NVL(to_ts(p.get_string('closed_at')), p_ts);
    l_downtime NUMBER := p.get_number('downtime_h');
    l_cause    VARCHAR2(1000) := SUBSTR(p.get_string('cause_summary'), 1, 1000);
  BEGIN
    ensure_incident(p_inc, p_mid, p_ts);
    UPDATE incidents
       SET status = 'closed', closed_at = l_closed, downtime_h = l_downtime, cause_summary = l_cause
     WHERE incident_id = p_inc
    RETURNING tech_id INTO l_tech;
    IF l_tech IS NOT NULL THEN
      UPDATE technicians SET available = 'Y' WHERE tech_id = l_tech;
    END IF;
    -- Consumir repuestos: primero lo reservado para este incidente, luego stock libre
    FOR i IN 0 .. NVL(l_parts.get_size, 0) - 1 LOOP
      l_line := TREAT(l_parts.get(i) AS JSON_OBJECT_T);
      l_part := l_line.get_string('part_id');
      l_zone := l_line.get_string('zone_id');
      l_qty  := l_line.get_number('qty');
      SELECT NVL(SUM(qty), 0) INTO l_res FROM part_reservations
       WHERE incident_id = p_inc AND part_id = l_part AND zone_id = l_zone AND status = 'reserved';
      UPDATE part_reservations SET status = 'consumed'
       WHERE incident_id = p_inc AND part_id = l_part AND zone_id = l_zone AND status = 'reserved';
      UPDATE part_stock
         SET on_hand = GREATEST(on_hand - l_qty, 0), reserved = GREATEST(reserved - LEAST(l_res, l_qty), 0)
       WHERE part_id = l_part AND zone_id = l_zone;
      INSERT INTO part_movements (part_id, zone_id, qty, kind, incident_id, ts)
      VALUES (l_part, l_zone, l_qty, 'consumption', p_inc, l_closed);
    END LOOP;
    -- Reservas que no se usaron vuelven al stock libre
    FOR r IN (SELECT reservation_id, part_id, zone_id, qty FROM part_reservations
               WHERE incident_id = p_inc AND status = 'reserved') LOOP
      UPDATE part_stock SET reserved = GREATEST(reserved - r.qty, 0) WHERE part_id = r.part_id AND zone_id = r.zone_id;
      UPDATE part_reservations SET status = 'released' WHERE reservation_id = r.reservation_id;
    END LOOP;
    UPDATE machine_status SET severity = 'ok', score = NULL, open_incident_id = NULL
     WHERE machine_id = p_mid AND open_incident_id = p_inc;
  END on_closed;

  -- Reserva atomica: zona del incidente, luego la mas cercana; si no hay, faltante + pedido sugerido
  FUNCTION on_reserve(p_inc VARCHAR2, p JSON_OBJECT_T) RETURN JSON_OBJECT_T IS
    l_lines    JSON_ARRAY_T := p.get_array('lines');
    l_zone     VARCHAR2(16) := p.get_string('zone_id');
    l_line     JSON_OBJECT_T;
    l_part     VARCHAR2(8);
    l_qty      NUMBER;
    l_done     BOOLEAN;
    l_id       NUMBER;
    l_reserved JSON_ARRAY_T := JSON_ARRAY_T();
    l_missing  JSON_ARRAY_T := JSON_ARRAY_T();
    l_item     JSON_OBJECT_T;
    l_out      JSON_OBJECT_T := JSON_OBJECT_T();
  BEGIN
    FOR i IN 0 .. l_lines.get_size - 1 LOOP
      l_line := TREAT(l_lines.get(i) AS JSON_OBJECT_T);
      l_part := l_line.get_string('part_id');
      l_qty  := l_line.get_number('qty');
      l_done := FALSE;
      FOR z IN (SELECT c.zone_id
                  FROM zones c CROSS JOIN (SELECT lat, lon FROM zones WHERE zone_id = l_zone) o
                 ORDER BY CASE WHEN c.zone_id = l_zone THEN 0 ELSE 1 END,
                          POWER(c.lat - o.lat, 2) + POWER(c.lon - o.lon, 2)) LOOP
        UPDATE part_stock SET reserved = reserved + l_qty
         WHERE part_id = l_part AND zone_id = z.zone_id AND on_hand - reserved >= l_qty;
        IF SQL%ROWCOUNT = 1 THEN
          INSERT INTO part_reservations (incident_id, part_id, zone_id, qty)
          VALUES (p_inc, l_part, z.zone_id, l_qty) RETURNING reservation_id INTO l_id;
          l_item := JSON_OBJECT_T();
          l_item.put('reservation_id', l_id);
          l_item.put('part_id', l_part);
          l_item.put('zone_id', z.zone_id);
          l_item.put('qty', l_qty);
          l_reserved.append(l_item);
          l_done := TRUE;
          EXIT;
        END IF;
      END LOOP;
      IF NOT l_done THEN
        l_item := JSON_OBJECT_T();
        l_item.put('part_id', l_part);
        l_item.put('qty', l_qty);
        l_missing.append(l_item);
        INSERT INTO reorder_suggestions (part_id, zone_id, qty, source, incident_id)
        SELECT l_part, l_zone, l_qty + reorder_point, 'shortage', p_inc
          FROM part_stock WHERE part_id = l_part AND zone_id = l_zone;
      END IF;
    END LOOP;
    l_out.put('reserved', l_reserved);
    l_out.put('missing', l_missing);
    l_out.put('reorder_suggested', l_missing.get_size > 0);
    RETURN l_out;
  END on_reserve;

  FUNCTION apply_kpis(p_payload IN CLOB) RETURN CLOB IS
    p      JSON_OBJECT_T := JSON_OBJECT_T.parse(p_payload);
    l_day  DATE := TO_DATE(p.get_string('day'), 'YYYY-MM-DD');
    l_rows JSON_ARRAY_T := p.get_array('machines');
    r      JSON_OBJECT_T;
    l_out  JSON_OBJECT_T := JSON_OBJECT_T();
    l_mid  VARCHAR2(8);
    l_op   NUMBER; l_util NUMBER; l_idle NUMBER; l_fuel NUMBER; l_peers NUMBER; l_cost NUMBER; l_cl NUMBER;
    l_quad VARCHAR2(16);
  BEGIN
    FOR i IN 0 .. l_rows.get_size - 1 LOOP
      r := TREAT(l_rows.get(i) AS JSON_OBJECT_T);
      l_mid := r.get_string('machine_id');
      l_op := r.get_number('operating_h'); l_util := r.get_number('utilization');
      l_idle := r.get_number('idle_ratio'); l_fuel := r.get_number('fuel_lph');
      l_peers := r.get_number('fuel_vs_peers_pct'); l_cost := r.get_number('cost_per_h');
      l_cl := r.get_number('cluster_id'); l_quad := r.get_string('quadrant');
      MERGE INTO fleet_kpis_daily k
      USING (SELECT l_day AS day, l_mid AS machine_id FROM dual) s
         ON (k.day = s.day AND k.machine_id = s.machine_id)
      WHEN MATCHED THEN UPDATE SET
        operating_h = l_op, utilization = l_util, idle_ratio = l_idle, fuel_lph = l_fuel,
        fuel_vs_peers_pct = l_peers, cost_per_h = l_cost, cluster_id = l_cl, quadrant = l_quad
      WHEN NOT MATCHED THEN INSERT
        (day, machine_id, operating_h, utilization, idle_ratio, fuel_lph, fuel_vs_peers_pct, cost_per_h, cluster_id,
         quadrant)
        VALUES (s.day, s.machine_id, l_op, l_util, l_idle, l_fuel, l_peers, l_cost, l_cl, l_quad);
    END LOOP;
    l_out.put('upserted', l_rows.get_size);
    RETURN l_out.to_clob;
  END apply_kpis;

  FUNCTION apply(p_event IN CLOB) RETURN CLOB IS
    e      JSON_OBJECT_T := JSON_OBJECT_T.parse(p_event);
    l_id   VARCHAR2(36) := e.get_string('event_id');
    l_type VARCHAR2(40) := e.get_string('event_type');
    l_inc  VARCHAR2(40) := e.get_string('incident_id');
    l_mid  VARCHAR2(8)  := e.get_string('machine_id');
    l_ts   TIMESTAMP WITH TIME ZONE := to_ts(e.get_string('occurred_at'));
    p      JSON_OBJECT_T := e.get_object('payload');
    l_out  JSON_OBJECT_T := JSON_OBJECT_T();
    l_status VARCHAR2(16) := 'applied';
  BEGIN
    IF l_id IS NULL OR l_type IS NULL OR p IS NULL THEN
      RAISE_APPLICATION_ERROR(-20001, 'evento sin event_id, event_type o payload');
    END IF;
    l_out.put('event_id', l_id);
    BEGIN
      INSERT INTO processed_events (event_id, event_type) VALUES (l_id, l_type);
    EXCEPTION
      WHEN DUP_VAL_ON_INDEX THEN
        l_out.put('status', 'duplicate');
        l_out.put_null('reservation');
        RETURN l_out.to_clob;
    END;

    IF l_type LIKE 'incident.%' OR l_type LIKE 'part.%' THEN
      IF l_inc IS NULL OR l_mid IS NULL THEN
        RAISE_APPLICATION_ERROR(-20002, l_type || ' sin incident_id o machine_id');
      END IF;
      timeline(l_id, l_inc, l_type, e.get_string('source'), p.to_clob, l_ts);
    END IF;

    l_out.put_null('reservation');
    CASE l_type
      WHEN 'incident.opened' THEN on_opened(l_inc, l_mid, p);
      WHEN 'incident.escalated' THEN on_escalated(l_inc, l_mid, p, l_ts);
      WHEN 'incident.assigned' THEN on_assigned(l_inc, l_mid, p, l_ts);
      WHEN 'incident.closed' THEN on_closed(l_inc, l_mid, p, l_ts);
      WHEN 'part.reserve.requested' THEN
        ensure_incident(l_inc, l_mid, l_ts);
        l_out.put('reservation', on_reserve(l_inc, p));
      WHEN 'part.reserved' THEN NULL;   -- resultado que AWS reenvia: solo linea de tiempo
      WHEN 'part.shortage' THEN NULL;
      WHEN 'kpi.daily' THEN
        l_out.put('kpis', JSON_OBJECT_T.parse(apply_kpis(p.to_clob)));
      ELSE
        l_status := 'ignored';         -- maintenance.order.suggested, fuel.anomaly: incremento T18
    END CASE;
    l_out.put('status', l_status);
    RETURN l_out.to_clob;
  END apply;

  -- Estado de maquinas para el mapa: el ultimo gana por updated_at
  FUNCTION apply_snapshot(p_body IN CLOB) RETURN CLOB IS
    b      JSON_OBJECT_T := JSON_OBJECT_T.parse(p_body);
    l_rows JSON_ARRAY_T := b.get_array('machines');
    r      JSON_OBJECT_T;
    l_n    PLS_INTEGER := 0;
    l_out  JSON_OBJECT_T := JSON_OBJECT_T();
    l_mid VARCHAR2(8); l_st VARCHAR2(16); l_sev VARCHAR2(16); l_score NUMBER; l_lat NUMBER; l_lon NUMBER;
    l_zone VARCHAR2(16); l_hours NUMBER; l_fuel NUMBER; l_inc VARCHAR2(40); l_upd TIMESTAMP WITH TIME ZONE;
  BEGIN
    FOR i IN 0 .. l_rows.get_size - 1 LOOP
      r := TREAT(l_rows.get(i) AS JSON_OBJECT_T);
      l_mid := r.get_string('machine_id'); l_st := r.get_string('status'); l_sev := r.get_string('severity');
      l_score := r.get_number('score'); l_lat := r.get_number('lat'); l_lon := r.get_number('lon');
      l_zone := r.get_string('zone_id'); l_hours := r.get_number('hours_total');
      l_fuel := r.get_number('fuel_remaining_ratio'); l_inc := r.get_string('open_incident_id');
      l_upd := to_ts(r.get_string('updated_at'));
      UPDATE machine_status
         SET status = l_st, severity = l_sev, score = l_score, lat = l_lat, lon = l_lon, zone_id = l_zone,
             hours_total = l_hours, fuel_remaining = l_fuel, open_incident_id = l_inc, updated_at = l_upd
       WHERE machine_id = l_mid AND (updated_at IS NULL OR updated_at <= l_upd);
      l_n := l_n + SQL%ROWCOUNT;
    END LOOP;
    l_out.put('upserted', l_n);
    RETURN l_out.to_clob;
  END apply_snapshot;

  -- Pronostico de 14 dias (GCP) contra el stock: pedido = pronostico + punto de reorden - disponible
  FUNCTION apply_reorders(p_body IN CLOB) RETURN CLOB IS
    b      JSON_OBJECT_T := JSON_OBJECT_T.parse(p_body);
    l_rows JSON_ARRAY_T := b.get_array('forecast');
    r      JSON_OBJECT_T;
    l_qty  NUMBER;
    l_sugg JSON_ARRAY_T := JSON_ARRAY_T();
    l_item JSON_OBJECT_T;
    l_out  JSON_OBJECT_T := JSON_OBJECT_T();
    l_part VARCHAR2(8); l_zone VARCHAR2(16); l_fc NUMBER;
  BEGIN
    FOR i IN 0 .. l_rows.get_size - 1 LOOP
      r := TREAT(l_rows.get(i) AS JSON_OBJECT_T);
      l_part := r.get_string('part_id'); l_zone := r.get_string('zone_id'); l_fc := r.get_number('forecast_14d');
      BEGIN
        SELECT GREATEST(CEIL(l_fc + reorder_point - (on_hand - reserved)), 0) INTO l_qty
          FROM part_stock WHERE part_id = l_part AND zone_id = l_zone;
      EXCEPTION
        WHEN NO_DATA_FOUND THEN l_qty := 0;
      END;
      IF l_qty > 0 THEN
        INSERT INTO reorder_suggestions (part_id, zone_id, qty, forecast_14d, source)
        VALUES (l_part, l_zone, l_qty, l_fc, 'forecast');
        l_item := JSON_OBJECT_T();
        l_item.put('part_id', l_part);
        l_item.put('zone_id', l_zone);
        l_item.put('qty', l_qty);
        l_sugg.append(l_item);
      END IF;
    END LOOP;
    l_out.put('suggestions', l_sugg);
    RETURN l_out.to_clob;
  END apply_reorders;

END fleet_events;
/
SHOW ERRORS PACKAGE BODY fleet_events
