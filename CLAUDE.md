# CLAUDE.md — SISTRIX Visibility LED Ticker

## Proyecto

Panel LED RGB tipo Tidbyt que muestra el índice de visibilidad de SISTRIX en tiempo real, con rotación de dominios, modos de visualización (weekly/daily), y gestión remota via web panel.

## Quién es el usuario

Natzir Turrado — consultor SEO independiente basado en Barcelona. Trabaja con marcas como Destinia, Civitatis, PCComponentes, Suntransfers, MediaMarkt, Freepik, etc. Tiene nivel técnico alto (Python, BigQuery, APIs), pero es su primer proyecto con hardware Raspberry Pi. Comunicar en español.

## Stack de hardware

- **Raspberry Pi 4** (ya configurada y funcionando, accesible por SSH como `natzir@raspberrypi.local`)
- **Panel LED RGB 64x32 HUB75 P3** (Waveshare, pitch 3mm, 192x96mm) — PENDIENTE DE COMPRA
- **Adafruit RGB Matrix Bonnet** — PENDIENTE DE COMPRA
- **Carcasa 3D** de Etsy (Portugal) para panel P3 64x32 — PENDIENTE DE COMPRA
- **Fuente 5V 4A** para el panel — PENDIENTE DE COMPRA
- **Botón físico** Gebildet 12mm momentary push button (blue LED, 12-24V, normally open SPST) — COMPRADO, cableado a GPIO26

## Stack de software

- **Raspberry Pi OS Lite 64-bit** (sin escritorio, solo terminal)
- **Python 3.13** (el que viene con el SO)
- **rgbmatrix** — librería de hzeller para controlar el panel HUB75. Instalada desde `~/rpi-rgb-led-matrix` via `pip install -e .` (pyproject.toml, no Makefile como en versiones antiguas)
- **Flask** — web panel para gestión remota
- **Pillow** — renderizado de frames para el panel
- **requests** — llamadas a la API de SISTRIX

## Estructura del proyecto en la Pi

```
~/sistrix-led/
├── config.json          # Configuración: API key, dominios, display settings
├── display.py           # Script principal que controla el panel LED
├── web_panel.py         # Flask web panel + simulador LED en browser
├── setup.sh             # Script de instalación (systemd services, deps)
└── cache/               # Caché de datos SISTRIX (JSON por dominio)
    ├── DEST_es_weekly.json
    ├── CVTS_es_weekly.json
    └── ...
```

## Servicios systemd

- `sistrix-web.service` — Web panel Flask en puerto 5001 (user: natzir)
- `sistrix-display.service` — Display LED (user: root, necesario para GPIO). NO activar hasta tener el hardware.

## API de SISTRIX

Endpoint principal: `https://api.sistrix.com/domain.sichtbarkeitsindex`

Parámetros:
- `api_key` — clave personal
- `domain` — dominio a consultar
- `country` — código de país (es, de, uk, fr, it, us, etc.)
- `format=json`
- `history=true` — datos semanales históricos (1 crédito por fecha devuelta)
- `daily=true` — datos diarios (~últimos 100 días, 1 crédito por fecha)

Respuesta: `answer[0].sichtbarkeitsindex` → array de `{date, value}`

**Importante sobre créditos**: cada punto de datos devuelto consume 1 crédito. La caché inteligente ya implementada reduce llamadas:
- Modo weekly → caché válida 24h (datos solo cambian 1x/semana)
- Modo daily → caché válida 6h (datos solo cambian 1x/día)

## Funcionalidades implementadas

### display.py
- Renderiza frames de 64x32 pixels con PIL
- Layout: línea 1 (label + modo D/W + cambio %), línea 2 (valor + país), parte inferior (sparkline)
- Rotación automática entre dominios activos
- Auto-recarga de config.json sin reiniciar
- Fallback a caché si la API falla
- Modo simulación (guarda PNG) si no hay hardware LED conectado
- **Screen off**: lee `screen_off` del config, muestra frame negro cuando está apagado
- **Botón GPIO**: GPIO26 con pull-up interno, FALLING edge, 300ms debounce → toggle `screen_off` en config.json

### web_panel.py
- Web panel completo en http://raspberrypi.local:5001 (puerto 5001)
- **Simulador LED**: réplica visual del panel 64x32 con efecto LED (puntos circulares + glow), rotación automática, controles ◀ ▶
- **Brand card**: tarjeta personalizada con logo (favicon o imagen URL), nombre y mensaje con scroll animado (requestAnimationFrame)
- **Layout editor**: editar posición y tamaño de elementos arrastrando/redimensionando (4 esquinas), doble-clic para editar texto. Cada elemento es independiente: LABEL, MODE, CHANGE%, VALUE, COUNTRY, SPARKLINE (sin edición de color en data layout)
- **Gestión de dominios**: añadir, eliminar, activar/desactivar, cambiar modo weekly/daily, drag & drop reorder
- **API key**: configurar y guardar (validación contra endpoint /credits)
- **Display settings**: brillo, velocidad rotación, frecuencia refresco
- **Caché status**: ver estado de datos cacheados (fresco/caducado/hace cuánto)
- **i18n completo**: 6 idiomas (es, en, fr, it, de, pt) — todos los labels, titles, tooltips, overlay editor, toasts, selects y botones traducidos. Idioma se aplica antes de renderizar contenido dinámico. Key `click_again_confirm` compartida entre delete y refresh.
- **API REST**: todos los endpoints en /api/* para gestión programática
- **Accesibilidad**: WCAG 2.1 AA (ARIA combobox/listbox, focus trapping, aria-live, keyboard navigation, touch targets)
- **Performance**: gzip + ETag caching, unified `/api/init`, config mtime caching, debounced resize, visibility API polling
- **Fuentes pixel**: bitmap fonts 3x5, 4x6, 5x7 con anchos variables (`.` y espacio más estrechos). Incluyen caracteres especiales: `$`, `&`, `(`, `)`, `=`, `#`, `@`
- **Gradient color picker**: degradado de 2 colores editables, almacenado como `gradient:#color1:#color2`, interpolación por posición X del pixel
- **Color picker**: paleta de swatches + rainbow + gradient expandible + custom color
- **Change% alineado a la derecha**: último carácter siempre en LED_W-1, crece hacia la izquierda
- Canvas double-buffering (offscreen canvas → visible canvas blit)
- UI dark monospace con design tokens CSS (spacing, radius, colors)
- Responsive: funciona en desktop y mobile (touch events, CLS optimizado, touch targets 44px)
- **Footer**: enlace natzir.com + iconos sociales (X, LinkedIn, Email)
- **Refresh con confirmación**: botón ACTUALIZAR → toast "Añadirá solo los datos faltantes · Clica de nuevo para confirmar" → resultado: "Sin cambios" o "Datos actualizados · X créditos consumidos · Y créditos disponibles"
- **Toast notifications**: sistema de notificaciones con duración configurable (3.5s)
- **Favicon**: LED verde SVG data URI
- **Screen on/off**: botón ⏻ en panel header, muestra "⏻ OFF" rojo cuando apagado, sincronizado con display.py via config.json
- **Delete con toast+confirm**: patrón armed button — "¿Eliminar LABEL? · Clica de nuevo para confirmar"
- **Seguridad**: `_safe_config()` elimina API key de respuestas, `esc()` para XSS, `textContent` vs `innerHTML`
- **Thread safety**: `copy.deepcopy()` en `load_config()`, `ThreadPoolExecutor` a nivel módulo
- **config.default.json**: template limpio para nuevos usuarios (sin API key, sin dominios, brand card con logo Sistrix + "LED Ticker by Natzir" rainbow)

### config.json
```json
{
  "sistrix_api_key": "...",
  "display": {
    "brightness": 60,
    "cycle_seconds": 10,
    "refresh_minutes": 60,
    "screen_off": false
  },
  "domains": [
    {"domain": "destinia.com", "country": "es", "label": "DEST", "mode": "daily", "type": "domain", "active": true},
    ...
  ],
  "brand": { "name": "...", "message": "...", "logo_pixels": [...], "enabled": true, "layout": {...} },
  "data_layout": { "labelX": 2, "labelY": 1, "modeX": 59, "modeY": 8, ... }
}
```

## Estado actual

1. ✅ Pi configurada y accesible por SSH
2. ✅ rgbmatrix instalada (via pip editable desde ~/rpi-rgb-led-matrix)
3. ✅ Pillow y requests instalados
4. ✅ Archivos del proyecto creados y funcionando localmente
5. ✅ Web panel probado en browser (simulador LED funcional)
6. ✅ API key SISTRIX configurada y validada
7. ✅ Repositorio git inicializado
8. ✅ WCAG 2.1 AA accessibility audit completado (axe-core 0 violations)
9. ✅ Performance optimizado (gzip, ETag, unified init, CLS fixes)
10. ✅ Bug fixes: request validation, cache mutation, API response safety
11. ✅ Bitmap symbols, gradient color picker, footer/UI redesign
12. ✅ Screen on/off: web panel + display.py + GPIO button (GPIO26)
13. ✅ Security: API key sanitization, XSS protection, thread safety
14. ✅ Delete con toast+confirm, W3C validation clean
15. ✅ i18n completo: todos los labels, titles, tooltips, selects, overlays traducidos en 6 idiomas
16. ✅ Refresh UX: no-changes detection, créditos consumidos/disponibles
17. ✅ Code cleanup: dedup DEFAULT_BRAND_LAYOUT, initFormSelects(), unused params
18. ✅ Bitmap font fixes: X y W corregidas en 5x7
19. ⬜ Subir archivos a la Pi (scp) — PENDIENTE
16. ⬜ Ejecutar setup.sh en la Pi — PENDIENTE
17. ⬜ Comprar y montar hardware (panel + bonnet + carcasa) — PENDIENTE
18. ⬜ Cablear botón físico a GPIO26 + GND — PENDIENTE
19. ⬜ Activar sistrix-display.service con panel real — PENDIENTE

## Notas para desarrollo

- El Adafruit RGB Matrix Bonnet NO soporta Pi 5. Usar Pi 4.
- `display.py` debe ejecutarse como root (acceso GPIO): `sudo python3 display.py`
- El flag `--led-gpio-mapping="adafruit-hat"` es necesario para el Bonnet de Adafruit
- El flag `--led-slowdown-gpio=2` es necesario para Pi 4
- Las fuentes usadas son DejaVu Sans Mono (ya instaladas en Raspberry Pi OS)
- El web panel sirve todo el HTML/CSS/JS inline (single-file Flask app, sin archivos estáticos)
- El simulador LED en el browser usa Canvas API con renderizado pixel-art (image-rendering: pixelated)
- **IMPORTANTE**: No usar regex para modificar strings i18n del JS (riesgo de romper sintaxis). Usar Edit linea a linea.
- **IMPORTANTE**: Hacer `git commit` antes de refactorizaciones grandes
- **IMPORTANTE**: El usuario no quiere resúmenes de lo que se acaba de hacer, prefiere respuestas directas
- Para desarrollo local: `python3 web_panel.py` sirve en puerto 5001
- La caché gzip del HTML se vacía al reiniciar el servidor
- Los anchos de caracteres pixel son variables: `.` ocupa 3px y espacio 2px en fuente large
- **Puppeteer** disponible para screenshots automáticos (desktop 1280x900, mobile 390x844). Usar `puppeteer-core` con Chrome local

## Próximos pasos posibles

- Añadir más métricas: keywords totales, top keywords ganadas/perdidas
- Alertas: notificación si la visibilidad cae más de X% (push, email, Telegram)
- Modo comparación: dos dominios lado a lado
- Scroll horizontal para labels largos
- Integración con GSC (Google Search Console) además de SISTRIX
- OTA updates: actualizar código desde el web panel
- Logs viewer en el web panel
