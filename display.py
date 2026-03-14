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
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

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
    def active_domains(self) -> list:
        return [d for d in self.all_domains if d.get("active", False)]


config = Config()

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
        cache_file = CACHE_DIR / f"{label}_{country}_{mode}.json"
        with open(cache_file, "w") as f:
            json.dump({
                "current_value": current,
                "previous_value": previous,
                "history": history_values,
                "updated": datetime.now().isoformat(),
            }, f)

        print(f"[OK] {label} ({country}) [{mode}]: {current:.2f} ({vd.change_pct:+.1f}%)")
        return vd

    except Exception as e:
        print(f"[ERROR] {domain}: {e}")

        # Try loading from cache
        cache_file = CACHE_DIR / f"{label}_{country}_{mode}.json"
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
# RENDERER
# ============================================================

from PIL import Image, ImageDraw, ImageFont

# Fonts — loaded once
_fonts = {}

def get_fonts():
    if not _fonts:
        try:
            _fonts["small"] = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8
            )
            _fonts["large"] = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 10
            )
        except (IOError, OSError):
            _fonts["small"] = ImageFont.load_default()
            _fonts["large"] = _fonts["small"]
    return _fonts


def render_frame(vd: VisibilityData) -> Image.Image:
    """
    Renders a 64x32 frame:
    - Line 1: LABEL [mode]     +X.X%
    - Line 2: 123.45           ES
    - Bottom: sparkline
    """
    img = Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fonts = get_fonts()

    color = (0, 220, 0) if vd.is_up else (255, 40, 40)
    white = (255, 255, 255)
    dim = (100, 100, 100)

    # --- Line 1: Label + mode + change % ---
    label_with_mode = f"{vd.label}"
    draw.text((1, 0), label_with_mode, fill=white, font=fonts["small"])

    # Mode indicator (D/W) in dim
    lw = draw.textbbox((0, 0), label_with_mode, font=fonts["small"])[2]
    draw.text((lw + 2, 0), vd.mode_label, fill=dim, font=fonts["small"])

    # Change % right-aligned
    change_str = f"{vd.change_pct:+.1f}%"
    bbox = draw.textbbox((0, 0), change_str, font=fonts["small"])
    cw = bbox[2] - bbox[0]
    draw.text((PANEL_COLS - cw - 1, 0), change_str, fill=color, font=fonts["small"])

    # --- Line 2: Current value + country ---
    if vd.current_value >= 100:
        value_str = f"{vd.current_value:.1f}"
    elif vd.current_value >= 10:
        value_str = f"{vd.current_value:.2f}"
    else:
        value_str = f"{vd.current_value:.2f}"
    draw.text((1, 10), value_str, fill=white, font=fonts["large"])

    draw.text((52, 10), vd.country.upper(), fill=dim, font=fonts["small"])

    # --- Sparkline (y=21 to y=31) ---
    chart_y_start = 21
    chart_y_end = 31
    chart_height = chart_y_end - chart_y_start

    history = list(reversed(vd.history))  # oldest → newest
    if len(history) > 1:
        min_val = min(history)
        max_val = max(history)
        val_range = max_val - min_val if max_val != min_val else 1

        num_points = min(len(history), PANEL_COLS - 2)
        step = (PANEL_COLS - 2) / max(num_points - 1, 1)

        points = []
        for i in range(num_points):
            x = int(1 + i * step)
            idx = len(history) - num_points + i
            normalized = (history[idx] - min_val) / val_range
            y = chart_y_end - int(normalized * chart_height)
            points.append((x, y))

        for i in range(len(points) - 1):
            draw.line([points[i], points[i + 1]], fill=color, width=1)

    return img


def render_loading() -> Image.Image:
    """Loading screen while fetching data."""
    img = Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fonts = get_fonts()
    draw.text((4, 4), "SISTRIX", fill=(0, 120, 255), font=fonts["large"])
    draw.text((4, 18), "Loading...", fill=(80, 80, 80), font=fonts["small"])
    return img


def render_no_data() -> Image.Image:
    """Screen when no data is available."""
    img = Image.new("RGB", (PANEL_COLS, PANEL_ROWS), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    fonts = get_fonts()
    draw.text((4, 4), "NO DATA", fill=(255, 40, 40), font=fonts["large"])
    draw.text((4, 18), "Check config", fill=(80, 80, 80), font=fonts["small"])
    return img


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
    options.gpio_slowdown = 2
    options.drop_privileges = True
    return RGBMatrix(options=options)


def display_frame(matrix, img: Image.Image):
    if matrix:
        matrix.SetImage(img)
    else:
        # Debug mode: save preview
        scaled = img.resize((PANEL_COLS * 8, PANEL_ROWS * 8), Image.NEAREST)
        scaled.save(BASE_DIR / "preview_current.png")


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 50)
    print("  SISTRIX Visibility LED Ticker")
    print("  Panel: 64x32 RGB | Mode: HUB75")
    print("=" * 50)

    if not config.api_key or config.api_key == "TU_API_KEY_AQUI":
        print("\n[ERROR] Configure your SISTRIX API key in config.json")
        print("        Edit: nano ~/sistrix-led/config.json")
        sys.exit(1)

    matrix = setup_matrix()
    domains_data: list[VisibilityData] = []
    last_fetch = datetime.min

    # Show loading screen
    display_frame(matrix, render_loading())

    while True:
        now = datetime.now()

        # Reload config and data if due
        if (now - last_fetch).total_seconds() > config.refresh_minutes * 60:
            config.reload()
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

        # Cycle through active domains
        for vd in domains_data:
            # Reload config each cycle (in case domains are added/removed)
            config.reload()

            img = render_frame(vd)
            display_frame(matrix, img)

            print(f"  [{vd.label}] {vd.current_value:.2f} ({vd.change_pct:+.1f}%) [{vd.mode}]")
            time.sleep(config.cycle_seconds)


if __name__ == "__main__":
    main()
