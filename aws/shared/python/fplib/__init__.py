"""fplib: libreria compartida de FleetPulse en AWS (Lambda layer `fp-shared`).

Reloj, logs JSON, secretos, API key, roles (authz), ID token de Google (gcp_auth), contratos, outbox/inbox, geohash,
IDs, ficha del fabricante (ids.oem_specs) y sobre de eventos.
Ningun modulo crea clientes de AWS al importarse.
"""
