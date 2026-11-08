#!/usr/bin/env python3
"""Fuel Tracker – fuel log for Home Assistant (app/add-on).

Python standard library only. Data in SQLite (/data/fuel_tracker.db),
photos in /media/fuel_tracker (so HA's AI Task can read them as media-source).
"""
import base64
import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.4.1"
DATA_DIR = os.environ.get("FT_DATA_DIR", "/data")
MEDIA_ROOT = os.environ.get("FT_MEDIA_ROOT", "/media")
MEDIA_SUB = "fuel_tracker"
MEDIA_DIR = os.path.join(MEDIA_ROOT, MEDIA_SUB)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HA_API = os.environ.get("FT_HA_API", "http://supervisor/core/api")
PORT = int(os.environ.get("FT_PORT", "8099"))
DEV = os.environ.get("FT_DEV") == "1"
KM_PER_MI = 1.609344


def _container_env(name):
    val = os.environ.get(name, "")
    if val:
        return val
    # s6-overlay does not always pass the container environment on to CMD
    for base in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


TOKEN = _container_env("SUPERVISOR_TOKEN")
_tz = _container_env("TZ")
if _tz:
    os.environ["TZ"] = _tz
    try:
        time.tzset()
    except AttributeError:
        pass


# Defaults only - every value can be changed in the app (Settings)
DEFAULT_SETTINGS = {
    "currency": "kr",
    "distance_unit": "km",          # km | mi
    "volume_unit": "L",             # L | gal_us | gal_uk
    "consumption_style": "dpv",     # dpv = distance per volume (km/L, mpg) | vp100 = volume per 100 distance (L/100 km)
    "ai_task_entity": "ai_task.claude_ai_task",
    "publish_sensors": True,
    "warn_distance": 2000,          # in the chosen distance unit
    "fuel_types": [
        {"name": "Petrol 95 E10", "co2_kg_per_l": 2.31},
        {"name": "Petrol 95 E5", "co2_kg_per_l": 2.31},
        {"name": "Petrol 98", "co2_kg_per_l": 2.31},
        {"name": "Diesel", "co2_kg_per_l": 2.68},
    ],
}
KM_PER = {"km": 1.0, "mi": KM_PER_MI}
L_PER = {"L": 1.0, "gal_us": 3.785411784, "gal_uk": 4.54609}
VOL_LABEL = {"L": "L", "gal_us": "gal", "gal_uk": "gal"}

DB_LOCK = threading.Lock()
PUBLISH_LOCK = threading.Lock()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- database

def db():
    con = sqlite3.connect(os.path.join(DATA_DIR, "fuel_tracker.db"))
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    with DB_LOCK, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                odo_unit TEXT NOT NULL DEFAULT 'km',
                odo_rollover REAL NOT NULL DEFAULT 0,
                tank_l REAL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
                ts TEXT NOT NULL,
                odometer REAL,
                liters REAL NOT NULL,
                price_total REAL,
                price_per_l REAL,
                full_tank INTEGER NOT NULL DEFAULT 1,
                missed_previous INTEGER NOT NULL DEFAULT 0,
                fuel_type TEXT,
                station TEXT,
                note TEXT,
                img_pump TEXT,
                img_odo TEXT,
                ai_pump TEXT,
                ai_odo TEXT,
                created TEXT
            );
            """
        )


def rows(sql, args=()):
    with DB_LOCK, db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with DB_LOCK, db() as con:
        return con.execute(sql, args).lastrowid


def get_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    for r in rows("SELECT key, value FROM settings"):
        if r["key"] in s:
            try:
                s[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
    return s


def save_settings(new):
    cur = get_settings()
    clean = {}
    if "currency" in new:
        clean["currency"] = str(new["currency"]).strip()[:12] or DEFAULT_SETTINGS["currency"]
    if new.get("distance_unit") in KM_PER:
        clean["distance_unit"] = new["distance_unit"]
    if new.get("volume_unit") in L_PER:
        clean["volume_unit"] = new["volume_unit"]
    if new.get("consumption_style") in ("dpv", "vp100"):
        clean["consumption_style"] = new["consumption_style"]
    if "ai_task_entity" in new:
        e = str(new["ai_task_entity"]).strip()
        if not re.fullmatch(r"ai_task\.[a-z0-9_]+", e):
            raise ValueError("AI Task entity must look like ai_task.something")
        clean["ai_task_entity"] = e
    if "publish_sensors" in new:
        clean["publish_sensors"] = bool(new["publish_sensors"])
    if "warn_distance" in new:
        w = to_num(new["warn_distance"])
        clean["warn_distance"] = w if w and w > 0 else None
    if "fuel_types" in new and isinstance(new["fuel_types"], list):
        fts = []
        for ft in new["fuel_types"]:
            name = str((ft or {}).get("name") or "").strip()
            if name:
                fts.append({"name": name[:40], "co2_kg_per_l": to_num(ft.get("co2_kg_per_l"))})
        clean["fuel_types"] = fts
    with DB_LOCK, db() as con:
        for k, v in clean.items():
            con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, json.dumps(v)))
    cur.update(clean)
    return cur


def get_vehicle(vid):
    r = rows("SELECT * FROM vehicles WHERE id=?", (vid,))
    return r[0] if r else None


def delete_image(name):
    if name and re.fullmatch(r"[A-Za-z0-9_\-]+\.jpg", name):
        try:
            os.remove(os.path.join(MEDIA_DIR, name))
        except OSError:
            pass


# ---------------------------------------------------------------- HA API

def ha_request(method, path, payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HA_API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body else None


PUMP_INSTRUCTIONS = """The image shows the display of a fuel pump at a filling station.
Read:
- liters: the volume dispensed, as the number shown (often labelled "Liter", "Litres", "Volume", "Ltr", "Gallons").
- volume_unit: "L" if the display shows liters, "gal" if it shows gallons, otherwise "unknown".
- total_price: the amount to pay (often labelled "Amount", "Total", "Sale", "Beløb", "Betrag", "Kr", "€").
- price_per_liter: the unit price per liter (or per gallon if the display uses gallons).
- fuel_type: the fuel grade if visible (e.g. "Petrol 95 E10", "Diesel"), otherwise leave empty.
Many displays use a comma as decimal separator: "38,47" means 38.47. Return numbers with a dot.
If a number cannot be read reliably, omit the field rather than guessing.
Use note (short, in English) to mention anything hard to read or numbers that do not add up."""

ODO_INSTRUCTIONS = """The image shows the odometer of a vehicle (a digital display or mechanical number
drums on an older vehicle). Read the TOTAL distance (not a trip meter). Work step by step:
1. Find the odometer window. In digits_seen, list the whole-number drums/digits from left to right,
   then a bar, then the tenths drum if there is one, e.g. "1 2 4 0 8 | 7". Each physical drum appears
   exactly once - never list the tenths drum both before and after the bar.
2. Decide whether the rightmost drum is TENTHS. On mechanical odometers the tenths drum almost always
   looks different from the others: a white, red or silver background, inverted colours, a separate
   frame or a smaller window. If it looks different in any of these ways, it IS tenths. A digital
   display shows tenths after a decimal point or in a smaller font.
3. reading = the whole digits followed by the tenths as a decimal (e.g. 1 2 4 0 8 + tenths 7 -> 12408.7).
   Set has_tenths accordingly.
- unit: "km" if the dial or display says km/KILOMETER, "miles" if it says MILES/mi/MPH, otherwise "unknown".
If a drum sits between two digits, use the lower digit. A drum partly hidden by the needle is still a drum.
Omit reading if it cannot be read reliably. Use note (short, in English) to mention any uncertainty."""

LOCATE_INSTRUCTIONS = """The image shows a vehicle dashboard or instrument. Find the odometer window (the row
of number drums or the digital total-distance display, not a trip meter). Give the position of its centre
as fractions of the image: cx (0 = left edge, 1 = right edge) and cy (0 = top edge, 1 = bottom edge), and
its approximate width as a fraction of the image width. Set found=false if no odometer is visible."""

NUM = {"number": {"mode": "box", "step": "any"}}
TXT = {"text": {}}
BOOL = {"boolean": {}}

PUMP_STRUCTURE = {
    "liters": {"selector": NUM, "description": "Liters dispensed"},
    "total_price": {"selector": NUM, "description": "Total amount"},
    "price_per_liter": {"selector": NUM, "description": "Price per liter or gallon"},
    "volume_unit": {"selector": TXT, "description": "L, gal or unknown"},
    "fuel_type": {"selector": TXT, "description": "Fuel grade"},
    "note": {"selector": TXT, "description": "Short note about uncertainties"},
}
ODO_STRUCTURE = {
    "digits_seen": {"selector": TXT, "description": "Whole drums left to right | tenths drum"},
    "reading": {"selector": NUM, "description": "Odometer reading"},
    "has_tenths": {"selector": BOOL, "description": "Whether reading includes tenths"},
    "unit": {"selector": TXT, "description": "km, miles or unknown"},
    "note": {"selector": TXT, "description": "Short note about uncertainties"},
}

LOCATE_STRUCTURE = {
    "found": {"selector": BOOL, "description": "Odometer visible"},
    "cx": {"selector": NUM, "description": "Centre x, 0-1"},
    "cy": {"selector": NUM, "description": "Centre y, 0-1"},
    "width": {"selector": NUM, "description": "Width as fraction of image width"},
}
TASKS = {
    "pump": ("Read fuel pump display", PUMP_INSTRUCTIONS, PUMP_STRUCTURE),
    "odo": ("Read odometer", ODO_INSTRUCTIONS, ODO_STRUCTURE),
    "odo_locate": ("Locate odometer", LOCATE_INSTRUCTIONS, LOCATE_STRUCTURE),
}


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _response_data(res):
    res = res or {}
    data = res.get("service_response", res).get("data")
    if isinstance(data, str):
        data = _extract_json(data)
    return data if isinstance(data, dict) else {}


def ai_read(kind, filename, context=""):
    task_name, instructions, structure = TASKS[kind]
    if context:
        instructions += "\n" + context
    base = {
        "task_name": task_name,
        "entity_id": get_settings()["ai_task_entity"],
        "attachments": [{
            "media_content_id": f"media-source://media_source/local/{MEDIA_SUB}/{filename}",
            "media_content_type": "image/jpeg",
        }],
    }
    path = "/services/ai_task/generate_data?return_response"
    try:
        return _response_data(ha_request("POST", path, dict(base, instructions=instructions, structure=structure))), None
    except urllib.error.HTTPError as e:
        log("AI call with structure failed", e.code, e.read().decode("utf-8", "replace")[:500], "- retrying as free text")
    except Exception as e:
        log("AI error", repr(e))
        return {}, str(e)
    try:
        keys = ", ".join(structure)
        res = ha_request("POST", path, dict(base, instructions=instructions + f"\nReply ONLY with one JSON object with the keys: {keys}."))
        return _response_data(res), None
    except urllib.error.HTTPError as e:
        return {}, f"Home Assistant returned {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return {}, str(e)


def to_num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", s))
    except ValueError:
        return None


# ---------------------------------------------------------------- statistics

def slugify(name):
    s = name.lower()
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa"), ("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "vehicle"


def co2_factor(fuel_type, fuel_types):
    name = (fuel_type or "").strip().lower()
    for ft in fuel_types:
        if ft["name"].strip().lower() == name:
            return ft.get("co2_kg_per_l")
    return None


def compute_stats(vehicle, settings=None):
    settings = settings or get_settings()
    fills = rows("SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts, id", (vehicle["id"],))
    factor = KM_PER_MI if vehicle["odo_unit"] == "mi" else 1.0
    roll = vehicle["odo_rollover"] or 0
    offset = 0.0
    prev_raw = prev_cum = None
    for f in fills:
        f["cum_km"] = f["dist_km"] = None
        if f["odometer"] is None:
            continue
        raw = f["odometer"]
        if prev_raw is not None and raw < prev_raw and roll:
            offset += roll
        prev_raw = raw
        f["cum_km"] = (raw + offset) * factor
        if prev_cum is not None:
            f["dist_km"] = f["cum_km"] - prev_cum
        prev_cum = f["cum_km"]

    # full tank to full tank
    segments = []
    last_full_cum = None
    acc_l = acc_cost = 0.0
    acc_complete = True
    for f in fills:
        if f["missed_previous"]:
            last_full_cum = None
            acc_l = acc_cost = 0.0
            acc_complete = True
        acc_l += f["liters"] or 0
        if f["price_total"] is not None:
            acc_cost += f["price_total"]
        else:
            acc_complete = False
        if f["full_tank"] and f["cum_km"] is not None:
            if last_full_cum is not None:
                dist = f["cum_km"] - last_full_cum
                if dist > 0 and acc_l > 0:
                    segments.append({
                        "fill_id": f["id"], "ts": f["ts"], "km": dist, "liters": acc_l,
                        "cost": acc_cost if acc_complete else None,
                        "km_per_l": dist / acc_l,
                        "l_per_100km": acc_l / dist * 100,
                        "cost_per_km": (acc_cost / dist) if acc_complete else None,
                    })
            last_full_cum = f["cum_km"]
            acc_l = acc_cost = 0.0
            acc_complete = True

    seg_km = sum(s["km"] for s in segments)
    seg_l = sum(s["liters"] for s in segments)
    costed = [s for s in segments if s["cost"] is not None]
    seg_cost = sum(s["cost"] for s in costed)
    seg_cost_km = sum(s["km"] for s in costed)

    odo_fills = [f for f in fills if f["cum_km"] is not None]
    tracked_km = (odo_fills[-1]["cum_km"] - odo_fills[0]["cum_km"]) if len(odo_fills) > 1 else 0
    total_l = sum(f["liters"] or 0 for f in fills)
    total_cost = sum(f["price_total"] or 0 for f in fills)
    prices = [{"ts": f["ts"], "price_per_l": f["price_per_l"]} for f in fills if f["price_per_l"]]

    def bucket(key_len):
        out = {}
        for f in fills:
            b = out.setdefault(f["ts"][:key_len], {"period": f["ts"][:key_len], "km": 0.0, "liters": 0.0, "cost": 0.0, "fills": 0, "_sk": 0.0, "_sl": 0.0})
            b["liters"] += f["liters"] or 0
            b["cost"] += f["price_total"] or 0
            b["fills"] += 1
            b["km"] += f["dist_km"] or 0
        for s in segments:
            b = out[s["ts"][:key_len]]
            b["_sk"] += s["km"]
            b["_sl"] += s["liters"]
        res = []
        for k in sorted(out):
            b = out[k]
            sk, sl = b.pop("_sk"), b.pop("_sl")
            b["km_per_l"] = sk / sl if sl else None
            res.append(b)
        return res

    last = fills[-1] if fills else None
    now = dt.datetime.now()
    days_since = None
    span_days = None
    if last:
        try:
            days_since = max(0, (now - dt.datetime.fromisoformat(last["ts"])).days)
            span_days = max(1, (dt.datetime.fromisoformat(last["ts"]) - dt.datetime.fromisoformat(fills[0]["ts"])).days)
        except ValueError:
            pass
    avg_kmpl = seg_km / seg_l if seg_l else None
    year = now.strftime("%Y")

    return {
        "vehicle": vehicle,
        "count": len(fills),
        "tracked_km": tracked_km,
        "total_liters": total_l,
        "total_cost": total_cost,
        "avg_km_per_l": avg_kmpl,
        "avg_l_per_100km": (seg_l / seg_km * 100) if seg_km else None,
        "avg_cost_per_km": (seg_cost / seg_cost_km) if seg_cost_km else None,
        "avg_price_per_l": (sum(p["price_per_l"] for p in prices) / len(prices)) if prices else None,
        "last_price_per_l": prices[-1]["price_per_l"] if prices else None,
        "last_segment": segments[-1] if segments else None,
        "best_segment": max(segments, key=lambda s: s["km_per_l"]) if segments else None,
        "worst_segment": min(segments, key=lambda s: s["km_per_l"]) if segments else None,
        "last_fill": last,
        "last_odometer": odo_fills[-1]["odometer"] if odo_fills else None,
        "days_since_last": days_since,
        "avg_km_per_fill": (tracked_km / (len(odo_fills) - 1)) if len(odo_fills) > 1 else None,
        "km_per_month": (tracked_km / span_days * 30.44) if span_days and tracked_km else None,
        "range_km": (vehicle["tank_l"] * avg_kmpl) if (vehicle.get("tank_l") and avg_kmpl) else None,
        "co2_kg": sum((f["liters"] or 0) * (co2_factor(f["fuel_type"], settings["fuel_types"]) or 0) for f in fills),
        "co2_unknown_l": sum((f["liters"] or 0) for f in fills if co2_factor(f["fuel_type"], settings["fuel_types"]) is None),
        "year": year,
        "year_cost": sum(f["price_total"] or 0 for f in fills if f["ts"].startswith(year)),
        "year_km": sum(f["dist_km"] or 0 for f in fills if f["ts"].startswith(year)),
        "segments": segments,
        "prices": prices,
        "months": bucket(7),
        "years": bucket(4),
    }


# ---------------------------------------------------------------- HA sensors

PUBLISHED_FILE = os.path.join(DATA_DIR, "published_sensors.json")


def _load_published():
    try:
        with open(PUBLISHED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_published(ids):
    try:
        with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
    except OSError:
        pass


def publish_sensors():
    cfg = get_settings()
    if not cfg["publish_sensors"]:
        return
    if not TOKEN:
        log("No SUPERVISOR_TOKEN - cannot publish sensors")
        return
    with PUBLISH_LOCK:
        published = set()
        failed = False
        for v in rows("SELECT * FROM vehicles WHERE active=1"):
            try:
                st = compute_stats(v, cfg)
            except Exception:
                log("Statistics error", traceback.format_exc())
                continue
            slug = slugify(v["name"])
            last_seg = st["last_segment"]
            odo_unit = "mi" if v["odo_unit"] == "mi" else "km"
            cur = cfg["currency"]
            du, vu = cfg["distance_unit"], cfg["volume_unit"]
            dl, vl = du, VOL_LABEL[vu]

            def cons(kmpl):
                if not kmpl:
                    return None
                dpv = kmpl * L_PER[vu] / KM_PER[du]
                return dpv if cfg["consumption_style"] == "dpv" else 100 / dpv
            cons_unit = f"{dl}/{vl}" if cfg["consumption_style"] == "dpv" else f"{vl}/100 {dl}"
            if cons_unit == "mi/gal":
                cons_unit = "mpg"
            per_dist = lambda x: x * KM_PER[du] if x is not None else None
            per_vol = lambda x: x * L_PER[vu] if x is not None else None
            sensors = {
                "consumption_last": (cons(last_seg["km_per_l"]) if last_seg else None, cons_unit, "mdi:gas-station", "Consumption last tank"),
                "consumption_avg": (cons(st["avg_km_per_l"]), cons_unit, "mdi:gas-station-outline", "Consumption average"),
                "odometer": (st["last_odometer"], odo_unit, "mdi:counter", "Odometer at last refuel"),
                "cost_per_distance": (per_dist(st["avg_cost_per_km"]), f"{cur}/{dl}", "mdi:cash", f"Fuel cost per {dl}"),
                "fuel_price": (per_vol(st["last_price_per_l"]), f"{cur}/{vl}", "mdi:currency-usd", "Last fuel price"),
                "cost_this_year": (st["year_cost"], cur, "mdi:cash-multiple", f"Fuel cost {st['year']}"),
                "last_refuel": (st["last_fill"]["ts"] if st["last_fill"] else None, None, "mdi:calendar", "Last refuel"),
            }
            for key, (val, unit, icon, label) in sensors.items():
                eid = f"sensor.fuel_tracker_{slug}_{key}"
                if isinstance(val, float):
                    val = round(val, 2)
                state = "unknown" if val is None else val
                attrs = {"friendly_name": f"{v['name']} {label}", "icon": icon}
                if unit:
                    attrs["unit_of_measurement"] = unit
                    attrs["state_class"] = "measurement"
                if key == "last_refuel" and val:
                    attrs["device_class"] = "timestamp"
                    try:
                        state = dt.datetime.fromisoformat(val).astimezone().isoformat()
                    except ValueError:
                        pass
                try:
                    ha_request("POST", f"/states/{eid}", {"state": state, "attributes": attrs}, timeout=10)
                    published.add(eid)
                except Exception as e:
                    log("Could not update sensor", eid, repr(e))
                    failed = True
        # remove sensors of deleted, renamed or hidden vehicles
        stale = _load_published() - published
        removed = 0
        for eid in stale:
            try:
                ha_request("DELETE", f"/states/{eid}", timeout=10)
                removed += 1
            except Exception as e:
                log("Could not remove old sensor", eid, repr(e))
        if not failed:
            _save_published(published)
        log(f"Updated {len(published)} sensors" + (f", removed {removed}" if removed else ""))


def publish_async():
    threading.Thread(target=publish_sensors, daemon=True).start()


# ---------------------------------------------------------------- HTTP

FILL_FIELDS = ("vehicle_id", "ts", "odometer", "liters", "price_total", "price_per_l",
               "full_tank", "missed_previous", "fuel_type", "station", "note",
               "img_pump", "img_odo", "ai_pump", "ai_odo")


def clean_fill(body):
    f = {k: body.get(k) for k in FILL_FIELDS}
    for k in ("odometer", "liters", "price_total", "price_per_l"):
        f[k] = to_num(f[k])
    for k in ("full_tank", "missed_previous"):
        f[k] = 1 if f[k] in (True, 1, "1", "true", "on") else 0
    f["vehicle_id"] = int(f["vehicle_id"] or 0)
    if not f["liters"] or f["liters"] <= 0:
        raise ValueError("Liters is missing")
    if f["price_total"] is None and f["price_per_l"]:
        f["price_total"] = round(f["price_per_l"] * f["liters"], 2)
    if f["price_per_l"] is None and f["price_total"]:
        f["price_per_l"] = round(f["price_total"] / f["liters"], 3)
    ts = (f["ts"] or "").strip() or dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    dt.datetime.fromisoformat(ts)
    f["ts"] = ts[:16]
    for k in ("ai_pump", "ai_odo"):
        if f[k] is not None and not isinstance(f[k], str):
            f[k] = json.dumps(f[k], ensure_ascii=False)
    for k in ("img_pump", "img_odo"):
        if f[k] and not re.fullmatch(r"[A-Za-z0-9_\-]+\.jpg", f[k]):
            f[k] = None
    return f


class Handler(BaseHTTPRequestHandler):
    server_version = "FuelTracker/" + VERSION

    def log_message(self, fmt, *args):
        if DEV:
            log(self.address_string(), fmt % args)

    def _allowed(self):
        return DEV or self.client_address[0] in ("172.30.32.2", "127.0.0.1")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 25 * 1024 * 1024:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def route(self, method):
        if not self._allowed():
            self.send_error(403)
            return
        u = urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if method == "GET" and p in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self.send_bytes(f.read(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            if p == "/api/status":
                return self.send_json({"has_token": bool(TOKEN), "version": VERSION, "settings": get_settings()})
            if p == "/api/settings":
                if method == "GET":
                    return self.send_json(get_settings())
                if method == "POST":
                    res = save_settings(self.read_json())
                    publish_async()
                    return self.send_json(res)
            if p == "/api/ai_entities":
                try:
                    states = ha_request("GET", "/states", timeout=15) or []
                    ents = [{"entity_id": x["entity_id"], "name": x.get("attributes", {}).get("friendly_name") or x["entity_id"]}
                            for x in states if x.get("entity_id", "").startswith("ai_task.")]
                except Exception as e:
                    log("Could not list AI Task entities", repr(e))
                    ents = []
                return self.send_json(ents)

            if p == "/api/vehicles":
                if method == "GET":
                    vs = rows("SELECT v.*, (SELECT COUNT(*) FROM fills f WHERE f.vehicle_id=v.id) AS fill_count "
                              "FROM vehicles v ORDER BY v.active DESC, v.id")
                    return self.send_json(vs)
                if method == "POST":
                    b = self.read_json()
                    name = (b.get("name") or "").strip()
                    if not name:
                        raise ValueError("Name is missing")
                    unit = "mi" if b.get("odo_unit") == "mi" else "km"
                    roll = to_num(b.get("odo_rollover")) or 0
                    tank = to_num(b.get("tank_l"))
                    active = 0 if b.get("active") in (False, 0, "0") else 1
                    if b.get("id"):
                        execute("UPDATE vehicles SET name=?, odo_unit=?, odo_rollover=?, tank_l=?, active=? WHERE id=?",
                                (name, unit, roll, tank, active, int(b["id"])))
                        vid = int(b["id"])
                    else:
                        vid = execute("INSERT INTO vehicles(name, odo_unit, odo_rollover, tank_l, active) VALUES (?,?,?,?,?)",
                                      (name, unit, roll, tank, active))
                    publish_async()
                    return self.send_json(get_vehicle(vid))
            m = re.fullmatch(r"/api/vehicles/(\d+)", p)
            if m and method == "DELETE":
                vid = int(m.group(1))
                for r in rows("SELECT img_pump, img_odo FROM fills WHERE vehicle_id=?", (vid,)):
                    delete_image(r["img_pump"])
                    delete_image(r["img_odo"])
                execute("DELETE FROM fills WHERE vehicle_id=?", (vid,))
                execute("DELETE FROM vehicles WHERE id=?", (vid,))
                publish_async()
                return self.send_json({"ok": True})

            if p == "/api/fills":
                if method == "GET":
                    return self.send_json(rows("SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts DESC, id DESC",
                                               (int(q.get("vehicle", 0)),)))
                if method == "POST":
                    b = self.read_json()
                    f = clean_fill(b)
                    if not get_vehicle(f["vehicle_id"]):
                        raise ValueError("Unknown vehicle")
                    if b.get("id"):
                        execute(f"UPDATE fills SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), int(b["id"])))
                        fid = int(b["id"])
                    else:
                        f["created"] = dt.datetime.now().isoformat(timespec="seconds")
                        fid = execute(f"INSERT INTO fills({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})", tuple(f.values()))
                    publish_async()
                    return self.send_json(rows("SELECT * FROM fills WHERE id=?", (fid,))[0])
            m = re.fullmatch(r"/api/fills/(\d+)", p)
            if m and method == "DELETE":
                r = rows("SELECT img_pump, img_odo FROM fills WHERE id=?", (int(m.group(1)),))
                if r:
                    delete_image(r[0]["img_pump"])
                    delete_image(r[0]["img_odo"])
                execute("DELETE FROM fills WHERE id=?", (int(m.group(1)),))
                publish_async()
                return self.send_json({"ok": True})

            if p == "/api/stats":
                v = get_vehicle(int(q.get("vehicle", 0)))
                if not v:
                    return self.send_json({"error": "Unknown vehicle"}, 404)
                return self.send_json(compute_stats(v))

            if p == "/api/extract" and method == "POST":
                b = self.read_json()
                kind = b.get("kind") if b.get("kind") in TASKS else "odo"
                context = ""
                prev = b.get("previous") or {}
                prev_odo = to_num(prev.get("odometer"))
                if kind == "odo" and prev_odo is not None:
                    unit = "miles" if prev.get("unit") == "mi" else "km"
                    when = str(prev.get("ts") or "")[:10]
                    context = (f"Context: the previous recorded reading for this vehicle was {prev_odo:g} {unit}"
                               + (f" on {when}" if when else "") + ". The new reading is normally equal to or somewhat higher "
                               "(usually by less than a few thousand). Use this to sanity-check the number of drums and any "
                               "partly hidden digit, but report what the drums actually show; if they clearly show something "
                               "else, say so in note.")
                img = b.get("image") or ""
                if "," in img:
                    img = img.split(",", 1)[1]
                raw = base64.b64decode(img)
                if raw[:2] != b"\xff\xd8":
                    raise ValueError("The image must be a JPEG")
                name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{kind}-{os.urandom(3).hex()}.jpg"
                with open(os.path.join(MEDIA_DIR, name), "wb") as f:
                    f.write(raw)
                t0 = time.time()
                data, err = ai_read(kind, name, context)
                log(f"Read {kind} in {time.time() - t0:.1f}s:", json.dumps(data, ensure_ascii=False), err or "")
                return self.send_json({"image": name, "data": data, "error": err})

            m = re.fullmatch(r"/api/img/([A-Za-z0-9_\-]+\.jpg)", p)
            if m:
                path = os.path.join(MEDIA_DIR, m.group(1))
                if not os.path.exists(path):
                    return self.send_error(404)
                with open(path, "rb") as f:
                    return self.send_bytes(f.read(), "image/jpeg", {"Cache-Control": "max-age=86400"})

            if p == "/api/export.csv":
                v = get_vehicle(int(q.get("vehicle", 0)))
                if not v:
                    return self.send_error(404)
                out = io.StringIO()
                w = csv.writer(out)
                cols = ["ts", "odometer", "liters", "price_total", "price_per_l", "full_tank",
                        "missed_previous", "fuel_type", "station", "note"]
                cur = get_settings()["currency"]
                w.writerow(["vehicle", "odometer_unit", "currency"] + cols)
                for r in rows("SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts, id", (v["id"],)):
                    w.writerow([v["name"], v["odo_unit"], cur] + ["" if r[c] is None else r[c] for c in cols])
                fn = slugify(v["name"]) + "-fuel.csv"
                return self.send_bytes(("﻿" + out.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                                       {"Content-Disposition": f'attachment; filename="{fn}"'})
            self.send_json({"error": "Not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log("Error", traceback.format_exc())
            self.send_json({"error": repr(e)}, 500)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


def main():
    init_db()
    cfg = get_settings()
    log(f"Fuel Tracker {VERSION} starting on port {PORT} - AI entity: {cfg['ai_task_entity']}, "
        f"token: {'yes' if TOKEN else 'MISSING'}")
    publish_async()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
