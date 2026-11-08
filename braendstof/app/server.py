#!/usr/bin/env python3
"""Brændstof – benzinlog for jeeperne (Home Assistant app/add-on).

Kun Pythons standardbibliotek. Data i SQLite (/data/braendstof.db),
billeder i /media/braendstof (så HA's AI Task kan læse dem som media-source).
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DATA_DIR = os.environ.get("BS_DATA_DIR", "/data")
MEDIA_ROOT = os.environ.get("BS_MEDIA_ROOT", "/media")
MEDIA_SUB = "braendstof"
MEDIA_DIR = os.path.join(MEDIA_ROOT, MEDIA_SUB)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HA_API = os.environ.get("BS_HA_API", "http://supervisor/core/api")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
PORT = int(os.environ.get("BS_PORT", "8099"))
DEV = os.environ.get("BS_DEV") == "1"
KM_PER_MI = 1.609344
CO2_KG_PER_L = 2.31  # ca. for benzin (forbrænding)

os.environ.setdefault("TZ", "Europe/Copenhagen")
try:
    time.tzset()
except AttributeError:
    pass


def load_options():
    try:
        with open(os.path.join(DATA_DIR, "options.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


OPTIONS = load_options()
AI_ENTITY = OPTIONS.get("ai_task_entity") or "ai_task.claude_ai_task"
PUBLISH = OPTIONS.get("publish_sensors", True)

DB_LOCK = threading.Lock()


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- database

def db():
    con = sqlite3.connect(os.path.join(DATA_DIR, "braendstof.db"))
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
                odo_rollover REAL NOT NULL DEFAULT 100000,
                tank_l REAL,
                active INTEGER NOT NULL DEFAULT 1,
                sort INTEGER NOT NULL DEFAULT 0
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
        n = con.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        if n == 0:
            con.execute("INSERT INTO vehicles(name, sort) VALUES ('CJ-3B 1955', 1)")
            con.execute("INSERT INTO vehicles(name, sort) VALUES ('Willys Truck 1958', 2)")


def rows(sql, args=()):
    with DB_LOCK, db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with DB_LOCK, db() as con:
        cur = con.execute(sql, args)
        return cur.lastrowid


def get_vehicle(vid):
    r = rows("SELECT * FROM vehicles WHERE id=?", (vid,))
    return r[0] if r else None


# ---------------------------------------------------------------- HA API

def ha_request(method, path, payload=None, timeout=90):
    url = HA_API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body else None


PUMP_INSTRUCTIONS = """Billedet viser displayet på en tankstander (benzinpumpe) i Danmark.
Aflæs:
- liters: antal liter der er tanket (feltet hedder typisk "Liter", "Volumen" eller "Ltr").
- total_price: beløbet i kr. der skal betales (typisk "Beløb", "Kr", "Pris" eller "Total").
- price_per_liter: literprisen i kr. (typisk "Kr/L", "Pris pr. liter").
- fuel_type: brændstoftype hvis den kan ses (fx "Blyfri 95", "E10", "Diesel"), ellers tom.
Danske displays bruger komma som decimaltegn: "38,47" betyder 38.47. Returner tal med punktum.
Er et tal ikke læseligt, så udelad feltet hellere end at gætte.
Skriv i note (på dansk, kort) hvis noget var svært at læse eller tallene ikke passer sammen."""

ODO_INSTRUCTIONS = """Billedet viser speedometer/km-tæller (odometer) på en gammel Willys jeep (1950'erne).
Tælleren er typisk mekaniske tromler. Aflæs den samlede kilometerstand (IKKE en tripmåler).
- reading: tallet på tælleren som et tal. Hvis den yderste højre tromle tydeligvis er tiendedele
  (anden farve, ofte hvid/rød baggrund), så medtag den som decimal (fx 45678.3) og sæt has_tenths=true.
- unit: "km" hvis skiven siger km/KILOMETER, "miles" hvis den siger MILES/MPH, ellers "ukendt".
Hvis en tromle står midt mellem to tal, så brug det laveste tal.
Udelad reading hvis tallet ikke kan læses sikkert. Skriv i note (på dansk, kort) om usikkerheder."""

NUM = {"number": {"mode": "box", "step": "any"}}
TXT = {"text": {}}
BOOL = {"boolean": {}}

PUMP_STRUCTURE = {
    "liters": {"selector": NUM, "description": "Antal liter tanket"},
    "total_price": {"selector": NUM, "description": "Beløb i kr."},
    "price_per_liter": {"selector": NUM, "description": "Literpris i kr."},
    "fuel_type": {"selector": TXT, "description": "Brændstoftype"},
    "note": {"selector": TXT, "description": "Kort note om usikkerheder"},
}
ODO_STRUCTURE = {
    "reading": {"selector": NUM, "description": "Kilometerstand på tælleren"},
    "has_tenths": {"selector": BOOL, "description": "Om reading indeholder tiendedele"},
    "unit": {"selector": TXT, "description": "km, miles eller ukendt"},
    "note": {"selector": TXT, "description": "Kort note om usikkerheder"},
}


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def ai_read(kind, filename):
    instructions = PUMP_INSTRUCTIONS if kind == "pump" else ODO_INSTRUCTIONS
    structure = PUMP_STRUCTURE if kind == "pump" else ODO_STRUCTURE
    attachment = {
        "media_content_id": f"media-source://media_source/local/{MEDIA_SUB}/{filename}",
        "media_content_type": "image/jpeg",
    }
    base = {
        "task_name": "Aflæs tankdisplay" if kind == "pump" else "Aflæs km-tæller",
        "entity_id": AI_ENTITY,
        "attachments": [attachment],
    }
    path = "/services/ai_task/generate_data?return_response"
    try:
        res = ha_request("POST", path, dict(base, instructions=instructions, structure=structure))
        data = (res or {}).get("service_response", res or {}).get("data")
        if isinstance(data, str):
            data = _extract_json(data)
        return data or {}, None
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:500]
        log("AI med struktur fejlede", e.code, err, "– prøver med fri tekst")
    except Exception as e:  # netværk osv.
        log("AI-fejl", repr(e))
        return {}, str(e)
    # Fald tilbage: bed om JSON som tekst
    try:
        keys = ", ".join(structure.keys())
        res = ha_request(
            "POST", path,
            dict(base, instructions=instructions + f"\nSvar KUN med ét JSON-objekt med nøglerne: {keys}."),
        )
        data = (res or {}).get("service_response", res or {}).get("data")
        if isinstance(data, dict):
            return data, None
        return _extract_json(str(data)), None
    except urllib.error.HTTPError as e:
        return {}, f"HA svarede {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return {}, str(e)


def to_num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.\-]", "", s))
    except ValueError:
        return None


# ---------------------------------------------------------------- statistik

def slugify(name):
    s = name.lower()
    for a, b in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "bil"


def compute_stats(vehicle):
    fills = rows(
        "SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts, id", (vehicle["id"],)
    )
    factor = KM_PER_MI if vehicle["odo_unit"] == "mi" else 1.0
    roll = vehicle["odo_rollover"] or 0
    offset = 0.0
    prev_raw = None
    prev_cum = None
    for f in fills:
        f["cum_km"] = None
        f["dist_km"] = None
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

    segments = []
    last_full_cum = None
    acc_l = acc_kr = 0.0
    acc_complete = True
    for f in fills:
        if f["missed_previous"]:
            last_full_cum = None
            acc_l = acc_kr = 0.0
        acc_l += f["liters"] or 0
        if f["price_total"] is not None:
            acc_kr += f["price_total"]
        else:
            acc_complete = False
        if f["full_tank"] and f["cum_km"] is not None:
            if last_full_cum is not None:
                dist = f["cum_km"] - last_full_cum
                if dist > 0 and acc_l > 0:
                    segments.append({
                        "fill_id": f["id"],
                        "ts": f["ts"],
                        "km": dist,
                        "liters": acc_l,
                        "kr": acc_kr if acc_complete else None,
                        "km_per_l": dist / acc_l,
                        "l_per_100km": acc_l / dist * 100,
                        "kr_per_km": (acc_kr / dist) if acc_complete else None,
                    })
            last_full_cum = f["cum_km"]
            acc_l = acc_kr = 0.0
            acc_complete = True

    seg_km = sum(s["km"] for s in segments)
    seg_l = sum(s["liters"] for s in segments)
    seg_kr_list = [s for s in segments if s["kr"] is not None]
    seg_kr = sum(s["kr"] for s in seg_kr_list)
    seg_kr_km = sum(s["km"] for s in seg_kr_list)

    odo_fills = [f for f in fills if f["cum_km"] is not None]
    tracked_km = (odo_fills[-1]["cum_km"] - odo_fills[0]["cum_km"]) if len(odo_fills) > 1 else 0
    total_l = sum(f["liters"] or 0 for f in fills)
    total_kr = sum(f["price_total"] or 0 for f in fills)
    prices = [
        {"ts": f["ts"], "kr_per_l": f["price_per_l"]}
        for f in fills if f["price_per_l"]
    ]

    # pr. måned og år
    def bucket(key_len):
        out = {}
        for f in fills:
            k = f["ts"][:key_len]
            b = out.setdefault(k, {"periode": k, "km": 0.0, "liters": 0.0, "kr": 0.0, "fills": 0})
            b["liters"] += f["liters"] or 0
            b["kr"] += f["price_total"] or 0
            b["fills"] += 1
            if f["dist_km"]:
                b["km"] += f["dist_km"]
        for s in segments:
            k = s["ts"][:key_len]
            b = out[k]
            b.setdefault("_sk", 0.0)
            b.setdefault("_sl", 0.0)
            b["_sk"] += s["km"]
            b["_sl"] += s["liters"]
        res = []
        for k in sorted(out):
            b = out[k]
            sk, sl = b.pop("_sk", 0), b.pop("_sl", 0)
            b["km_per_l"] = sk / sl if sl else None
            res.append(b)
        return res

    months = bucket(7)
    years = bucket(4)

    last = fills[-1] if fills else None
    now = dt.datetime.now()
    days_since = None
    if last:
        try:
            days_since = (now - dt.datetime.fromisoformat(last["ts"])).days
        except ValueError:
            pass
    avg_kmpl = seg_km / seg_l if seg_l else None
    this_year = now.strftime("%Y")
    year_kr = sum(f["price_total"] or 0 for f in fills if f["ts"].startswith(this_year))
    year_km = sum(f["dist_km"] or 0 for f in fills if f["ts"].startswith(this_year))

    first_ts = fills[0]["ts"] if fills else None
    span_days = None
    if first_ts and last:
        try:
            span_days = max(1, (dt.datetime.fromisoformat(last["ts"]) - dt.datetime.fromisoformat(first_ts)).days)
        except ValueError:
            pass

    return {
        "vehicle": vehicle,
        "count": len(fills),
        "tracked_km": tracked_km,
        "total_liters": total_l,
        "total_kr": total_kr,
        "avg_km_per_l": avg_kmpl,
        "avg_l_per_100km": (seg_l / seg_km * 100) if seg_km else None,
        "avg_us_mpg": avg_kmpl * 2.352145 if avg_kmpl else None,
        "avg_kr_per_km": (seg_kr / seg_kr_km) if seg_kr_km else None,
        "avg_kr_per_l": (sum(p["kr_per_l"] for p in prices) / len(prices)) if prices else None,
        "last_kr_per_l": prices[-1]["kr_per_l"] if prices else None,
        "last_segment": segments[-1] if segments else None,
        "best_segment": max(segments, key=lambda s: s["km_per_l"]) if segments else None,
        "worst_segment": min(segments, key=lambda s: s["km_per_l"]) if segments else None,
        "last_fill": last,
        "last_odometer": odo_fills[-1]["odometer"] if odo_fills else None,
        "last_cum_km": odo_fills[-1]["cum_km"] if odo_fills else None,
        "days_since_last": days_since,
        "avg_km_per_fill": (tracked_km / (len(odo_fills) - 1)) if len(odo_fills) > 1 else None,
        "km_per_month": (tracked_km / span_days * 30.44) if span_days and tracked_km else None,
        "range_km": (vehicle["tank_l"] * avg_kmpl) if (vehicle.get("tank_l") and avg_kmpl) else None,
        "co2_kg": total_l * CO2_KG_PER_L,
        "year": this_year,
        "year_kr": year_kr,
        "year_km": year_km,
        "segments": segments,
        "prices": prices,
        "months": months,
        "years": years,
    }


def publish_sensors():
    if not PUBLISH or not TOKEN:
        return
    for v in rows("SELECT * FROM vehicles WHERE active=1"):
        try:
            st = compute_stats(v)
        except Exception:
            log("Stat-fejl", traceback.format_exc())
            continue
        slug = slugify(v["name"])
        unit_odo = "mi" if v["odo_unit"] == "mi" else "km"
        last_seg = st["last_segment"]
        sensors = {
            "km_pr_liter": (last_seg["km_per_l"] if last_seg else None, "km/L", "mdi:gas-station", "Seneste km/L", {}),
            "km_pr_liter_gns": (st["avg_km_per_l"], "km/L", "mdi:gas-station-outline", "Gns. km/L", {}),
            "kilometertaeller": (st["last_odometer"], unit_odo, "mdi:counter", "Km-tæller ved seneste tankning", {}),
            "kr_pr_km": (st["avg_kr_per_km"], "kr/km", "mdi:cash", "Gns. kr/km", {}),
            "literpris": (st["last_kr_per_l"], "kr/L", "mdi:currency-usd", "Seneste literpris", {}),
            "udgift_i_aar": (st["year_kr"], "kr", "mdi:cash-multiple", f"Brændstof {st['year']}", {}),
            "sidst_tanket": (st["last_fill"]["ts"] if st["last_fill"] else None, None, "mdi:calendar", "Sidst tanket", {}),
        }
        for key, (val, unit, icon, label, extra) in sensors.items():
            if isinstance(val, float):
                val = round(val, 2)
            state = "unknown" if val is None else val
            attrs = {"friendly_name": f"{v['name']} {label}", "icon": icon}
            if unit:
                attrs["unit_of_measurement"] = unit
                attrs["state_class"] = "measurement"
            if key == "sidst_tanket" and val:
                attrs["device_class"] = "timestamp"
                try:
                    state = dt.datetime.fromisoformat(val).astimezone().isoformat()
                except ValueError:
                    pass
            attrs.update(extra)
            try:
                ha_request("POST", f"/states/sensor.braendstof_{slug}_{key}", {"state": state, "attributes": attrs}, timeout=10)
            except Exception as e:
                log("Kunne ikke opdatere sensor", key, repr(e))
                return


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
    f["vehicle_id"] = int(f["vehicle_id"])
    if not f["liters"] or f["liters"] <= 0:
        raise ValueError("Antal liter mangler")
    if f["price_total"] is None and f["price_per_l"]:
        f["price_total"] = round(f["price_per_l"] * f["liters"], 2)
    if f["price_per_l"] is None and f["price_total"]:
        f["price_per_l"] = round(f["price_total"] / f["liters"], 3)
    ts = (f["ts"] or "").strip()
    if not ts:
        ts = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    dt.datetime.fromisoformat(ts)  # validér
    f["ts"] = ts[:16]
    for k in ("ai_pump", "ai_odo"):
        if f[k] is not None and not isinstance(f[k], str):
            f[k] = json.dumps(f[k], ensure_ascii=False)
    for k in ("img_pump", "img_odo"):
        if f[k] and not re.fullmatch(r"[A-Za-z0-9_\-]+\.jpg", f[k]):
            f[k] = None
    return f


class Handler(BaseHTTPRequestHandler):
    server_version = "Braendstof/0.1"

    def log_message(self, fmt, *args):
        if DEV:
            log(self.address_string(), fmt % args)

    def _allowed(self):
        if DEV:
            return True
        return self.client_address[0] in ("172.30.32.2", "127.0.0.1")

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
            raise ValueError("For stor forespørgsel")
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
                return self.send_json({"ai_entity": AI_ENTITY, "has_token": bool(TOKEN), "version": "0.1.0"})
            if p == "/api/vehicles":
                if method == "GET":
                    return self.send_json(rows("SELECT * FROM vehicles ORDER BY active DESC, sort, id"))
                if method == "POST":
                    b = self.read_json()
                    name = (b.get("name") or "").strip()
                    if not name:
                        raise ValueError("Navn mangler")
                    unit = "mi" if b.get("odo_unit") == "mi" else "km"
                    roll = to_num(b.get("odo_rollover"))
                    roll = 0 if roll is None else roll
                    tank = to_num(b.get("tank_l"))
                    active = 0 if b.get("active") in (False, 0, "0") else 1
                    if b.get("id"):
                        execute("UPDATE vehicles SET name=?, odo_unit=?, odo_rollover=?, tank_l=?, active=? WHERE id=?",
                                (name, unit, roll, tank, active, int(b["id"])))
                        vid = int(b["id"])
                    else:
                        vid = execute("INSERT INTO vehicles(name, odo_unit, odo_rollover, tank_l, active, sort) VALUES (?,?,?,?,?,99)",
                                      (name, unit, roll, tank, active))
                    publish_async()
                    return self.send_json(get_vehicle(vid))
            if p == "/api/fills":
                if method == "GET":
                    vid = int(q.get("vehicle", 0))
                    return self.send_json(rows("SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts DESC, id DESC", (vid,)))
                if method == "POST":
                    b = self.read_json()
                    f = clean_fill(b)
                    if not get_vehicle(f["vehicle_id"]):
                        raise ValueError("Ukendt bil")
                    if b.get("id"):
                        sets = ", ".join(f"{k}=?" for k in f)
                        execute(f"UPDATE fills SET {sets} WHERE id=?", (*f.values(), int(b["id"])))
                        fid = int(b["id"])
                    else:
                        f["created"] = dt.datetime.now().isoformat(timespec="seconds")
                        cols = ", ".join(f)
                        marks = ", ".join("?" for _ in f)
                        fid = execute(f"INSERT INTO fills({cols}) VALUES ({marks})", tuple(f.values()))
                    publish_async()
                    return self.send_json(rows("SELECT * FROM fills WHERE id=?", (fid,))[0])
            m = re.fullmatch(r"/api/fills/(\d+)", p)
            if m and method == "DELETE":
                execute("DELETE FROM fills WHERE id=?", (int(m.group(1)),))
                publish_async()
                return self.send_json({"ok": True})
            if p == "/api/stats":
                v = get_vehicle(int(q.get("vehicle", 0)))
                if not v:
                    return self.send_json({"error": "Ukendt bil"}, 404)
                return self.send_json(compute_stats(v))
            if p == "/api/extract" and method == "POST":
                b = self.read_json()
                kind = "pump" if b.get("kind") == "pump" else "odo"
                img = b.get("image") or ""
                if "," in img:
                    img = img.split(",", 1)[1]
                raw = base64.b64decode(img)
                if raw[:2] != b"\xff\xd8":
                    raise ValueError("Billedet skal være JPEG")
                name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{kind}-{os.urandom(3).hex()}.jpg"
                with open(os.path.join(MEDIA_DIR, name), "wb") as f:
                    f.write(raw)
                t0 = time.time()
                data, err = ai_read(kind, name)
                log(f"Aflæst {kind} på {time.time() - t0:.1f}s:", json.dumps(data, ensure_ascii=False), err or "")
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
                w = csv.writer(out, delimiter=";")
                cols = ["ts", "odometer", "liters", "price_total", "price_per_l", "full_tank",
                        "missed_previous", "fuel_type", "station", "note"]
                w.writerow(["bil"] + cols)
                for r in rows("SELECT * FROM fills WHERE vehicle_id=? ORDER BY ts, id", (v["id"],)):
                    w.writerow([v["name"]] + [("" if r[c] is None else str(r[c]).replace(".", ",") if isinstance(r[c], float) else r[c]) for c in cols])
                data = ("﻿" + out.getvalue()).encode("utf-8")
                fn = slugify(v["name"]) + "-braendstof.csv"
                return self.send_bytes(data, "text/csv; charset=utf-8",
                                       {"Content-Disposition": f'attachment; filename="{fn}"'})
            self.send_json({"error": "Ikke fundet"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log("Fejl", traceback.format_exc())
            self.send_json({"error": repr(e)}, 500)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


def main():
    init_db()
    log(f"Brændstof starter på port {PORT} – AI-entitet: {AI_ENTITY}")
    publish_async()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
