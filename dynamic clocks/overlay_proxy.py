from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _load_dotenv() -> None:
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
            break


_load_dotenv()


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_LABEL = "Hà Nội, Việt Nam"
DEFAULT_LAT = 21.0285
DEFAULT_LON = 105.8542
DEFAULT_ZOOM = 16
USER_AGENT = "gptclock-overlay-proxy/1.0"

LOCATIONIQ_KEY = os.getenv("LOCATIONIQ_KEY", "")
OWM_KEY = os.getenv("OWM_KEY", "")

LOCATIONIQ_BASE = "https://eu1.locationiq.com/v1/reverse"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org/reverse"
OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"
NOMINATIM_DELAY = 1.1
VN_SUBPROVINCE_PATTERN = re.compile(
    r"(?i)^(?:xã|phường|đặc\s*khu|xa|phuong|dac\s*khu)\s+.*"
)


def fetch_json(url: str, params: dict, timeout: int = 15) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_whitespace(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def normalize_admin_name(value: str | None) -> str | None:
    text = normalize_whitespace(value)
    if not text:
        return None

    lowered = text.lower()
    if lowered.startswith("phường "):
        return f"Phường {text[7:].strip()}"
    if lowered.startswith("xã "):
        return f"Xã {text[3:].strip()}"
    if lowered.startswith("đặc khu "):
        return f"Đặc Khu {text[8:].strip()}"
    if lowered.startswith("phuong "):
        return f"Phường {text[7:].strip()}"
    if lowered.startswith("xa "):
        return f"Xã {text[3:].strip()}"
    if lowered.startswith("dac khu "):
        return f"Đặc Khu {text[8:].strip()}"

    if text.lower().endswith(" ward"):
        return f"Phường {text[:-5].strip()}"
    if text.lower().endswith(" commune"):
        return f"Xã {text[:-8].strip()}"
    if text.lower().startswith("district "):
        return f"Quận {text[9:].strip()}"
    if text.lower().endswith(" district"):
        return f"Quận {text[:-9].strip()}"
    return text


def canonical_part(value: str | None) -> str:
    text = normalize_admin_name(value)
    if not text:
        return ""

    folded = "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    ).lower()

    for token in (
        "phuong", "xa", "dac khu", "ward", "commune", "district",
        "quan", "huyen", "thi tran", "thi xa", "thanh pho", "city",
    ):
        folded = folded.replace(token, " ")

    return " ".join("".join(ch if ch.isalnum() else " " for ch in folded).split())


def dedupe_parts(parts: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = normalize_admin_name(part)
        key = canonical_part(normalized)
        if not normalized or not key or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def is_clean_vn_city(city: str | None) -> bool:
    if not city:
        return False
    return bool(VN_SUBPROVINCE_PATTERN.match(city))


def pick_best_vietnam_subprovince_part(parts: list[str]) -> str | None:
    matched = [part for part in parts if VN_SUBPROVINCE_PATTERN.match(part.strip())]
    return matched[-1] if matched else None


def extract_vn_city(result: dict) -> str | None:
    address = result.get("address", {})
    display_name = result.get("display_name", "")
    country_code = (address.get("country_code") or "vn").lower()

    if country_code != "vn":
        fallback = address.get("village") or address.get("town") or address.get("city")
        return normalize_admin_name(fallback) or None

    if display_name:
        parts = [part.strip() for part in re.split(r"[,，]", display_name)]
        best = pick_best_vietnam_subprovince_part(parts)
        if best:
            return normalize_admin_name(best)

    for field in ("suburb", "hamlet", "quarter"):
        value = normalize_admin_name(address.get(field))
        if value:
            return value

    fallback = address.get("town") or result.get("name")
    return normalize_admin_name(fallback) or None


def merge_vn_results(liq_city: str | None, nom_city: str | None) -> dict:
    if is_clean_vn_city(liq_city):
        return {"winner": "locationiq", "city": normalize_admin_name(liq_city)}
    if is_clean_vn_city(nom_city):
        return {"winner": "nominatim", "city": normalize_admin_name(nom_city)}
    if liq_city is not None:
        return {"winner": "locationiq_fallback", "city": normalize_admin_name(liq_city)}
    return {"winner": "none", "city": None}


def merge_other_country_results(liq_city: str | None, nom_city: str | None) -> dict:
    if nom_city:
        return {"winner": "nominatim", "city": normalize_admin_name(nom_city)}
    if liq_city:
        return {"winner": "locationiq_fallback", "city": normalize_admin_name(liq_city)}
    return {"winner": "none", "city": None}


def extract_parent_city(*results: dict) -> str | None:
    for result in results:
        address = result.get("address", {})
        for field in ("city", "town", "state", "province"):
            value = normalize_admin_name(address.get(field))
            if value:
                return value
    return None


def build_location_label(local: str | None, parent: str | None, fallback_label: str) -> str:
    parts = dedupe_parts([local, parent])
    return ", ".join(parts[:2] if local else parts[:1]) or fallback_label


def fetch_location(lat: float, lon: float, label: str, zoom: int) -> str:
    liq_payload: dict = {}
    nom_payload: dict = {}
    liq_city: str | None = None
    nom_city: str | None = None
    last_error: Exception | None = None

    try:
        liq_payload = fetch_json(
            LOCATIONIQ_BASE,
            {
                "lat": lat,
                "lon": lon,
                "format": "json",
                "zoom": zoom,
                "key": LOCATIONIQ_KEY,
                "addressdetails": 1,
            },
        )
        liq_city = extract_vn_city(liq_payload)
    except Exception as error:  # pragma: no cover - network path
        last_error = error

    country_code = (liq_payload.get("address", {}).get("country_code") or "").lower()
    is_vn = country_code == "vn"
    need_nominatim = (not is_vn) or (not is_clean_vn_city(liq_city))

    if need_nominatim:
        try:
            time.sleep(NOMINATIM_DELAY)
            nom_payload = fetch_json(
                NOMINATIM_BASE,
                {
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 13,
                    "addressdetails": 1,
                },
            )
            nom_city = extract_vn_city(nom_payload)
        except Exception as error:  # pragma: no cover - network path
            last_error = error

    merged = (
        merge_vn_results(liq_city, nom_city)
        if is_vn
        else merge_other_country_results(liq_city, nom_city)
    )
    local = normalize_admin_name(merged["city"])
    parent = extract_parent_city(nom_payload, liq_payload)
    location_label = build_location_label(local, parent, label)
    if location_label:
        return location_label

    if last_error is not None:
        raise last_error
    return label


def fetch_weather(lat: float, lon: float) -> dict:
    payload = fetch_json(
        OWM_BASE,
        {
            "lat": lat,
            "lon": lon,
            "appid": OWM_KEY,
            "units": "metric",
            "lang": "vi",
        },
    )
    return {
        "condition_id": payload["weather"][0]["id"],
        "description": payload["weather"][0]["description"],
        "temp_c": payload["main"]["temp"],
        "feels_like_c": payload["main"]["feels_like"],
        "humidity": payload["main"]["humidity"],
        "wind_mps": payload.get("wind", {}).get("speed", 0.0),
    }


class ContextCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.entries: dict[tuple[float, float, str], tuple[float, dict]] = {}

    def get(self, key: tuple[float, float, str]) -> dict | None:
        entry = self.entries.get(key)
        if not entry:
            return None
        cached_at, payload = entry
        if time.time() - cached_at > self.ttl_seconds:
            return None
        return payload

    def get_stale(self, key: tuple[float, float, str]) -> dict | None:
        entry = self.entries.get(key)
        return entry[1] if entry else None

    def set(self, key: tuple[float, float, str], payload: dict) -> None:
        self.entries[key] = (time.time(), payload)


def build_payload(args: argparse.Namespace, lat: float, lon: float, label: str) -> dict:
    weather = fetch_weather(lat, lon)
    location_label = fetch_location(lat, lon, label, args.zoom)
    now = time.localtime()
    return {
        "location": {
            "label": location_label,
            "lat": lat,
            "lon": lon,
        },
        "weather": weather,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", now),
        "updated_hhmm": time.strftime("%H:%M", now),
        "mode_label": "Proxy cục bộ 15 phút",
    }


def parse_float(value: str | None, fallback: float) -> float:
    if value in (None, ""):
        return fallback
    return float(value)


def json_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(args: argparse.Namespace, cache: ContextCache):
    class OverlayHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path != "/api/context":
                json_error(self, 404, "unknown endpoint")
                return

            query = urllib.parse.parse_qs(parsed.query)
            try:
                lat = parse_float(query.get("lat", [None])[0], args.lat)
                lon = parse_float(query.get("lon", [None])[0], args.lon)
            except ValueError:
                json_error(self, 400, "lat/lon must be numbers")
                return

            label = normalize_whitespace(query.get("label", [args.label])[0]) or args.label
            cache_key = (round(lat, 5), round(lon, 5), label)
            cached = cache.get(cache_key)
            if cached:
                body = json.dumps(cached, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", f"public, max-age={args.ttl_seconds}")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                payload = build_payload(args, lat, lon, label)
                cache.set(cache_key, payload)
            except urllib.error.HTTPError as error:
                stale = cache.get_stale(cache_key)
                if stale:
                    stale_payload = dict(stale)
                    stale_payload["mode_label"] = "Proxy cục bộ (cache cũ)"
                    stale_payload["warning"] = f"upstream {error.code}"
                    body = json.dumps(stale_payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                json_error(self, 502, f"upstream {error.code}: {error.reason}")
                return
            except Exception as error:  # pragma: no cover - defensive path
                stale = cache.get_stale(cache_key)
                if stale:
                    stale_payload = dict(stale)
                    stale_payload["mode_label"] = "Proxy cục bộ (cache cũ)"
                    stale_payload["warning"] = str(error)
                    body = json.dumps(stale_payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                json_error(self, 500, str(error))
                return

            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"public, max-age={args.ttl_seconds}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args_) -> None:
            return

    return OverlayHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tiny proxy for GPTCLOCK1 overlay")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cache = ContextCache(args.ttl_seconds)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args, cache))
    print(f"Proxy running on http://{args.host}:{args.port}")
    print(f"Default context: {args.label} ({args.lat}, {args.lon})")
    print(f"Cache TTL: {args.ttl_seconds}s")
    server.serve_forever()


if __name__ == "__main__":
    main()