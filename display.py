#!/usr/bin/env python3
"""
SISTRIX Visibility Index LED Matrix Display
============================================
Displays the SISTRIX visibility index on a 64x32 RGB LED panel.

Features:
- Multi-domain rotation
- Modes: weekly (last year) / daily (last month)
- Remote configuration via web panel
- Auto-reload config without restart

Requires: Raspberry Pi + HUB75 64x32 panel + Adafruit Bonnet
"""

import time
import json
import os
import sys
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

# ============================================================
# PATHS
# ============================================================
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_DEFAULT = BASE_DIR / "config.default.json"
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

if not CONFIG_PATH.exists() and CONFIG_DEFAULT.exists():
    import shutil
    shutil.copy(CONFIG_DEFAULT, CONFIG_PATH)

PANEL_ROWS = 32
PANEL_COLS = 64

# ============================================================
# CONFIG LOADER (auto-reload on file change)
# ============================================================

class Config:
    def __init__(self):
        self._last_mtime = 0
        self._data = {}
        self.reload()

    def reload(self):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != self._last_mtime:
                with open(CONFIG_PATH) as f:
                    self._data = json.load(f)
                self._last_mtime = mtime
                print(f"[CONFIG] Reloaded ({len(self.active_domains)} active domains)")
        except Exception as e:
            print(f"[CONFIG ERROR] {e}")

    @property
    def api_key(self) -> str:
        return self._data.get("sistrix_api_key", "")

    @property
    def brightness(self) -> int:
        return self._data.get("display", {}).get("brightness", 60)

    @property
    def cycle_seconds(self) -> int:
        return self._data.get("display", {}).get("cycle_seconds", 10)

    @property
    def refresh_minutes(self) -> int:
        return self._data.get("display", {}).get("refresh_minutes", 60)

    @property
    def all_domains(self) -> list:
        return self._data.get("domains", [])

    @property
    def screen_off(self) -> bool:
        return self._data.get("display", {}).get("screen_off", False)

    @property
    def active_domains(self) -> list:
        return [d for d in self.all_domains if d.get("active", False)]

    @property
    def brand(self) -> dict:
        return self._data.get("brand", {})

    def set_screen_off(self, value: bool):
        """Write screen_off to config.json (used by GPIO button)."""
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            data.setdefault("display", {})["screen_off"] = value
            with open(CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
            self._data = data
            self._last_mtime = os.path.getmtime(CONFIG_PATH)
            print(f"[SCREEN] {'OFF' if value else 'ON'}")
        except Exception as e:
            print(f"[ERROR] set_screen_off: {e}")


config = Config()


# ============================================================
# GPIO BUTTON (physical on/off)
# ============================================================

BUTTON_GPIO = 26  # GPIO26 — free pin not used by Adafruit Bonnet

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

def _button_pressed(channel):
    """Callback for physical button press — toggles screen_off."""
    config.reload()
    config.set_screen_off(not config.screen_off)

def setup_button():
    if not HAS_GPIO:
        print("[INFO] RPi.GPIO not available — no physical button support")
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(BUTTON_GPIO, GPIO.FALLING, callback=_button_pressed, bouncetime=300)
        print(f"[BUTTON] GPIO{BUTTON_GPIO} configured (press to toggle screen)")
    except Exception as e:
        print(f"[BUTTON] GPIO error (button disabled): {e}")

# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class VisibilityData:
    domain: str
    label: str
    country: str
    mode: str  # "weekly" or "daily"
    current_value: float = 0.0
    previous_value: float = 0.0
    history: list = field(default_factory=list)
    last_updated: Optional[datetime] = None

    @property
    def change_pct(self) -> float:
        if self.previous_value == 0:
            return 0.0
        return ((self.current_value - self.previous_value) / self.previous_value) * 100

    @property
    def is_up(self) -> bool:
        return self.current_value >= self.previous_value

    @property
    def mode_label(self) -> str:
        return "D" if self.mode == "daily" else "W"


# ============================================================
# SISTRIX API
# ============================================================

def fetch_visibility(domain_config: dict) -> Optional[VisibilityData]:
    """
    Fetches SISTRIX visibility data.
    mode=weekly → history=true (weekly data, ~1 year)
    mode=daily  → daily=true (daily data, ~3 months)
    """
    domain = domain_config["domain"]
    country = domain_config["country"]
    label = domain_config["label"]
    mode = domain_config.get("mode", "weekly")

    url = "https://api.sistrix.com/domain.sichtbarkeitsindex"
    params = {
        "api_key": config.api_key,
        "domain": domain,
        "country": country,
        "format": "json",
    }

    # history=true always to get historical series
    # daily=true additionally for daily data (~30 days)
    params["history"] = "true"
    if mode == "daily":
        params["daily"] = "true"

    cache_file = CACHE_DIR / f"{label}_{country}_{mode}.json"

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("answer", [{}])[0].get("sichtbarkeitsindex", [])
        if not entries:
            print(f"[WARN] No data for {domain} ({country}) mode={mode}")
            return None

        # Sort by date desc
        entries.sort(key=lambda x: x.get("date", ""), reverse=True)

        # Limit points by mode
        if mode == "daily":
            max_points = 30   # Last month of daily data
        else:
            max_points = 52   # Last year of weekly data

        history_values = [float(e.get("value", 0)) for e in entries[:max_points]]
        current = history_values[0] if history_values else 0
        previous = history_values[1] if len(history_values) > 1 else current

        vd = VisibilityData(
            domain=domain,
            label=label,
            country=country,
            mode=mode,
            current_value=current,
            previous_value=previous,
            history=history_values,
            last_updated=datetime.now(),
        )

        # Local cache
        with open(cache_file, "w") as f:
            json.dump({
                "current_value": current,
                "previous_value": previous,
                "history": history_values,
                "updated": datetime.now().isoformat(),
                "cached_at": datetime.now().isoformat(),
            }, f)

        print(f"[OK] {label} ({country}) [{mode}]: {current:.2f} ({vd.change_pct:+.1f}%)")
        return vd

    except Exception as e:
        print(f"[ERROR] {domain}: {e}")

        # Try loading from cache
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    cached = json.load(f)
                print(f"[CACHE] Using cache for {label}")
                return VisibilityData(
                    domain=domain,
                    label=label,
                    country=country,
                    mode=mode,
                    current_value=cached["current_value"],
                    previous_value=cached["previous_value"],
                    history=cached["history"],
                    last_updated=datetime.fromisoformat(cached["updated"]),
                )
            except Exception:
                pass
        return None


def fetch_all_active() -> list[VisibilityData]:
    """Fetches data for all active domains."""
    config.reload()
    results = []
    for d in config.active_domains:
        vd = fetch_visibility(d)
        if vd:
            results.append(vd)
        time.sleep(0.5)  # Rate limiting
    return results


# ============================================================
# RENDERER — Bitmap fonts identical to web panel
# ============================================================

from PIL import Image

# Bitmap fonts — exact same as web panel JS
F3x5 = {
    '0':'111101101101111','1':'010110010010111','2':'111001010100111','3':'111001111001111',
    '4':'101101111001001','5':'111100111001111','6':'111100111101111','7':'111001001010010',
    '8':'111101111101111','9':'111101111001111',
    'A':'010101111101101','B':'110101110101110','C':'011100100100011','D':'110101101101110',
    'E':'111100111100111','F':'111100111100100','G':'011100101101011','H':'101101111101101',
    'I':'111010010010111','J':'001001001101010','K':'101110100110101','L':'100100100100111',
    'M':'101111111101101','N':'101101111111101','O':'010101101101010','P':'110101110100100',
    'Q':'010101101111011','R':'110101110101101','S':'111100111001111','T':'111010010010010',
    'U':'101101101101111','V':'101101101101010','W':'101101111111101','X':'101101010101101',
    'Y':'101101010010010','Z':'111001010100111',
    '.':'000000000000010',',':'000000000010100','-':'000000111000000','+':'000010111010000',
    '%':'100001010100001','!':'010010010000010',' ':'000000000000000','/':'001001010100100',
    ':':'000010000010000',
    '$':'010111110111010','&':'010101010101110',
    '(':'010100100100010',')':'010001001001010',
    '=':'000111000111000','#':'101111101111101',
    '@':'010101111101011',
}

F5x7 = {
    '0':'01110100011000110001100011000101110',
    '1':'00100011000010000100001000010011111',
    '2':'01110100010000100010001000100011111',
    '3':'01110100010000100110000011000101110',
    '4':'00010001100101010010111110001000010',
    '5':'11111100001111000001000011000101110',
    '6':'01110100001000011110100011000101110',
    '7':'11111000010001000100001000010000100',
    '8':'01110100011000101110100011000101110',
    '9':'01110100011000101111000010001001100',
    'A':'01110100011000111111100011000100000',
    'B':'11110100011111010001100011111000000',
    'C':'01110100011000010000100010111000000',
    'D':'11100100101000110001100101110000000',
    'E':'11111100001111010000100001111100000',
    'F':'11111100001111010000100001000000000',
    'G':'01110100011000010111100010111000000',
    'H':'10001100011111110001100011000100000',
    'I':'11111001000010000100001001111100000',
    'J':'00111000100001000010100100110000000',
    'K':'10001100101110010010100011000100000',
    'L':'10000100001000010000100001111100000',
    'M':'10001110111010110001100011000100000',
    'N':'10001110011010110011100011000100000',
    'O':'01110100011000110001100010111000000',
    'P':'11110100011000111110100001000000000',
    'Q':'01110100011000110101100100110100000',
    'R':'11110100011000111110100101000100000',
    'S':'01110100000111000001100010111000000',
    'T':'11111001000010000100001000010000000',
    'U':'10001100011000110001100010111000000',
    'V':'10001100011000110001010100010000000',
    'W':'10001100011000110101101010101000000',
    'X':'10001100010101000100010101000100000',
    'Y':'10001100010101000100001000010000000',
    'Z':'11111000010001000100010001111100000',
    '.':'00000000000000000000000000000000100',
    ',':'00000000000000000000000100010001000',
    '-':'00000000000000001110000000000000000',
    '+':'00000001000010011111001000010000000',
    '!':'00100001000010000100000000010000000',
    '/':'00001000100010001000100010000000000',
    ':':'00000001000000000000001000000000000',
    '%':'10001000100010001000100010000000000',
    ' ':'00000000000000000000000000000000000',
    '$':'00100011111010001110001011111000100',
    '&':'01100100100110010010101010111000000',
}

# Narrow character widths: (renderWidth, offsetFromLeft)
NARROW_3x5 = {}
NARROW_5x7 = {'.': (3, 1)}
SPACE_WIDTH = 2


def _parse_font(font_dict, width, height):
    """Parse bitmap font string to list of (row, col) pixel positions."""
    parsed = {}
    for ch, bits in font_dict.items():
        pixels = []
        for i, b in enumerate(bits):
            if b == '1':
                row = i // width
                col = i % width
                pixels.append((col, row))
        parsed[ch] = pixels
    return parsed

PF3x5 = _parse_font(F3x5, 3, 5)
PF5x7 = _parse_font(F5x7, 5, 7)


def _draw_bitmap_text(img, x, y, text, font_name, color):
    """Draw text using bitmap font, pixel by pixel. Returns total width."""
    text = text.upper()
    if font_name == "large":
        parsed = PF5x7
        src_w, src_h = 5, 7
        narrow = NARROW_5x7
    else:
        parsed = PF3x5
        src_w, src_h = 3, 5
        narrow = NARROW_3x5

    cx = x
    for ch in text:
        if ch == ' ':
            cx += SPACE_WIDTH + 1
            continue
        nr = narrow.get(ch)
        cw = nr[0] if nr else src_w
        off = nr[1] if nr else 0
        pixels = parsed.get(ch)
        if pixels:
            for px, py in pixels:
                if off <= px < off + cw:
                    dx = cx + px - off
                    dy = y + py
                    if 0 <= dx < PANEL_COLS and 0 <= dy < PANEL_ROWS:
                        img.putpixel((dx, dy), color)
        cx += cw + 1
    return cx - x


def _measure_text(text, font_name):
    """Measure text width in pixels."""
    text = text.upper()
    if font_name == "large":
        src_w = 5
        narrow = NARROW_5x7
    else:
        src_w = 3
        narrow = NARROW_3x5

    w = 0
    for i, ch in enumerate(text):
        if ch == ' ':
            w += SPACE_WIDTH + 1
        else:
            nr = narrow.get(ch)
            w += nr[0] if nr else src_w
            if i < len(text) - 1:
                w += 1
    return w


def _new_frame():
    """Create a blank 64x32 frame."""
    return Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))


def render_frame(vd: VisibilityData) -> Image.Image:
    img = _new_frame()

    layout = config._data.get("data_layout", {})

    # Colors from config
    label_color = _hex_to_rgb(layout.get("labelColor", "#ffffff"))
    value_color = _hex_to_rgb(layout.get("valueColor", "#ffffff"))
    change_up = _hex_to_rgb(layout.get("changeUpColor", "#00dc00"))
    change_down = _hex_to_rgb(layout.get("changeDownColor", "#ff2828"))
    country_color = _hex_to_rgb(layout.get("countryColor", "#444444"))
    mode_color = _hex_to_rgb(layout.get("modeColor", "#999999"))
    change_color = change_up if vd.is_up else change_down

    # --- Line 1: Label + mode + change % ---
    label_x = layout.get("labelX", 2)
    label_y = layout.get("labelY", 1)
    label_font = layout.get("labelFont", "small")
    _draw_bitmap_text(img, label_x, label_y, vd.label, label_font, label_color)

    mode_x = layout.get("modeX", 59)
    mode_y = layout.get("modeY", 8)
    mode_font = layout.get("modeFont", "small")
    _draw_bitmap_text(img, mode_x, mode_y, vd.mode_label, mode_font, mode_color)

    # Change % right-aligned
    change_str = f"{vd.change_pct:+.1f}%"
    change_y = layout.get("changeY", 1)
    change_font = layout.get("changeFont", "small")
    cw = _measure_text(change_str, change_font)
    _draw_bitmap_text(img, PANEL_COLS - cw, change_y, change_str, change_font, change_color)

    # --- Line 2: Current value + country ---
    if vd.current_value >= 100:
        value_str = f"{vd.current_value:.1f}"
    else:
        value_str = f"{vd.current_value:.2f}"

    value_x = layout.get("valueX", 2)
    value_y = layout.get("valueY", 8)
    value_font = layout.get("valueFont", "large")
    _draw_bitmap_text(img, value_x, value_y, value_str, value_font, value_color)

    country_x = layout.get("countryX", 52)
    country_y = layout.get("countryY", 10)
    country_font = layout.get("countryFont", "small")
    _draw_bitmap_text(img, country_x, country_y, vd.country, country_font, country_color)

    # --- Sparkline ---
    spark_y = layout.get("sparkY", 21)
    spark_h = layout.get("sparkH", 10)
    spark_fill = layout.get("sparkFill", True)
    spark_up = layout.get("sparkUpColor", "#00c853")
    spark_down = layout.get("sparkDownColor", "#ff2d55")
    chart_y_end = spark_y + spark_h
    spark_color = _hex_to_rgb(spark_up if vd.is_up else spark_down)

    history = list(reversed(vd.history))
    if len(history) > 1:
        min_val = min(history)
        max_val = max(history)
        val_range = max_val - min_val if max_val != min_val else 1

        num_points = min(len(history), PANEL_COLS)
        step = (PANEL_COLS - 1) / max(num_points - 1, 1)

        points = []
        for i in range(num_points):
            x = round(i * step)
            idx = len(history) - num_points + i
            normalized = (history[idx] - min_val) / val_range
            y = chart_y_end - round(normalized * spark_h)
            points.append((x, y))

        # Fill area under sparkline
        if spark_fill:
            fill_color = (spark_color[0] // 3, spark_color[1] // 3, spark_color[2] // 3)
            for i in range(len(points) - 1):
                x0, y0 = points[i]
                x1, y1 = points[i + 1]
                for x in range(x0, x1 + 1):
                    t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
                    line_y = round(y0 + t * (y1 - y0))
                    for y in range(line_y, chart_y_end + 1):
                        if 0 <= x < PANEL_COLS and 0 <= y < PANEL_ROWS:
                            img.putpixel((x, y), fill_color)

        # Draw sparkline
        for i in range(len(points) - 1):
            _draw_line(img, points[i][0], points[i][1], points[i+1][0], points[i+1][1], spark_color)

    return img


def _draw_line(img, x0, y0, x1, y1, color):
    """Bresenham line drawing."""
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        if 0 <= x0 < PANEL_COLS and 0 <= y0 < PANEL_ROWS:
            img.putpixel((x0, y0), color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def render_loading() -> Image.Image:
    img = _new_frame()
    _draw_bitmap_text(img, 4, 4, "SISTRIX", "large", (0, 120, 255))
    _draw_bitmap_text(img, 4, 18, "Loading...", "small", (80, 80, 80))
    return img


def render_no_data() -> Image.Image:
    img = _new_frame()
    _draw_bitmap_text(img, 4, 4, "NO DATA", "large", (255, 40, 40))
    _draw_bitmap_text(img, 4, 18, "Check config", "small", (80, 80, 80))
    return img


def render_brand(scroll_offset: int = 0) -> Image.Image:
    img = _new_frame()
    brand = config.brand
    if not brand or not brand.get("enabled", False):
        return render_no_data()

    layout = brand.get("layout", {})

    # --- Logo (pixel art from config) ---
    logo_pixels = brand.get("logo_pixels", [])
    logo_x = layout.get("logoX", 1)
    logo_y = layout.get("logoY", 2)
    logo_size = layout.get("logoSize", 16)
    if logo_pixels:
        rows = len(logo_pixels)
        cols = len(logo_pixels[0]) if rows > 0 else 0
        scale = max(1, logo_size // max(rows, cols, 1))
        for ry, row in enumerate(logo_pixels):
            for rx, pixel in enumerate(row):
                if isinstance(pixel, (list, tuple)) and len(pixel) == 3:
                    r, g, b = pixel
                    if r > 0 or g > 0 or b > 0:
                        for sy in range(scale):
                            for sx in range(scale):
                                px = logo_x + rx * scale + sx
                                py = logo_y + ry * scale + sy
                                if 0 <= px < PANEL_COLS and 0 <= py < PANEL_ROWS:
                                    img.putpixel((px, py), (r, g, b))

    # --- Name ---
    name = brand.get("name", "")
    name_x = layout.get("nameX", 20)
    name_y = layout.get("nameY", 7)
    name_font = layout.get("nameFont", "small")
    name_color_str = layout.get("nameColor", "#ffffff")
    name_color = _hex_to_rgb(name_color_str)
    if name:
        _draw_bitmap_text(img, name_x, name_y, name, name_font, name_color)

    # --- Message (scrolling) ---
    message = brand.get("message", "")
    msg_y = layout.get("msgY", 19)
    msg_font = layout.get("msgFont", "small")
    msg_color_str = layout.get("msgColor", "#ffffff")
    if message:
        msg_w = _measure_text(message, msg_font)
        msg_x = PANEL_COLS - (scroll_offset % (msg_w + PANEL_COLS))
        if msg_color_str == "rainbow":
            _draw_rainbow_bitmap(img, msg_x, msg_y, message, msg_font)
        else:
            msg_color = _hex_to_rgb(msg_color_str)
            _draw_bitmap_text(img, msg_x, msg_y, message, msg_font, msg_color)

    return img


def _hex_to_rgb(hex_str: str) -> tuple:
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return (255, 255, 255)


RAINBOW_COLORS = [
    (255, 0, 0), (255, 127, 0), (255, 255, 0),
    (0, 255, 0), (0, 0, 255), (75, 0, 130), (148, 0, 211),
]

def _draw_rainbow_bitmap(img, x, y, text, font_name):
    text = text.upper()
    if font_name == "large":
        parsed = PF5x7
        src_w = 5
        narrow = NARROW_5x7
    else:
        parsed = PF3x5
        src_w = 3
        narrow = NARROW_3x5

    cx = x
    for i, ch in enumerate(text):
        color = RAINBOW_COLORS[i % len(RAINBOW_COLORS)]
        if ch == ' ':
            cx += SPACE_WIDTH + 1
            continue
        nr = narrow.get(ch)
        cw = nr[0] if nr else src_w
        off = nr[1] if nr else 0
        pixels = parsed.get(ch)
        if pixels:
            for px, py in pixels:
                if off <= px < off + cw:
                    dx = cx + px - off
                    dy = y + py
                    if 0 <= dx < PANEL_COLS and 0 <= dy < PANEL_ROWS:
                        img.putpixel((dx, dy), color)
        cx += cw + 1


# ============================================================
# LED MATRIX
# ============================================================

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
    HAS_MATRIX = True
except ImportError:
    HAS_MATRIX = False
    print("[INFO] rgbmatrix not available — simulation mode (PNG)")


def setup_matrix():
    if not HAS_MATRIX:
        return None
    options = RGBMatrixOptions()
    options.rows = PANEL_ROWS
    options.cols = PANEL_COLS
    options.chain_length = 1
    options.parallel = 1
    options.hardware_mapping = "adafruit-hat"
    options.brightness = config.brightness
    options.gpio_slowdown = 5
    options.scan_mode = 0
    options.pwm_lsb_nanoseconds = 300
    options.pwm_bits = 7
    options.drop_privileges = False
    return RGBMatrix(options=options)


def display_frame(matrix, img: Image.Image):
    if matrix:
        matrix.SetImage(img)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 50)
    print("  SISTRIX Visibility LED Ticker")
    print("  Panel: 64x32 RGB | Mode: HUB75")
    print("=" * 50)

    setup_button()
    matrix = setup_matrix()
    domains_data: list[VisibilityData] = []
    last_fetch = datetime.min
    black_frame = Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))
    scroll_offset = 0

    # Show loading screen
    display_frame(matrix, render_loading())

    while True:
        config.reload()
        now = datetime.now()

        # Screen off — show black, skip everything
        if config.screen_off:
            display_frame(matrix, black_frame)
            time.sleep(1)
            continue

        # No API key or no domains → show brand card
        if not config.api_key or config.api_key == "TU_API_KEY_AQUI" or not config.active_domains:
            display_frame(matrix, render_brand(scroll_offset))
            scroll_offset += 1
            time.sleep(0.05)
            continue

        # Reload config and data if due
        if (now - last_fetch).total_seconds() > config.refresh_minutes * 60:
            print(f"\n[{now.strftime('%H:%M')}] Updating {len(config.active_domains)} domains...")
            new_data = fetch_all_active()

            if new_data:
                domains_data = new_data
            elif not domains_data:
                display_frame(matrix, render_no_data())
                time.sleep(30)
                continue

            last_fetch = now

        if not domains_data:
            display_frame(matrix, render_no_data())
            time.sleep(10)
            continue

        # Cycle through active domains + brand card
        for vd in domains_data:
            config.reload()

            if config.screen_off:
                display_frame(matrix, black_frame)
                break

            img = render_frame(vd)
            display_frame(matrix, img)

            print(f"  [{vd.label}] {vd.current_value:.2f} ({vd.change_pct:+.1f}%) [{vd.mode}]")
            time.sleep(config.cycle_seconds)

        # Brand card slide (with scrolling message)
        if config.brand.get("enabled") and not config.screen_off:
            brand_start = time.time()
            while time.time() - brand_start < config.cycle_seconds:
                config.reload()
                if config.screen_off:
                    break
                display_frame(matrix, render_brand(scroll_offset))
                scroll_offset += 1
                time.sleep(0.05)


if __name__ == "__main__":
    main()
