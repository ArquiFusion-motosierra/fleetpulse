// Cuentas de evaluacion del user pool de Cognito, agrupadas por parque para la pantalla de login.
// Debe coincidir con scripts/cognito-seed.sh (dominio por defecto example.com). La contrasena va solo en el README.
const domain = "example.com";
const SEED = [
  ["operador", "operator", "mineria"], ["tecnico", "technician", "mineria", "TEC-01"], ["supervisor", "supervisor", "mineria"],
  ["direccion", "management", "mineria"], ["admin", "admin", "mineria"], ["admin.mineria", "admin", "mineria"],
  ["operador.agro", "operator", "agro"], ["tecnico.agro", "technician", "agro", "TEC-11"], ["supervisor.agro", "supervisor", "agro"],
  ["direccion.agro", "management", "agro"], ["admin.agro", "admin", "agro"],
  ["operador.industria", "operator", "industria"], ["tecnico.industria", "technician", "industria", "TEC-21"],
  ["supervisor.industria", "supervisor", "industria"], ["direccion.industria", "management", "industria"],
  ["admin.industria", "admin", "industria"],
  ["jefe.mantenimiento", "maintenance_head", "all"],
];
export const DEMO_ACCOUNTS = SEED.map(([name, role, tenant, tech_id]) => ({ tenant, email: `${name}@${domain}`, role, tech_id }));
