# Auditoria de recursos graficos - StockArmobile

Fecha: 2026-07-14

## 1) Resumen

Se preparo una estructura centralizada para branding y recursos visuales sin cambiar logica de negocio ni eliminar assets legacy.

Objetivo de migracion de branding:

- Ruta objetivo estable: `static/images/branding/`
- Compatibilidad mantenida con recursos anteriores en `static/assets/icons/`

## 2) Estructura creada

- `static/images/branding/`
- `static/images/products/`
- `static/images/users/`
- `static/images/uploads/`
- `static/images/categories/`
- `static/images/banners/`
- `static/images/tutorials/`
- `static/images/placeholders/`

Se agrego `README.md` en `static/images/branding/` y `.gitkeep` en carpetas vacias.

## 3) Branding preparado (placeholders actuales)

Archivos en `static/images/branding/`:

- `logo.png`
- `logo-dark.png`
- `logo-light.png`
- `isotipo.png`
- `isotipo-dark.png`
- `isotipo-light.png`
- `favicon.ico`
- `apple-touch-icon.png`
- `icon-192.png`
- `icon-512.png`
- `splash.png`
- `logo-pdf.png`
- `logo-email.png`

Nota: por ahora son placeholders tecnicos para no romper rutas. Deben reemplazarse por los artes finales.

## 4) Referencias encontradas y preparadas

### Referencias actualizadas a branding centralizado

- `templates/base_master.html`
  - `favicon.ico`
  - `apple-touch-icon.png`
- `templates/landing/index.html`
  - `favicon.ico`
  - `apple-touch-icon.png`
- `app.py`
  - OG image hacia `images/branding/icon-512.png`
  - rutas de compatibilidad: `/favicon.ico` y `/apple-touch-icon.png`
- `static/manifest.json`
  - iconos PWA y shortcuts hacia `static/images/branding/`
- `static/service-worker.js`
  - pre-cache de iconos PWA en `static/images/branding/`
- `templates/referrals/dashboard.html`
  - descarga de logo hacia `images/branding/logo.png`
- `referrals.py`
  - ZIP de imagenes usando branding centralizado con fallback legacy

### Referencias visuales adicionales detectadas (no branding)

- `referrals.py` usa piezas de material comercial en `static/assets/social/*.svg` y videos en `static/assets/videos/*.mp4`.
- `products.py` usa carga dinamica de fotos en `/static/uploads/products/...`.

## 5) Inventario de imagenes actuales en static/

Archivos detectados:

- `static/assets/icons/icon-192.png`
- `static/assets/icons/icon-512.png`
- `static/assets/social/facebook-post.svg`
- `static/assets/social/instagram-post.svg`
- `static/assets/social/linkedin-post.svg`
- `static/assets/social/story-post.svg`
- `static/assets/social/thumb-como-vender.svg`
- `static/assets/social/thumb-demo-30.svg`
- `static/assets/social/thumb-demo-60.svg`
- `static/assets/social/thumb-demo-90.svg`
- `static/assets/social/thumb-referidos.svg`
- `static/assets/social/whatsapp-status.svg`

## 6) Duplicados

Analisis por SHA256:

- No se encontraron duplicados exactos entre archivos graficos existentes.

## 7) Obsoletos y limpieza propuesta

### Candidatos a obsoleto cuando se complete branding final

- `static/assets/icons/icon-192.png`
- `static/assets/icons/icon-512.png`

Motivo: fueron reemplazados como ruta objetivo por `static/images/branding/icon-192.png` y `static/images/branding/icon-512.png`.

### Candidatos a conservar

- `static/assets/social/*.svg`: actualmente en uso por el portal de vendedores.
- `static/uploads/products/*`: activos para fotos de productos.

## 8) Reemplazo futuro de identidad visual

Cuando se carguen artes definitivos, reemplazar archivos en:

- `static/images/branding/logo.png`
- `static/images/branding/logo-dark.png`
- `static/images/branding/logo-light.png`
- `static/images/branding/isotipo.png`
- `static/images/branding/isotipo-dark.png`
- `static/images/branding/isotipo-light.png`
- `static/images/branding/favicon.ico`
- `static/images/branding/apple-touch-icon.png`
- `static/images/branding/icon-192.png`
- `static/images/branding/icon-512.png`
- `static/images/branding/splash.png`
- `static/images/branding/logo-pdf.png`
- `static/images/branding/logo-email.png`

No deberia ser necesario cambiar codigo adicional para esos archivos.
