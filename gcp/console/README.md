# console (T15)

Cloud Run `console`: React + Vite + MapLibre (satélite de Esri, sin llave) compilado a estáticos y servido por FastAPI, que también es el BFF `/api/*` hacia las APIs de AWS. La API key de AWS se lee de GCP Secret Manager (`aws-console-api-key`) con la identidad de Cloud Run; nunca llega al navegador ni a variables de entorno.

| Pantalla | Qué muestra |
|---|---|
| Mapa | 100 máquinas con color por severidad (verde, amarillo, naranja, rojo), zonas, incidentes activos; refresco cada 15 s |
| Incidente | Causa (Gemini o plantilla), repuestos reservados, línea de tiempo, grupo por zona, reincidencia y botón **Cerrar en AWS** (con login va a `POST /me/incidents/{id}/close` con el ID token: cierran supervisor, admin y el técnico asignado, y `closed_by` sale del token) |
| Ficha de máquina | Modelo, estado, horas, historial con tiempo detenido, repuestos usados y reincidencias |
| Stock | Disponible / en mano / reservado por repuesto y zona, marcando lo que está bajo el punto de reorden |

| Comando | |
|---|---|
| `cd gcp/console && MOCK=1 uvicorn app.main:app --port 8080` + `cd web && npm run dev` | Desarrollo local con datos de prueba (http://localhost:5173) |
| `cd gcp/console/web && npm run build` y `MOCK=1 uvicorn app.main:app --port 8080` | Igual que en Cloud Run (http://localhost:8080) |
| `pytest -q gcp/console/tests` | Tests del BFF |
| `gcp/console/deploy.sh` | Despliega (ver variables y pasos únicos en el script). `MOCK=1` para un respaldo de grabación |
