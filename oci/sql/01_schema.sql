-- FleetPulse OCI - 01_schema.sql (T06/T10). Ejecutar como ADMIN en fpdb (SQLcl o Database Actions).
--   sql -cloudconfig wallet.zip admin@fpdb_low @oci/sql/01_schema.sql
-- Crea el usuario FLEET (dueno de los datos, separado de ADMIN) y sus tablas.
-- La telemetria nunca entra aqui: solo registro operativo y relacional.
-- ASUNCION: la clave de FLEET se pasa como variable de sustitucion y nunca se versiona.

WHENEVER SQLERROR EXIT SQL.SQLCODE

DEFINE fleet_pwd = '&1'

DECLARE
  n NUMBER;
BEGIN
  SELECT COUNT(*) INTO n FROM dba_users WHERE username = 'FLEET';
  IF n = 0 THEN
    EXECUTE IMMEDIATE 'CREATE USER fleet IDENTIFIED BY "&fleet_pwd" QUOTA UNLIMITED ON data';
  END IF;
END;
/
GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE PROCEDURE, CREATE TYPE TO fleet;

-- REST para FLEET en <ords>/fleet/ (el modulo fleet/v1 lo define 03_ords.sql como FLEET)
BEGIN
  ORDS_ADMIN.ENABLE_SCHEMA(p_enabled => TRUE, p_schema => 'FLEET', p_url_mapping_type => 'BASE_PATH',
                           p_url_mapping_pattern => 'fleet', p_auto_rest_auth => TRUE);
  COMMIT;
END;
/

ALTER SESSION SET CURRENT_SCHEMA = fleet;

CREATE TABLE zones (
  zone_id      VARCHAR2(16) PRIMARY KEY,
  name         VARCHAR2(80) NOT NULL,
  lat          NUMBER(9,6)  NOT NULL,
  lon          NUMBER(9,6)  NOT NULL,
  altitude_m   NUMBER(5)
);

CREATE TABLE machine_models (
  model_id            VARCHAR2(4) PRIMARY KEY,
  name                VARCHAR2(80) NOT NULL,
  service_interval_h  NUMBER(6) NOT NULL
);

CREATE TABLE machines (
  machine_id      VARCHAR2(8) PRIMARY KEY,
  model_id        VARCHAR2(4) NOT NULL REFERENCES machine_models,
  serial          VARCHAR2(32) NOT NULL UNIQUE,
  zone_id         VARCHAR2(16) NOT NULL REFERENCES zones,
  commissioned_at DATE NOT NULL
);

-- Estado actual para el mapa de APEX (lo actualiza POST /machines/snapshot; el ultimo gana por updated_at)
CREATE TABLE machine_status (
  machine_id        VARCHAR2(8) PRIMARY KEY REFERENCES machines,
  status            VARCHAR2(16) DEFAULT 'off' NOT NULL
                    CHECK (status IN ('operating', 'idle', 'off', 'in_maintenance')),
  severity          VARCHAR2(16) DEFAULT 'ok' NOT NULL
                    CHECK (severity IN ('ok', 'observe', 'schedule', 'immediate')),
  score             NUMBER(6,2),
  lat               NUMBER(9,6),
  lon               NUMBER(9,6),
  zone_id           VARCHAR2(16) REFERENCES zones,
  hours_total       NUMBER(10,2),
  fuel_remaining    NUMBER(4,3),
  open_incident_id  VARCHAR2(40),
  updated_at        TIMESTAMP WITH TIME ZONE
);

CREATE TABLE technicians (
  tech_id    VARCHAR2(8) PRIMARY KEY,
  name       VARCHAR2(80) NOT NULL,
  specialty  VARCHAR2(16) NOT NULL
             CHECK (specialty IN ('hydraulics', 'engine', 'electrical', 'transmission')),
  zone_id    VARCHAR2(16) NOT NULL REFERENCES zones,
  available  CHAR(1) DEFAULT 'Y' NOT NULL CHECK (available IN ('Y', 'N'))
);

CREATE TABLE parts (
  part_id        VARCHAR2(8) PRIMARY KEY,
  sku            VARCHAR2(20) NOT NULL UNIQUE,
  name           VARCHAR2(80) NOT NULL,
  unit_cost_usd  NUMBER(10,2) NOT NULL
);

CREATE TABLE bom (
  model_id         VARCHAR2(4) REFERENCES machine_models,
  part_id          VARCHAR2(8) REFERENCES parts,
  failure_mode     VARCHAR2(32) NOT NULL,
  qty              NUMBER(4) NOT NULL,
  replace_every_h  NUMBER(6),
  CONSTRAINT bom_pk PRIMARY KEY (model_id, part_id, failure_mode)
);

-- Stock por ubicacion (problema 3). La reserva es un UPDATE condicional atomico en FLEET_EVENTS.
CREATE TABLE part_stock (
  part_id        VARCHAR2(8)  REFERENCES parts,
  zone_id        VARCHAR2(16) REFERENCES zones,
  on_hand        NUMBER(6) NOT NULL CHECK (on_hand >= 0),
  reserved       NUMBER(6) DEFAULT 0 NOT NULL CHECK (reserved >= 0),
  reorder_point  NUMBER(6) NOT NULL,
  CONSTRAINT part_stock_pk PRIMARY KEY (part_id, zone_id),
  CONSTRAINT part_stock_reserved_le_on_hand CHECK (reserved <= on_hand)
);

CREATE TABLE incidents (
  incident_id     VARCHAR2(40) PRIMARY KEY,
  machine_id      VARCHAR2(8) NOT NULL REFERENCES machines,
  zone_id         VARCHAR2(16) REFERENCES zones,
  group_id        VARCHAR2(40),
  severity        VARCHAR2(16) NOT NULL CHECK (severity IN ('observe', 'schedule', 'immediate')),
  status          VARCHAR2(16) NOT NULL CHECK (status IN ('open', 'assigned', 'closed')),
  score           NUMBER(6,2),
  failure_mode    VARCHAR2(32),
  opened_at       TIMESTAMP WITH TIME ZONE NOT NULL,
  assigned_at     TIMESTAMP WITH TIME ZONE,
  closed_at       TIMESTAMP WITH TIME ZONE,
  downtime_h      NUMBER(8,2),
  tech_id         VARCHAR2(8) REFERENCES technicians,
  approval        VARCHAR2(16),
  explanation     VARCHAR2(1000),
  explanation_src VARCHAR2(16),
  cause_summary   VARCHAR2(1000),
  recurrence_flag CHAR(1) DEFAULT 'N' NOT NULL CHECK (recurrence_flag IN ('Y', 'N'))
);
CREATE INDEX incidents_machine_ix ON incidents (machine_id, opened_at);
CREATE INDEX incidents_status_ix ON incidents (status);

-- Linea de tiempo del incidente: un registro por evento de dominio aplicado
CREATE TABLE incident_events (
  event_id     VARCHAR2(36) PRIMARY KEY,
  incident_id  VARCHAR2(40) NOT NULL,
  type         VARCHAR2(40) NOT NULL,
  actor        VARCHAR2(40),
  payload      CLOB CHECK (payload IS JSON),
  ts           TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX incident_events_inc_ix ON incident_events (incident_id, ts);

CREATE TABLE part_reservations (
  reservation_id  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  incident_id     VARCHAR2(40) NOT NULL,
  part_id         VARCHAR2(8) NOT NULL REFERENCES parts,
  zone_id         VARCHAR2(16) NOT NULL REFERENCES zones,
  qty             NUMBER(4) NOT NULL CHECK (qty > 0),
  status          VARCHAR2(16) DEFAULT 'reserved' NOT NULL
                  CHECK (status IN ('reserved', 'consumed', 'released')),
  created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX part_reservations_inc_ix ON part_reservations (incident_id);

CREATE TABLE part_movements (
  movement_id  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  part_id      VARCHAR2(8) NOT NULL REFERENCES parts,
  zone_id      VARCHAR2(16) NOT NULL REFERENCES zones,
  qty          NUMBER(6) NOT NULL,
  kind         VARCHAR2(16) NOT NULL CHECK (kind IN ('consumption', 'restock')),
  incident_id  VARCHAR2(40),
  ts           TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX part_movements_ix ON part_movements (part_id, zone_id, ts);

CREATE TABLE maintenance_orders (
  order_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  machine_id   VARCHAR2(8) NOT NULL REFERENCES machines,
  kind         VARCHAR2(16) NOT NULL CHECK (kind IN ('preventive', 'corrective')),
  due_at       TIMESTAMP WITH TIME ZONE,
  status       VARCHAR2(16) DEFAULT 'suggested' NOT NULL
               CHECK (status IN ('suggested', 'scheduled', 'done', 'cancelled')),
  incident_id  VARCHAR2(40),
  reason       VARCHAR2(400),
  created_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE reorder_suggestions (
  id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  part_id       VARCHAR2(8) NOT NULL REFERENCES parts,
  zone_id       VARCHAR2(16) NOT NULL REFERENCES zones,
  qty           NUMBER(6) NOT NULL,
  forecast_14d  NUMBER(8,2),
  source        VARCHAR2(16) DEFAULT 'forecast' NOT NULL CHECK (source IN ('forecast', 'shortage')),
  incident_id   VARCHAR2(40),
  created_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE fleet_kpis_daily (
  day          DATE,
  machine_id   VARCHAR2(8) REFERENCES machines,
  operating_h  NUMBER(5,2),
  utilization  NUMBER(5,4),
  idle_ratio   NUMBER(5,4),
  fuel_lph     NUMBER(7,2),
  fuel_vs_peers_pct NUMBER(7,2),
  cost_per_h   NUMBER(8,2),
  cluster_id   NUMBER(3),
  quadrant     VARCHAR2(16),
  CONSTRAINT fleet_kpis_daily_pk PRIMARY KEY (day, machine_id)
);

-- Inbox: idempotencia de todo lo que llega desde AWS (se inserta primero, en la misma transaccion)
CREATE TABLE processed_events (
  event_id      VARCHAR2(36) PRIMARY KEY,
  event_type    VARCHAR2(40) NOT NULL,
  processed_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

-- Vista para la ficha de maquina en APEX: historial con reincidencia y repuestos usados
CREATE OR REPLACE VIEW v_machine_history AS
SELECT i.machine_id, i.incident_id, i.severity, i.status, i.failure_mode, i.opened_at, i.closed_at,
       i.downtime_h, i.tech_id, t.name AS tech_name, i.recurrence_flag, i.cause_summary,
       (SELECT LISTAGG(m.part_id || ' x' || ABS(m.qty), ', ') WITHIN GROUP (ORDER BY m.part_id)
          FROM part_movements m WHERE m.incident_id = i.incident_id AND m.kind = 'consumption') AS parts_used
  FROM incidents i LEFT JOIN technicians t ON t.tech_id = i.tech_id;

-- Vista para el stock por zona en APEX
CREATE OR REPLACE VIEW v_stock AS
SELECT s.zone_id, s.part_id, p.sku, p.name, s.on_hand, s.reserved, s.on_hand - s.reserved AS available,
       s.reorder_point, CASE WHEN s.on_hand - s.reserved <= s.reorder_point THEN 'Y' ELSE 'N' END AS below_reorder
  FROM part_stock s JOIN parts p ON p.part_id = s.part_id;
