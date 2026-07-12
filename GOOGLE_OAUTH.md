# Google OAuth 2.0

## Variables de entorno

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

## Callback / Redirect URI

- Produccion Render: `https://TU-APP.onrender.com/auth/google/callback`
- Local: `http://localhost:5000/auth/google/callback`

## Flujo

1. El usuario hace clic en `Continuar con Google`.
2. Google devuelve `email`, `name`, `given_name`, `family_name`, `picture` y `sub`.
3. Si el email ya existe, se enlaza la cuenta y se inicia sesion.
4. Si no existe, se crea usuario, empresa principal y trial de 10 dias.

## Seguridad

- Authlib gestiona `state` y protección CSRF del flujo OAuth.
- El callback se genera con `url_for(..., _external=True)` y `ProxyFix` para respetar HTTPS en Render.
- El logout limpia la sesión y mantiene el flujo tradicional de Flask-Login.