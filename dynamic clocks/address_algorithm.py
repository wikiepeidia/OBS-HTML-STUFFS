"""
Algorithm.py — Nominatim + LocationIQ algorithm test
Tests the exact logic from NominatimService.kt against real Vietnamese addresses.

Usage:
    python Algorithm.py                  # run all tests
    python Algorithm.py --unit           # unit (logic) tests only, no network
    python Algorithm.py --api            # API integration tests only
    python Algorithm.py --search         # run location search tests
    python Algorithm.py --lat 21.02 --lon 105.85  # ad-hoc coordinate probe
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load key=value pairs from .env in parent directories into os.environ."""
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

LOCATIONIQ_KEY = os.getenv("LOCATIONIQ_KEY", "")
LOCATIONIQ_EU  = os.getenv("LOCATIONIQ_EU", "https://eu1.locationiq.com/v1/")
LOCATIONIQ_US  = os.getenv("LOCATIONIQ_US", "https://us1.locationiq.com/v1/")
NOMINATIM_BASE = os.getenv("NOMINATIM_BASE", "https://nominatim.openstreetmap.org/")
USER_AGENT     = os.getenv("USER_AGENT", "vn-weather-algorithm-test/1.0")


# Nominatim rate-limit: 1 req/s
NOMINATIM_DELAY = 1.1

# ---------------------------------------------------------------------------
# VN sub-province regex  (mirrors NominatimService.vnSubProvinceRegex)
# ---------------------------------------------------------------------------
VN_SUBPROVINCE_PATTERN = re.compile(
    r"(?i)^(?:xã|phường|đặc\s*khu|xa|phuong|dac\s*khu)\s+.*"
)


# ---------------------------------------------------------------------------
# Pure-logic helpers  (mirrors NominatimService.Companion.*)
# ---------------------------------------------------------------------------

def is_locationiq_key(value) -> bool:
    """Mirrors isLocationIqKey: key must start with 'pk.'"""
    return isinstance(value, str) and value.strip().startswith("pk.")


def classify_locationiq_key_state(value) -> str:
    """Mirrors classifyLocationIqKeyState → MISSING / MALFORMED / VALID"""
    if not value or not value.strip():
        return "MISSING"
    if is_locationiq_key(value):
        return "VALID"
    return "MALFORMED"


def is_clean_vn_city(city) -> bool:
    """Mirrors isCleanVnCity: city must strictly start with Phường/Xã/Đặc khu prefix"""
    if not city:
        return False
    return bool(VN_SUBPROVINCE_PATTERN.match(city))


def pick_best_vietnam_subprovince_part(parts: list) -> "str | None":
    """
    Mirrors pickBestVietnamSubProvincePart.
    Always picks the LAST matching ward token (lastOrNull in Kotlin).
    Multiple admin levels in display_name → innermost ward wins.
    """
    matched = [p for p in parts if VN_SUBPROVINCE_PATTERN.match(p.strip())]
    return matched[-1] if matched else None


def merge_vn_results(liq_city, nom_city) -> dict:
    """
    Mirrors NominatimService.requestNearestLocation + mergeVnResults decision:
      - LocationIQ clean → use LocationIQ (authoritative, Nominatim not needed)
      - LocationIQ dirty & Nominatim clean → use Nominatim (GiggleRescue)
      - Both dirty → LIQ last resort
      - Both missing → none
    Returns dict with 'winner' and 'city'.
    """
    if is_clean_vn_city(liq_city):
        return {"winner": "locationiq", "city": liq_city}
    if is_clean_vn_city(nom_city):
        return {"winner": "nominatim", "city": nom_city}
    if liq_city is not None:
        return {"winner": "locationiq_fallback", "city": liq_city}
    return {"winner": "none", "city": None}


def merge_other_country_results(liq_city, nom_city) -> dict:
    """
    Mirrors mergeOtherCountryResults (685a6db79):
      - Outside VN, Nominatim is always asked and its city wins if non-empty.
      - Otherwise keep LocationIQ's city if present.
    """
    if nom_city:
        return {"winner": "nominatim", "city": nom_city}
    if liq_city:
        return {"winner": "locationiq_fallback", "city": liq_city}
    return {"winner": "none", "city": None}


def initial_city(result: dict, country_code: str) -> "str | None":
    """Mirrors NominatimService.initialCity (685a6db79)."""
    address = result.get("address", {})
    if (country_code or "").lower() != "vn":
        return address.get("village") or address.get("town") or address.get("city")
    return address.get("town") or result.get("name")


def initial_district(address: dict, country_code: str) -> "str | None":
    """Mirrors NominatimService.initialDistrict (685a6db79)."""
    if (country_code or "").lower() == "vn":
        return address.get("village")
    for field in (
        "neighbourhood", "allotments", "quarter", "hamlet", "croft",
        "isolated_dwelling", "city_district", "district", "borough",
        "suburb", "subdivision",
    ):
        val = address.get(field)
        if val:
            return val
    return None


def resolve_locationiq_candidates(override=None) -> list:
    """
    Mirrors resolveLocationIqBaseUrlCandidates.
    Deprecated ap1 redirects to eu1; primary eu1 → fallback us1, primary us1 → fallback eu1, else both.
    """
    primary = override if override else LOCATIONIQ_EU
    if "ap1.locationiq.com" in primary.lower():
        primary = LOCATIONIQ_EU
    if "eu1.locationiq.com" in primary:
        return list(dict.fromkeys([primary, LOCATIONIQ_US]))
    if "us1.locationiq.com" in primary:
        return list(dict.fromkeys([primary, LOCATIONIQ_EU]))
    return list(dict.fromkeys([primary, LOCATIONIQ_EU, LOCATIONIQ_US]))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, params: dict, timeout: int = 10) -> "dict | list":
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def locationiq_reverse(lat: float, lon: float, key: str, endpoint: str = LOCATIONIQ_EU) -> dict:
    return http_get(endpoint + "reverse", {
        "lat": lat, "lon": lon,
        "format": "json",
        "zoom": 18,
        "key": key,
        "addressdetails": 1,
    })


def nominatim_reverse(lat: float, lon: float) -> dict:
    time.sleep(NOMINATIM_DELAY)
    return http_get(NOMINATIM_BASE + "reverse", {
        "lat": lat, "lon": lon,
        "format": "jsonv2",
        "zoom": 13,
        "addressdetails": 1,
    })


def locationiq_search(query: str, key: str, endpoint: str = LOCATIONIQ_EU) -> list:
    return http_get(endpoint + "search", {
        "q": query,
        "format": "json",
        "limit": 5,
        "key": key,
        "addressdetails": 1,
        "countrycodes": "vn",
    })


def nominatim_search(query: str) -> list:
    time.sleep(NOMINATIM_DELAY)
    return http_get(NOMINATIM_BASE + "search", {
        "q": query,
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 1,
        "countrycodes": "vn",
    })


# ---------------------------------------------------------------------------
# Extract city from raw API response  (mirrors convertLocation VN logic)
# ---------------------------------------------------------------------------

def extract_vn_city(result: dict, is_search: bool = False) -> "str | None":
    """
    Applies the same city extraction logic as NominatimService.convertLocation (+ search fallback):
    - Non-VN: address.village / town / city (upstream mapping)
    - VN:
      1. Try display_name regex (last matching ward token)
      2. Fallback to suburb / hamlet / quarter
      3. Final fallback to address.town or result.name (NEVER display_name[0] POI/house number)
    - Search fallback (685a6db79): if city and district are both empty, use address.city or address.state
    """
    address      = result.get("address", {})
    display_name = result.get("display_name", "")
    country_code = (address.get("country_code") or "vn").lower()

    city     = initial_city(result, country_code)
    district = initial_district(address, country_code)

    if country_code == "vn":
        vn_resolved = False
        # Step 1: display_name regex
        if display_name:
            parts = [p.strip() for p in re.split(r"[,，]", display_name)]
            best  = pick_best_vietnam_subprovince_part(parts)
            if best:
                city = best
                district = None
                vn_resolved = True

        # Step 2: structured fields
        if not vn_resolved:
            for field in ("suburb", "hamlet", "quarter"):
                val = address.get(field)
                if val:
                    city = val
                    district = None
                    break

    # Search fallback (685a6db79): label city-level results (e.g. "Hà Nội") when city & district are empty
    if is_search and not city and not district:
        city = address.get("city") or address.get("state")

    return city


# ---------------------------------------------------------------------------
# Test coordinates: key Vietnamese locations
# ---------------------------------------------------------------------------
VN_COORDS = [
    # (label, lat, lon, description)
    ("Hanoi – Hoàn Kiếm",         21.02849, 105.85141, "Old Quarter / Hồ Hoàn Kiếm"),
    ("Hanoi – Đống Đa",           21.02694, 105.84311, "Inner district, typical Phường addresses"),
    ("Hanoi – Long Biên suburb",  21.04444, 105.88527, "Suburban ward"),
    ("HCMC – Bến Nghé",           10.77648, 106.70327, "District 1 core"),
    ("HCMC – Bình Thạnh",         10.81389, 106.71111, "Dense residential ward"),
    ("HCMC – Thủ Đức",            10.85444, 106.77500, "New Thu Duc City"),
    ("Da Nang – Hải Châu",        16.07194, 108.22083, "Central Da Nang ward"),
    ("Huế – Phú Hội",             16.46778, 107.59694, "Huế inner ward"),
    ("Cần Thơ – Ninh Kiều",       10.03333, 105.78333, "Mekong Delta urban core"),
    ("Hội An – Minh An",          15.87972, 108.33500, "Old Town ward"),
    ("Nha Trang – Lộc Thọ",       12.24167, 109.19472, "Coastal ward"),
    ("Hạ Long – Bãi Cháy",        20.95194, 107.04167, "Tourist zone ward"),
    ("Đà Lạt – Phường 1",         11.94556, 108.44417, "Highland city ward"),
    ("Vũng Tàu – Phường 1",       10.34583, 107.08444, "Beach city ward"),
    ("Vinh – Hà Huy Tập",         18.67139, 105.68528, "North-central city ward"),
]


# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------

def run_unit_tests() -> bool:
    print("=" * 70)
    print("UNIT TESTS — pure logic (no network)")
    print("=" * 70)

    passed = failed = 0

    def check(label, actual, expected):
        nonlocal passed, failed
        ok     = actual == expected
        status = "PASS" if ok else "FAIL"
        if not ok:
            print(f"  [{status}] {label}")
            print(f"         expected: {expected!r}")
            print(f"         actual:   {actual!r}")
            failed += 1
        else:
            print(f"  [{status}] {label}")
            passed += 1

    # --- isLocationIqKey ---
    print("\n-- isLocationIqKey --")
    check("valid key",             is_locationiq_key("pk.test_dummy_key_12345"),             True)
    check("valid key with space",  is_locationiq_key(" pk.abc123 "),                         True)
    check("empty string",          is_locationiq_key(""),                                    False)
    check("None",                  is_locationiq_key(None),                                  False)
    check("URL string",            is_locationiq_key("https://eu1.locationiq.com/v1/"),      False)
    check("Nominatim URL",         is_locationiq_key("https://nominatim.openstreetmap.org/"),False)

    # --- classifyLocationIqKeyState ---
    print("\n-- classifyLocationIqKeyState --")
    check("valid key → VALID",         classify_locationiq_key_state("pk.test_dummy_key_12345"),         "VALID")
    check("None → MISSING",            classify_locationiq_key_state(None),                              "MISSING")
    check("blank → MISSING",           classify_locationiq_key_state("  "),                              "MISSING")
    check("URL → MALFORMED",           classify_locationiq_key_state("https://eu1.locationiq.com/v1/"),  "MALFORMED")
    check("random string → MALFORMED", classify_locationiq_key_state("sometoken"),                       "MALFORMED")

    # --- isCleanVnCity ---
    print("\n-- isCleanVnCity --")
    check("Phường prefix",        is_clean_vn_city("Phường 1"),           True)
    check("Phường with name",     is_clean_vn_city("Phường Bến Nghé"),    True)
    check("Xã prefix",            is_clean_vn_city("Xã Tân Lập"),         True)
    check("Đặc Khu prefix",       is_clean_vn_city("Đặc Khu Vân Đồn"),   True)
    check("ascii phuong",         is_clean_vn_city("Phuong 5"),            True)
    check("ascii xa",             is_clean_vn_city("Xa Tan Lap"),          True)
    check("bare district",        is_clean_vn_city("Quận 1"),              False)
    check("city name",            is_clean_vn_city("Hà Nội"),              False)
    check("None",                 is_clean_vn_city(None),                  False)
    check("empty",                is_clean_vn_city(""),                    False)
    check("prefix alone (no space+text)", is_clean_vn_city("xã"),         False)

    # --- pickBestVietnamSubProvincePart ---
    print("\n-- pickBestVietnamSubProvincePart --")
    p1 = ["Phường Bến Nghé", "Quận 1", "Hồ Chí Minh", "Việt Nam"]
    check("single ward → picks it",
          pick_best_vietnam_subprovince_part(p1), "Phường Bến Nghé")

    p2 = ["Xã Tân Thành", "Phường Lộc Thọ", "Nha Trang", "Việt Nam"]
    check("two ward tokens → LAST wins",
          pick_best_vietnam_subprovince_part(p2), "Phường Lộc Thọ")

    p3 = ["Quận Hải Châu", "Đà Nẵng", "Việt Nam"]
    check("no ward token → None",
          pick_best_vietnam_subprovince_part(p3), None)

    p4 = ["Đặc Khu Vân Đồn", "Quảng Ninh", "Việt Nam"]
    check("Đặc Khu prefix wins",
          pick_best_vietnam_subprovince_part(p4), "Đặc Khu Vân Đồn")

    p5 = ["Đặc Khu A", "Phường B", "Quận C", "Hà Nội"]
    check("mixed: last ward token (Phường B) wins over Đặc Khu A",
          pick_best_vietnam_subprovince_part(p5), "Phường B")

    check("empty list → None",
          pick_best_vietnam_subprovince_part([]), None)

    # --- mergeVnResults (LocationIQ-first + GiggleRescue) ---
    print("\n-- mergeVnResults (LocationIQ-first + GiggleRescue logic) --")
    r = merge_vn_results(liq_city="Phường Bến Nghé", nom_city="Phường Đa Kao")
    check("LIQ clean → LIQ authoritative",    r["winner"], "locationiq")
    check("LIQ clean → keeps LIQ city",       r["city"],   "Phường Bến Nghé")

    r = merge_vn_results(liq_city="Quận 1", nom_city="Phường Bến Nghé")
    check("LIQ dirty, Nominatim clean → rescue winner", r["winner"], "nominatim")
    check("LIQ dirty, Nominatim clean → rescue city",   r["city"],   "Phường Bến Nghé")

    r = merge_vn_results(liq_city="Phường Hoàn Kiếm", nom_city="Hoàn Kiếm")
    check("LIQ clean, nom dirty → LIQ wins",            r["winner"], "locationiq")
    check("LIQ clean, nom dirty → city",                r["city"],   "Phường Hoàn Kiếm")

    r = merge_vn_results(liq_city="Hà Nội", nom_city="Hà Nội")
    check("both dirty → LIQ last resort", r["winner"], "locationiq_fallback")

    r = merge_vn_results(liq_city=None, nom_city=None)
    check("both None → none",  r["winner"], "none")
    check("both None → city",  r["city"],   None)

    # --- extract_vn_city & 685a6db79 rules ---
    print("\n-- extract_vn_city & 685a6db79 rules --")
    ref_hcmc = {
        "display_name": "Trụ sở Ủy ban Nhân dân Thành phố Hồ Chí Minh, 86, Lê Thánh Tôn, Khu phố 3, Phường Sài Gòn, Thành phố Thủ Đức, Thành phố Hồ Chí Minh, 71006, Việt Nam",
        "address": {"country_code": "vn", "city": "Thành phố Hồ Chí Minh"},
    }
    check("reference HCMC ward → Phường Sài Gòn",
          extract_vn_city(ref_hcmc), "Phường Sài Gòn")

    poi_only = {
        "display_name": "86 Lê Thánh Tôn, Quận 1, Thành phố Hồ Chí Minh, Việt Nam",
        "address": {"country_code": "vn", "city": "Thành phố Hồ Chí Minh"},
    }
    check("no ward/town/name → does NOT leak POI from display_name",
          extract_vn_city(poi_only), None)
    check("search city-level fallback → Thành phố Hồ Chí Minh",
          extract_vn_city(poi_only, is_search=True), "Thành phố Hồ Chí Minh")

    abroad_res = {
        "display_name": "Eiffel Tower, Paris, France",
        "address": {"country_code": "fr", "city": "Paris", "suburb": "Gros-Caillou"},
    }
    check("abroad initial_city → Paris",
          extract_vn_city(abroad_res), "Paris")
    check("abroad initial_district → Gros-Caillou",
          initial_district(abroad_res["address"], "fr"), "Gros-Caillou")

    r_abroad = merge_other_country_results(liq_city="Paris LIQ", nom_city="Paris Nom")
    check("abroad merge → Nominatim wins", r_abroad["winner"], "nominatim")
    r_abroad_fb = merge_other_country_results(liq_city="Paris LIQ", nom_city=None)
    check("abroad merge (nom empty) → LIQ fallback", r_abroad_fb["winner"], "locationiq_fallback")

    # --- resolveLocationIqCandidates ---
    print("\n-- resolveLocationIqCandidates (endpoint fallback) --")
    cands = resolve_locationiq_candidates()
    check("default: eu1 first",  cands[0], LOCATIONIQ_EU)
    check("default: us1 second", cands[1], LOCATIONIQ_US)

    cands_us = resolve_locationiq_candidates(LOCATIONIQ_US)
    check("us1 primary: us1 first",  cands_us[0], LOCATIONIQ_US)
    check("us1 primary: eu1 second", cands_us[1], LOCATIONIQ_EU)

    cands_ap1 = resolve_locationiq_candidates("https://ap1.locationiq.com/v1/")
    check("deprecated ap1 → redirects to eu1", cands_ap1[0], LOCATIONIQ_EU)

    print(f"\n  Unit tests: {passed} passed, {failed} failed")
    return failed == 0


# ---------------------------------------------------------------------------
# API INTEGRATION TESTS
# ---------------------------------------------------------------------------

def run_api_tests(coords: list = None):
    print("=" * 70)
    print("API INTEGRATION TESTS — live network calls")
    print(f"  LocationIQ key state: {classify_locationiq_key_state(LOCATIONIQ_KEY)}")
    print(f"  Endpoint candidates:  {resolve_locationiq_candidates()}")
    print("=" * 70)

    target_coords   = coords or VN_COORDS
    results_summary = []

    for label, lat, lon, desc in target_coords:
        print(f"\n{'─' * 70}")
        print(f"  {label}  ({lat}, {lon})")
        print(f"  {desc}")
        print(f"{'─' * 70}")

        liq_city = nom_city = None
        liq_cc = "?"

        # LocationIQ reverse (zoom=18 — same as app)
        try:
            liq_raw   = locationiq_reverse(lat, lon, LOCATIONIQ_KEY)
            liq_city  = extract_vn_city(liq_raw)
            liq_clean = is_clean_vn_city(liq_city)
            liq_cc    = liq_raw.get("address", {}).get("country_code", "?").upper()
            print(f"  LocationIQ  city: {str(liq_city):<40}  clean={liq_clean}  cc={liq_cc}")
            print(f"              display_name: {liq_raw.get('display_name','')[:72]}")
        except Exception as e:
            print(f"  LocationIQ  ERROR: {e}")

        is_vn = liq_cc == "VN"
        need_nominatim = (not is_vn) or (not is_clean_vn_city(liq_city))

        # Nominatim reverse (lazy-fetched only when LIQ is dirty/failed or non-VN — matches NominatimService.kt)
        if need_nominatim:
            try:
                nom_raw   = nominatim_reverse(lat, lon)
                nom_city  = extract_vn_city(nom_raw)
                nom_clean = is_clean_vn_city(nom_city)
                nom_cc    = nom_raw.get("address", {}).get("country_code", "?").upper()
                print(f"  Nominatim   city: {str(nom_city):<40}  clean={nom_clean}  cc={nom_cc}")
                print(f"              display_name: {nom_raw.get('display_name','')[:72]}")
            except Exception as e:
                print(f"  Nominatim   ERROR: {e}")
        else:
            print("  Nominatim   skipped (LocationIQ returned clean VN ward)")

        # Merge decision (mirrors requestNearestLocation)
        merge   = merge_vn_results(liq_city=liq_city, nom_city=nom_city) if is_vn else merge_other_country_results(liq_city=liq_city, nom_city=nom_city)
        rescued = is_vn and merge["winner"] == "nominatim" and not is_clean_vn_city(liq_city)
        tag     = "  ← [GiggleRescue]" if rescued else ""
        print(f"  → winner={merge['winner']}, final_city={merge['city']!r}{tag}")

        results_summary.append({
            "label": label, "lat": lat, "lon": lon,
            "liq_city": liq_city, "nom_city": nom_city,
            "final_city": merge["city"], "winner": merge["winner"],
            "rescued": rescued,
        })

    # Summary table
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    n              = len(results_summary)
    rescued_count  = sum(1 for r in results_summary if r["rescued"])
    liq_clean      = sum(1 for r in results_summary if is_clean_vn_city(r["liq_city"]))
    nom_clean      = sum(1 for r in results_summary if is_clean_vn_city(r["nom_city"]))
    final_clean    = sum(1 for r in results_summary if is_clean_vn_city(r["final_city"]))

    print(f"  Locations tested : {n}")
    print(f"  LIQ clean cities : {liq_clean}/{n}")
    print(f"  Nom clean cities : {nom_clean}/{n}")
    print(f"  Final clean      : {final_clean}/{n}")
    print(f"  GiggleRescues    : {rescued_count}")

    w = 32
    print(f"\n  {'Label':<{w}} {'LIQ city':<{w}} {'Nom city':<{w}} {'Final':<{w}} Winner")
    print(f"  {'─'*w} {'─'*w} {'─'*w} {'─'*w} {'─'*15}")
    for r in results_summary:
        liq_s = (r["liq_city"]   or "—")[:w-1]
        nom_s = (r["nom_city"]   or "—")[:w-1]
        fin_s = (r["final_city"] or "—")[:w-1]
        star  = " ★" if r["rescued"] else ""
        print(f"  {r['label'][:w]:<{w}} {liq_s:<{w}} {nom_s:<{w}} {fin_s:<{w}} {r['winner']}{star}")

    return results_summary


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------

def run_search_tests():
    queries = [
        "Phường Bến Nghé, Quận 1, Hồ Chí Minh",
        "Xã Tân Lập, Đồng Nai",
        "Hội An",
        "Đà Lạt",
        "36 Đinh Tiên Hoàng, Hà Nội",
    ]
    print("=" * 70)
    print("SEARCH TESTS")
    print("=" * 70)
    for q in queries:
        print(f"\n  Query: {q!r}")
        try:
            for i, r in enumerate(locationiq_search(q, LOCATIONIQ_KEY)[:3], 1):
                city  = extract_vn_city(r, is_search=True)
                clean = is_clean_vn_city(city)
                print(f"    LIQ [{i}] city={city!r}  clean={clean}  → {r.get('display_name','')[:60]}")
        except Exception as e:
            print(f"    LIQ ERROR: {e}")
        try:
            for i, r in enumerate(nominatim_search(q)[:3], 1):
                city  = extract_vn_city(r, is_search=True)
                clean = is_clean_vn_city(city)
                print(f"    NOM [{i}] city={city!r}  clean={clean}  → {r.get('display_name','')[:60]}")
        except Exception as e:
            print(f"    NOM ERROR: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nominatim+LocationIQ algorithm test")
    parser.add_argument("--unit",   action="store_true", help="Unit tests only, no network")
    parser.add_argument("--api",    action="store_true", help="API integration tests only")
    parser.add_argument("--search", action="store_true", help="Run search tests")
    parser.add_argument("--lat",    type=float,          help="Ad-hoc probe latitude")
    parser.add_argument("--lon",    type=float,          help="Ad-hoc probe longitude")
    args = parser.parse_args()

    if args.lat is not None and args.lon is not None:
        run_api_tests([(f"{args.lat},{args.lon}", args.lat, args.lon, "ad-hoc probe")])
        sys.exit(0)

    any_flag = args.unit or args.api or args.search

    if not any_flag or args.unit:
        ok = run_unit_tests()
        if args.unit:
            sys.exit(0 if ok else 1)

    if not any_flag or args.api:
        run_api_tests()

    if args.search:
        run_search_tests()