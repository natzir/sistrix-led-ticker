#!/usr/bin/env python3
"""
SISTRIX LED Ticker — Web Panel + Live Preview
===============================================
Web panel with integrated LED panel simulator.
Accessible at: http://raspberrypi.local:5001

Features:
- Remote domain management
- Visual simulator for 64x32 panel
- Smart cache (only queries API when fresh data is needed)
- Real-time preview without hardware
"""

import json
import io
import gzip
import hashlib
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, make_response
from datetime import datetime, timedelta
import requests as http_requests
from PIL import Image, ImageFilter, ImageEnhance

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
_config_lock = threading.Lock()
_config_cache = None
_config_mtime = 0


def load_config():
    global _config_cache, _config_mtime
    with _config_lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = 0
        if _config_cache is None or mtime != _config_mtime:
            with open(CONFIG_PATH) as f:
                _config_cache = json.load(f)
            _config_mtime = mtime
        return _config_cache


def save_config(data):
    global _config_cache, _config_mtime
    with _config_lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        _config_cache = data
        _config_mtime = CONFIG_PATH.stat().st_mtime


# ============================================================
# SMART CACHE — Only queries API when fresh data is needed
# ============================================================

def get_cache_path(label, country, mode):
    return CACHE_DIR / f"{label}_{country}_{mode}.json"


def read_cache(label, country, mode):
    """Reads cached data if available."""
    path = get_cache_path(label, country, mode)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def write_cache(label, country, mode, data):
    """Writes data to cache."""
    path = get_cache_path(label, country, mode)
    data["cached_at"] = datetime.now().isoformat()
    with open(path, "w") as f:
        json.dump(data, f)


def cache_is_fresh(cached_data, mode, skip_time_check=False):
    """
    Determines if the cache is fresh enough.
    Two checks:
    1. Time since last fetch (don't hammer the API) — skipped if skip_time_check=True
    2. Age of the latest data point (ensure we have recent data)
    """
    if not cached_data or "cached_at" not in cached_data:
        return False

    cached_at = datetime.fromisoformat(cached_data["cached_at"])
    now = datetime.now()

    # Check 1: minimum interval between API calls
    if not skip_time_check:
        if mode == "daily":
            if (now - cached_at) >= timedelta(hours=6):
                return False
        else:
            # weekly: stale if a Friday has passed since cache was written
            if cached_at.date() != now.date():
                days_until_friday = (4 - cached_at.weekday()) % 7 or 7
                next_friday = (cached_at + timedelta(days=days_until_friday)).date()
                if next_friday <= now.date():
                    return False

    # Check 2: is the latest data point too old?
    # SISTRIX daily has ~2 day lag, weekly ~1 week
    dates = cached_data.get("dates", [])
    if dates:
        try:
            latest = datetime.fromisoformat(dates[0].replace("Z", "")).date()
            max_age = timedelta(days=3) if mode == "daily" else timedelta(days=9)
            if (now.date() - latest) > max_age:
                return False
        except (ValueError, TypeError):
            pass

    return True


def fetch_sistrix(domain_config, force=False, refresh=False, api_key=None):
    """
    Fetches SISTRIX data with smart caching.
    - force=True: always call the API (ignores cache entirely)
    - refresh=True: skip time check but respect data-age check
    (only re-fetches domains whose data is actually old)
    """
    if api_key is None:
        api_key = load_config().get("sistrix_api_key", "")
    domain = domain_config["domain"]
    country = domain_config["country"]
    label = domain_config["label"]
    mode = domain_config.get("mode", "weekly")
    addr_type = domain_config.get("type", "domain")

    # Try cache first
    if not force:
        cached = read_cache(label, country, mode)
        if cached:
            if refresh:
                # User clicked Refresh: only re-fetch if data itself is old
                if cache_is_fresh(cached, mode, skip_time_check=True):
                    cached["_from_cache"] = True
                    return cached
            elif cache_is_fresh(cached, mode):
                cached["_from_cache"] = True
                return cached

    # No API key, return cache only (if available)
    if not api_key or api_key == "TU_API_KEY_AQUI":
        cached = read_cache(label, country, mode)
        if cached:
            cached["_from_cache"] = True
            return cached
        return None

    # Call the API — address_object: domain, host, path, or url
    url = "https://api.sistrix.com/domain.sichtbarkeitsindex"
    params = {
        "api_key": api_key,
        addr_type: domain,
        "country": country,
        "format": "json",
    }

    params["history"] = "true"
    if mode == "daily":
        params["daily"] = "true"

    try:
        resp = http_requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("answer", [{}])[0].get("sichtbarkeitsindex", [])
        if not entries:
            return read_cache(label, country, mode)

        entries.sort(key=lambda x: x.get("date", ""), reverse=True)

        max_points = 30 if mode == "daily" else 52
        history = []
        dates = []
        for e in entries[:max_points]:
            history.append(float(e.get("value", 0)))
            dates.append(e.get("date", ""))

        result = {
            "domain": domain,
            "label": label,
            "country": country,
            "mode": mode,
            "current_value": history[0] if history else 0,
            "previous_value": history[1] if len(history) > 1 else (history[0] if history else 0),
            "history": history,
            "dates": dates,
            "_from_cache": False,
            "_credits_note": f"API call: {len(entries)} credits used",
        }

        write_cache(label, country, mode, result)
        return result

    except Exception as e:
        print(f"[API ERROR] {domain}: {e}")
        cached = read_cache(label, country, mode)
        if cached:
            cached["_from_cache"] = True
            return cached
        return None


# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config/display", methods=["POST"])
def update_display():
    config = load_config()
    data = request.json
    if "brightness" in data:
        config["display"]["brightness"] = max(10, min(100, int(data["brightness"])))
    if "cycle_seconds" in data:
        config["display"]["cycle_seconds"] = max(3, min(60, int(data["cycle_seconds"])))
    if "refresh_minutes" in data:
        config["display"]["refresh_minutes"] = max(10, min(1440, int(data["refresh_minutes"])))
    save_config(config)
    return jsonify({"ok": True, "display": config["display"]})


@app.route("/api/config/data_layout", methods=["GET"])
def get_data_layout():
    config = load_config()
    return jsonify(config.get("data_layout", {}))


@app.route("/api/config/data_layout", methods=["POST"])
def update_data_layout():
    config = load_config()
    config["data_layout"] = request.json
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/domains", methods=["GET"])
def get_domains():
    config = load_config()
    return jsonify(config.get("domains", []))


@app.route("/api/domains", methods=["POST"])
def add_domain():
    config = load_config()
    data = request.json
    required = ["domain", "country", "label"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    addr_type = data.get("type", "domain")
    if addr_type not in ("domain", "host", "path", "url"):
        addr_type = "domain"
    new_domain = {
        "domain": data["domain"].strip().lower(),
        "country": data["country"].strip().lower(),
        "label": data["label"].strip().upper()[:8],
        "mode": data.get("mode", "weekly"),
        "type": addr_type,
        "active": data.get("active", True),
    }
    config["domains"].append(new_domain)
    save_config(config)
    return jsonify({"ok": True, "domain": new_domain})


def get_domain_or_404(config, index):
    """Returns domain at index or None if invalid."""
    if index < 0 or index >= len(config["domains"]):
        return None
    return config["domains"][index]


@app.route("/api/domains/<int:index>", methods=["PUT"])
def update_domain(index):
    config = load_config()
    if not get_domain_or_404(config, index):
        return jsonify({"error": "Invalid index"}), 404
    data = request.json
    domain = config["domains"][index]
    for key in ["active", "mode", "label", "domain", "country", "type"]:
        if key in data:
            if key == "active":
                domain[key] = bool(data[key])
            elif key == "mode" and data[key] in ("weekly", "daily"):
                domain[key] = data[key]
            elif key == "type" and data[key] in ("domain", "host", "path", "url"):
                domain[key] = data[key]
            elif key == "label":
                domain[key] = data[key].strip().upper()[:8]
            else:
                domain[key] = data[key].strip().lower()
    save_config(config)
    return jsonify({"ok": True, "domain": domain})


@app.route("/api/domains/<int:index>", methods=["DELETE"])
def delete_domain(index):
    config = load_config()
    if not get_domain_or_404(config, index):
        return jsonify({"error": "Invalid index"}), 404
    removed = config["domains"].pop(index)
    save_config(config)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/domains/<int:index>/toggle", methods=["POST"])
def toggle_domain(index):
    config = load_config()
    if not get_domain_or_404(config, index):
        return jsonify({"error": "Invalid index"}), 404
    config["domains"][index]["active"] = not config["domains"][index]["active"]
    save_config(config)
    return jsonify({"ok": True, "domain": config["domains"][index]})


@app.route("/api/domains/reorder", methods=["POST"])
def reorder_domains():
    config = load_config()
    order = request.json.get("order", [])
    domains = config.get("domains", [])
    if sorted(order) != list(range(len(domains))):
        return jsonify({"error": "Invalid order"}), 400
    config["domains"] = [domains[i] for i in order]
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/preview", methods=["GET"])
def get_preview_data():
    """Returns data for all active domains for the preview."""
    force = request.args.get("force") == "true"
    refresh = request.args.get("refresh") == "true"
    return jsonify(get_preview_data_internal(load_config(), force=force, refresh=refresh))


def get_preview_data_internal(config, force=False, refresh=False):
    active = [(i, d) for i, d in enumerate(config.get("domains", [])) if d.get("active")]
    api_key = config.get("sistrix_api_key", "")

    def fetch_one(item):
        idx, d = item
        return (idx, d, fetch_sistrix(d, force=force, refresh=refresh, api_key=api_key))

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for idx, d, data in pool.map(fetch_one, active):
            if data:
                change = 0
                if data.get("previous_value", 0) != 0:
                    change = ((data["current_value"] - data["previous_value"]) / data["previous_value"]) * 100
                dates = data.get("dates", [])
                results.append({
                    "configIndex": idx,
                    "label": d["label"],
                    "domain": d["domain"],
                    "country": d["country"],
                    "mode": d.get("mode", "weekly"),
                    "type": d.get("type", "domain"),
                    "current_value": data["current_value"],
                    "change_pct": round(change, 1),
                    "is_up": data["current_value"] >= data.get("previous_value", 0),
                    "history": data.get("history", []),
                    "from_cache": data.get("_from_cache", False),
                    "last_date": dates[0] if dates else "",
                })
    return results



@app.route("/api/cache/status", methods=["GET"])
def cache_status():
    return jsonify(get_cache_status_internal(load_config()))


def get_cache_status_internal(config):
    status = []
    if CACHE_DIR.exists():
        for f in sorted(CACHE_DIR.glob("*.json")):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                    cached_at = data.get("cached_at", "?")
                    fresh = cache_is_fresh(data, data.get("mode", "weekly"))
                    status.append({
                        "file": f.name,
                        "label": data.get("label", "?"),
                        "value": data.get("current_value"),
                        "mode": data.get("mode", "?"),
                        "cached_at": cached_at,
                        "is_fresh": fresh,
                    })
            except Exception:
                status.append({"file": f.name, "error": True})
    return status


def _parse_sistrix_credits(api_response):
    """Extract credits value from SISTRIX API response."""
    answer = api_response.get("answer", [])
    for item in answer:
        if "credits" in item:
            cred = item["credits"]
            if isinstance(cred, list) and cred:
                return cred[0].get("value")
            elif isinstance(cred, dict):
                return cred.get("value")
    return None


@app.route("/api/apikey", methods=["POST"])
def update_api_key():
    config = load_config()
    data = request.json
    if "api_key" not in data:
        return jsonify({"error": "Missing api_key"}), 400
    key = data["api_key"].strip()
    # Allow clearing the key
    if not key:
        config["sistrix_api_key"] = ""
        save_config(config)
        return jsonify({"ok": True, "credits": None})
    # Validate against SISTRIX credits endpoint (free, no credit cost)
    try:
        resp = http_requests.get(
            "https://api.sistrix.com/credits",
            params={"api_key": key, "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        credits_left = _parse_sistrix_credits(result)
        if credits_left is None:
            return jsonify({"ok": False, "error": "invalid_key"}), 401
        config["sistrix_api_key"] = key
        save_config(config)
        return jsonify({"ok": True, "credits": int(float(credits_left))})
    except Exception as e:
        print(f"[API VALIDATE ERROR] {e}")
        return jsonify({"ok": False, "error": str(e)}), 401


@app.route("/api/credits")
def get_credits():
    config = load_config()
    key = config.get("sistrix_api_key", "")
    if not key or key == "TU_API_KEY_AQUI":
        return jsonify({"credits": None})
    try:
        resp = http_requests.get(
            "https://api.sistrix.com/credits",
            params={"api_key": key, "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        credits_left = _parse_sistrix_credits(result)
        return jsonify({"credits": int(float(credits_left)) if credits_left is not None else None})
    except Exception:
        return jsonify({"credits": None})


@app.route("/api/theme", methods=["POST"])
def update_theme():
    config = load_config()
    data = request.json
    theme = data.get("theme", "dark")
    if theme in ("dark", "light"):
        config["theme"] = theme
        save_config(config)
        return jsonify({"ok": True, "theme": theme})
    return jsonify({"error": "Invalid theme"}), 400


@app.route("/api/brand", methods=["GET"])
def get_brand():
    config = load_config()
    return jsonify(config.get("brand", {}))


@app.route("/api/brand", methods=["POST"])
def update_brand():
    config = load_config()
    data = request.json
    if "brand" not in config:
        config["brand"] = {}
    for key in ["name", "message", "enabled", "layout", "logo_pixels", "logo_source"]:
        if key in data:
            config["brand"][key] = data[key]
    save_config(config)
    return jsonify({"ok": True, "brand": config["brand"]})


@app.route("/api/brand/favicon", methods=["POST"])
def fetch_favicon():
    """Fetches favicon from a domain URL and converts to 16x16 pixel grid."""
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400

    # Try common favicon locations
    if not url.startswith("http"):
        url = "https://" + url
    domain_base = url.rstrip("/")
    favicon_urls = [
        f"https://www.google.com/s2/favicons?domain={url}&sz=32",
        f"{domain_base}/favicon.ico",
        f"{domain_base}/apple-touch-icon.png",
    ]

    img = None
    for fav_url in favicon_urls:
        try:
            resp = http_requests.get(fav_url, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            break
        except Exception:
            continue

    if not img:
        return jsonify({"error": "Could not fetch favicon"}), 404

    # === LED-optimized favicon pipeline ===
    # 1. Sharpen source before downscale to preserve edges
    if img.size[0] > 16:
        img = img.filter(ImageFilter.SHARPEN)

    # 2. High-quality downscale to 16x16
    img = img.resize((16, 16), Image.LANCZOS)

    # 3. Boost saturation — vivid colors look better on LEDs
    rgb = img.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(1.4)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.3)

    # 4. Get alpha channel back for transparency detection
    alpha = img.split()[3] if img.mode == "RGBA" else None

    # 5. Convert to pixel grid with posterization for clean LED colors
    # Snap to ~6 levels per channel: 0, 51, 102, 153, 204, 255
    def posterize(v):
        if v < 25:
            return 0
        return min(255, round(v / 51) * 51)

    pixels = []
    for y in range(16):
        row = []
        for x in range(16):
            a = alpha.getpixel((x, y)) if alpha else 255
            if a < 50:
                row.append([0, 0, 0])
            else:
                r, g, b = rgb.getpixel((x, y))
                row.append([posterize(r), posterize(g), posterize(b)])
        pixels.append(row)

    # Save to config
    config = load_config()
    if "brand" not in config:
        config["brand"] = {}
    config["brand"]["logo_pixels"] = pixels
    config["brand"]["logo_source"] = url
    save_config(config)

    return jsonify({"ok": True, "pixels": pixels})


@app.route("/api/language", methods=["POST"])
def update_language():
    config = load_config()
    data = request.json
    lang = data.get("language", "en")
    if lang in ("es", "en", "fr", "it", "de", "pt"):
        config["language"] = lang
        save_config(config)
        return jsonify({"ok": True, "language": lang})
    return jsonify({"error": "Invalid language"}), 400


SISTRIX_COUNTRIES = [
    {"code": "de", "name": "Germany"}, {"code": "at", "name": "Austria"},
    {"code": "ch", "name": "Switzerland"}, {"code": "nl", "name": "Netherlands"},
    {"code": "fr", "name": "France"}, {"code": "it", "name": "Italy"},
    {"code": "es", "name": "Spain"}, {"code": "pl", "name": "Poland"},
    {"code": "uk", "name": "United Kingdom"}, {"code": "us", "name": "USA"},
    {"code": "se", "name": "Sweden"}, {"code": "br", "name": "Brazil"},
    {"code": "tr", "name": "Turkey"}, {"code": "be", "name": "Belgium"},
    {"code": "ie", "name": "Ireland"}, {"code": "pt", "name": "Portugal"},
    {"code": "dk", "name": "Denmark"}, {"code": "no", "name": "Norway"},
    {"code": "fi", "name": "Finland"}, {"code": "gr", "name": "Greece"},
    {"code": "hu", "name": "Hungary"}, {"code": "sk", "name": "Slovakia"},
    {"code": "cz", "name": "Czech Republic"}, {"code": "ca", "name": "Canada"},
    {"code": "au", "name": "Australia"}, {"code": "mx", "name": "Mexico"},
    {"code": "ru", "name": "Russia"}, {"code": "jp", "name": "Japan"},
    {"code": "in", "name": "India"}, {"code": "za", "name": "South Africa"},
    {"code": "ro", "name": "Romania"}, {"code": "si", "name": "Slovenia"},
    {"code": "hr", "name": "Croatia"}, {"code": "bg", "name": "Bulgaria"},
    {"code": "th", "name": "Thailand"}, {"code": "vn", "name": "Vietnam"},
    {"code": "id", "name": "Indonesia"}, {"code": "pe", "name": "Peru"},
    {"code": "ar", "name": "Argentina"}, {"code": "co", "name": "Colombia"},
    {"code": "cy", "name": "Cyprus"}, {"code": "mt", "name": "Malta"},
    {"code": "my", "name": "Malaysia"}, {"code": "ph", "name": "Philippines"},
    {"code": "nz", "name": "New Zealand"}, {"code": "ae", "name": "United Arab Emirates"},
    {"code": "eg", "name": "Egypt"}, {"code": "cl", "name": "Chile"},
    {"code": "pk", "name": "Pakistan"}, {"code": "sg", "name": "Singapore"},
    {"code": "ng", "name": "Nigeria"}, {"code": "ve", "name": "Venezuela"},
    {"code": "ua", "name": "Ukraine"},
]


@app.route("/api/countries", methods=["GET"])
def get_countries():
    return jsonify(SISTRIX_COUNTRIES)


@app.route("/api/init", methods=["GET"])
def api_init():
    """Single endpoint for initial page load — replaces 4 separate requests."""
    config = load_config()
    # Preview data
    preview = get_preview_data_internal(config)
    # Cache status
    cache = get_cache_status_internal(config)
    # Brand
    brand = config.get("brand", {"name": "", "message": "", "logo": None, "enabled": True})
    brand_layout = brand.get("layout", {})
    return jsonify({
        "config": config,
        "preview": preview,
        "cache": cache,
        "brand": brand,
        "brand_layout": brand_layout,
        "countries": SISTRIX_COUNTRIES
    })


# ============================================================
# WEB INTERFACE WITH LED SIMULATOR
# ============================================================

_index_cache = None
_index_etag = None

@app.route("/")

def index():
    global _index_cache, _index_etag

    # Check ETag — return 304 if unchanged
    if _index_etag and request.headers.get('If-None-Match') == _index_etag:
        return make_response('', 304)

    if _index_cache is None:
        html = _build_index_html()
        _index_cache = gzip.compress(html.encode(), compresslevel=6)
        _index_etag = '"' + hashlib.md5(_index_cache).hexdigest() + '"'

    resp = make_response(_index_cache)
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'private, max-age=60'
    resp.headers['ETag'] = _index_etag
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


def _build_index_html():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>SISTRIX LED Ticker</title>
<style>
 /* ===== DESIGN TOKENS ===== */
 :root {
 /* Spacing scale (base 4) */
 --space-1: 2px; --space-2: 4px; --space-3: 6px;
 --space-4: 8px; --space-5: 12px; --space-6: 16px;
 --space-7: 20px; --space-8: 24px; --space-9: 32px;
 /* Border radius */
 --radius-sm: 4px; --radius-md: 8px; --radius-lg: 12px;
 /* Typography */
 --text-xs: 11px; --text-sm: 12px; --text-base: 14px; --text-lg: 16px;
 }

 :root, [data-theme="dark"] {
 --bg: #08080d;
 --surface: #141420;
 --surface-sunken: #0a0a0f;
 --border: #1e1e2e;
 --hover: #1a1a2e;
 --text: #e0e0e0;
 --dim: #888;
 --text-disabled: #555;
 --accent: #00c853;
 --accent-hover: #00b848;
 --red: #ff2d55;
 --blue: #0a84ff;
 --yellow: #ffd60a;
 --led-bg: #1a1a1a;
 --led-border: #222;
 --led-shadow: rgba(0,0,0,0.6);
 --toggle-off: #333;
 --toast-text: black;
 --input-bg: #0a0a0f;
 --focus-ring: rgba(0,200,83,0.4);
 --mode-weekly-bg: #1a2a1a;
 --mode-daily-bg: #2a2a1a;
 }
 [data-theme="light"] {
 --bg: #f5f5f7;
 --surface: #ffffff;
 --surface-sunken: #eaeaee;
 --border: #d0d0d6;
 --hover: #ededf0;
 --text: #1a1a1a;
 --dim: #555;
 --text-disabled: #999;
 --accent: #007a32;
 --accent-hover: #006828;
 --red: #c41830;
 --blue: #0060c0;
 --yellow: #8a6d00;
 --led-bg: #222;
 --led-border: #333;
 --led-shadow: rgba(0,0,0,0.3);
 --toggle-off: #ccc;
 --toast-text: white;
 --input-bg: #eaeaee;
 --focus-ring: rgba(0,122,50,0.3);
 --mode-weekly-bg: #e0f0e0;
 --mode-daily-bg: #f0f0d0;
 }
 * { margin:0; padding:0; box-sizing:border-box; }
 body {
 font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
 background: var(--bg); color: var(--text);
 min-height: 100vh; padding: var(--space-7);
 overflow-x: hidden; max-width: 100vw;
 }
 h1 { font-size:var(--text-base); text-transform:uppercase; letter-spacing:4px; color:var(--accent); margin-bottom:var(--space-4); }
 .subtitle { font-size:var(--text-sm); color:var(--dim); margin-bottom:var(--space-9); }
 .section { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-md); padding:var(--space-7); margin-bottom:var(--space-7); overflow:hidden; }
 .section-title, h2.section-title { font-size:var(--text-sm); text-transform:uppercase; letter-spacing:2px; color:var(--dim); margin-bottom:var(--space-6); display:flex; justify-content:space-between; align-items:center; font-weight:normal; }

 /* ===== LED SIMULATOR ===== */
 .led-wrapper {
 display:flex; flex-direction:column; align-items:center; gap:var(--space-5);
 }
 .led-arrow {
 background:none; border:none; color:var(--dim); font-size:32px; cursor:pointer;
 padding:0 var(--space-3); line-height:1; flex-shrink:0;
 transition:color 0.2s; border-radius:var(--radius-sm);
 display:flex; align-items:center;
 }
 .led-arrow:hover { color:var(--text); }
 .led-arrow:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 .led-arrow:disabled { opacity:0.2; cursor:not-allowed; }
 .led-stage {
 display:flex; align-items:start; justify-content:center; gap:var(--space-3);
 }
 .led-center {
 display:flex; flex-direction:column; gap:var(--space-3);
 }
 .led-status-row { display:flex; justify-content:flex-end; align-items:center; min-height:20px; }
 .led-outer {
 background: var(--led-bg);
 border-radius: var(--radius-md);
 padding: var(--space-5);
 box-shadow: 0 4px 24px var(--led-shadow), inset 0 1px 0 rgba(255,255,255,0.03);
 border: 1px solid var(--led-border);
 }
 .led-canvas-wrap {
 position: relative;
 image-rendering: pixelated;
 }
 #ledCanvas {
 image-rendering: pixelated;
 image-rendering: crisp-edges;
 border-radius: var(--space-1);
 }
 .led-controls {
 display:flex; gap:var(--space-3); align-items:center; font-size:var(--text-sm); color:var(--dim); margin-top:var(--space-3);
 }
 .led-info { font-size:var(--text-sm); color:var(--dim); }
 /* Domain cards */
 .domain-card {
 display:flex; align-items:center; gap:var(--space-4); padding:var(--space-4) var(--space-5);
 border:1px solid var(--border); border-radius:var(--radius-md); margin-bottom:var(--space-4); transition:all 0.2s;
 min-width:0;
 }
 .domain-card.inactive { opacity:0.55; }
 .domain-card:hover { border-color:var(--hover); }
 .domain-label { font-size:var(--text-base); font-weight:bold; min-width:70px; }
 .domain-info { flex:1; font-size:var(--text-sm); color:var(--dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
 .domain-type-tag { font-size:var(--text-xs); color:var(--blue); opacity:0.8; text-transform:uppercase; flex-shrink:0; }
 .domain-country-tag { font-size:var(--text-xs); color:#ffb340; text-transform:uppercase; flex-shrink:0; }
 .drag-handle { cursor:grab; color:var(--dim); font-size:var(--text-base); user-select:none; padding:0 var(--space-1); flex-shrink:0; opacity:0.4; transition:opacity 0.2s; line-height:1; }
 .drag-handle:hover { opacity:1; }
 .drag-handle:active { cursor:grabbing; }
 .reorder-btns { display:none; flex-direction:column; gap:1px; flex-shrink:0; }
 .btn-reorder { background:none; border:1px solid var(--border); color:var(--dim); font-size:8px; line-height:1; padding:2px 4px; cursor:pointer; border-radius:var(--radius-sm); }
 .btn-reorder:hover:not(:disabled) { color:var(--text); border-color:var(--accent); }
 .btn-reorder:disabled { opacity:0.2; cursor:default; }
 .btn-reorder:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
 @media (hover:none) { .reorder-btns { display:flex; } .drag-handle { display:none; } }
 .domain-card.dragging { opacity:0.4; border-style:dashed; }
 .domain-card.drag-over { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
 .domain-mode {
 font-size:var(--text-xs); padding:var(--space-1) var(--space-4); border-radius:var(--radius-sm); text-transform:uppercase;
 letter-spacing:1px; cursor:pointer; border:none; font-family:inherit;
 }
 .mode-weekly { background:var(--mode-weekly-bg); color:var(--accent); }
 .mode-daily { background:var(--mode-daily-bg); color:var(--yellow); }
 .domain-mode:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 button:disabled, .btn:disabled { opacity:0.5; cursor:not-allowed; pointer-events:none; }
 .toggle-btn {
 width:44px; height:28px; border-radius:14px; border:none; cursor:pointer;
 position:relative; transition:background 0.2s; flex-shrink:0;
 }
 .toggle-btn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 .toggle-btn.on { background:var(--accent); }
 .toggle-btn.off { background:var(--toggle-off); }
 .toggle-btn::after {
 content:''; position:absolute; width:22px; height:22px; border-radius:50%;
 background:white; top:3px; transition:left 0.2s;
 }
 .toggle-btn.on::after { left:19px; }
 .toggle-btn.off::after { left:3px; }
 .toggle-sm { width:32px; height:18px; border-radius:9px; vertical-align:middle; margin-left:var(--space-2); position:relative; }
 .toggle-sm::before { content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:44px; height:44px; }
 .toggle-sm::after { width:14px; height:14px; top:2px; }
 .toggle-sm.on::after { left:16px; }
 .toggle-sm.off::after { left:2px; }
 /* Icon buttons (delete, cancel, small actions) */
 .btn-icon {
 display:inline-flex; align-items:center; justify-content:center;
 width:34px; height:34px; border-radius:var(--radius-sm); position:relative;
 border:1px solid transparent; background:none; padding:0; flex-shrink:0;
 cursor:pointer; font-family:inherit; font-size:var(--text-sm);
 transition:all 0.2s; opacity:0.4;
 }
 .btn-icon:hover { opacity:1; }
 .btn-icon:focus-visible { opacity:1; outline:2px solid var(--accent); outline-offset:2px; }
 .btn-icon-danger { color:var(--red); }
 .btn-icon-danger:hover { background:var(--red); color:white; opacity:1; border-color:var(--red); }
 @media (hover:none) { .btn-icon::before { content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:44px; height:44px; } }
 .btn-icon-muted { color:var(--dim); }
 .btn-icon-muted:hover { color:var(--text); background:var(--hover); border-color:var(--border); }

 /* Outline button variant */
 .btn-outline {
 background:var(--surface); border:1px solid var(--border); color:var(--text);
 padding:0 var(--space-4); border-radius:var(--radius-sm); height:34px; min-width:34px;
 display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box;
 cursor:pointer; font-family:inherit; font-size:var(--text-sm); transition:all 0.2s;
 }
 .btn-outline:hover { border-color:var(--dim); }
 .btn-outline:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 .btn-outline.active { border-color:var(--accent); color:var(--accent); }

 /* Danger filled button */
 /* Clickable text (domain labels, etc.) */
 .clickable { cursor:pointer; }
 .clickable:hover { color:var(--accent); }

 /* Add form */
 .add-form { display:grid; grid-template-columns:repeat(auto-fit, minmax(80px, 1fr)); gap:var(--space-4); align-items:end; margin-top:var(--space-6); }
 .add-form label { font-size:var(--text-xs); color:var(--dim); text-transform:uppercase; letter-spacing:1px; display:block; margin-bottom:var(--space-2); }
 .add-form input, .add-form select {
 background-color:var(--surface-sunken); border:1px solid var(--border); color:var(--text);
 padding:0 var(--space-4); border-radius:var(--radius-sm); font-family:inherit; font-size:var(--text-sm); width:100%; height:34px; box-sizing:border-box;
 }
 .add-form input:focus, .add-form select:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 2px var(--focus-ring); }
 .add-form .btn { white-space:nowrap; }
 .btn { background:var(--accent); color:black; border:none; padding:0 var(--space-5); border-radius:var(--radius-sm); cursor:pointer; font-family:inherit; font-weight:bold; font-size:var(--text-xs); text-transform:uppercase; letter-spacing:1px; height:34px; min-width:70px; display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box; }
 .btn:hover { background:var(--accent-hover); }
 .btn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 .btn-small { height:34px; min-width:50px; padding:0 var(--space-4); }

 .status-bar { display:flex; gap:var(--space-6); font-size:var(--text-sm); color:var(--dim); margin-top:var(--space-7); flex-wrap:wrap; align-items:center; }
 .status-bar .btn-refresh { font-size:var(--text-xs); padding:var(--space-1) var(--space-4); border-radius:var(--radius-sm); background:none; border:1px solid var(--border); color:var(--dim); cursor:pointer; font-family:inherit; transition:all 0.2s; display:inline-flex; align-items:center; }
 .status-bar .btn-refresh:hover { border-color:var(--accent); color:var(--accent); }
 .status-dot { display:inline-block; width:var(--space-3); height:var(--space-3); border-radius:50%; margin-right:var(--space-2); }
 .dot-green { background:var(--accent); }
 .dot-red { background:var(--red); }

 /* Two-column layout on wide screens */
 .main-layout { display:grid; grid-template-columns:1fr; gap:var(--space-6); }
 @media (min-width:900px) {
 .main-layout { grid-template-columns:1fr 1fr; align-items:start; }
 .col-panel { position:sticky; top:var(--space-6); }
 }

 @media (max-width:700px) {
 body { padding:var(--space-5); }
 .section { padding:var(--space-5); }
 .add-form { grid-template-columns:1fr 1fr; }
 .add-form > div:last-child { grid-column: 1 / -1; }
 .settings-grid { grid-template-columns:1fr; }
 .led-outer { padding:var(--space-4); }
 #ledCanvas { width:100% !important; height:auto !important; max-width:100%; display:block; }
 .led-controls { flex-wrap:wrap; justify-content:center; gap:var(--space-3); }
 .led-arrow { font-size:22px; padding:var(--space-2); }
 .domain-card { gap:var(--space-3); padding:var(--space-4); flex-wrap:wrap; }
 .domain-label { min-width:auto; font-size:var(--text-sm); }
 .domain-info { min-width:0; font-size:var(--text-xs); max-width:calc(100vw - 200px); }
 .domain-mode { padding:var(--space-2) var(--space-4); font-size:var(--text-xs); min-height:36px; display:inline-flex; align-items:center; }
 .section .btn { width:100%; text-align:center; }
 .edit-row .btn, .edit-row .btn-icon { width:auto; flex:0 0 auto; }
 .edit-row .btn-icon { width:34px; height:34px; min-width:34px; min-height:34px; }
 .toast { bottom:calc(var(--space-7) + env(safe-area-inset-bottom, 0px)); right:var(--space-5); left:var(--space-5); }
 }
 /* Custom dropdown (countries) */
 .custom-select { position:relative; width:100%; }
 .custom-select-trigger {
 background:var(--surface-sunken); border:1px solid var(--border); color:var(--text);
 padding:var(--space-3) var(--space-4); border-radius:var(--radius-sm); font-family:inherit;
 font-size:var(--text-sm); height:34px; box-sizing:border-box; cursor:pointer; display:flex; align-items:center;
 justify-content:space-between; user-select:none;
 }
 .custom-select-trigger:hover { border-color:var(--hover); }
 .custom-select-sm .custom-select-trigger { height:30px; padding:var(--space-3) var(--space-4); font-size:var(--text-sm); background:var(--surface); box-sizing:border-box; }
 .custom-select-trigger.open { border-color:var(--accent); box-shadow:0 0 0 2px var(--focus-ring); }
 .custom-select-trigger::after { content:'▾'; font-size:10px; color:var(--dim); margin-left:var(--space-2); }
 .custom-select-dropdown {
 display:none; position:absolute; left:0; right:0; z-index:50;
 background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
 max-height:200px; overflow-y:auto; box-shadow:0 8px 24px rgba(0,0,0,0.4);
 }
 .custom-select-dropdown.above { bottom:100%; margin-bottom:4px; }
 .custom-select-dropdown.below { top:100%; margin-top:4px; }
 .custom-select-dropdown.open { display:block; }
 .custom-select-search {
 position:sticky; top:0; background:var(--surface); padding:var(--space-3);
 border-bottom:1px solid var(--border);
 }
 .custom-select-search input {
 width:100%; background:var(--surface-sunken); border:1px solid var(--border); color:var(--text);
 padding:var(--space-2) var(--space-3); border-radius:var(--radius-sm); font-family:inherit;
 font-size:var(--text-sm); outline:none;
 }
 .custom-select-search input:focus { border-color:var(--accent); }
 .custom-select-option {
 padding:var(--space-3) var(--space-4); cursor:pointer; font-size:var(--text-sm);
 white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
 }
 .custom-select-option:hover, .custom-select-option.highlighted { background:var(--hover); }
 .custom-select-option.selected { color:var(--accent); font-weight:bold; }
 .custom-select-option.highlighted { background:var(--surface-sunken); outline:2px solid var(--accent); outline-offset:-2px; }

 /* Layout editor */
 #ledCanvas.edit-mode { cursor:crosshair; }
 #ledCanvas.edit-mode.dragging { cursor:grabbing; }
 .btn-outline.active { background:var(--accent); color:black; border-color:var(--accent); }

 .toast { position:fixed; bottom:var(--space-7); right:var(--space-7); background:var(--accent); color:var(--toast-text); padding:var(--space-4) var(--space-7); border-radius:var(--radius-md); font-size:var(--text-sm); font-weight:bold; transform:translateY(80px); transition:transform 0.3s; z-index:100; }
 .toast.show { transform:translateY(0); }

 /* Inline edit popup for canvas elements */
 .led-edit-popup { position:fixed; z-index:100; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-md); padding:var(--space-4); box-shadow:0 8px 32px rgba(0,0,0,0.6); display:flex; flex-direction:column; gap:var(--space-3); min-width:240px; max-width:320px; }
 .led-edit-popup .edit-row { display:flex; gap:var(--space-4); align-items:center; }
 .led-edit-popup input[type="text"] { background:var(--surface-sunken); border:1px solid var(--border); color:var(--text); padding:0 10px; border-radius:var(--radius-sm); font-family:inherit; font-size:var(--text-sm); flex:1; outline:none; height:32px; box-sizing:border-box; }
 .led-edit-popup input[type="text"]:focus { border-color:var(--accent); }
 .led-edit-popup .btn-ok { background:var(--accent); color:var(--toast-text); border:none; border-radius:var(--radius-sm); padding:0 var(--space-4); font-family:inherit; font-size:var(--text-xs); cursor:pointer; font-weight:bold; flex-shrink:0; height:34px; box-sizing:border-box; text-transform:uppercase; letter-spacing:1px; }
 .led-edit-popup .btn-ok:hover { opacity:0.85; }
 .led-edit-popup .color-grid { display:flex; flex-wrap:wrap; gap:4px; }
 .led-edit-popup .color-swatch { width:24px; height:24px; border-radius:var(--radius-sm); border:2px solid transparent; cursor:pointer; flex-shrink:0; transition:border-color 0.15s, transform 0.15s; position:relative; }
 .led-edit-popup .color-swatch::before { content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); min-width:44px; min-height:44px; }
 .led-edit-popup .color-swatch:hover { border-color:var(--text); transform:scale(1.15); }
 .led-edit-popup .color-swatch.active { border-color:var(--accent); }
 .led-edit-popup .color-swatch.rainbow { background:linear-gradient(90deg, #ff0000, #ff8800, #ffff00, #00ff00, #0088ff, #8800ff); }
 .led-edit-popup .color-custom { display:flex; align-items:center; gap:var(--space-2); font-size:var(--text-xs); color:var(--dim); cursor:pointer; }
 .led-edit-popup .color-custom-dot { width:24px; height:24px; border-radius:var(--radius-sm); border:2px solid var(--border); cursor:pointer; }


 /* Reusable form classes */
 .layout-input { width:100%; background:var(--surface-sunken); border:1px solid var(--border); color:var(--text); padding:var(--space-2); border-radius:var(--radius-sm); font-family:inherit; font-size:var(--text-sm); text-align:center; height:34px; box-sizing:border-box; }
 .edit-grid { display:flex; flex-direction:column; gap:var(--space-3); width:100%; }
 .edit-row { display:flex; gap:var(--space-4); align-items:center; }
 .edit-input { background-color:var(--surface-sunken); border:1px solid var(--border); color:var(--text); padding:var(--space-2) var(--space-3); border-radius:var(--radius-sm); font-family:inherit; height:34px; box-sizing:border-box; font-size:var(--text-sm); }

 /* Brand card */
 .social-link { color:var(--dim); display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; padding:0; position:relative; }
 .social-link::before { content:''; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:44px; height:44px; }
 .social-link:hover { color:var(--text); }
 .header-btn { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:var(--space-3) var(--space-4); border-radius:var(--radius-sm); cursor:pointer; font-family:inherit; font-size:var(--text-sm); height:30px; display:inline-flex; align-items:center; box-sizing:border-box; }
 .header-btn:hover { border-color:var(--dim); }
 .header-btn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
 .apikey-popup { position:absolute; top:calc(100% + 6px); right:0; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-md); padding:var(--space-4); display:flex; gap:var(--space-3); align-items:center; z-index:100; box-shadow:0 4px 12px rgba(0,0,0,.3); white-space:nowrap; }
 .apikey-popup input { background:var(--surface-sunken); border:1px solid var(--border); color:var(--text); padding:var(--space-2) var(--space-3); border-radius:var(--radius-sm); font-family:inherit; font-size:var(--text-sm); min-width:0; flex:1; }
 .apikey-popup .btn { flex-shrink:0; }
 @media (max-width:500px) { .apikey-popup { position:fixed; top:auto; bottom:var(--space-6); left:var(--space-4); right:var(--space-4); } }
</style>
</head>
<body>

<header style="display:flex;justify-content:space-between;align-items:start;">
 <div>
 <h1>SISTRIX LED Ticker</h1>
 <p class="subtitle" style="display:flex;align-items:center;gap:8px;">
 <span>by <a href="https://natzir.com" target="_blank" style="color:var(--accent);text-decoration:none;">Natzir</a></span>
 <a href="https://x.com/natzir9" target="_blank" title="X / Twitter" aria-label="X / Twitter" class="social-link"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a>
 <a href="https://www.linkedin.com/in/natzir/" target="_blank" title="LinkedIn" aria-label="LinkedIn" class="social-link"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a>
 <a href="mailto:hola@natzir.com" title="hola@natzir.com" aria-label="Email" class="social-link"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M1.5 8.67v8.58a3 3 0 003 3h15a3 3 0 003-3V8.67l-8.928 5.493a3 3 0 01-3.144 0L1.5 8.67z"/><path d="M22.5 6.908V6.75a3 3 0 00-3-3h-15a3 3 0 00-3 3v.158l9.714 5.978a1.5 1.5 0 001.572 0L22.5 6.908z"/></svg></a>
 </p>
 </div>
 <div style="display:flex;gap:8px;align-items:center;">
 <div style="position:relative;">
 <button id="btnApiKey" onclick="toggleApiKeyPopup()" class="header-btn" style="gap:6px;">
 <span id="apiDot" class="status-dot dot-red" style="margin:0;" aria-hidden="true"></span><span class="api-label">Add API</span>
 </button>
 <div id="apiKeyPopup" class="apikey-popup" style="display:none;">
 <input type="password" id="apiKey" data-i18n-placeholder="apikey_placeholder" placeholder="Your SISTRIX API key" aria-label="SISTRIX API Key" style="width:260px;max-width:60vw;">
 <button class="btn btn-small" onclick="saveApiKey()" data-i18n="save">Save</button>
 </div>
 </div>
 <button id="themeToggle" onclick="toggleTheme()" class="header-btn" title="Toggle theme" aria-label="Toggle theme">&#9790;</button>
 <div id="langSelect" class="custom-select custom-select-sm" style="width:60px;" aria-label="Language"></div>
 </div>
</header>

<main class="main-layout">
 <!-- LEFT COLUMN: LED PANEL -->
 <div class="col-panel">
 <div class="section">
 <h2 class="section-title">
 <span data-i18n="sim_title">Panel</span>
 </h2>
 <div class="led-wrapper">
 <div class="led-stage">
 <button id="btnPrev" onclick="prevDomain()" class="led-arrow" aria-label="Previous domain">&#8249;</button>
 <div class="led-center">
 <div class="led-status-row"><span id="previewStatus" class="led-info" aria-live="polite"></span></div>
 <div class="led-outer" id="ledOuter">
 <div class="led-canvas-wrap">
 <canvas id="ledCanvas" width="384" height="192" role="img" aria-label="LED panel simulator showing domain visibility index">LED panel simulator</canvas>
 </div>
 </div>
 <div class="led-controls">
 <button id="btnPlayPause" onclick="toggleAutoRotate()" class="btn-outline active" aria-label="Toggle auto-rotation" style="min-width:70px;"><span id="domainCounter">&#9654; -/-</span></button>
 <div id="cycleBtns" role="group" aria-label="Rotation speed" style="display:flex;align-items:center;gap:4px;"></div>
 <div style="display:flex;gap:var(--space-3);margin-left:auto;">
 <button id="btnReset" onclick="resetCurrentLayout()" class="btn-outline" style="display:none;" aria-label="Reset layout" title="Reset layout"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 1 9 9"/><polyline points="3 7 3 12 8 12"/></svg></button>
 <button id="btnEdit" onclick="toggleEdit()" class="btn-outline" data-i18n="edit">&#9998; Edit</button>
 </div>
 </div>
 </div>
 <button id="btnNext" onclick="nextDomain()" class="led-arrow" aria-label="Next domain">&#8250;</button>
 </div>
 <!-- Hidden BL inputs for JS references -->
 <div style="display:none;">
 <input type="hidden" id="bl_nameColor" value="#ffffff"><input type="hidden" id="bl_msgColor" value="#00c853">
 <input type="number" id="bl_logoX" value="1"><input type="number" id="bl_logoY" value="1">
 <input type="number" id="bl_nameX" value="19"><input type="number" id="bl_nameY" value="6">
 <input type="number" id="bl_msgX" value="0"><input type="number" id="bl_msgY" value="21">
 <div id="bl_logoSize"></div><div id="bl_nameFont"></div><div id="bl_msgFont"></div><div id="bl_msgSpeed"></div>
 </div>
 </div>
 </div>
 </div>

 <!-- RIGHT COLUMN: CONFIG -->
 <div class="col-config">
 <!-- DOMAINS -->
 <div class="section">
 <h2 class="section-title" data-i18n="domains_title">Domains</h2>
 <div id="domainList" aria-live="polite" aria-relevant="additions removals"></div>
 <form class="add-form" onsubmit="event.preventDefault();addDomain()">
 <div><label for="newLabel" data-i18n="label">Label</label><input type="text" id="newLabel" placeholder="EXMP" maxlength="8" required aria-required="true"></div>
 <div><label for="newDomain" data-i18n="address">Address</label><input type="text" id="newDomain" placeholder="example.com" data-i18n-placeholder="domain_placeholder" required aria-required="true"></div>
 <div><label for="newType" data-i18n="type">Type</label><div id="newType" class="custom-select"></div></div>
 <div><label for="newCountry" data-i18n="country">Country</label><div id="newCountry" class="custom-select"></div></div>
 <div><label for="newMode" data-i18n="mode">Mode</label><div id="newMode" class="custom-select"></div></div>
 <div><label aria-hidden="true">&nbsp;</label><button type="submit" class="btn" data-i18n="add">+ Add</button></div>
 </form>
 </div>
 </div>
</main>

<footer class="status-bar" id="statusBar" role="status"></footer>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
// ===== I18N =====
const I18N = {
 es: { sim_title:'Panel',
 last_update:'Última comprobación:', refresh_btn:'Actualizar', apikey_placeholder:'Tu API key de SISTRIX', save:'Guardar',
 domains_title:'Direcciones', domain:'Dominio', country:'País', mode:'Modo', type:'Tipo', address:'Dirección',
 domain_placeholder:'ejemplo.com', add:'+ Añadir', active_domains:'direcciones activas',
 loading_data:'Cargando datos de SISTRIX...', added:'Añadido',
 updated:'Actualizado', mode_changed:'Modo cambiado', deleted:'Eliminado',
 loading_dots:'Cargando...', fetching:'Pidiendo datos a SISTRIX...',
 data_updated:'Datos actualizados desde API', error_update:'Error al actualizar',
 confirm_delete:'¿Eliminar?', enable:'Activar', disable:'Desactivar', fill_fields:'Rellena dominio y label', refresh_confirm_short:'Solo recarga los días que faltan · Clic para confirmar', credits_available:'créditos disponibles', apikey_removed:'API Key eliminada', apikey_checking:'Validando API Key...', apikey_valid:'API Key válida', apikey_invalid:'API Key no válida', credits:'créditos', loading_data_short:'Cargando datos...',
 cache:'caché', api:'api', brand_title:'Tarjeta personalizada', brand_fetch:'Obtener favicon', brand_or:'o', brand_saved:'Marca guardada', brand_logo_ok:'Logo cargado', brand_logo_err:'No se pudo cargar el logo', layout_reset:'Layout reseteado', brand_upload:'Subir imagen', brand_delete_logo:'Eliminar',
 edit:'\u270E Editar', done_editing:'Guardar', reset:'Restablecer', edit_hint_touch:'Mantén pulsado para editar texto/color',
 label:'Etiqueta', mode_weekly:'Semanal', mode_daily:'Diario', bl_speed:'Velocidad',
 bl_slow:'Lento', bl_fast:'Rápido', bl_delete_logo:'Eliminar logo',
 },
 en: { sim_title:'Panel',
 last_update:'Last check:', refresh_btn:'Refresh', apikey_placeholder:'Your SISTRIX API key', save:'Save',
 domains_title:'Addresses', domain:'Domain', country:'Country', mode:'Mode', type:'Type', address:'Address',
 domain_placeholder:'example.com', add:'+ Add', active_domains:'active addresses',
 loading_data:'Loading SISTRIX data...', added:'Added',
 updated:'Updated', mode_changed:'Mode changed', deleted:'Deleted',
 loading_dots:'Loading...', fetching:'Fetching data from SISTRIX...',
 data_updated:'Data updated from API', error_update:'Update error',
 confirm_delete:'Delete?', enable:'Enable', disable:'Disable', fill_fields:'Fill in domain and label', refresh_confirm_short:'Only fetches missing days · Click to confirm', credits_available:'credits left', apikey_removed:'API Key removed', apikey_checking:'Validating API Key...', apikey_valid:'API Key valid', apikey_invalid:'Invalid API Key', credits:'credits', loading_data_short:'Loading data...',
 cache:'cache', api:'api', brand_title:'Personalized card', brand_fetch:'Get favicon', brand_or:'or', brand_saved:'Brand saved', brand_logo_ok:'Logo loaded', brand_logo_err:'Could not load logo', layout_reset:'Layout reset', brand_upload:'Upload image', brand_delete_logo:'Delete',
 edit:'\u270E Edit', done_editing:'Save', reset:'Reset', edit_hint_touch:'Long press to edit text/color',
 label:'Label', mode_weekly:'Weekly', mode_daily:'Daily', bl_speed:'Speed',
 bl_slow:'Slow', bl_fast:'Fast', bl_delete_logo:'Delete logo',
 },
 fr: { sim_title:'Panel',
 last_update:'Dernière vérif.:', refresh_btn:'Actualiser', apikey_placeholder:'Votre clé API SISTRIX', save:'Enregistrer',
 domains_title:'Adresses', domain:'Domaine', country:'Pays', mode:'Mode', type:'Type', address:'Adresse',
 domain_placeholder:'exemple.com', add:'+ Ajouter', active_domains:'adresses actives',
 loading_data:'Chargement des données SISTRIX...', added:'Ajouté',
 updated:'Mis à jour', mode_changed:'Mode changé', deleted:'Supprimé',
 loading_dots:'Chargement...', fetching:'Récupération des données SISTRIX...',
 data_updated:'Données mises à jour depuis l\\'API', error_update:'Erreur de mise à jour',
 confirm_delete:'Supprimer ?', enable:'Activer', disable:'Désactiver', fill_fields:'Remplissez domaine et label', refresh_confirm_short:'Ne recharge que les jours manquants · Cliquez pour confirmer', credits_available:'crédits disponibles', apikey_removed:'Clé API supprimée', apikey_checking:'Validation de la clé API...', apikey_valid:'Clé API valide', apikey_invalid:'Clé API invalide', credits:'crédits', loading_data_short:'Chargement...',
 cache:'cache', api:'api', brand_title:'Carte personnalisée', brand_fetch:'Obtenir favicon', brand_or:'ou', brand_saved:'Marque enregistrée', brand_logo_ok:'Logo chargé', brand_logo_err:'Impossible de charger le logo', layout_reset:'Layout réinitialisé', brand_upload:'Télécharger image', brand_delete_logo:'Supprimer',
 edit:'\u270E Éditer', done_editing:'Enregistrer', reset:'Réinitialiser', edit_hint_touch:'Appui long pour éditer texte/couleur',
 label:'Libellé', mode_weekly:'Hebdomadaire', mode_daily:'Quotidien', bl_speed:'Vitesse',
 bl_slow:'Lent', bl_fast:'Rapide', bl_delete_logo:'Supprimer le logo',
 },
 it: { sim_title:'Panel',
 last_update:'Ultimo controllo:', refresh_btn:'Aggiorna', apikey_placeholder:'La tua API key SISTRIX', save:'Salva',
 domains_title:'Indirizzi', domain:'Dominio', country:'Paese', mode:'Modalità', type:'Tipo', address:'Indirizzo',
 domain_placeholder:'esempio.com', add:'+ Aggiungi', active_domains:'indirizzi attivi',
 loading_data:'Caricamento dati SISTRIX...', added:'Aggiunto',
 updated:'Aggiornato', mode_changed:'Modalità cambiata', deleted:'Eliminato',
 loading_dots:'Caricamento...', fetching:'Recupero dati da SISTRIX...',
 data_updated:'Dati aggiornati dall\\'API', error_update:'Errore di aggiornamento',
 confirm_delete:'Eliminare?', enable:'Attivare', disable:'Disattivare', fill_fields:'Compila dominio e label', refresh_confirm_short:'Ricarica solo i giorni mancanti · Clicca per confermare', credits_available:'crediti disponibili', apikey_removed:'API Key rimossa', apikey_checking:'Validazione API Key...', apikey_valid:'API Key valida', apikey_invalid:'API Key non valida', credits:'crediti', loading_data_short:'Caricamento...',
 cache:'cache', api:'api', brand_title:'Scheda personalizzata', brand_fetch:'Ottieni favicon', brand_or:'o', brand_saved:'Marca salvata', brand_logo_ok:'Logo caricato', brand_logo_err:'Impossibile caricare il logo', layout_reset:'Layout reimpostato', brand_upload:'Carica immagine', brand_delete_logo:'Elimina',
 edit:'\u270E Modifica', done_editing:'Salva', reset:'Ripristina', edit_hint_touch:'Tieni premuto per modificare testo/colore',
 label:'Etichetta', mode_weekly:'Settimanale', mode_daily:'Giornaliero', bl_speed:'Velocità',
 bl_slow:'Lento', bl_fast:'Veloce', bl_delete_logo:'Elimina logo',
 },
 de: { sim_title:'Panel',
 last_update:'Letzte Prüfung:', refresh_btn:'Aktualisieren', apikey_placeholder:'Dein SISTRIX API-Schlüssel', save:'Speichern',
 domains_title:'Adressen', domain:'Domain', country:'Land', mode:'Modus', type:'Typ', address:'Adresse',
 domain_placeholder:'beispiel.de', add:'+ Hinzufügen', active_domains:'aktive Adressen',
 loading_data:'Lade SISTRIX-Daten...', added:'Hinzugefügt',
 updated:'Aktualisiert', mode_changed:'Modus geändert', deleted:'Gelöscht',
 loading_dots:'Laden...', fetching:'Daten von SISTRIX abrufen...',
 data_updated:'Daten von API aktualisiert', error_update:'Fehler beim Aktualisieren',
 confirm_delete:'Löschen?', enable:'Aktivieren', disable:'Deaktivieren', fill_fields:'Domain und Label ausfüllen', refresh_confirm_short:'Lädt nur fehlende Tage nach · Klicken zum Bestätigen', credits_available:'Credits verfügbar', apikey_removed:'API Key entfernt', apikey_checking:'API Key wird überprüft...', apikey_valid:'API Key gültig', apikey_invalid:'Ungültiger API Key', credits:'Credits', loading_data_short:'Lade Daten...',
 cache:'Cache', api:'API', brand_title:'Personalisierte Karte', brand_fetch:'Favicon laden', brand_or:'oder', brand_saved:'Marke gespeichert', brand_logo_ok:'Logo geladen', brand_logo_err:'Logo konnte nicht geladen werden', layout_reset:'Layout zurückgesetzt', brand_upload:'Bild hochladen', brand_delete_logo:'Löschen',
 edit:'\u270E Bearbeiten', done_editing:'Speichern', reset:'Zurücksetzen', edit_hint_touch:'Lang drücken um Text/Farbe zu bearbeiten',
 label:'Label', mode_weekly:'Wöchentlich', mode_daily:'Täglich', bl_speed:'Geschwindigkeit',
 bl_slow:'Langsam', bl_fast:'Schnell', bl_delete_logo:'Logo löschen',
 },
 pt: { sim_title:'Panel',
 last_update:'Última verificação:', refresh_btn:'Atualizar', apikey_placeholder:'A tua API key SISTRIX', save:'Guardar',
 domains_title:'Endereços', domain:'Domínio', country:'País', mode:'Modo', type:'Tipo', address:'Endereço',
 domain_placeholder:'exemplo.com', add:'+ Adicionar', active_domains:'endereços ativos',
 loading_data:'A carregar dados SISTRIX...', added:'Adicionado',
 updated:'Atualizado', mode_changed:'Modo alterado', deleted:'Eliminado',
 loading_dots:'A carregar...', fetching:'A obter dados do SISTRIX...',
 data_updated:'Dados atualizados da API', error_update:'Erro ao atualizar',
 confirm_delete:'Eliminar?', enable:'Ativar', disable:'Desativar', fill_fields:'Preenche domínio e label', refresh_confirm_short:'Só recarrega os dias em falta · Clique para confirmar', credits_available:'créditos disponíveis', apikey_removed:'API Key removida', apikey_checking:'A validar API Key...', apikey_valid:'API Key válida', apikey_invalid:'API Key inválida', credits:'créditos', loading_data_short:'A carregar dados...',
 cache:'cache', api:'api', brand_title:'Cartão personalizado', brand_fetch:'Obter favicon', brand_or:'ou', brand_saved:'Marca guardada', brand_logo_ok:'Logo carregado', brand_logo_err:'Não foi possível carregar o logo', layout_reset:'Layout reposto', brand_upload:'Carregar imagem', brand_delete_logo:'Eliminar',
 edit:'\u270E Editar', done_editing:'Guardar', reset:'Repor', edit_hint_touch:'Mantém pressionado para editar texto/cor',
 label:'Etiqueta', mode_weekly:'Semanal', mode_daily:'Diário', bl_speed:'Velocidade',
 bl_slow:'Lento', bl_fast:'Rápido', bl_delete_logo:'Eliminar logo',
 },
};

// ===== STATE =====
let currentLang = 'en';
let currentTheme = 'dark';
let currentConfig = {};
let lastCacheData = [];
let lastDomainHash = '';
let sistrixCredits = null;

function t(key) { return (I18N[currentLang] || I18N.en)[key] || (I18N.en)[key] || key; }

function applyI18n() {
 document.documentElement.lang = currentLang;
 document.querySelectorAll('[data-i18n]').forEach(el => {
 el.textContent = t(el.dataset.i18n);
 });
 document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
 el.placeholder = t(el.dataset.i18nPlaceholder);
 });
}

async function setLang(lang) {
 currentLang = lang;
 applyI18n();
 // Re-init speed select with translated labels
 const curSpeed = BL.msgSpeed.value;
 initCustomSelect(BL.msgSpeed, [
 {value:'100',text:t('bl_slow')},{value:'60',text:'Normal'},{value:'30',text:t('bl_fast')}
 ], curSpeed);
 BL.msgSpeed.onchange = () => saveBrandLayout();
 if (currentConfig.domains) { lastDomainHash = ''; renderDomains(currentConfig.domains); }
 updateStatusBar();
 if (totalSlides() > 0) renderSlide();
 postJSON('/api/language', {language:lang});
}

// ===== THEME =====
function applyTheme(theme) {
 currentTheme = theme;
 document.documentElement.setAttribute('data-theme', theme);
 DOM.themeToggle.textContent = theme === 'dark' ? '\u263E' : '\u2600';
}

async function toggleTheme() {
 const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
 applyTheme(newTheme);
 await postJSON('/api/theme', {theme:newTheme});
}

// ===== LED SIMULATOR =====
const canvas = document.getElementById('ledCanvas');
const displayCtx = canvas.getContext('2d');
const LED_W = 64, LED_H = 32;
const SCALE = 6; // each LED pixel = 6x6 on screen
// Double-buffer: draw to offscreen canvas, then blit to visible canvas
const offCanvas = document.createElement('canvas');
let ctx = offCanvas.getContext('2d');
canvas.width = LED_W * SCALE;
canvas.height = LED_H * SCALE;
offCanvas.width = canvas.width;
offCanvas.height = canvas.height;

// Cached DOM references (static elements only)
const $ = id => document.getElementById(id);
const DOM = {
 statusBar: $('statusBar'), domainList: $('domainList'),
 previewStatus: $('previewStatus'), btnPlayPause: $('btnPlayPause'), domainCounter: $('domainCounter'), ledOuter: $('ledOuter'),
 apiKey: $('apiKey'), cycleBtns: $('cycleBtns'), langSelect: $('langSelect'),
 themeToggle: $('themeToggle'), toast: $('toast'),
 newDomain: $('newDomain'),
 newCountry: $('newCountry'), newLabel: $('newLabel'), newMode: $('newMode'), newType: $('newType'),
};
// Brand layout inputs
const BL_IDS = ['logoX','logoY','logoSize','nameX','nameY','msgX','msgY','nameColor','msgColor','msgSpeed','nameFont','msgFont'];
const BL = {};
BL_IDS.forEach(id => BL[id] = $('bl_' + id));

let previewData = [];
let currentIndex = 0;
let autoRotate = true;
let rotateInterval = null;
let cycleTime = 10000;

// Data card layout (positions of each element on LED)
const DEFAULT_DATA_LAYOUT = { labelX:1, labelY:0, labelFont:'small', labelH:null, labelScale:1, changeY:0, changeFont:'small', changeH:null, changeX:null, changeScale:1, valueX:1, valueY:10, valueFont:'large', valueH:null, valueScale:1, countryX:52, countryY:10, countryFont:'small', countryH:null, countryScale:1, sparkY:21, sparkH:10, labelColor:'#ffffff', valueColor:'#ffffff', changeUpColor:'#00dc00', changeDownColor:'#ff2828', countryColor:'#999999', sparkUpColor:'#00c853', sparkDownColor:'#ff2d55' };
let dataLayout = { ...DEFAULT_DATA_LAYOUT };
let dataLayoutEditMode = false;

function drawLED(data) {
 // Clear any leftover pixels from a previous interrupted draw
 for (const k in _pixelBatch) delete _pixelBatch[k];
 const bg = '#000';
 ctx.fillStyle = bg;
 ctx.fillRect(0, 0, canvas.width, canvas.height);

 if (!data) {
 drawText('NO DATA', 4, 4, '#ff2d55', 'large');
 drawText('Add domains', 4, 18, '#444', 'small');
 flushPixels();
 return;
 }

 const DL = dataLayout;
 const isUp = data.is_up;
 const changeColor = isUp ? ((DL.changeUpColor||'#00dc00')) : ((DL.changeDownColor||'#ff2828'));
 const labelColor = (DL.labelColor||'#ffffff');
 const valueColor = (DL.valueColor||'#ffffff');
 const countryColor = (DL.countryColor||'#999999');

 // Label + mode
 const lh = DL.labelH;
 drawText(data.label, DL.labelX, DL.labelY, labelColor, DL.labelFont, lh);
 const labelW = measureText(data.label, DL.labelFont, lh);
 const modeChar = data.mode === 'daily' ? 'D' : 'W';
 drawText(modeChar, DL.labelX + labelW + 1, DL.labelY, countryColor, DL.labelFont, lh);

 // Change %
 const cf = DL.changeFont || 'small';
 const ch = DL.changeH;
 const changeStr = (data.change_pct >= 0 ? '+' : '') + data.change_pct.toFixed(1) + '%';
 const changeW = measureText(changeStr, cf, ch);
 const changeX = (DL.changeX != null) ? DL.changeX : (LED_W - changeW - 1);
 drawText(changeStr, changeX, DL.changeY, changeColor, cf, ch);

 // Value
 const vh = DL.valueH;
 let valueStr;
 if (data.current_value >= 100) valueStr = data.current_value.toFixed(1);
 else valueStr = data.current_value.toFixed(2);
 drawText(valueStr, DL.valueX, DL.valueY, valueColor, DL.valueFont, vh);

 // Country
 const ctf = DL.countryFont || 'small';
 const cth = DL.countryH;
 drawText(data.country.toUpperCase(), DL.countryX, DL.countryY, countryColor, ctf, cth);

 // Sparkline
 const history = [...data.history].reverse();
 if (history.length > 1) {
 const chartTop = DL.sparkY, chartBottom = DL.sparkY + DL.sparkH, chartH = DL.sparkH;
 const minV = Math.min(...history);
 const maxV = Math.max(...history);
 const range = maxV - minV || 1;
 const numPts = Math.min(history.length, LED_W - 2);
 const step = (LED_W - 2) / Math.max(numPts - 1, 1);

 const points = [];
 for (let i = 0; i < numPts; i++) {
 const x = Math.round(1 + i * step);
 const idx = history.length - numPts + i;
 const norm = (history[idx] - minV) / range;
 const y = chartBottom - Math.round(norm * chartH);
 points.push([x, y]);
 }

 const sparkColor = isUp ? ((DL.sparkUpColor||'#00c853')) : ((DL.sparkDownColor||'#ff2d55'));
 for (let i = 0; i < points.length - 1; i++) {
 drawLine(points[i][0], points[i][1], points[i+1][0], points[i+1][1], sparkColor);
 }
 }
 flushPixels();
 if (dataLayoutEditMode) { dataEditor.drawOverlay(data); displayCtx.drawImage(offCanvas, 0, 0); }
}

// Bitmap pixel fonts — each char is an array of rows, each row is a binary string
// FONT_3x5: ultra-compact 3px wide, 5px tall + 1px spacing = 4px per char
const F3x5 = {
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
};

// FONT_4x6: intermediate font — 4px wide, 6px tall, hand-crafted for clean rendering at h=6
const F4x6 = {
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
};

// FONT_5x7: larger font for the main value — 5px wide, 7px tall + 1px spacing = 6px per char
const F5x7 = {
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
 'W':'10001100011000110101101011000100000',
 'X':'10001010100010001000101011000100000',
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
 // Accented vowels (acute)
 'Á':'00010011101000111111100011000100000',
 'É':'00010111111000011110100001111100000',
 'Í':'00010111110010000100001001111100000',
 'Ó':'00010011101000110001100010111000000',
 'Ú':'00010100011000110001100010111000000',
 // Accented vowels (grave)
 'À':'01000011101000111111100011000100000',
 'È':'01000111111000011110100001111100000',
 'Ì':'01000111110010000100001001111100000',
 'Ò':'01000011101000110001100010111000000',
 'Ù':'01000100011000110001100010111000000',
 // Accented vowels (circumflex)
 'Â':'00100011101000111111100011000100000',
 'Ê':'00100111111000011110100001111100000',
 'Î':'00100111110010000100001001111100000',
 'Ô':'00100011101000110001100010111000000',
 'Û':'00100100011000110001100010111000000',
 // Accented vowels (umlaut/diaeresis)
 'Ä':'01010011101000111111100011000100000',
 'Ë':'01010111111000011110100001111100000',
 'Ï':'01010111110010000100001001111100000',
 'Ö':'01010011101000110001100010111000000',
 'Ü':'01010100011000110001100010111000000',
 // Tilde
 'Ã':'01010011101000111111100011000100000',
 'Õ':'01010011101000110001100010111000000',
 'Ñ':'01010100011100110101100111000100000',
 // Cedilla
 'Ç':'01110100001000010000011100010001000',
};

// Pre-parse font strings to Uint8Arrays for faster rendering
function parseFont(font) {
 const parsed = {};
 for (const ch in font) parsed[ch] = Uint8Array.from(font[ch], c => c === '1' ? 1 : 0);
 return parsed;
}
const PF3x5 = parseFont(F3x5);
const PF4x6 = parseFont(F4x6);
const PF5x7 = parseFont(F5x7);

// Accent stripping for small font (no room for diacritics at 3x5)
const ACCENT_MAP = {
 'Á':'A','À':'A','Â':'A','Ã':'A','Ä':'A',
 'É':'E','È':'E','Ê':'E','Ë':'E',
 'Í':'I','Ì':'I','Î':'I','Ï':'I',
 'Ó':'O','Ò':'O','Ô':'O','Õ':'O','Ö':'O',
 'Ú':'U','Ù':'U','Û':'U','Ü':'U',
 'Ñ':'N','Ç':'C',
};

function stripAccents(ch) { return ACCENT_MAP[ch] || ch; }

// Batched pixel rendering — collects pixels by color, draws once per color
const _pixelBatch = {};

function drawPixel(px, py, color) {
 if (px < 0 || px >= LED_W || py < 0 || py >= LED_H) return;
 if (!_pixelBatch[color]) _pixelBatch[color] = [];
 _pixelBatch[color].push(px, py);
}

function flushPixels() {
 const s = SCALE, r = s * 0.35, gr = s * 0.6, TAU = Math.PI * 2;
 for (const color in _pixelBatch) {
 const pts = _pixelBatch[color];
 // LED dots
 ctx.fillStyle = color;
 ctx.beginPath();
 for (let i = 0; i < pts.length; i += 2) {
 const cx = pts[i] * s + s/2, cy = pts[i+1] * s + s/2;
 ctx.moveTo(cx + r, cy);
 ctx.arc(cx, cy, r, 0, TAU);
 }
 ctx.fill();
 // Glow pass
 ctx.globalAlpha = 0.15;
 ctx.beginPath();
 for (let i = 0; i < pts.length; i += 2) {
 const cx = pts[i] * s + s/2, cy = pts[i+1] * s + s/2;
 ctx.moveTo(cx + gr, cy);
 ctx.arc(cx, cy, gr, 0, TAU);
 }
 ctx.fill();
 ctx.globalAlpha = 1;
 }
 // Clear batch
 for (const k in _pixelBatch) delete _pixelBatch[k];
 // Blit offscreen canvas to visible canvas (eliminates flicker)
 displayCtx.drawImage(offCanvas, 0, 0);
}

// Build a mapping from target pixels to source pixels using Bresenham distribution
// This ensures strokes are evenly distributed (no 2-1-2-1 artifacts)
function buildScaleMap(src, dst) {
 if (dst === src) return null; // no scaling needed
 const map = new Uint8Array(dst);
 // Each source pixel gets floor(dst/src) or ceil(dst/src) output pixels
 // Distribute extras evenly using Bresenham-style error accumulation
 let pos = 0;
 for (let s = 0; s < src; s++) {
 const nextPos = Math.round((s + 1) * dst / src);
 const count = nextPos - pos;
 for (let p = 0; p < count; p++) map[pos + p] = s;
 pos = nextPos;
 }
 return map;
}

// Cache scale maps to avoid rebuilding every frame
const _scaleMaps = {};
function getScaleMap(src, dst) {
 if (dst === src) return null;
 const key = src + '_' + dst;
 if (!_scaleMaps[key]) _scaleMaps[key] = buildScaleMap(src, dst);
 return _scaleMaps[key];
}

function rainbowColor(px) {
 const RAINBOW = [
 [255,0,0],[255,136,0],[255,255,0],[0,255,0],[0,136,255],[136,0,255],[255,0,255]
 ];
 const t = ((((px % 64) + 64) % 64) / 64) * (RAINBOW.length - 1);
 const i = Math.floor(t), f = t - i;
 const a = RAINBOW[i], b = RAINBOW[Math.min(i + 1, RAINBOW.length - 1)];
 return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
}

function drawText(text, x, y, color, size, h) {
 const isRainbow = color === 'rainbow';
 const str = text.normalize('NFC').toUpperCase();
 let isLarge = size === 'large';
 // Use native 4x6 font when small font scaled to h=6
 if (!isLarge && h === 6) {
 for (let ci = 0; ci < str.length; ci++) {
 if (x >= LED_W) break;
 const bits = PF4x6[str[ci]] || PF4x6[stripAccents(str[ci])];
 if (!bits) { x += 5; continue; }
 if (x + 4 >= 0) {
 for (let row = 0; row < 6; row++)
 for (let col = 0; col < 4; col++)
 if (bits[row * 4 + col]) drawPixel(x + col, y + row, isRainbow ? rainbowColor(x + col) : color);
 }
 x += 5;
 }
 return;
 }
 // Auto-promote small font to large when scaled beyond native 5px
 if (!isLarge && h && h >= 7) isLarge = true;
 const srcW = isLarge ? 5 : 3, srcH = isLarge ? 7 : 5;
 const font = isLarge ? PF5x7 : PF3x5;
 h = h || srcH;
 if (h === srcH) {
 // Native size — fast path, no scaling
 for (let ci = 0; ci < str.length; ci++) {
 if (x >= LED_W) break;
 const bits = font[str[ci]] || font[stripAccents(str[ci])];
 if (!bits) { x += srcW + 1; continue; }
 if (x + srcW >= 0) {
 for (let row = 0; row < srcH; row++)
 for (let col = 0; col < srcW; col++)
 if (bits[row * srcW + col]) drawPixel(x + col, y + row, isRainbow ? rainbowColor(x + col) : color);
 }
 x += srcW + 1;
 }
 return;
 }
 // Scaled — Bresenham-distributed pixel mapping for even strokes
 const charPxW = Math.round(srcW * h / srcH);
 const charStep = charPxW + Math.max(1, Math.round(h / srcH));
 const mapY = getScaleMap(srcH, h);
 const mapX = getScaleMap(srcW, charPxW);
 for (let ci = 0; ci < str.length; ci++) {
 if (x >= LED_W) break;
 const bits = font[str[ci]] || font[stripAccents(str[ci])];
 if (!bits) { x += charStep; continue; }
 if (x + charPxW >= 0) {
 for (let oy = 0; oy < h; oy++) {
 for (let ox = 0; ox < charPxW; ox++) {
 if (bits[mapY[oy] * srcW + mapX[ox]]) drawPixel(x + ox, y + oy, isRainbow ? rainbowColor(x + ox) : color);
 }
 }
 }
 x += charStep;
 }
}

function measureText(text, size, h) {
 const str = text.normalize('NFC').toUpperCase();
 let isLarge = size === 'large';
 // Native 4x6 font at h=6
 if (!isLarge && h === 6) return str.length * 5;
 if (!isLarge && h && h >= 7) isLarge = true;
 const srcW = isLarge ? 5 : 3, srcH = isLarge ? 7 : 5;
 h = h || srcH;
 if (h === srcH) return str.length * (srcW + 1);
 const charPxW = Math.round(srcW * h / srcH);
 const charStep = charPxW + Math.max(1, Math.round(h / srcH));
 return str.length * charStep;
}

function textHeight(size, h) {
 if (h) return h;
 return size === 'large' ? 7 : 5;
}

function drawLine(x0, y0, x1, y1, color) {
 // Bresenham for pixel-perfect line on LED grid
 const dx = Math.abs(x1 - x0), dy = Math.abs(y1 - y0);
 const sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
 let err = dx - dy;

 while (true) {
 drawPixel(x0, y0, color);
 if (x0 === x1 && y0 === y1) break;
 const e2 = 2 * err;
 if (e2 > -dy) { err -= dy; x0 += sx; }
 if (e2 < dx) { err += dx; y0 += sy; }
 }
}

let showingBrand = false;

function totalSlides() {
 return previewData.length + 1; // brand is always the last slide
}

function isBrandSlide() {
 return currentIndex >= previewData.length;
}

function nextDomain() {
 if (dataLayoutEditMode || layoutEditMode) return;
 const total = totalSlides();
 if (total === 0) return;
 stopMessageScroll();
 currentIndex = (currentIndex + 1) % total;
 renderSlide();
}

function prevDomain() {
 if (dataLayoutEditMode || layoutEditMode) return;
 const total = totalSlides();
 if (total === 0) return;
 stopMessageScroll();
 currentIndex = (currentIndex - 1 + total) % total;
 renderSlide();
}

function renderSlide() {
 stopMessageScroll();
 const total = totalSlides();
 if (total === 0) {
 showingBrand = false;
 drawLED(null);
 DOM.domainCounter.innerHTML = `${autoRotate ? '&#9654;' : '&#9646;&#9646;'} -/-`;
 return;
 }
 DOM.domainCounter.innerHTML = `${autoRotate ? '&#9654;' : '&#9646;&#9646;'} ${currentIndex + 1}/${total}`;

 if (isBrandSlide()) {
 showingBrand = true;
 const brandLabel = t('brand_title');
 const toggleOn = brandData.enabled;
 if (toggleOn) {
 drawBrandCard();
 startMessageScroll();
 } else {
 drawBrandDisabled();
 }
 DOM.previewStatus.innerHTML = `${brandLabel} <button class="toggle-btn toggle-sm ${toggleOn ? 'on' : 'off'}" onclick="toggleBrandEnabled()" aria-label="${toggleOn ? t('disable') : t('enable')}" role="switch" aria-checked="${toggleOn}"></button>`;
 } else {
 showingBrand = false;
 const d = previewData[currentIndex];
 drawLED(d);
 const status = DOM.previewStatus;
 if (d) {
 const info = `${d.domain.replace(/^https?:\/\//, '')} · ${d.type || 'domain'} · ${d.country.toUpperCase()} · ${d.mode === 'daily' ? t('mode_daily') : t('mode_weekly')}`;
 status.innerHTML = info;
 canvas.setAttribute('aria-label', `${d.label}: ${d.current_value ?? ''} - ${info}`);
 }
 }
}

// Keep renderCurrent as alias for compatibility with internal calls
function renderCurrent() {
 if (showingBrand && !isBrandSlide()) showingBrand = false;
 renderSlide();
}

function toggleAutoRotate() {
 autoRotate = !autoRotate;
 DOM.btnPlayPause.classList.toggle('active', autoRotate);
 if (autoRotate) { startRotation(); } else { stopRotation(); }
 renderSlide();
}

function startRotation() {
 stopRotation();
 rotateInterval = setInterval(() => {
 // Auto-rotate only among active slides (skip disabled brand)
 const activeSlides = brandData.enabled ? totalSlides() : previewData.length;
 if (activeSlides > 1) {
 stopMessageScroll();
 currentIndex = (currentIndex + 1) % activeSlides;
 renderSlide();
 }
 }, cycleTime);
}

function stopRotation() {
 if (rotateInterval) { clearInterval(rotateInterval); rotateInterval = null; }
}

async function loadPreview() {
 try {
 await updatePreviewData();
 } catch(e) {
 console.error('Preview error:', e);
 }
}

// ===== CACHE STATUS =====
let _refreshArmed = false;
let _refreshTimer = null;
function clickRefresh() {
 const btn = document.querySelector('.btn-refresh');
 if (!btn) return;
 if (!_refreshArmed) {
 _refreshArmed = true;
 const creditsInfo = sistrixCredits != null ? ` (${sistrixCredits.toLocaleString('de-DE')} ${t('credits_available')})` : '';
 btn.innerHTML = `${t('refresh_confirm_short')}${creditsInfo}`;
 btn.style.borderColor = 'var(--accent)';
 btn.style.color = 'var(--accent)';
 _refreshTimer = setTimeout(() => { _refreshArmed = false; btn.innerHTML = `${t('refresh_btn')}`; btn.style.borderColor = ''; btn.style.color = ''; }, 4000);
 return;
 }
 clearTimeout(_refreshTimer);
 _refreshArmed = false;
 doRefresh();
}
async function doRefresh() {
 const btn = document.querySelector('.btn-refresh');
 if (btn) { btn.disabled = true; btn.textContent = t('loading_dots'); btn.style.borderColor = ''; btn.style.color = ''; }
 toast(t('fetching'), true);
 try {
 await updatePreviewData(false, true);
 await loadCacheStatus();
 // Update credits after refresh
 fetch('/api/credits').then(r => r.json()).then(d => { sistrixCredits = d.credits; updateStatusBar(); });
 toast(t('data_updated'));
 } catch(e) {
 toast(t('error_update'));
 }
 if (btn) { btn.disabled = false; btn.innerHTML = `${t('refresh_btn')}`; }
}

async function loadCacheStatus() {
 try {
 const res = await fetch('/api/cache/status');
 lastCacheData = await res.json();
 updateStatusBar();

 } catch(e) {
 console.error(e);
 }
}


function formatTime(isoStr) {
 const d = new Date(isoStr);
 const now = new Date();
 const sameDay = d.toDateString() === now.toDateString();
 const time = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
 if (sameDay) return time;
 return d.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + time;
}



// ===== HELPERS =====
function postJSON(url, data) {
 return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
}
function putJSON(url, data) {
 return fetch(url, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
}
async function updatePreviewData(force=false, refresh=false) {
 const url = force ? '/api/preview?force=true' : (refresh ? '/api/preview?refresh=true' : '/api/preview');
 const res = await fetch(url);
 previewData = await res.json();
 const total = totalSlides();
 if (total > 0) {
 currentIndex = Math.min(currentIndex, total - 1);
 renderSlide();
 if (autoRotate && !dataLayoutEditMode && !layoutEditMode) startRotation();
 } else {
 drawLED(null);
 DOM.domainCounter.innerHTML = `${autoRotate ? '&#9654;' : '&#9646;&#9646;'} -/-`;
 }
}

function updateStatusBar() {
 if (!currentConfig.domains) return;
 const active = currentConfig.domains.filter(d => d.active).length;
 const total = currentConfig.domains.length;
 const hasKey = currentConfig.sistrix_api_key && currentConfig.sistrix_api_key !== 'TU_API_KEY_AQUI';
 const statusBar = DOM.statusBar;

 let updatedPart = '';
 if (lastCacheData.length) {
 const newest = lastCacheData.reduce((a, b) => (a.cached_at > b.cached_at ? a : b), lastCacheData[0]);
 const lastTime = newest.cached_at ? formatTime(newest.cached_at) : '';
 if (lastTime) updatedPart = `<span class="status-item"><span class="status-dot dot-green" aria-hidden="true"></span>${t('last_update')} ${lastTime}</span>`;
 }

 statusBar.innerHTML = `
 <span class="status-item"><span class="status-dot dot-green" aria-hidden="true"></span>${active}/${total} ${t('active_domains')}</span>
 ${updatedPart}
 <button class="btn-refresh" onclick="clickRefresh()">${t('refresh_btn')}</button>
 `;
}

let _toastTimer = null;
function toast(msg, persist) {
 clearTimeout(_toastTimer);
 DOM.toast.textContent = msg;
 DOM.toast.classList.add('show');
 if (!persist) _toastTimer = setTimeout(() => DOM.toast.classList.remove('show'), 2000);
}

async function loadConfig() {
 const res = await fetch('/api/config');
 const config = await res.json();
 currentConfig = config;
 const keyInput = DOM.apiKey;
 const hasKey = config.sistrix_api_key && config.sistrix_api_key !== 'TU_API_KEY_AQUI';
 if (hasKey) keyInput.value = config.sistrix_api_key;
 else keyInput.value = '';
 const dot = $('apiDot');
 dot.className = 'status-dot ' + (hasKey ? 'dot-green' : 'dot-red');
 $('btnApiKey').querySelector('.api-label').textContent = hasKey ? 'API' : 'Add API';
 if (!hasKey) sistrixCredits = null;
 cycleTime = config.display.cycle_seconds * 1000;
 renderCycleBtns();
 renderDomains(config.domains);
 updateStatusBar();
 // Fetch credits in background
 if (hasKey && sistrixCredits === null) {
 fetch('/api/credits').then(r => r.json()).then(d => { sistrixCredits = d.credits; updateStatusBar(); });
 }
 // Restore language and theme from config
 if (config.language && config.language !== currentLang) {
 currentLang = config.language;
 DOM.langSelect.value = currentLang;
 applyI18n();
 }
 if (config.theme && config.theme !== currentTheme) {
 applyTheme(config.theme);
 }
 // Restore data layout
 if (config.data_layout) {
 Object.assign(dataLayout, config.data_layout);
 }
}

function renderDomains(domains) {
 const hash = JSON.stringify(domains);
 if (hash === lastDomainHash) return;
 lastDomainHash = hash;
 DOM.domainList.innerHTML = domains.map((d, i) => {
 return `
 <div class="domain-card ${d.active ? '' : 'inactive'}" id="dcard-${i}" data-index="${i}">
 <span class="drag-handle" title="Drag to reorder" aria-hidden="true">⠿</span>
 <span class="reorder-btns" role="group" aria-label="Reorder">
 <button class="btn-reorder" onclick="moveDomain(${i},-1)" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>▲</button>
 <button class="btn-reorder" onclick="moveDomain(${i},1)" aria-label="Move down" ${i === domains.length - 1 ? 'disabled' : ''}>▼</button>
 </span>
 <span class="domain-label clickable" onclick="editDomain(${i})" onkeydown="if(event.key==='Enter')editDomain(${i})" tabindex="0" role="button" title="Click to edit">${d.label}</span>
 <span class="domain-info clickable" onclick="editDomain(${i})" onkeydown="if(event.key==='Enter')editDomain(${i})" tabindex="0" role="button" title="${d.domain}">${d.domain.replace(/^https?:\/\//, '')}</span>
 <span class="domain-type-tag">${d.type || 'domain'}</span>
 <span class="domain-country-tag">${d.country.toUpperCase()}</span>
 <button class="domain-mode mode-${d.mode}" onclick="toggleMode(${i},'${d.mode}')">${t('mode_'+d.mode)}</button>
 <button class="toggle-btn toggle-sm ${d.active ? 'on' : 'off'}" onclick="toggleDomain(${i})" aria-label="${d.active ? t('disable') : t('enable')} ${d.label}" role="switch" aria-checked="${d.active}"></button>
 <button class="btn-icon btn-icon-danger" onclick="deleteDomain(${i})" aria-label="${t('confirm_delete')} ${d.label}">✕</button>
 </div>`;
 }).join('');
 initDragAndDrop();
}

async function moveDomain(idx, dir) {
 const len = currentConfig.domains.length;
 const to = idx + dir;
 if (to < 0 || to >= len) return;
 const order = [...Array(len).keys()];
 const [moved] = order.splice(idx, 1);
 order.splice(to, 0, moved);
 await postJSON('/api/domains/reorder', {order});
 lastDomainHash = '';
 await loadConfig();
 await loadPreview();
 // Restore focus to the moved card's button
 setTimeout(() => {
  const card = document.getElementById('dcard-' + to);
  if (card) { const btn = card.querySelector('.btn-reorder:not(:disabled)'); if (btn) btn.focus(); }
 }, 100);
}

let _dragSrcIndex = null;
// Single global mouseup handler — avoids listener leak
document.addEventListener('mouseup', () => {
 document.querySelectorAll('.domain-card[draggable="true"]').forEach(c => { c.draggable = false; });
});
function initDragAndDrop() {
 const cards = DOM.domainList.querySelectorAll('.domain-card');
 cards.forEach(card => {
  card.draggable = false;
  const handle = card.querySelector('.drag-handle');
  if (handle) {
   handle.addEventListener('mousedown', () => { card.draggable = true; });
  }
  card.addEventListener('dragstart', e => {
   _dragSrcIndex = +card.dataset.index;
   card.classList.add('dragging');
   e.dataTransfer.effectAllowed = 'move';
  });
  card.addEventListener('dragend', () => {
   card.draggable = false;
   card.classList.remove('dragging');
   DOM.domainList.querySelectorAll('.drag-over').forEach(c => c.classList.remove('drag-over'));
  });
  card.addEventListener('dragover', e => {
   e.preventDefault();
   e.dataTransfer.dropEffect = 'move';
   card.classList.add('drag-over');
  });
  card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
  card.addEventListener('drop', async e => {
   e.preventDefault();
   card.classList.remove('drag-over');
   const to = +card.dataset.index;
   if (_dragSrcIndex === null || _dragSrcIndex === to) return;
   const order = [...Array(currentConfig.domains.length).keys()];
   const [moved] = order.splice(_dragSrcIndex, 1);
   order.splice(to, 0, moved);
   await postJSON('/api/domains/reorder', {order});
   lastDomainHash = '';
   await loadConfig();
   await loadPreview();
  });
  // Touch: only touchstart per card, global move/end registered once above
  if (handle) {
   handle.addEventListener('touchstart', e => {
    e.preventDefault();
    _touchIndex = +card.dataset.index;
    _touchCard = card;
    _touchClone = card.cloneNode(true);
    _touchClone.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;opacity:0.8;width:'+card.offsetWidth+'px;left:'+card.getBoundingClientRect().left+'px;top:'+card.getBoundingClientRect().top+'px;';
    document.body.appendChild(_touchClone);
    card.classList.add('dragging');
   }, {passive:false});
  }
 });

}
// Touch drag — global handlers registered once to avoid leak
let _touchCard = null, _touchClone = null, _touchIndex = null;
document.addEventListener('touchmove', e => {
 if (!_touchClone) return;
 e.preventDefault();
 const y = e.touches[0].clientY;
 _touchClone.style.top = (y - 20) + 'px';
 DOM.domainList.querySelectorAll('.domain-card').forEach(c => c.classList.remove('drag-over'));
 const target = document.elementFromPoint(e.touches[0].clientX, y);
 const targetCard = target?.closest('.domain-card');
 if (targetCard && targetCard !== _touchCard) targetCard.classList.add('drag-over');
}, {passive:false});
document.addEventListener('touchend', async () => {
 if (!_touchClone) return;
 _touchClone.remove(); _touchClone = null;
 if (_touchCard) _touchCard.classList.remove('dragging');
 const overCard = DOM.domainList.querySelector('.drag-over');
 if (overCard) {
  overCard.classList.remove('drag-over');
  const to = +overCard.dataset.index;
  if (_touchIndex !== null && _touchIndex !== to) {
   const order = [...Array(currentConfig.domains.length).keys()];
   const [moved] = order.splice(_touchIndex, 1);
   order.splice(to, 0, moved);
   await postJSON('/api/domains/reorder', {order});
   lastDomainHash = '';
   await loadConfig();
   await loadPreview();
  }
 }
 _touchCard = null; _touchIndex = null;
});

function cancelEdit() {
 lastDomainHash = '';
 renderDomains(currentConfig.domains);
}

function editDomain(i) {
 const card = document.getElementById('dcard-' + i);
 if (card.querySelector('input')) return; // already editing
 const cfg = currentConfig.domains[i];
 const curType = cfg.type || 'domain';
 const curMode = cfg.mode || 'weekly';
 card.innerHTML = `
 <div class="edit-grid">
 <div class="edit-row">
  <input type="text" value="${cfg.label}" maxlength="8" id="ed-label-${i}" class="edit-input" style="font-weight:bold;text-transform:uppercase;width:70px;flex-shrink:0;">
  <input type="text" value="${cfg.domain}" id="ed-domain-${i}" class="edit-input" style="flex:1;min-width:100px;">
  <div id="ed-type-${i}" class="custom-select" style="width:80px;flex-shrink:0;"></div>
 </div>
 <div class="edit-row">
  <div id="ed-country-${i}" class="custom-select" style="width:70px;flex-shrink:0;"></div>
  <div id="ed-mode-${i}" class="custom-select" style="width:90px;flex-shrink:0;"></div>
  <button class="btn btn-small" onclick="saveDomainEdit(${i})" style="flex:0 0 auto;" data-i18n="save">${t('save')}</button>
  <button class="btn-icon btn-icon-danger" onclick="cancelEdit()" aria-label="Cancel" style="flex:0 0 auto;">✕</button>
 </div>
 </div>
 `;
 initCustomSelect(document.getElementById('ed-type-' + i), [
  {value:'domain',text:'Domain'},{value:'host',text:'Host'},{value:'path',text:'Path'},{value:'url',text:'URL'}
 ], curType);
 initCustomSelect(document.getElementById('ed-country-' + i), countryOptions(window._countries || []), cfg.country);
 initCustomSelect(document.getElementById('ed-mode-' + i), [
  {value:'weekly',text:t('mode_weekly')},{value:'daily',text:t('mode_daily')}
 ], curMode);
 card.querySelector('input').focus();
}

async function saveDomainEdit(i) {
 const label = document.getElementById('ed-label-' + i).value.trim();
 const domain = document.getElementById('ed-domain-' + i).value.trim();
 const country = document.getElementById('ed-country-' + i).value.toLowerCase();
 const type = document.getElementById('ed-type-' + i).value;
 const mode = document.getElementById('ed-mode-' + i).value;
 if (!label || !domain) { toast(t('fill_fields')); return; }
 const old = currentConfig.domains[i];
 const domainChanged = old.domain !== domain || old.country !== country || old.type !== type;
 await putJSON('/api/domains/' + i, {label, domain, country, type, mode});
 lastDomainHash = '';
 await loadConfig();
 if (domainChanged) toast(t('loading_data_short'), true);
 await updatePreviewData(domainChanged);
 await loadCacheStatus();
 toast(t('updated'));
}

async function toggleDomain(i) {
 await postJSON(`/api/domains/${i}/toggle`, {});
 await loadConfig();
 toast(t('loading_data_short'), true);
 await loadPreview();
 await loadCacheStatus();
 toast(t('updated'));
}

async function toggleMode(i, current) {
 await putJSON(`/api/domains/${i}`, {mode: current === 'weekly' ? 'daily' : 'weekly'});
 await loadConfig();
 toast(t('loading_data_short'), true);
 await loadPreview();
 await loadCacheStatus();
 toast(t('mode_changed'));
}

async function deleteDomain(i) {
 if (!confirm(t('confirm_delete'))) return;
 await fetch(`/api/domains/${i}`, {method: 'DELETE'});
 await loadConfig();
 await loadPreview();
 await loadCacheStatus();
 toast(t('deleted'));
}

async function addDomain() {
 const domain = DOM.newDomain.value.trim();
 const country = DOM.newCountry.value;
 const label = DOM.newLabel.value.trim();
 const mode = DOM.newMode.value;
 const type = DOM.newType.value;
 if (!domain || !label) { toast(t('fill_fields')); return; }
 if (type === 'path' && !domain.includes('/')) { toast('Include / in path (e.g. example.com/blog/)'); return; }
 if (type === 'url' && !domain.includes('/')) { toast('Include full path (e.g. example.com/page)'); return; }
 await postJSON('/api/domains', {domain, country, label, mode, type, active: true});
 DOM.newDomain.value = '';
 DOM.newLabel.value = '';
 await loadConfig();
 toast(t('loading_data'), true);
 await loadPreview();
 await loadCacheStatus();
 toast(t('added'));
}

const CYCLE_OPTIONS = [5, 10, 15];
function renderCycleBtns() {
 const html = CYCLE_OPTIONS.map(s =>
 `<button class="btn-outline${cycleTime === s * 1000 ? ' active' : ''}" onclick="setCycle(${s})" aria-pressed="${cycleTime === s * 1000}">${s}s</button>`
 ).join('');
 if (DOM.cycleBtns.innerHTML !== html) DOM.cycleBtns.innerHTML = html;
}
async function setCycle(seconds) {
 await postJSON('/api/config/display', {cycle_seconds: seconds});
 cycleTime = seconds * 1000;
 renderCycleBtns();
 if (autoRotate) startRotation();
}

function toggleApiKeyPopup() {
 const popup = $('apiKeyPopup');
 const opening = popup.style.display === 'none';
 popup.style.display = opening ? 'flex' : 'none';
 if (opening) { popup.setAttribute('role', 'dialog'); popup.setAttribute('aria-modal', 'true'); setTimeout(() => DOM.apiKey.focus(), 10); }
 else { $('btnApiKey').focus(); }
}
document.addEventListener('click', e => {
 const popup = $('apiKeyPopup');
 if (popup.style.display !== 'none' && !e.target.closest('#apiKeyPopup') && !e.target.closest('#btnApiKey')) {
 popup.style.display = 'none';
 $('btnApiKey').focus();
 }
});

async function saveApiKey() {
 const key = DOM.apiKey.value.trim();
 if (!key) {
 await postJSON('/api/apikey', {api_key: ''});
 toast(t('apikey_removed'));
 $('apiKeyPopup').style.display = 'none';
 loadConfig();
 return;
 }
 toast(t('apikey_checking'), true);
 const res = await fetch('/api/apikey', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({api_key: key})});
 const data = await res.json();
 if (data.ok) {
 sistrixCredits = data.credits;
 toast(t('apikey_valid') + (data.credits != null ? ` (${data.credits} ${t('credits')})` : ''));
 $('apiKeyPopup').style.display = 'none';
 loadConfig();
 loadPreview();
 } else {
 toast(t('apikey_invalid'));
 }
}

// ===== BRAND =====
let brandData = {};
let brandLogoPixels = null;
let brandLayout = { logoX:1, logoY:1, logoSize:16, nameX:19, nameY:6, nameH:null, msgY:21, msgH:null, nameColor:'#ffffff', msgColor:'#00c853', msgSpeed:60, nameFont:'small', msgFont:'small' };

function initBrandSelects() {
 const onChange = () => saveBrandLayout();
 initCustomSelect(BL.logoSize, [
 {value:'12',text:'12'},{value:'16',text:'16'},{value:'20',text:'20'},{value:'24',text:'24'}
 ], '16');
 BL.logoSize.onchange = onChange;
 initCustomSelect(BL.nameFont, [
 {value:'small',text:'3x5'},{value:'large',text:'5x7'}
 ], 'small');
 BL.nameFont.onchange = onChange;
 initCustomSelect(BL.msgFont, [
 {value:'small',text:'3x5'},{value:'large',text:'5x7'}
 ], 'small');
 BL.msgFont.onchange = onChange;
 initCustomSelect(BL.msgSpeed, [
 {value:'100',text:t('bl_slow')},{value:'60',text:'Normal'},{value:'30',text:t('bl_fast')}
 ], '60');
 BL.msgSpeed.onchange = onChange;
}

let _brandFaviconUrl = '';

async function loadBrand() {
 try {
 const res = await fetch('/api/brand');
 brandData = await res.json();
 if (brandData.logo_pixels) {
 brandLogoPixels = brandData.logo_pixels;
 }
 if (brandData.layout) {
 brandLayout = {...brandLayout, ...brandData.layout};
 }
 BL_IDS.forEach(id => {
 const v = brandLayout[id] ?? '';
 BL[id].value = typeof v === 'number' ? String(v) : v;
 });
    renderCurrent();
 } catch(e) {}
}

async function saveBrandLayout() {
 const INT_FIELDS = new Set(['logoX','logoY','logoSize','nameX','nameY','msgX','msgY','msgSpeed']);
 BL_IDS.forEach(id => {
 brandLayout[id] = INT_FIELDS.has(id) ? parseInt(BL[id].value) : BL[id].value;
 });
 await postJSON('/api/brand',{layout:brandLayout});
 // Live preview on any layout change
 previewBrandCard();
}

function previewBrandCard() {
 // Jump to brand slide
 currentIndex = previewData.length;
 showingBrand = true;
 stopRotation();
 stopMessageScroll();
 drawBrandCard();
 startMessageScroll();
 const total = totalSlides();
 DOM.domainCounter.innerHTML = `${autoRotate ? '&#9654;' : '&#9646;&#9646;'} ${currentIndex + 1}/${total}`;
 DOM.previewStatus.innerHTML = `${t('brand_title')} <button class="toggle-btn toggle-sm on" onclick="toggleBrandEnabled()" aria-label="${t('disable')}" role="switch" aria-checked="true"></button>`;
 DOM.ledOuter.scrollIntoView({behavior:'smooth', block:'center'});
}

async function saveBrand() {
 await postJSON('/api/brand',{name: brandData.name || '', message: brandData.message || ''});
 toast(t('brand_saved'));
}



async function toggleBrandEnabled() {
 const enabled = !brandData.enabled;
 await postJSON('/api/brand',{enabled});
 brandData.enabled = enabled;
 // If disabling while in edit mode, exit edit first
 if (!enabled && layoutEditMode) {
 toggleEdit();
 }
 renderSlide();
 toast(t('brand_saved'));
}

async function deleteLogo() {
 brandLogoPixels = null;
 brandData.logo_pixels = null;
 await postJSON('/api/brand',{logo_pixels:null, logo_source:null});
 if (showingBrand) drawBrandCard();
}

function uploadLogo(fileInput) {
 const file = fileInput.files[0];
 if (!file) return;
 const reader = new FileReader();
 reader.onload = () => {
 const img = new Image();
 img.onload = () => {
 // Resize to 16x16 on a canvas
 const c = document.createElement('canvas');
 c.width = 16; c.height = 16;
 const cx = c.getContext('2d');
 cx.imageSmoothingEnabled = true;
 cx.imageSmoothingQuality = 'high';
 cx.drawImage(img, 0, 0, 16, 16);
 const imgData = cx.getImageData(0, 0, 16, 16).data;
 const boost = v => v < 30 ? 0 : v > 225 ? 255 : Math.min(255, Math.round(v * 1.15));
 const pixels = [];
 for (let y = 0; y < 16; y++) {
 const row = [];
 for (let x = 0; x < 16; x++) {
 const i = (y * 16 + x) * 4;
 const a = imgData[i + 3];
 if (a < 50) row.push([0, 0, 0]);
 else row.push([boost(imgData[i]), boost(imgData[i+1]), boost(imgData[i+2])]);
 }
 pixels.push(row);
 }
 brandLogoPixels = pixels;
 brandData.logo_pixels = pixels;
 postJSON('/api/brand', {logo_pixels: pixels});
 if (showingBrand) drawBrandCard();
 toast(t('brand_logo_ok'));
 };
 img.src = reader.result;
 };
 reader.readAsDataURL(file);
 fileInput.value = '';
}

async function fetchFavicon(urlParam) {
 const url = (urlParam || _brandFaviconUrl || '').trim();
 if (!url) return;
 toast(t('loading_dots'));
 try {
 const res = await postJSON('/api/brand/favicon',{url});
 const data = await res.json();
 if (data.ok) {
 brandLogoPixels = data.pixels;
 brandData.logo_pixels = data.pixels;
 if (showingBrand) drawBrandCard();
 toast(t('brand_logo_ok'));
 } else {
 toast(t('brand_logo_err'));
 }
 } catch(e) {
 toast(t('brand_logo_err'));
 }
}

// ===== BRAND CARD DRAWING =====
let scrollOffset = 0;
let scrollRAF = null;
let scrollLastTime = 0;

function drawBrandDisabled() {
 const bg = '#000';
 ctx.fillStyle = bg;
 ctx.fillRect(0, 0, canvas.width, canvas.height);
 const dim = '#333';
 const w = measureText('OFF', 'large');
 drawText('OFF', Math.floor((LED_W - w) / 2), Math.floor((LED_H - 7) / 2), dim, 'large');
 flushPixels();
}

function drawBrandCard() {
 // Clear any leftover pixels from a previous interrupted draw
 for (const k in _pixelBatch) delete _pixelBatch[k];
 const bg = '#000';
 ctx.fillStyle = bg;
 ctx.fillRect(0, 0, canvas.width, canvas.height);

 const L = brandLayout;
 const nameColor = L.nameColor;
 const msgColor = L.msgColor;

 // Logo
 if (brandLogoPixels) {
 const srcSize = brandLogoPixels.length;
 const tgtSize = L.logoSize;
 const scale = tgtSize / srcSize;
 for (let y = 0; y < tgtSize; y++) {
 for (let x = 0; x < tgtSize; x++) {
 const sy = Math.floor(y / scale);
 const sx = Math.floor(x / scale);
 if (sy < srcSize && sx < srcSize) {
 const [r, g, b] = brandLogoPixels[sy][sx];
 if (r + g + b > 30) {
 drawPixel(L.logoX + x, L.logoY + y, `rgb(${r},${g},${b})`);
 }
 }
 }
 }
 } else if (layoutEditMode) {
 // Placeholder logo area in edit mode
 const dim = '#333';
 for (let y = 0; y < L.logoSize; y++) {
 drawPixel(L.logoX, L.logoY + y, dim);
 drawPixel(L.logoX + L.logoSize - 1, L.logoY + y, dim);
 }
 for (let x = 0; x < L.logoSize; x++) {
 drawPixel(L.logoX + x, L.logoY, dim);
 drawPixel(L.logoX + x, L.logoY + L.logoSize - 1, dim);
 }
 }

 // Company name
 const name = (brandData.name || '').toUpperCase();
 const nh = L.nameH;
 if (name) {
 drawText(name, L.nameX, L.nameY, nameColor, L.nameFont || 'small', nh);
 } else if (layoutEditMode) {
 drawText('TITLE', L.nameX, L.nameY, '#333', L.nameFont || 'small', nh);
 }

 // Message (full width, scrolling if needed)
 const msg = (brandData.message || '').toUpperCase();
 const mFont = L.msgFont || 'small';
 const mh = L.msgH;
 if (msg) {
 const msgW = measureText(msg, mFont, mh);
 if (layoutEditMode) {
 const mx = L.msgX || 0;
 const cx = mx > 0 ? mx : Math.floor((LED_W - msgW) / 2);
 drawText(msg, cx, L.msgY, msgColor, mFont, mh);
 } else {
 const totalCycle = msgW + LED_W;
 const drawX = LED_W - (scrollOffset % totalCycle);
 drawText(msg, drawX, L.msgY, msgColor, mFont, mh);
 drawText(msg, drawX + totalCycle, L.msgY, msgColor, mFont, mh);
 }
 } else if (layoutEditMode) {
 drawText('MESSAGE', L.msgX || 0, L.msgY, '#333', mFont, mh);
 }
 flushPixels();
 if (layoutEditMode) { brandEditor.drawOverlay(); displayCtx.drawImage(offCanvas, 0, 0); }
}



function startMessageScroll() {
 if (layoutEditMode) return;
 stopMessageScroll();
 scrollOffset = 0;
 const msg = (brandData.message || '').toUpperCase();
 const msgW = measureText(msg, brandLayout.msgFont || 'small', brandLayout.msgH);
 if (msgW > 0) {
 scrollLastTime = 0;
 const speed = brandLayout.msgSpeed || 60;
 function scrollTick(ts) {
 if (!scrollRAF) return;
 if (!showingBrand) { stopMessageScroll(); return; }
 if (!scrollLastTime) scrollLastTime = ts;
 const elapsed = ts - scrollLastTime;
 if (elapsed >= speed) {
 scrollLastTime = ts - (elapsed % speed);
 scrollOffset++;
 drawBrandCard();
 }
 if (scrollRAF) scrollRAF = requestAnimationFrame(scrollTick);
 }
 scrollRAF = requestAnimationFrame(scrollTick);
 }
}

function stopMessageScroll() {
 if (scrollRAF) { cancelAnimationFrame(scrollRAF); scrollRAF = null; }
 scrollOffset = 0;
}

// ===== GENERIC LAYOUT EDITOR =====
let layoutEditMode = false;
const DEFAULT_LAYOUT = { logoX:1, logoY:1, logoSize:18, nameX:24, nameY:7, nameH:7, nameScale:1, msgX:0, msgY:21, msgH:8, msgScale:1, nameColor:'#ffffff', msgColor:'rainbow', msgSpeed:60, nameFont:'small', msgFont:'small' };

function createLayoutEditor(opts) {
 const st = { drag:null, resize:null, hover:null, dragOX:0, dragOY:0,
 resizeStartX:0, resizeStartY:0, resizeOrigW:0, resizeOrigH:0, resizeTop:false, resizeLeft:false,
 resizeOrigX:0, resizeOrigY:0,
 clickStart:null, tapMoved:false, longPressTimer:null, longPressFired:false };

 function drawOverlay(data) {
 const bounds = opts.getBounds(data);
 bounds.forEach(b => {
 const sx = b.x*SCALE, sy = b.y*SCALE, sw = b.w*SCALE, sh = b.h*SCALE, c = b.color;
 ctx.strokeStyle = c; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
 ctx.strokeRect(sx-1, sy-1, sw+2, sh+2); ctx.setLineDash([]);
 if (st.drag === b.id || st.resize === b.id || st.hover === b.id) {
 ctx.fillStyle = c; ctx.globalAlpha = 0.12; ctx.fillRect(sx, sy, sw, sh); ctx.globalAlpha = 1;
 }
 ctx.font = '9px monospace'; ctx.fillStyle = c; ctx.fillText(b.overlayLabel, sx, sy-3);
 const hs = b.resizable ? 5 : 4; ctx.fillStyle = c;
 [[sx-1,sy-1],[sx+sw-hs+1,sy-1],[sx-1,sy+sh-hs+1],[sx+sw-hs+1,sy+sh-hs+1]].forEach(([hx,hy]) => ctx.fillRect(hx,hy,hs,hs));
 });
 }

 function doHitTest(lx, ly, data) {
 const bounds = opts.getBounds(data);
 for (let i = bounds.length-1; i >= 0; i--) {
 const b = bounds[i];
 if (b.resizable) {
 for (const [cx,cy,top,left] of [[b.x,b.y,true,true],[b.x+b.w,b.y,true,false],[b.x,b.y+b.h,false,true],[b.x+b.w,b.y+b.h,false,false]]) {
 if (Math.abs(lx-cx) <= 2 && Math.abs(ly-cy) <= 2) return {...b, action:'resize', topCorner:top, leftCorner:left};
 }
 }
 }
 for (let i = bounds.length-1; i >= 0; i--) {
 const b = bounds[i];
 if (lx >= b.x-1 && lx <= b.x+b.w && ly >= b.y-1 && ly <= b.y+b.h) return {...b, action:'move'};
 }
 return null;
 }

 function onDown(e) {
 if (_editPopup) return;
 e.preventDefault();
 const {lx,ly} = canvasToLED(e), data = opts.getData(), hit = doHitTest(lx, ly, data);
 if (!hit) return;
 st.clickStart = {lx, ly, id:hit.id, clientX:e.clientX, clientY:e.clientY};
 if (hit.action === 'resize') {
 st.resize = hit.id; st.resizeStartX = lx; st.resizeStartY = ly;
 st.resizeOrigW = hit.w; st.resizeOrigH = hit.h; st.resizeTop = !!hit.topCorner; st.resizeLeft = !!hit.leftCorner;
 st.resizeOrigX = hit.x; st.resizeOrigY = hit.y;
 canvas.classList.add('dragging');
 } else {
 st.drag = hit.id; st.dragOX = lx - hit.x; st.dragOY = ly - hit.y;
 canvas.classList.add('dragging');
 }
 }

 function onMove(e) {
 const {lx,ly} = canvasToLED(e), data = opts.getData(), layout = opts.getLayout();
 if (st.resize) {
 const b = opts.getBounds(data).find(b => b.id === st.resize);
 if (!b) return;
 if (b.sizeKey) {
 // Uniform square resize (logo)
 const dy = st.resizeTop ? (st.resizeStartY-ly) : (ly-st.resizeStartY);
 const dx = st.resizeLeft ? (st.resizeStartX-lx) : (lx-st.resizeStartX);
 const delta = Math.max(dy, dx);
 const newSize = Math.max(4, st.resizeOrigW + delta);
 layout[b.sizeKey] = newSize;
 // Move origin when dragging from top or left corners
 if (st.resizeTop && b.fy) layout[b.fy] = Math.max(0, st.resizeOrigY - (newSize - st.resizeOrigW));
 if (st.resizeLeft && b.fx) layout[b.fx] = Math.max(0, st.resizeOrigX - (newSize - st.resizeOrigW));
 // Clamp size to available space
 const maxW = b.fx ? LED_W - layout[b.fx] : LED_W - st.resizeOrigX;
 const maxH = b.fy ? LED_H - layout[b.fy] : LED_H - st.resizeOrigY;
 layout[b.sizeKey] = Math.min(layout[b.sizeKey], maxW, maxH);
 if (opts.syncInput) {
 opts.syncInput(b.sizeKey, layout[b.sizeKey]);
 if (b.fx) opts.syncInput(b.fx, layout[b.fx]);
 if (b.fy) opts.syncInput(b.fy, layout[b.fy]);
 }
 } else if (b.hKey) {
 const delta = st.resizeTop ? (st.resizeStartY-ly) : (ly-st.resizeStartY);
 const baseH = b.resizeMin || (b.font === 'large' ? 7 : 5);
 const newH = Math.max(baseH, st.resizeOrigH + delta);
 // Move Y origin when dragging from top corners
 if (st.resizeTop && b.fy) {
 layout[b.fy] = Math.max(0, st.resizeOrigY - (newH - st.resizeOrigH));
 layout[b.hKey] = Math.min(newH, LED_H - layout[b.fy]);
 } else {
 layout[b.hKey] = Math.min(newH, LED_H - (b.fy ? layout[b.fy] : st.resizeOrigY));
 }
 const finalH = layout[b.hKey];
 if (b.font && finalH === baseH) layout[b.hKey] = null;
 // Anchor right edge when resizing from left corners
 if (st.resizeLeft && b.fx) {
 const nb = opts.getBounds(data).find(nb => nb.id === b.id);
 if (nb) layout[b.fx] = Math.max(0, st.resizeOrigX + st.resizeOrigW - nb.w);
 }
 }
 opts.redraw(data);
 } else if (st.drag) {
 const b = opts.getBounds(data).find(b => b.id === st.drag);
 if (!b) return;
 if (b.fx) { layout[b.fx] = Math.max(0, Math.min(LED_W-b.w, Math.round(lx-st.dragOX))); if (opts.syncInput) opts.syncInput(b.fx, layout[b.fx]); }
 if (b.fy) { layout[b.fy] = Math.max(0, Math.min(LED_H-b.h, Math.round(ly-st.dragOY))); if (opts.syncInput) opts.syncInput(b.fy, layout[b.fy]); }
 opts.redraw(data);
 } else {
 const hit = doHitTest(lx, ly, data);
 canvas.style.cursor = hit ? (hit.action === 'resize' ? ((hit.topCorner !== hit.leftCorner) ? 'nesw-resize' : 'nwse-resize') : 'grab') : 'crosshair';
 const nh = hit ? hit.id : null;
 if (nh !== st.hover) { st.hover = nh; opts.redraw(data); }
 }
 }

 function onUp() {
 if (st.drag || st.resize) { canvas.classList.remove('dragging'); opts.onSave(); st.drag = null; st.resize = null; }
 st.clickStart = null;
 }

 function onTouchStart(e) {
 e.preventDefault(); st.tapMoved = false; st.longPressFired = false;
 const tx = e.touches[0].clientX, ty = e.touches[0].clientY;
 clearTimeout(st.longPressTimer);
 st.longPressTimer = setTimeout(() => { if (!st.tapMoved) { st.longPressFired = true; opts.onDblClick({clientX:tx, clientY:ty}); } }, 500);
 onDown({clientX:tx, clientY:ty, preventDefault(){}});
 }
 function onTouchMove(e) { e.preventDefault(); st.tapMoved = true; clearTimeout(st.longPressTimer); if (st.drag || st.resize) onMove({clientX:e.touches[0].clientX, clientY:e.touches[0].clientY}); }
 function onTouchEnd(e) {
 clearTimeout(st.longPressTimer);
 if (!st.longPressFired) onUp();
 }

 function enter() {
 if (opts.onEnter && opts.onEnter() === false) return;
 opts.setActive(true); stopRotation();
 opts.redraw(opts.getData());
 canvas.classList.add('edit-mode');
 canvas.addEventListener('mousedown', onDown); canvas.addEventListener('mousemove', onMove);
 canvas.addEventListener('mouseup', onUp); canvas.addEventListener('mouseleave', onUp);
 canvas.addEventListener('dblclick', opts.onDblClick);
 canvas.addEventListener('touchstart', onTouchStart, {passive:false});
 canvas.addEventListener('touchmove', onTouchMove, {passive:false});
 canvas.addEventListener('touchend', onTouchEnd);
 if ('ontouchstart' in window) toast(t('edit_hint_touch'));
 }

 function exit() {
 opts.setActive(false); closeEditPopup();
 canvas.classList.remove('edit-mode', 'dragging');
 canvas.removeEventListener('mousedown', onDown); canvas.removeEventListener('mousemove', onMove);
 canvas.removeEventListener('mouseup', onUp); canvas.removeEventListener('mouseleave', onUp);
 canvas.removeEventListener('dblclick', opts.onDblClick);
 canvas.removeEventListener('touchstart', onTouchStart); canvas.removeEventListener('touchmove', onTouchMove);
 canvas.removeEventListener('touchend', onTouchEnd);
 st.drag = null; st.resize = null; st.hover = null;
 }

 return { st, drawOverlay, hitTest: doHitTest, enter, exit };
}

// ===== BRAND ELEMENT BOUNDS =====
function getElementBounds() {
 const L = brandLayout, bounds = [];
 if (brandLogoPixels || layoutEditMode) {
 bounds.push({ id:'logo', x:L.logoX, y:L.logoY, w:L.logoSize, h:L.logoSize, fx:'logoX', fy:'logoY', resizable:true, sizeKey:'logoSize', color:'#0a84ff', overlayLabel:'LOGO' });
 }
 const name = (brandData.name || '').toUpperCase() || (layoutEditMode ? 'TITLE' : '');
 if (name) {
 const f = L.nameFont || 'small', nh = L.nameH;
 bounds.push({ id:'name', x:L.nameX, y:L.nameY, w:measureText(name,f,nh), h:textHeight(f,nh), fx:'nameX', fy:'nameY', resizable:true, hKey:'nameH', font:f, color:'#ffffff', overlayLabel:'TITLE' });
 }
 const msg = (brandData.message || '').toUpperCase() || (layoutEditMode ? 'MESSAGE' : '');
 if (msg) {
 const mf = L.msgFont || 'small', mh = L.msgH;
 const mw = measureText(msg, mf, mh), mx = L.msgX || 0;
 const dx = (mw <= LED_W && mx === 0) ? Math.floor((LED_W - mw) / 2) : mx;
 bounds.push({ id:'msg', x:dx, y:L.msgY, w:Math.min(mw, LED_W), h:textHeight(mf,mh), fx:'msgX', fy:'msgY', resizable:true, hKey:'msgH', font:mf, color:'#00c853', overlayLabel:'MSG' });
 }
 return bounds;
}

function canvasToLED(e) {
 const rect = canvas.getBoundingClientRect();
 const rx = (e.clientX - rect.left) * (canvas.width / rect.width);
 const ry = (e.clientY - rect.top) * (canvas.height / rect.height);
 return { lx: Math.floor(rx / SCALE), ly: Math.floor(ry / SCALE) };
}

// ---- Inline edit popup ----
let _editPopup = null;
const COLOR_PALETTE = [
 '#ffffff','#cccccc','#888888','#444444',
 '#ff0000','#ff4444','#ff8800','#ffaa00',
 '#ffff00','#88ff00','#00ff00','#00cc44',
 '#00ffff','#0088ff','#0044ff','#4400ff',
 '#8800ff','#cc00ff','#ff00ff','#ff0088',
 '#00c853','#00dc00','#ff2d55','#ff2828',
];
let _editPopupReturnFocus = null;
function showEditPopup(screenX, screenY, currentVal, onConfirm, opts) {
 closeEditPopup();
 opts = opts || {};
 _editPopupReturnFocus = document.activeElement;
 const popup = document.createElement('div');
 popup.className = 'led-edit-popup';
 popup.setAttribute('role', 'dialog');
 popup.setAttribute('aria-modal', 'true');
 popup.setAttribute('aria-label', opts.colorOnly ? 'Color picker' : 'Edit element');

 // Text input row (skip if colorOnly mode)
 let inp = null;
 if (!opts.colorOnly) {
 const row = document.createElement('div');
 row.className = 'edit-row';
 inp = document.createElement('input');
 inp.type = 'text'; inp.value = currentVal;
 row.appendChild(inp);
 const okBtn = document.createElement('button');
 okBtn.className = 'btn-ok';
 okBtn.textContent = t('save');
 row.appendChild(okBtn);
 popup.appendChild(row);
 }

 if (opts.color) {
 let currentColor = opts.color;
 const colorInp = document.createElement('input');
 colorInp.type = 'color'; colorInp.value = opts.color;
 colorInp.style.display = 'none';
 popup.appendChild(colorInp);

 // Color grid
 const grid = document.createElement('div');
 grid.className = 'color-grid';
 COLOR_PALETTE.forEach(c => {
 const sw = document.createElement('div');
 sw.className = 'color-swatch' + (c.toLowerCase() === currentColor.toLowerCase() ? ' active' : '');
 sw.style.background = c;
 sw.addEventListener('click', e => {
 e.stopPropagation();
 currentColor = c;
 grid.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
 sw.classList.add('active');
 customDot.style.background = c;
 if (opts.onColor) opts.onColor(c);
 });
 grid.appendChild(sw);
 });

 // Rainbow swatch
 const rainbowSw = document.createElement('div');
 rainbowSw.className = 'color-swatch rainbow';
 rainbowSw.title = 'Rainbow';
 rainbowSw.addEventListener('click', e => {
 e.stopPropagation();
 if (opts.onColor) opts.onColor('rainbow');
 grid.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
 rainbowSw.classList.add('active');
 });
 grid.appendChild(rainbowSw);

 // Custom color picker
 const customWrap = document.createElement('div');
 customWrap.className = 'color-custom';
 const customDot = document.createElement('div');
 customDot.className = 'color-custom-dot';
 customDot.style.background = currentColor;
 customDot.addEventListener('click', e => { e.stopPropagation(); colorInp.click(); });
 customWrap.appendChild(customDot);
 const customLabel = document.createElement('span');
 customLabel.textContent = 'Custom';
 customWrap.appendChild(customLabel);
 customWrap.addEventListener('click', e => { e.stopPropagation(); colorInp.click(); });
 colorInp.addEventListener('input', () => {
 currentColor = colorInp.value;
 customDot.style.background = currentColor;
 grid.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
 if (opts.onColor) opts.onColor(currentColor);
 });

 grid.appendChild(customWrap);
 popup.appendChild(grid);
 }

 document.body.appendChild(popup);
 _editPopup = popup;
 // Position near click on desktop, centered on mobile
 const isMobile = window.innerWidth < 700;
 if (isMobile) {
 popup.style.left = '50%'; popup.style.top = '50%'; popup.style.transform = 'translate(-50%,-50%)';
 } else {
 // Place near click, then clamp to viewport
 const pw = popup.offsetWidth, ph = popup.offsetHeight;
 let px = screenX - pw / 2, py = screenY + 12;
 px = Math.max(8, Math.min(window.innerWidth - pw - 8, px));
 if (py + ph > window.innerHeight - 8) py = screenY - ph - 12;
 py = Math.max(8, py);
 popup.style.left = px + 'px'; popup.style.top = py + 'px';
 }
 if (inp) {
 inp.focus(); inp.select();
 const confirm = () => { const v = inp.value; closeEditPopup(); onConfirm(v); };
 popup.querySelector('.btn-ok').addEventListener('click', e => { e.stopPropagation(); confirm(); });
 inp.addEventListener('keydown', e => { if (e.key === 'Enter') confirm(); if (e.key === 'Escape') closeEditPopup(); });
 }
 popup.addEventListener('mousedown', e => e.stopPropagation());
 popup.addEventListener('touchstart', e => e.stopPropagation());
 popup.addEventListener('keydown', e => {
 if (e.key === 'Escape') closeEditPopup();
 if (e.key === 'Tab') {
  const focusable = popup.querySelectorAll('input, button, [tabindex]:not([tabindex="-1"]), .color-swatch, .color-custom-dot');
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
 }
 });
 setTimeout(() => { document.addEventListener('mousedown', _onDocClickClose); document.addEventListener('touchstart', _onDocClickClose); }, 0);
}
function closeEditPopup() {
 if (_editPopup) { _editPopup.remove(); _editPopup = null; }
 if (_editPopupReturnFocus) { try { _editPopupReturnFocus.focus(); } catch(e) {} _editPopupReturnFocus = null; }
 document.removeEventListener('mousedown', _onDocClickClose);
 document.removeEventListener('touchstart', _onDocClickClose);
}
function _onDocClickClose() { closeEditPopup(); }

function showLogoPopup(screenX, screenY) {
 closeEditPopup();
 const popup = document.createElement('div');
 popup.className = 'led-edit-popup';
 popup.setAttribute('role', 'dialog');
 popup.setAttribute('aria-modal', 'true');
 popup.setAttribute('aria-label', 'Logo picker');
 popup.style.cssText += 'flex-direction:column;gap:8px;align-items:stretch;min-width:200px;';
 // Row 1: domain input + fetch button
 const row1 = document.createElement('div');
 row1.className = 'edit-row';
 const inp = document.createElement('input');
 inp.type = 'text'; inp.value = _brandFaviconUrl || '';
 inp.placeholder = 'domain.com';
 const fetchBtn = document.createElement('button');
 fetchBtn.className = 'btn-ok';
 fetchBtn.textContent = t('brand_fetch');
 row1.appendChild(inp); row1.appendChild(fetchBtn);
 // Divider — o —
 const divider = document.createElement('div');
 divider.style.cssText = 'display:flex;align-items:center;gap:8px;color:var(--dim);font-size:var(--text-xs,11px);';
 const line1 = document.createElement('span');
 line1.style.cssText = 'flex:1;height:1px;background:var(--border);';
 const orText = document.createElement('span');
 orText.textContent = t('brand_or');
 const line2 = document.createElement('span');
 line2.style.cssText = 'flex:1;height:1px;background:var(--border);';
 divider.appendChild(line1); divider.appendChild(orText); divider.appendChild(line2);
 // Row 2: upload + delete side by side
 const row2 = document.createElement('div');
 row2.style.cssText = 'display:flex;gap:6px;align-items:center;';
 const uploadLabel = document.createElement('label');
 uploadLabel.className = 'btn-outline';
 uploadLabel.style.cssText += 'flex:1;text-align:center;cursor:pointer;display:flex;align-items:center;justify-content:center;';
 uploadLabel.textContent = t('brand_upload');
 const fileInp = document.createElement('input');
 fileInp.type = 'file'; fileInp.accept = 'image/*'; fileInp.style.display = 'none';
 uploadLabel.appendChild(fileInp);
 const delBtn = document.createElement('button');
 delBtn.className = 'btn-outline';
 delBtn.style.cssText += 'flex:1;color:var(--red,#ff2d55);border-color:var(--red,#ff2d55);';
 delBtn.textContent = t('brand_delete_logo');
 row2.appendChild(uploadLabel); row2.appendChild(delBtn);
 popup.appendChild(row1); popup.appendChild(divider); popup.appendChild(row2);
 document.body.appendChild(popup);
 _editPopup = popup;
 const isMobile = window.innerWidth < 700;
 if (isMobile) {
 popup.style.left = '50%'; popup.style.top = '50%'; popup.style.transform = 'translate(-50%,-50%)';
 } else {
 const pw = popup.offsetWidth, ph = popup.offsetHeight;
 let px = screenX - pw / 2, py = screenY + 12;
 px = Math.max(8, Math.min(window.innerWidth - pw - 8, px));
 if (py + ph > window.innerHeight - 8) py = screenY - ph - 12;
 py = Math.max(8, py);
 popup.style.left = px + 'px'; popup.style.top = py + 'px';
 }
 inp.focus();
 fetchBtn.addEventListener('click', e => { e.stopPropagation(); const url = inp.value.trim(); if (url) { _brandFaviconUrl = url; closeEditPopup(); fetchFavicon(url); } });
 inp.addEventListener('keydown', e => { if (e.key === 'Enter') { const url = inp.value.trim(); if (url) { _brandFaviconUrl = url; closeEditPopup(); fetchFavicon(url); } } if (e.key === 'Escape') closeEditPopup(); });
 fileInp.addEventListener('change', () => { closeEditPopup(); uploadLogo(fileInp); });
 delBtn.addEventListener('click', e => { e.stopPropagation(); closeEditPopup(); deleteLogo(); });
 popup.addEventListener('mousedown', e => e.stopPropagation());
 popup.addEventListener('touchstart', e => e.stopPropagation());
 popup.addEventListener('keydown', e => {
 if (e.key === 'Escape') closeEditPopup();
 if (e.key === 'Tab') {
  const focusable = popup.querySelectorAll('input:not([type="file"]), button, label.btn-outline');
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
 }
 });
 setTimeout(() => { document.addEventListener('mousedown', _onDocClickClose); document.addEventListener('touchstart', _onDocClickClose); }, 0);
}

// ===== BRAND EDITOR =====
function onBrandDblClick(e) {
 const {lx, ly} = canvasToLED(e);
 const hit = brandEditor.hitTest(lx, ly);
 if (!hit) return;
 if (hit.id === 'name') {
 showEditPopup(e.clientX, e.clientY, brandData.name || '', val => {
 brandData.name = val; saveBrand(); drawBrandCard();
 }, {
 color: brandLayout.nameColor || '#ffffff',
 onColor: c => { brandLayout.nameColor = c; if(BL.nameColor) BL.nameColor.value = c; saveBrandLayout(); drawBrandCard(); }
 });
 } else if (hit.id === 'msg') {
 showEditPopup(e.clientX, e.clientY, brandData.message || '', val => {
 brandData.message = val; saveBrand(); drawBrandCard();
 }, {
 color: brandLayout.msgColor || '#00c853',
 onColor: c => { brandLayout.msgColor = c; if(BL.msgColor) BL.msgColor.value = c; saveBrandLayout(); drawBrandCard(); }
 });
 } else if (hit.id === 'logo') {
 showLogoPopup(e.clientX, e.clientY);
 }
}

const brandEditor = createLayoutEditor({
 getBounds: () => getElementBounds(),
 getLayout: () => brandLayout,
 redraw: () => drawBrandCard(),
 onSave: () => saveBrandLayout(),
 onDblClick: onBrandDblClick,
 syncInput: (key, val) => { if (BL[key]) BL[key].value = String(val); },
 getData: () => null,
 setActive: v => { layoutEditMode = v; },
 onEnter: () => { showingBrand = true; stopMessageScroll(); scrollOffset = 0; drawBrandCard(); },
});

// ===== DATA ELEMENT BOUNDS =====
function getDataElementBounds(data) {
 if (!data) return [];
 const DL = dataLayout, bounds = [];
 const lf = DL.labelFont || 'small', lh = DL.labelH;
 const labelStr = data.label + ' ' + (data.mode === 'daily' ? 'D' : 'W');
 bounds.push({ id:'label', x:DL.labelX, y:DL.labelY, w:measureText(labelStr, lf, lh), h:textHeight(lf, lh), fx:'labelX', fy:'labelY', resizable:true, hKey:'labelH', font:lf, color:'#ffffff', overlayLabel:'LABEL' });
 const cf = DL.changeFont || 'small', ch = DL.changeH;
 const changeStr = (data.change_pct >= 0 ? '+' : '') + data.change_pct.toFixed(1) + '%';
 const cw = measureText(changeStr, cf, ch);
 const changeX = (DL.changeX != null) ? DL.changeX : (LED_W - cw - 1);
 bounds.push({ id:'change', x:changeX, y:DL.changeY, w:cw, h:textHeight(cf, ch), fx:'changeX', fy:'changeY', resizable:true, hKey:'changeH', font:cf, color:'#ffaa00', overlayLabel:'%' });
 const vf = DL.valueFont || 'large', valH = DL.valueH;
 let valueStr;
 if (data.current_value >= 100) valueStr = data.current_value.toFixed(1);
 else valueStr = data.current_value.toFixed(2);
 bounds.push({ id:'value', x:DL.valueX, y:DL.valueY, w:measureText(valueStr, vf, valH), h:textHeight(vf, valH), fx:'valueX', fy:'valueY', resizable:true, hKey:'valueH', font:vf, color:'#0a84ff', overlayLabel:'VALUE' });
 const ctf = DL.countryFont || 'small', ctH = DL.countryH;
 bounds.push({ id:'country', x:DL.countryX, y:DL.countryY, w:measureText(data.country.toUpperCase(), ctf, ctH), h:textHeight(ctf, ctH), fx:'countryX', fy:'countryY', resizable:true, hKey:'countryH', font:ctf, color:'#888888', overlayLabel:'CC' });
 bounds.push({ id:'spark', x:1, y:DL.sparkY, w:LED_W-2, h:DL.sparkH, fx:null, fy:'sparkY', resizable:true, hKey:'sparkH', resizeMin:4, color:'#00c853', overlayLabel:'CHART' });
 return bounds;
}

async function saveDataLayout() { await postJSON('/api/config/data_layout', dataLayout); }

let _dataColorInput = null;
function getDataColorInput() {
 if (!_dataColorInput) { _dataColorInput = document.createElement('input'); _dataColorInput.type = 'color'; _dataColorInput.style.display = 'none'; document.body.appendChild(_dataColorInput); }
 return _dataColorInput;
}
function openDataColorPicker(layoutKey, fallback) {
 const inp = getDataColorInput();
 inp.value = dataLayout[layoutKey] || fallback;
 inp.oninput = () => { dataLayout[layoutKey] = inp.value; drawLED(previewData[currentIndex]); };
 inp.onchange = () => { saveDataLayout(); };
 setTimeout(() => inp.click(), 50);
}
function getColorKeyForElement(id, data) {
 if (id === 'label') return ['labelColor', '#ffffff'];
 if (id === 'value') return ['valueColor', '#ffffff'];
 if (id === 'country') return ['countryColor', '#999999'];
 if (id === 'change') { const isUp = data && data.is_up; return [isUp ? 'changeUpColor' : 'changeDownColor', isUp ? '#00dc00' : '#ff2828']; }
 if (id === 'spark') { const isUp = data && data.is_up; return [isUp ? 'sparkUpColor' : 'sparkDownColor', isUp ? '#00c853' : '#ff2d55']; }
 return null;
}

// ===== DATA EDITOR =====
function onDataDblClick(e) {
 const {lx, ly} = canvasToLED(e);
 const data = previewData[currentIndex];
 if (!data) return;
 const hit = dataEditor.hitTest(lx, ly, data);
 if (!hit) return;
 if (hit.id === 'label') {
 const pair = getColorKeyForElement('label', data);
 showEditPopup(e.clientX, e.clientY, data.label || '', val => {
 if (val.trim()) {
 const newLabel = val.trim().substring(0, 8).toUpperCase();
 data.label = newLabel;
 fetch('/api/domains/' + data.configIndex, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:'label',value:newLabel})});
 drawLED(data);
 }
 }, {
 color: dataLayout[pair[0]] || pair[1],
 onColor: c => { dataLayout[pair[0]] = c; saveDataLayout(); drawLED(previewData[currentIndex]); }
 });
 } else {
 const pair = getColorKeyForElement(hit.id, data);
 if (pair) {
  showEditPopup(e.clientX, e.clientY, '', null, {
   colorOnly: true,
   color: dataLayout[pair[0]] || pair[1],
   onColor: c => { dataLayout[pair[0]] = c; saveDataLayout(); drawLED(previewData[currentIndex]); }
  });
 }
 }
}

const dataEditor = createLayoutEditor({
 getBounds: data => getDataElementBounds(data),
 getLayout: () => dataLayout,
 redraw: data => drawLED(data),
 onSave: () => saveDataLayout(),
 onDblClick: onDataDblClick,
 getData: () => previewData[currentIndex],
 setActive: v => { dataLayoutEditMode = v; },
 onEnter: () => {
 if (!previewData.length) return false;
 showingBrand = false; renderCurrent();
 },
});

// ===== EDIT MODE TOGGLE =====
function toggleEdit() {
 const btn = document.getElementById('btnEdit');
 const resetBtn = document.getElementById('btnReset');
 const btnPrev = document.getElementById('btnPrev');
 const btnNext = document.getElementById('btnNext');
 const btnPlay = DOM.btnPlayPause;
 const isEditing = dataLayoutEditMode || layoutEditMode;

 if (isEditing) {
 closeEditPopup();
 if (layoutEditMode) brandEditor.exit();
 if (dataLayoutEditMode) dataEditor.exit();
 btn.className = 'btn-outline'; btn.textContent = t('edit');
 resetBtn.style.display = 'none';
 btnPrev.disabled = false; btnNext.disabled = false; btnPlay.disabled = false;
 renderSlide();
 if (autoRotate) startRotation();
 } else {
 if (isBrandSlide() && !brandData.enabled) {
 toggleBrandEnabled().then(() => {
 brandEditor.enter();
 btn.className = 'btn btn-small'; btn.textContent = t('done_editing');
 resetBtn.style.display = '';
 btnPrev.disabled = true; btnNext.disabled = true; btnPlay.disabled = true;
 });
 return;
 }
 if (isBrandSlide()) {
 brandEditor.enter();
 } else {
 dataEditor.enter();
 }
 btn.className = 'btn btn-small'; btn.textContent = t('done_editing');
 resetBtn.style.display = '';
 btnPrev.disabled = true; btnNext.disabled = true; btnPlay.disabled = true;
 }
}

async function resetCurrentLayout() {
 if (layoutEditMode) {
 Object.assign(brandLayout, DEFAULT_LAYOUT);
 BL_IDS.forEach(id => { const v = brandLayout[id]; BL[id].value = typeof v === 'number' ? String(v) : v; });
 await saveBrandLayout();
 drawBrandCard();
 } else if (dataLayoutEditMode) {
 Object.assign(dataLayout, DEFAULT_DATA_LAYOUT);
 await saveDataLayout();
 renderCurrent();
 }
 toast(t('layout_reset'));
}


// ===== CUSTOM DROPDOWN =====
function initCustomSelect(container, options, defaultVal) {
 container.innerHTML = '';
 container._value = defaultVal || (options[0]?.value ?? '');
 const trigger = document.createElement('div');
 trigger.className = 'custom-select-trigger';
 trigger.setAttribute('role', 'combobox');
 trigger.setAttribute('aria-haspopup', 'listbox');
 trigger.setAttribute('aria-expanded', 'false');
 trigger.setAttribute('tabindex', '0');
 const dropdown = document.createElement('div');
 dropdown.className = 'custom-select-dropdown';
 dropdown.setAttribute('role', 'listbox');
 const hasSearch = options.length > 6;
 const searchBox = document.createElement('div');
 searchBox.className = 'custom-select-search';
 const searchInput = document.createElement('input');
 searchInput.type = 'text';
 searchInput.placeholder = '...';
 searchInput.setAttribute('aria-label', 'Filter options');
 searchBox.appendChild(searchInput);
 if (hasSearch) dropdown.appendChild(searchBox);
 const optList = document.createElement('div');
 dropdown.appendChild(optList);
 container.appendChild(trigger);
 container.appendChild(dropdown);
 let _activeIdx = -1;

 function renderOptions(filter) {
 const f = (filter || '').toLowerCase();
 optList.innerHTML = '';
 _activeIdx = -1;
 let idx = 0;
 options.forEach(o => {
 if (f && !(o.search || o.text).toLowerCase().includes(f)) return;
 const div = document.createElement('div');
 const isSel = o.value === container._value;
 div.className = 'custom-select-option' + (isSel ? ' selected' : '');
 div.textContent = o.text;
 div.setAttribute('role', 'option');
 div.setAttribute('aria-selected', isSel ? 'true' : 'false');
 div.dataset.idx = idx;
 if (isSel) _activeIdx = idx;
 div.addEventListener('click', e => {
 e.stopPropagation();
 selectOption(o);
 });
 optList.appendChild(div);
 idx++;
 });
 }

 function selectOption(o) {
 container._value = o.value;
 trigger.textContent = o.text;
 close();
 if (container.onchange) container.onchange();
 }

 function highlightIdx(newIdx) {
 const items = optList.querySelectorAll('[role="option"]');
 if (!items.length) return;
 if (newIdx < 0) newIdx = items.length - 1;
 if (newIdx >= items.length) newIdx = 0;
 items.forEach(el => el.classList.remove('highlighted'));
 _activeIdx = newIdx;
 items[_activeIdx].classList.add('highlighted');
 items[_activeIdx].scrollIntoView({block:'nearest'});
 trigger.setAttribute('aria-activedescendant', '');
 }

 function positionDropdown() {
 const rect = container.getBoundingClientRect();
 const spaceBelow = window.innerHeight - rect.bottom;
 dropdown.classList.remove('above', 'below');
 dropdown.classList.add(spaceBelow < 220 ? 'above' : 'below');
 }

 function open() {
 positionDropdown();
 trigger.classList.add('open');
 dropdown.classList.add('open');
 trigger.setAttribute('aria-expanded', 'true');
 searchInput.value = '';
 renderOptions('');
 if (hasSearch) setTimeout(() => searchInput.focus(), 10);
 }
 function close() {
 trigger.classList.remove('open');
 dropdown.classList.remove('open');
 trigger.setAttribute('aria-expanded', 'false');
 trigger.focus();
 }

 function handleKeyNav(e) {
 const isOpen = dropdown.classList.contains('open');
 if (!isOpen && (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
  e.preventDefault(); open(); return;
 }
 if (!isOpen) return;
 if (e.key === 'ArrowDown') { e.preventDefault(); highlightIdx(_activeIdx + 1); }
 else if (e.key === 'ArrowUp') { e.preventDefault(); highlightIdx(_activeIdx - 1); }
 else if (e.key === 'Enter') {
  e.preventDefault();
  const items = optList.querySelectorAll('[role="option"]');
  if (_activeIdx >= 0 && items[_activeIdx]) {
   const val = options.find(o => o.text === items[_activeIdx].textContent);
   if (val) selectOption(val);
  }
 }
 else if (e.key === 'Escape') { e.preventDefault(); close(); }
 else if (e.key === 'Home') { e.preventDefault(); highlightIdx(0); }
 else if (e.key === 'End') { e.preventDefault(); highlightIdx(optList.querySelectorAll('[role="option"]').length - 1); }
 }

 trigger.addEventListener('click', e => {
 e.stopPropagation();
 dropdown.classList.contains('open') ? close() : open();
 });
 trigger.addEventListener('keydown', handleKeyNav);
 searchInput.addEventListener('input', () => renderOptions(searchInput.value));
 searchInput.addEventListener('click', e => e.stopPropagation());
 searchInput.addEventListener('keydown', e => {
 if (e.key === 'Escape') close();
 else if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === 'Home' || e.key === 'End') handleKeyNav(e);
 });

 // Set initial display
 const sel = options.find(o => o.value === container._value);
 trigger.textContent = sel ? sel.text : '';

 // Label association: find <label> with for pointing to container id
 if (container.id) {
 const label = document.querySelector('label[for="' + container.id + '"]');
 if (label) { label.removeAttribute('for'); label.addEventListener('click', () => trigger.focus()); trigger.setAttribute('aria-label', label.textContent.trim()); }
 }

 // Value getter
 Object.defineProperty(container, 'value', {
 get() { return container._value; },
 set(v) { container._value = v; const o = options.find(x => x.value === v); if (o) trigger.textContent = o.text; },
 configurable: true
 });
 return container;
}

// Close any open custom dropdown on outside click
document.addEventListener('mousedown', e => {
 if (e.target.closest('.custom-select')) return;
 document.querySelectorAll('.custom-select-dropdown.open').forEach(d => d.classList.remove('open'));
 document.querySelectorAll('.custom-select-trigger.open').forEach(t => { t.classList.remove('open'); t.setAttribute('aria-expanded', 'false'); });
});

function countryOptions(countries) {
 return countries.map(c => ({value: c.code, text: c.code.toUpperCase(), search: c.code + ' ' + c.name}));
}

function setCountries(countries) {
 window._countries = countries;
 initCustomSelect(DOM.newCountry, countryOptions(countries), 'es');
}
async function loadCountries() {
 const res = await fetch('/api/countries');
 setCountries(await res.json());
}

// Init type and mode custom selects
initCustomSelect(DOM.newType, [
 {value:'domain',text:'Domain'},{value:'host',text:'Host'},{value:'path',text:'Path'},{value:'url',text:'URL'}
], 'domain');
DOM.newType.onchange = () => {
 const placeholders = {domain:'example.com', host:'www.example.com', path:'example.com/blog/', url:'example.com/blog/post-1'};
 DOM.newDomain.placeholder = placeholders[DOM.newType.value] || 'example.com';
};
initCustomSelect(DOM.newMode, [
 {value:'weekly',text:t('mode_weekly')},{value:'daily',text:t('mode_daily')}
], 'weekly');

// Init
initCustomSelect(DOM.langSelect, [
 {value:'es',text:'ES'},{value:'en',text:'EN'},{value:'fr',text:'FR'},
 {value:'it',text:'IT'},{value:'de',text:'DE'},{value:'pt',text:'PT'}
], 'en');
DOM.langSelect.onchange = () => setLang(DOM.langSelect.value);
initBrandSelects();
loadCountries();
function syncArrowHeight() {
 const outer = DOM.ledOuter;
 const statusRow = document.querySelector('.led-status-row');
 const h = outer.offsetHeight;
 const offset = statusRow ? statusRow.offsetHeight + (parseFloat(getComputedStyle(outer.parentElement).gap) || 0) : 0;
 document.querySelectorAll('.led-arrow').forEach(a => {
 a.style.height = h + 'px';
 a.style.marginTop = offset + 'px';
 });
}
window.addEventListener('resize', syncArrowHeight);


// Unified init — single request replaces 5 separate ones
(async () => {
 try {
  const res = await fetch('/api/init');
  const data = await res.json();
  setCountries(data.countries);
  applyConfig(data.config);
  previewData = data.preview;
  lastCacheData = data.cache;
  updateStatusBar();
  if (data.brand) { brandData = Object.assign(brandData, data.brand); }
  if (data.brand_layout) { Object.assign(brandLayout, data.brand_layout); }
  renderSlide();
  syncArrowHeight();
 } catch(e) {
  // Fallback to individual requests
  loadCountries();
  await loadConfig();
  loadPreview();
  loadCacheStatus();
  loadBrand();
 }
})();

// Refresh preview every 5 minutes — pause when tab is hidden
let _pollTimer = null;
function startPolling() {
 stopPolling();
 _pollTimer = setInterval(() => { loadPreview(); loadCacheStatus(); }, 300000);
}
function stopPolling() { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; } }
startPolling();
document.addEventListener('visibilitychange', () => {
 if (document.hidden) stopPolling();
 else { startPolling(); loadPreview(); loadCacheStatus(); }
});
requestAnimationFrame(syncArrowHeight);

// Click outside domain edit → cancel (mousedown to avoid conflict with onclick)
document.addEventListener('mousedown', e => {
 const editing = DOM.domainList.querySelector('.edit-grid');
 if (!editing) return;
 if (!e.target.closest('.domain-card')) cancelEdit();
});

</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  SISTRIX LED Ticker — Web Panel + Preview")
    print("  http://raspberrypi.local:5001")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
