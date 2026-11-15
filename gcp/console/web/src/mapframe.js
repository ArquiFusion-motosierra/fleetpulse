// Puntos para encuadrar el mapa: las zonas del parque mostrado y solo las maquinas de esos parques con posicion.
export function framePoints(zones = [], machines = []) {
  const parks = new Set(zones.map((z) => z.tenant).filter(Boolean));
  const ok = (x) => x && x.lat != null && x.lon != null;
  return [
    ...zones.filter(ok),
    ...machines.filter((m) => ok(m) && (!parks.size || parks.has(m.tenant_id || "mineria"))),
  ];
}
