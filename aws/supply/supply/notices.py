"""Textos de los correos de supply-flow (SNS fp-notify con atributos `role` y `tenant_id`). Subject ASCII de una
linea y < 100 caracteres, como exige SNS. Nunca incluyen el task token: se aprueba y se recibe en la consola."""
from __future__ import annotations

ROLE_NAMES = {"maintenance_head": "jefe de mantenimiento", "admin": "admin del parque", "supervisor": "supervisor"}


def _what(doc: dict) -> str:
    if doc["kind"] == "transfer":
        return (f"transferencia {doc['id']}: {doc['qty']} x {doc['part_id']} de {doc['from_zone']} "
                f"({doc.get('from_tenant')}) a {doc['zone_id']} ({doc['tenant_id']})")
    return (f"orden de compra {doc['id']}: {doc['qty']} x {doc['part_id']} a {doc.get('supplier_id')} para "
            f"{doc['zone_id']} ({doc['tenant_id']}), USD {doc.get('cost_usd')}")


def _incident(doc: dict) -> str:
    return f" Resuelve el faltante del incidente {doc['incident_id']}." if doc.get("incident_id") else ""


def _subject(text: str) -> str:
    return text.encode("ascii", "ignore").decode()[:99]


def notify_role(doc: dict) -> str:
    """A quien se le pide la aprobacion: el jefe si solo el puede; si no, el admin del parque destino."""
    roles = doc.get("approver_roles") or ["maintenance_head"]
    return "maintenance_head" if roles == ["maintenance_head"] else "admin"


def approval_request(doc: dict) -> dict:
    roles = ", ".join(ROLE_NAMES.get(r, r) for r in doc.get("approver_roles") or [])
    return {"subject": _subject(f"[FleetPulse] Aprobar {doc['id']}"),
            "message": (f"Nueva sugerencia de abastecimiento: {_what(doc)}. Llega en {doc.get('eta_h')} h; evita "
                        f"{doc.get('downtime_avoided_h', 0)} h de parada (USD {doc.get('downtime_avoided_usd', 0)})."
                        f"{_incident(doc)} {doc.get('reason', '')} Aprueba: {roles}. Aprobar o rechazar en la consola, "
                        "pestana Abastecimiento (vence en 24 h).")}


def ordered(doc: dict) -> dict:
    return {"subject": _subject(f"[FleetPulse] Orden de compra {doc['id']}"),
            "message": (f"Pedido a {doc.get('supplier_id')}: {doc['qty']} x {doc['part_id']} para el almacen "
                        f"{doc['zone_id']} del parque {doc['tenant_id']}, entrega estimada {doc.get('eta')}. "
                        "Correo informativo de la demo.")}


def shipped(doc: dict) -> dict:
    return {"subject": _subject(f"[FleetPulse] En camino {doc['id']}"),
            "message": (f"Despachada la {_what(doc)}. Llegada estimada {doc.get('eta')}.{_incident(doc)} "
                        "Confirmar la recepcion en la consola al llegar.")}


def late(doc: dict) -> dict:
    return {"subject": _subject(f"[FleetPulse] Atrasada {doc['id']}"),
            "message": (f"La {_what(doc)} no se recibio a tiempo (estimada {doc.get('eta')}). Se espera una vez mas; "
                        "revisar con el parque destino.")}


def received(doc: dict, allocated: list[dict], role: str) -> dict:
    if allocated:
        incs = ", ".join(f"{a['incident_id']} ({a['machine_id']})" for a in allocated)
        body = f"Tu repuesto llego: {doc['part_id']} quedo reservado en {doc['zone_id']} para {incs}."
    else:
        body = f"Llego {doc['qty']} x {doc['part_id']} a {doc['zone_id']}; no habia incidentes esperandolo."
    if role == "admin":
        body = f"Recibida la {_what(doc)}. " + body
    return {"subject": _subject(f"[FleetPulse] Recibido {doc['part_id']} en {doc['zone_id']}"), "message": body}


def cancelled(doc: dict, reason: str) -> dict:
    return {"subject": _subject(f"[FleetPulse] Cancelada {doc['id']}"),
            "message": f"Se cancelo la {_what(doc)}. Motivo: {reason}.{_incident(doc)}"}
