"""`/supply/analytics` (T42): resumen de abastecimiento para el grafico de la consola.

Consulta las vistas v_supply_orders y v_stockouts_daily (BigQuery, con maximum_bytes_billed, nunca
streaming) filtrando por parque. Las funciones son puras: arman la respuesta a partir de las filas, asi
los tests usan un BigQuery falso. tenant='' => todos los parques (jefe de mantenimiento).
"""
from __future__ import annotations

ORDERS_SQL = """
SELECT status, kind,
       COUNT(*) AS orders,
       SUM(IFNULL(downtime_avoided_usd, 0)) AS downtime_avoided_usd,
       SUM(IFNULL(cost_usd, 0)) AS cost_usd
FROM `{dataset}.v_supply_orders`
WHERE (@tenant = '' OR tenant_id = @tenant)
GROUP BY status, kind
"""

STOCKOUTS_SQL = """
SELECT day, part_id, zone_id, shortages, resolved, avg_resolution_hours
FROM `{dataset}.v_stockouts_daily`
WHERE (@tenant = '' OR tenant_id = @tenant)
  AND day >= DATE_SUB(CURRENT_DATE('America/Lima'), INTERVAL @days - 1 DAY)
ORDER BY day, part_id, zone_id
"""

_IN_FLIGHT = ("approved", "ordered", "in_transit")


def _num(x) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def orders_summary(rows: list[dict]) -> dict:
    """Totales por estado + parada evitada y costo, desde las filas de v_supply_orders agrupadas."""
    by_status: dict[str, int] = {}
    downtime = cost = 0.0
    total = 0
    for r in rows:
        n = int(_num(r.get("orders")))
        total += n
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + n
        downtime += _num(r.get("downtime_avoided_usd"))
        cost += _num(r.get("cost_usd"))
    return {
        "total": total,
        "by_status": by_status,
        "in_flight": sum(by_status.get(s, 0) for s in _IN_FLIGHT),
        "received": by_status.get("received", 0),
        "downtime_avoided_usd": round(downtime, 2),
        "cost_usd": round(cost, 2),
    }


def stockouts_series(rows: list[dict]) -> list[dict]:
    """Serie de quiebres por dia/repuesto/almacen para el grafico."""
    return [{
        "day": str(r.get("day")),
        "part_id": r.get("part_id"),
        "zone_id": r.get("zone_id"),
        "shortages": int(_num(r.get("shortages"))),
        "resolved": int(_num(r.get("resolved"))),
        "avg_resolution_hours": r.get("avg_resolution_hours"),
    } for r in rows]
