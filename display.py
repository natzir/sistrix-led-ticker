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
import requests
from datetime import datetime
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

BUTTON_GPIO = 19  # GPIO19 — free pin not used by Adafruit Bonnet
BUTTON_DEBOUNCE = 1.0  # seconds — debounce to avoid multiple triggers per press

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

_button_ready = False
_button_last_press = 0

def setup_button():
    global _button_ready
    if not HAS_GPIO:
        print("[INFO] RPi.GPIO not available — no physical button support")
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        _button_ready = True
        print(f"[BUTTON] GPIO{BUTTON_GPIO} configured (polling mode)")
    except Exception as e:
        print(f"[BUTTON] GPIO error (button disabled): {e}")

def poll_button():
    """Poll GPIO button — simple debounce for clean GPIO pin."""
    global _button_last_press
    if not _button_ready:
        return
    try:
        now = time.time()
        if (now - _button_last_press) < BUTTON_DEBOUNCE:
            return
        if GPIO.input(BUTTON_GPIO) == 0:
            _button_last_press = now
            config.reload()
            new_state = not config.screen_off
            print(f"[BUTTON] Pressed! screen_off → {new_state}")
            config.set_screen_off(new_state)
    except Exception as e:
        print(f"[BUTTON] Error: {e}")

def sleep_with_poll(seconds):
    """Sleep while polling the button every 100ms. Breaks early on screen state change."""
    state_before = config.screen_off
    end = time.time() + seconds
    while time.time() < end:
        poll_button()
        if config.screen_off != state_before:
            break
        time.sleep(0.1)

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


DEMO_DATA = VisibilityData(
    domain="example.com",
    label="DEMO",
    country="es",
    mode="weekly",
    current_value=12.45,
    previous_value=12.10,
    history=[12.45, 12.10, 11.82, 12.01, 11.45, 11.60, 10.95, 10.42, 10.78, 10.15, 9.87, 9.35, 9.62, 8.90, 8.55, 8.20],
)

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

F4x6 = {
    '0':'011010011001100110010110','1':'010011000100010001001111',
    '2':'011010010010010010001111','3':'011000010110000100010110',
    '4':'100110011111000100010001','5':'111110001110000100011110',
    '6':'011010001110100110010110','7':'111100010010001001000100',
    '8':'011010010110100110010110','9':'011010011001011100010110',
    'A':'011010011001111110011001','B':'111010011110100110011110',
    'C':'011010011000100010010110','D':'111010011001100110011110',
    'E':'111110001110100010001111','F':'111110001110100010001000',
    'G':'011010001011100110010111','H':'100110011111100110011001',
    'I':'011001000100010001000110','J':'001100100010001010100110',
    'K':'100110101100110010101001','L':'100010001000100010001111',
    'M':'100111111111100110011001','N':'100111011111101110011001',
    'O':'011010011001100110010110','P':'111010011001111010001000',
    'Q':'011010011001100110100101','R':'111010011001111010101001',
    'S':'011110000110000100011110','T':'111101000100010001000100',
    'U':'100110011001100110010110','V':'100110011001100101100110',
    'W':'100110011001111111111001','X':'100110010110011010011001',
    'Y':'100110010110001000100010','Z':'111100010010010010001111',
    '.':'000000000000000000000100',',':'000000000000000001001000',
    '-':'000000001111000000000000','+':'000001001111010000000000',
    '%':'100100100010010001001001','!':'010001000100010000000100',
    ' ':'000000000000000000000000','/':'000100010010010010001000',
    ':':'000001000000000001000000',
    '$':'010011111100011100110100','&':'011010010110100110010111',
    '(':'001001000100010001000010',')':'010000100010001000100100',
    '=':'000011110000111100000000','#':'010111110101111101010000',
    '@':'011010011011101100010110',
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
PF4x6 = _parse_font(F4x6, 4, 6)
PF5x7 = _parse_font(F5x7, 5, 7)


def _build_scale_map(src, dst):
    """Bresenham-distributed pixel mapping for even strokes (matches web panel)."""
    if dst == src:
        return None
    result = [0] * dst
    pos = 0
    for s in range(src):
        next_pos = round((s + 1) * dst / src)
        for p in range(next_pos - pos):
            result[pos + p] = s
        pos = next_pos
    return result


def _resolve_font(font_name, h):
    """Resolve font parameters matching web panel logic."""
    is_large = font_name == "large"
    # Native 4x6 font when small font at h=6
    if not is_large and h == 6:
        return PF4x6, 4, 6, {}, h
    # Auto-promote small to large when h >= 7
    if not is_large and h is not None and h >= 7:
        is_large = True
    if is_large:
        src_w, src_h, narrow = 5, 7, NARROW_5x7
        parsed = PF5x7
    else:
        src_w, src_h, narrow = 3, 5, NARROW_3x5
        parsed = PF3x5
    actual_h = h if h else src_h
    return parsed, src_w, src_h, narrow, actual_h


def _draw_bitmap_text(img, x, y, text, font_name, color, h=None):
    """Draw text using bitmap font, pixel by pixel. Returns total width.
    color can be RGB tuple or color string ('rainbow', 'gradient:...', '#hex').
    """
    text = text.upper()
    parsed, src_w, src_h, narrow, actual_h = _resolve_font(font_name, h)

    # Resolve color: string → dynamic, tuple → solid
    if isinstance(color, str):
        ct, cd = _parse_color_str(color)
    else:
        ct, cd = "solid", color

    def _pixel_color(dx):
        return _color_at_x(ct, cd, dx) if ct != "solid" else cd

    # Native 4x6 path (no narrow chars)
    if not narrow and parsed is PF4x6:
        cx = x
        for ch in text:
            if ch == ' ':
                cx += SPACE_WIDTH + 1
                continue
            bits = parsed.get(ch)
            if bits:
                for px, py in bits:
                    dx = cx + px
                    dy = y + py
                    if 0 <= dx < PANEL_COLS and 0 <= dy < PANEL_ROWS:
                        img.putpixel((dx, dy), _pixel_color(dx))
            cx += 5  # 4px char + 1px spacing
        return cx - x

    # Native size — fast path
    if actual_h == src_h:
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
                            img.putpixel((dx, dy), _pixel_color(dx))
            cx += cw + 1
        return cx - x

    # Scaled — Bresenham mapping (matches web panel)
    char_px_w = round(src_w * actual_h / src_h)
    char_step = char_px_w + max(1, round(actual_h / src_h))
    map_y = _build_scale_map(src_h, actual_h)
    map_x = _build_scale_map(src_w, char_px_w)
    # Build raw bitmap dict for scaled rendering
    raw_font = F5x7 if src_w == 5 else F3x5
    cx = x
    for ch in text:
        if ch == ' ':
            cx += char_step
            continue
        bits = raw_font.get(ch)
        if bits:
            for oy in range(actual_h):
                for ox in range(char_px_w):
                    if bits[map_y[oy] * src_w + map_x[ox]] == '1':
                        dx = cx + ox
                        dy = y + oy
                        if 0 <= dx < PANEL_COLS and 0 <= dy < PANEL_ROWS:
                            img.putpixel((dx, dy), _pixel_color(dx))
        cx += char_step
    return cx - x


def _measure_text(text, font_name, h=None):
    """Measure text width in pixels."""
    text = text.upper()
    parsed, src_w, src_h, narrow, actual_h = _resolve_font(font_name, h)

    # Native 4x6
    if not narrow and parsed is PF4x6:
        return len(text.replace(' ', '')) * 5 + text.count(' ') * (SPACE_WIDTH + 1)

    # Native size
    if actual_h == src_h:
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

    # Scaled
    char_px_w = round(src_w * actual_h / src_h)
    char_step = char_px_w + max(1, round(actual_h / src_h))
    return len(text) * char_step


def _new_frame():
    """Create a blank 64x32 frame."""
    return Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))


def render_frame(vd: VisibilityData) -> Image.Image:
    img = _new_frame()

    layout = config._data.get("data_layout", {})

    # Colors from config (pass strings to support rainbow/gradient)
    label_color = layout.get("labelColor", "#ffffff")
    value_color = layout.get("valueColor", "#ffffff")
    change_up = layout.get("changeUpColor", "#00dc00")
    change_down = layout.get("changeDownColor", "#ff2828")
    country_color = layout.get("countryColor", "#dddddd")
    mode_color = layout.get("modeColor", "#dddddd")
    change_color = change_up if vd.is_up else change_down

    # --- Line 1: Label + mode + change % ---
    label_x = layout.get("labelX", 2)
    label_y = layout.get("labelY", 1)
    label_font = layout.get("labelFont", "small")
    label_h = layout.get("labelH")
    _draw_bitmap_text(img, label_x, label_y, vd.label, label_font, label_color, h=label_h)

    mode_x = layout.get("modeX", 59)
    mode_y = layout.get("modeY", 8)
    mode_font = layout.get("modeFont", "small")
    mode_h = layout.get("modeH")
    _draw_bitmap_text(img, mode_x, mode_y, vd.mode_label, mode_font, mode_color, h=mode_h)

    # Change % (right-aligned by default, or at changeX if set)
    change_str = f"{vd.change_pct:+.1f}%"
    change_y = layout.get("changeY", 1)
    change_font = layout.get("changeFont", "small")
    change_h = layout.get("changeH")
    cw = _measure_text(change_str, change_font, h=change_h)
    change_x_cfg = layout.get("changeX")
    if change_x_cfg is not None:
        change_x = change_x_cfg
    else:
        change_x = PANEL_COLS - 1 - cw
    _draw_bitmap_text(img, change_x, change_y, change_str, change_font, change_color, h=change_h)

    # --- Line 2: Current value + country ---
    if vd.current_value >= 100:
        value_str = f"{vd.current_value:.1f}"
    else:
        value_str = f"{vd.current_value:.2f}"

    value_x = layout.get("valueX", 2)
    value_y = layout.get("valueY", 8)
    value_font = layout.get("valueFont", "large")
    value_h = layout.get("valueH")
    _draw_bitmap_text(img, value_x, value_y, value_str, value_font, value_color, h=value_h)

    country_x = layout.get("countryX", 52)
    country_y = layout.get("countryY", 10)
    country_font = layout.get("countryFont", "small")
    country_h = layout.get("countryH")
    _draw_bitmap_text(img, country_x, country_y, vd.country, country_font, country_color, h=country_h)

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
    name_h = layout.get("nameH")
    name_color = layout.get("nameColor", "#ffffff")
    if name:
        _draw_bitmap_text(img, name_x, name_y, name, name_font, name_color, h=name_h)

    # --- Message (scrolling, matches web panel logic) ---
    message = brand.get("message", "")
    msg_y = layout.get("msgY", 19)
    msg_font = layout.get("msgFont", "small")
    msg_h = layout.get("msgH")
    msg_color = layout.get("msgColor", "#ffffff")
    if message:
        msg_w = _measure_text(message, msg_font, h=msg_h)
        total_cycle = msg_w + PANEL_COLS
        draw_x = PANEL_COLS - (scroll_offset % total_cycle)
        _draw_bitmap_text(img, draw_x, msg_y, message, msg_font, msg_color, h=msg_h)
        _draw_bitmap_text(img, draw_x + total_cycle, msg_y, message, msg_font, msg_color, h=msg_h)

    return img


def _hex_to_rgb(hex_str: str) -> tuple:
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return (255, 255, 255)


RAINBOW_COLORS = [
    (255, 0, 0), (255, 136, 0), (255, 255, 0),
    (0, 255, 0), (0, 136, 255), (136, 0, 255), (255, 0, 255),
]


def _parse_color_str(color_str):
    """Parse a color string. Returns ('solid', rgb), ('rainbow', None), or ('gradient', (c1, c2))."""
    if color_str == "rainbow":
        return ("rainbow", None)
    if isinstance(color_str, str) and color_str.startswith("gradient:"):
        parts = color_str.split(":")
        c1 = _hex_to_rgb(parts[1]) if len(parts) > 1 else (255, 255, 255)
        c2 = _hex_to_rgb(parts[2]) if len(parts) > 2 else c1
        return ("gradient", (c1, c2))
    return ("solid", _hex_to_rgb(color_str))


def _color_at_x(color_type, color_data, px):
    """Get the color for a pixel at x position."""
    if color_type == "solid":
        return color_data
    if color_type == "rainbow":
        idx = px % (PANEL_COLS or 64)
        t = idx / max(PANEL_COLS - 1, 1)
        pos = t * (len(RAINBOW_COLORS) - 1)
        i = int(pos)
        f = pos - i
        if i >= len(RAINBOW_COLORS) - 1:
            return RAINBOW_COLORS[-1]
        c1 = RAINBOW_COLORS[i]
        c2 = RAINBOW_COLORS[i + 1]
        return (
            int(c1[0] + (c2[0] - c1[0]) * f),
            int(c1[1] + (c2[1] - c1[1]) * f),
            int(c1[2] + (c2[2] - c1[2]) * f),
        )
    if color_type == "gradient":
        c1, c2 = color_data
        t = px / max(PANEL_COLS - 1, 1)
        return (
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        )
    return (255, 255, 255)


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
    options.gpio_slowdown = 6
    options.scan_mode = 0
    options.pwm_lsb_nanoseconds = 300
    options.pwm_bits = 7
    options.drop_privileges = False
    return RGBMatrix(options=options)


_canvas = None

def display_frame(matrix, img: Image.Image):
    global _canvas
    if matrix:
        if _canvas is None:
            _canvas = matrix.CreateFrameCanvas()
        _canvas.Clear()
        _canvas.SetImage(img)
        _canvas = matrix.SwapOnVSync(_canvas)


def show_brand_scroll(matrix, scroll_offset):
    """Show brand card with scrolling message for one cycle."""
    msg_speed = config.brand.get("layout", {}).get("msgSpeed", 42) / 1000.0
    brand_start = time.time()
    while time.time() - brand_start < config.cycle_seconds:
        config.reload()
        if config.screen_off:
            break
        display_frame(matrix, render_brand(scroll_offset))
        scroll_offset += 1
        time.sleep(msg_speed)
    return scroll_offset


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 50)
    print("  SISTRIX Visibility LED Ticker")
    print("  Panel: 64x32 RGB | Mode: HUB75")
    print("=" * 50)

    matrix = setup_matrix()
    setup_button()
    domains_data: list[VisibilityData] = []
    last_fetch = datetime.min
    black_frame = Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))
    scroll_offset = 0

    # Show loading screen
    display_frame(matrix, render_loading())

    while True:
        poll_button()
        config.reload()
        now = datetime.now()

        # Screen off — show black, poll button frequently
        if config.screen_off:
            display_frame(matrix, black_frame)
            for _ in range(20):
                poll_button()
                time.sleep(0.05)
            continue

        # No active domains → rotate demo data + brand card
        if not config.active_domains:
            # Demo data slide
            display_frame(matrix, render_frame(DEMO_DATA))
            sleep_with_poll(config.cycle_seconds)
            config.reload()
            if config.screen_off:
                continue
            scroll_offset = show_brand_scroll(matrix, scroll_offset)
            continue

        # Reload config and data if due
        if (now - last_fetch).total_seconds() > config.refresh_minutes * 60:
            print(f"\n[{now.strftime('%H:%M')}] Updating {len(config.active_domains)} domains...")
            new_data = fetch_all_active()

            if new_data:
                domains_data = new_data

            last_fetch = now

        if not domains_data:
            # No data yet (no API key, no cache) → show demo + brand
            display_frame(matrix, render_frame(DEMO_DATA))
            sleep_with_poll(config.cycle_seconds)
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
            sleep_with_poll(config.cycle_seconds)

        # Brand card slide (with scrolling message)
        if config.brand.get("enabled") and not config.screen_off:
            scroll_offset = show_brand_scroll(matrix, scroll_offset)


if __name__ == "__main__":
    main()
