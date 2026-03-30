# SISTRIX Visibility LED Ticker

A real-time LED panel that displays [SISTRIX](https://www.sistrix.com/) Visibility Index data on a 64x32 RGB matrix, with a full web panel for configuration and live preview.

Built with a Raspberry Pi 4 and a HUB75 LED panel. Fully configurable from any browser on your network.

<!-- ![LED Panel Photo](screenshots/panel.jpg) -->
<!-- ![Web Panel Screenshot](screenshots/web-panel.png) -->

## Features

### LED Display
- **64x32 RGB pixel matrix** with bitmap fonts (3x5, 4x6, 5x7) and variable scaling
- **Domain rotation**: auto-cycles through active domains showing label, value, change %, country, and sparkline chart
- **Brand card**: custom logo (pixel art), name, and scrolling message with rainbow/gradient color support
- **Smart colors**: hex, rainbow spectrum, and two-color gradient — all configurable per element
- **Physical button** (optional): GPIO-connected momentary button to toggle screen on/off
- **Instant startup**: loads cached data immediately, defers API refresh
- **Instant domain sync**: adding or removing domains updates the display immediately from cache
- **Color correction**: compensates for LED panel warm tint on white/gray colors
- **Demo mode**: shows sample data when no domains are configured
- **Flicker-free**: double-buffered rendering with `SwapOnVSync`

### Web Panel
- **LED simulator**: real-time 64x32 pixel preview with LED glow effect — what you see is what you get
- **Layout editor**: drag to reposition, resize by corners, double-click to edit text
- **Domain management**: add, remove, reorder (drag & drop), toggle active/inactive, switch weekly/daily mode
- **Brand card editor**: custom logo, name, scrolling message, color picker with gradient support
- **Smart caching**: reduces API credit usage (weekly data cached 24h, daily cached 6h)
- **Refresh with cost awareness**: shows credits consumed and available after each update
- **Screen on/off**: syncs between web panel and physical button in real-time
- **Slide sync**: web panel follows the LED panel's current slide in real-time
- **Edit mode**: rotation pauses automatically while editing brand card or layout
- **6 languages**: English, Spanish, French, Italian, German, Portuguese
- **Responsive**: works on desktop and mobile
- **Accessible**: WCAG 2.1 AA compliant (keyboard navigation, ARIA, focus trapping)
- **Secure**: API key never exposed to frontend, XSS protection, thread-safe config

## Hardware

### Required

| Component | Link | Notes |
|-----------|------|-------|
| Raspberry Pi 4 | [Raspberry Pi 4 4GB Starter Kit](https://amzn.to/4s3yoKr) | Pi 5 **not compatible** with Adafruit Bonnet |
| HUB75 RGB LED Panel 64x32 P3 | [Waveshare 64x32 P3](https://amzn.to/4bDSEgK) | 192x96mm, pitch 3mm |
| Adafruit RGB Matrix Bonnet | [RGB Matrix Bonnet](https://amzn.to/4bUhBDx) | GPIO HAT for driving the panel |
| 5V 3A+ Power Supply | [5V 3A DC adapter](https://amzn.to/4rZKp3y) | 5.5x2.1mm barrel jack. A USB charger + barrel jack adapter works too |

### Optional

| Component | Link | Notes |
|-----------|------|-------|
| Momentary push button | [Gebildet 12mm button](https://amzn.to/4dR8sOB) | Wired to GPIO 19 + GND — toggles screen on/off |
| Soldering kit | [60W soldering iron set](https://amzn.to/47zmeSe) | For soldering button wires to GPIO pins |
| Step drill bit | [Flintronic step drill set](https://amzn.to/4dPPqrS) | For drilling clean holes in the case (button, cables) |
| 3D printed case | Search on Etsy for "64x32 P3 LED case" | 192x96mm, depth 13–15mm. P3 = 3mm pixel pitch |
| M3 screws | [M3 screw set (560 pcs)](https://amzn.to/4uYcMlb) | Multiple lengths + nuts + washers — mount the panel to the case |

> **About the case**: I had a friend 3D-print a case sized 192x96x14mm for the P3 64x32 panel. I used a step drill bit to make holes for the button and cable passthrough, and M3 screws to secure the panel. You can find similar cases on Etsy — search for "64x32 P3 LED panel case".

> **No hardware?** The web panel works standalone as a full LED simulator — no Pi or panel needed for testing.

## Setup

### 1. Install dependencies on the Pi

```bash
git clone https://github.com/natzir/sistrix-led-ticker.git ~/sistrix-led
cd ~/sistrix-led
bash setup.sh
```

This installs Python dependencies, creates systemd services, and starts the web panel.

### 2. Install the RGB matrix driver

```bash
cd ~
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix
pip install -e . --break-system-packages
```

### 3. Open the web panel

Navigate to `http://raspberrypi.local:5001` from any device on your network.

1. Enter your SISTRIX API key
2. Add domains (e.g. `reddit.com`, country `us`, label `RDDT`)
3. Choose weekly or daily mode
4. Adjust brightness, rotation speed, and colors

### 4. Enable the LED display (when hardware is connected)

```bash
sudo systemctl enable sistrix-display
sudo systemctl start sistrix-display
```

## Configuration

All settings are stored in `config.json` and can be edited from the web panel. A clean `config.default.json` is used as template for new installations.

### Display settings

| Setting | Range | Default | Description |
|---------|-------|---------|-------------|
| `brightness` | 10–100 | 40 | LED panel brightness |
| `cycle_seconds` | 3–60 | 10 | Seconds per domain slide |
| `refresh_minutes` | 10–1440 | 60 | API refresh interval |

### Domain object

```json
{
  "domain": "example.com",
  "country": "es",
  "label": "EXMP",
  "mode": "weekly",
  "type": "domain",
  "active": true
}
```

Supported types: `domain`, `host`, `path`, `url`

### Layout customization

Every text element (label, value, mode, country, change %, sparkline) has configurable:
- **Position**: X, Y coordinates on the 64x32 grid
- **Font**: small (3x5), medium (4x6), large (5x7)
- **Height**: custom scaling via Bresenham distribution
- **Color**: hex (`#ffffff`), `rainbow`, or gradient (`gradient:#ff0000:#0000ff`)

## API Credit Usage

Each data point from SISTRIX costs 1 credit. The built-in smart cache minimizes usage:

- **Weekly mode**: ~52 data points on first fetch, then cached for 24h
- **Daily mode**: ~100 data points on first fetch, then cached for 6h
- **Quick check**: 1 credit to check if new data is available before full fetch
- **Refresh button**: only fetches missing data points, shows credits consumed

## Project Structure

```
sistrix-led/
├── display.py           # LED panel rendering + GPIO button
├── web_panel.py         # Flask web panel (HTML/CSS/JS inline)
├── config.default.json  # Default configuration template
├── config.json          # User configuration (auto-created)
├── state.json           # Current slide state (LED ↔ web sync)
├── setup.sh             # Installation script
└── cache/               # Cached API responses
    ├── RDDT_us_weekly.json
    └── ...
```

## Physical Button (Optional)

Wire a momentary push button between **GPIO 19** (pin 35) and **GND** (any ground pin).

The button toggles the screen on/off. State syncs with the web panel within 3 seconds.

If no button is connected, control the screen from the web panel using the **⏻** button.

## Running Without Hardware

For development or testing, run the web panel locally:

```bash
pip install flask pillow requests
python3 web_panel.py
```

Open `http://localhost:5001` — the LED simulator works without any Pi or LED panel.

## Tech Stack

- **Python 3** — Flask, Pillow, requests
- **[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)** — HUB75 LED driver
- **RPi.GPIO** — physical button input
- **Vanilla JS** — Canvas API, no frameworks

## License

MIT

## Author

[Natzir Turrado](https://natzir.com) — SEO consultant based in Barcelona.

**Blog post (Spanish):** [SISTRIX LED Ticker: montaje de Raspberry Pi + LED Matrix Panel](https://natzir.com/posicionamiento-buscadores/sistrix-led-ticker-montaje-de-raspberry-pi-led-matrix-panel/)
