// Login de la consola (T34). Modo real: Cognito Hosted UI con code + PKCE (oidc-client-ts). Modo de prueba: usuarios
// simulados que da el BFF (/api/auth/config). El ID token va en Authorization y el BFF lo reenvia a AWS.
import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const KEY = "fp.session";
let manager = null;
let config = null;

function decode(jwt) {
  try {
    return JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    return {};
  }
}

function save(session) {
  try { sessionStorage.setItem(KEY, JSON.stringify(session)); } catch { /* sin almacenamiento: dura la pestana */ }
  current = session;
}

let current = (() => {
  try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch { return null; }
})();

export async function loadConfig() {
  if (config) return config;
  const r = await fetch("/api/auth/config");
  if (!r.ok) throw new Error(r.status === 503 ? "El login no está configurado en el servidor." : `HTTP ${r.status}`);
  config = await r.json();
  if (config.mode === "cognito") {
    manager = new UserManager({
      authority: config.authority,
      client_id: config.client_id,
      redirect_uri: `${location.origin}/`,
      response_type: "code",
      scope: "openid email profile",
      userStore: new WebStorageStateStore({ store: sessionStorage }),
    });
  }
  return config;
}

// Al volver del Hosted UI con ?code=... se cambia el codigo por los tokens
export async function completeLogin() {
  if (!manager || !new URLSearchParams(location.search).has("code")) return current;
  const user = await manager.signinRedirectCallback();
  history.replaceState(null, "", location.pathname + location.hash);
  const claims = decode(user.id_token);
  save({ token: user.id_token, email: claims.email, role: claims["custom:role"], tech_id: claims["custom:tech_id"],
         name: claims.email, expires_at: claims.exp });
  return current;
}

export function loginCognito() {
  return manager.signinRedirect();
}

export function loginMock(user) {
  save({ token: user.token, email: user.email, role: user.role, tech_id: user.tech_id, name: user.name, mock: true });
  return current;
}

export function session() {
  if (current?.expires_at && current.expires_at * 1000 < Date.now()) return null;
  return current;
}

export function logout() {
  try { sessionStorage.removeItem(KEY); } catch { /* nada */ }
  const wasMock = current?.mock;
  current = null;
  if (config?.mode === "cognito" && !wasMock) {
    location.href = `${config.domain}/logout?client_id=${config.client_id}&logout_uri=${encodeURIComponent(`${location.origin}/`)}`;
  } else {
    location.reload();
  }
}
