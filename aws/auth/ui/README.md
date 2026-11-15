# Branding de la Hosted UI de Cognito (fp-auth)

Da la pantalla de login de Cognito con los colores de FleetPulse (fondo azul-noche, botón ámbar, logo)
en lugar del gris/blanco por defecto. **Son solo assets + CSS; no toca el stack ni el user pool.**

> Área AWS (Benja). Este PR solo agrega los archivos; **aplicarlos requiere credenciales AWS del Learner Lab**.

## Aplicar
Con las credenciales del lab activas (`source scripts/lab-creds.sh`):

```bash
aws/auth/ui/apply-ui.sh
```

El script lee el pool y el client de SSM (`/fleetpulse/auth/user_pool_id` y `/fleetpulse/auth/client_id`)
y corre `cognito-idp set-ui-customization` con `login.css` y `logo.png`.

### Alternativa por consola
Cognito → user pool `fleetpulse-users` → App integration → (Hosted UI) **UI customization**:
subir `logo.png` y pegar el contenido de `login.css`.

## Notas
- Es la **Hosted UI clásica**: Cognito solo permite personalizar un conjunto fijo de clases (fondo, banner,
  logo, inputs, botón, errores). **El card interno sigue claro** — no es personalizable sin migrar al
  "Managed login" nuevo, que cambia las páginas y no vale la pena a esta altura.
- `logo.png` debe pesar < 100 KB (el actual pesa ~5 KB).
- Revertir: `aws cognito-idp set-ui-customization --user-pool-id <POOL>` sin `--css` ni `--image-file`.
