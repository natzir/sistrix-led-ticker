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

- `sistrix-web.service` — Web panel Flask en puerto 5000 (user: natzir)
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

### web_panel.py
- Web panel completo en http://raspberrypi.local:5000
- **Simulador LED**: réplica visual del panel 64x32 con efecto LED (puntos circulares + glow), rotación automática, controles ◀ ▶
- **Gestión de dominios**: añadir, eliminar, activar/desactivar, cambiar modo weekly/daily
- **API key**: configurar y guardar
- **Display settings**: brillo, velocidad rotación, frecuencia refresco
- **Caché status**: ver estado de datos cacheados (fresco/caducado/hace cuánto)
- **API REST**: todos los endpoints en /api/* para gestión programática
- UI dark monospace (estilo terminal/hacker, coherente con el concepto LED)

### config.json
```json
{
  "sistrix_api_key": "TU_API_KEY_AQUI",
  "display": {
    "brightness": 60,
    "cycle_seconds": 10,
    "refresh_minutes": 60
  },
  "domains": [
    {"domain": "destinia.com", "country": "es", "label": "DEST", "mode": "weekly", "active": true},
    ...
  ]
}
```

## Estado actual

1. ✅ Pi configurada y accesible por SSH
2. ✅ rgbmatrix instalada (via pip editable desde ~/rpi-rgb-led-matrix)
3. ✅ Pillow y requests instalados
4. ✅ Archivos del proyecto creados (display.py, web_panel.py, config.json, setup.sh)
5. ⬜ Subir archivos a la Pi (scp) — PENDIENTE
6. ⬜ Ejecutar setup.sh en la Pi — PENDIENTE
7. ⬜ Configurar API key SISTRIX en el web panel — PENDIENTE
8. ⬜ Probar preview en browser — PENDIENTE
9. ⬜ Comprar y montar hardware (panel + bonnet + carcasa) — PENDIENTE
10. ⬜ Activar sistrix-display.service con panel real — PENDIENTE

## Notas para desarrollo

- El Adafruit RGB Matrix Bonnet NO soporta Pi 5. Usar Pi 4.
- `display.py` debe ejecutarse como root (acceso GPIO): `sudo python3 display.py`
- El flag `--led-gpio-mapping="adafruit-hat"` es necesario para el Bonnet de Adafruit
- El flag `--led-slowdown-gpio=2` es necesario para Pi 4
- Las fuentes usadas son DejaVu Sans Mono (ya instaladas en Raspberry Pi OS)
- El web panel sirve todo el HTML/CSS/JS inline (single-file Flask app, sin archivos estáticos)
- El simulador LED en el browser usa Canvas API con renderizado pixel-art (image-rendering: pixelated)

## Próximos pasos posibles

- Mejorar el simulador LED (más realismo, background grid)
- Añadir más métricas: keywords totales, top keywords ganadas/perdidas
- Alertas: notificación si la visibilidad cae más de X% (push, email, Telegram)
- Modo comparación: dos dominios lado a lado
- Scroll horizontal para labels largos
- Integración con GSC (Google Search Console) además de SISTRIX
- OTA updates: actualizar código desde el web panel
- Logs viewer en el web panel
