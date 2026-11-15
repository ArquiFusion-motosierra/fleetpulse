-- FleetPulse OCI - 03_ords.sql (T10). Ejecutar como FLEET despues de 02.
-- Modulo REST fleet/v1 en https://<id>-fpdb.adb.us-ashburn-1.oraclecloudapps.com/ords/fleet/v1/
-- Todo el modulo exige el privilegio fp.writer (OAuth2 client credentials, ver 04_oauth.sql).
-- Errores: ROLLBACK y 400 con {"error": ...}; SQS reintenta y tras 5 intentos va a la DLQ.

WHENEVER SQLERROR EXIT SQL.SQLCODE

BEGIN
  ORDS.DELETE_MODULE(p_module_name => 'fleet.v1');
EXCEPTION
  WHEN OTHERS THEN NULL;  -- primera vez: no existe
END;
/

DECLARE
  l_roles    OWA.vc_arr;
  l_patterns OWA.vc_arr;
  PROCEDURE post_handler(p_pattern VARCHAR2, p_call VARCHAR2) IS
  BEGIN
    ORDS.DEFINE_TEMPLATE(p_module_name => 'fleet.v1', p_pattern => p_pattern);
    ORDS.DEFINE_HANDLER(
      p_module_name => 'fleet.v1',
      p_pattern     => p_pattern,
      p_method      => 'POST',
      p_source_type => ORDS.source_type_plsql,
      p_source      =>
        'DECLARE l_res CLOB; l_err JSON_OBJECT_T := JSON_OBJECT_T(); BEGIN ' ||
        '  l_res := ' || p_call || '(:body_text); ' ||
        '  :status_code := 200; fleet_events.print_json(l_res); ' ||
        'EXCEPTION WHEN OTHERS THEN ROLLBACK; :status_code := 400; ' ||
        '  l_err.put(''error'', SQLERRM); fleet_events.print_json(l_err.to_clob); END;');
  END;
BEGIN
  ORDS.DEFINE_MODULE(p_module_name => 'fleet.v1', p_base_path => '/v1/', p_items_per_page => 25,
                     p_status => 'PUBLISHED', p_comments => 'FleetPulse: escritura desde AWS');
  post_handler('events', 'fleet_events.apply');
  post_handler('machines/snapshot', 'fleet_events.apply_snapshot');
  post_handler('reorders', 'fleet_events.apply_reorders');
  post_handler('kpis', 'fleet_events.apply_kpis');

  -- Verificacion para smoke.sh: incidente con su linea de tiempo
  ORDS.DEFINE_TEMPLATE(p_module_name => 'fleet.v1', p_pattern => 'incidents/:id');
  ORDS.DEFINE_HANDLER(
    p_module_name => 'fleet.v1',
    p_pattern     => 'incidents/:id',
    p_method      => 'GET',
    p_source_type => ORDS.source_type_collection_item,
    p_source      => q'[
      SELECT i.incident_id, i.machine_id, i.zone_id, i.group_id, i.severity, i.status, i.failure_mode, i.tech_id,
             i.opened_at, i.assigned_at, i.closed_at, i.downtime_h, i.recurrence_flag, i.explanation,
             CURSOR(SELECT e.type, e.actor, e.ts FROM incident_events e
                     WHERE e.incident_id = i.incident_id ORDER BY e.ts) AS timeline,
             CURSOR(SELECT r.part_id, r.zone_id, r.qty, r.status FROM part_reservations r
                     WHERE r.incident_id = i.incident_id ORDER BY r.reservation_id) AS reservations
        FROM incidents i
       WHERE i.incident_id = :id]');

  BEGIN
    ORDS.CREATE_ROLE(p_role_name => 'fp_writer');
  EXCEPTION
    WHEN OTHERS THEN NULL;  -- ya existe
  END;
  l_roles(1) := 'fp_writer';
  ORDS.DEFINE_PRIVILEGE(
    p_privilege_name => 'fp.writer',
    p_roles          => l_roles,
    p_patterns       => l_patterns,
    p_label          => 'FleetPulse writer',
    p_description    => 'oci-writer y snapshot-publisher en AWS');
  ORDS.SET_MODULE_PRIVILEGE(p_module_name => 'fleet.v1', p_privilege_name => 'fp.writer');
  COMMIT;
END;
/
