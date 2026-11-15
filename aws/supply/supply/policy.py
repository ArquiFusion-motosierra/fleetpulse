"""Punto de reorden y cantidad (EOQ-lite) del diseno de abastecimiento, 5.1 y 5.2. Funciones puras.

d = demanda diaria (MVP: consumo de 30 dias / 30; el preventivo de daily-planning y los reportes abiertos llegan con
T45). Con L = lead time del proveedor mas barato (dias) y z por criticidad:
    sigma = sqrt(d)                                  Poisson: pocos datos, honesto para la demo
    SS    = max(safety_stock, ceil(z * sigma * sqrt(L)))
    ROP   = max(reorder_point, ceil(d * L) + SS)     el reorder_point del seed es el piso
    EOQ   = ceil(sqrt(2 * D * S / H))                D = 365 d, S = USD 50 por orden, H = 25 % anual del costo
    pedido = max(EOQ, min_qty, ROP + SS - posicion)  solo si posicion = available + on_order + in_transit < ROP
"""
from __future__ import annotations

import math

Z = {"A": 2.05, "B": 1.65, "C": 1.28}
ORDER_COST_USD = 50
HOLDING_RATE = 0.25


def daily_demand(consumed_30d: float) -> float:
    return max(0.0, float(consumed_30d)) / 30


def plan(*, d: float, lead_time_d: float, criticality: str, safety_stock: int, reorder_point: int,
         unit_price_usd: float, min_qty: int, available: int, on_order: int = 0, in_transit: int = 0) -> dict:
    sigma = math.sqrt(d)
    ss = max(int(safety_stock), math.ceil(Z.get(criticality, Z["C"]) * sigma * math.sqrt(lead_time_d)))
    rop = max(int(reorder_point), math.ceil(d * lead_time_d) + ss)
    holding = HOLDING_RATE * float(unit_price_usd)
    eoq = math.ceil(math.sqrt(2 * d * 365 * ORDER_COST_USD / holding)) if d > 0 and holding > 0 else 0
    position = available + on_order + in_transit
    qty = max(eoq, int(min_qty), rop + ss - position) if position < rop else 0
    coverage = round((available + in_transit) / d, 1) if d > 0 else None  # None = infinito (sin demanda)
    return {"d": round(d, 3), "ss": ss, "rop": rop, "eoq": eoq, "position": position, "order_qty": qty,
            "days_of_coverage": coverage}
