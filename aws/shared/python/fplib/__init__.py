"""fplib: libreria compartida de FleetPulse en AWS (Lambda layer `fp-shared`).

Reloj, logs JSON, secretos, API key, contratos, outbox/inbox, geohash, IDs y sobre de eventos.
Ningun modulo crea clientes de AWS al importarse.
"""
