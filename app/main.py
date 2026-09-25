import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.getenv("LIVESTOCK_DB", "./livestock.db")
SERIAL_PORT = os.getenv("LORA_PORT", "/dev/serial0")
SERIAL_BAUD = int(os.getenv("LORA_BAUD", "9600"))
LORA_BAND = os.getenv("LORA_BAND", "868000000")
LORA_ADDRESS = os.getenv("LORA_ADDRESS", "2")
LORA_OPMODE = os.getenv("LORA_OPMODE", "1")

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"

BEHAVIORS = {
    0: "Feeding",
    1: "Rumination",
    2: "Standing",
    3: "Lying",
    4: "Walking",
}

db_lock = threading.Lock()
ws_clients = set()
ws_loop = None


def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    with con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS animals (
            animal_id TEXT PRIMARY KEY,
            name TEXT,
            species TEXT,
            breed TEXT,
            sex TEXT,
            birth_date TEXT,
            collar_address INTEGER UNIQUE,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS vaccinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id TEXT NOT NULL,
            vaccine TEXT NOT NULL,
            date TEXT,
            next_due TEXT,
            batch TEXT,
            notes TEXT,
            FOREIGN KEY(animal_id) REFERENCES animals(animal_id)
        );

        CREATE TABLE IF NOT EXISTS diagnoses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id TEXT NOT NULL,
            date TEXT,
            disease TEXT NOT NULL,
            diagnosis TEXT,
            treatment TEXT,
            veterinarian TEXT,
            notes TEXT,
            FOREIGN KEY(animal_id) REFERENCES animals(animal_id)
        );

        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            animal_id TEXT,
            collar_address INTEGER,
            packet_type TEXT,
            boot_count INTEGER,
            temperature REAL,
            latitude REAL,
            longitude REAL,
            behavior_id INTEGER,
            behavior TEXT,
            ax_mean REAL,
            ay_mean REAL,
            az_mean REAL,
            ax_sqsum REAL,
            ay_sqsum REAL,
            az_sqsum REAL,
            ax_std REAL,
            ay_std REAL,
            az_std REAL,
            rssi INTEGER,
            snr REAL,
            raw_payload TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_telemetry_animal_time
        ON telemetry(animal_id, received_at);

        CREATE TABLE IF NOT EXISTS geofence (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT,
            latitude REAL,
            longitude REAL,
            radius_m REAL
        );
        """)
    con.close()


def parse_rcv(line):
    # Reyax: +RCV=sender,length,payload,RSSI,SNR
    if not line.startswith("+RCV="):
        return None
    body = line[5:].strip()
    parts = body.split(",")
    if len(parts) < 5:
        return None

    try:
        sender = int(parts[0])
        length = int(parts[1])
        rssi = int(parts[-2])
        snr = float(parts[-1])
    except ValueError:
        return None

    payload = ",".join(parts[2:-2])
    return sender, length, payload, rssi, snr


def parse_payload(payload):
    packet_type = "LIVE"
    if payload.startswith("RET,"):
        packet_type = "RET"
        payload = payload[4:]
    elif payload.startswith("LIVE,"):
        packet_type = "LIVE"
        payload = payload[5:]

    p = payload.split(",")
    if len(p) < 14:
        return None

    def f(i):
        try:
            return float(p[i])
        except (ValueError, IndexError):
            return None

    def i(i):
        try:
            return int(float(p[i]))
        except (ValueError, IndexError):
            return None

    return {
        "packet_type": packet_type,
        "boot_count": i(0),
        "temperature": f(1),
        "latitude": f(2),
        "longitude": f(3),
        "behavior_id": i(4),
        "behavior": BEHAVIORS.get(i(4), "Unknown"),
        "ax_mean": f(5),
        "ay_mean": f(6),
        "az_mean": f(7),
        "ax_sqsum": f(8),
        "ay_sqsum": f(9),
        "az_sqsum": f(10),
        "ax_std": f(11),
        "ay_std": f(12),
        "az_std": f(13),
    }


def upsert_animal_from_collar(collar):
    con = db()
    row = con.execute(
        "SELECT animal_id FROM animals WHERE collar_address=?", (collar,)
    ).fetchone()
    if row:
        con.close()
        return row["animal_id"]

    animal_id = f"ANIMAL-{collar:03d}"
    try:
        con.execute(
            "INSERT INTO animals(animal_id, name, species, collar_address) VALUES(?,?,?,?)",
            (animal_id, animal_id, "Unknown", collar),
        )
        con.commit()
    except sqlite3.IntegrityError:
        pass
    con.close()
    return animal_id


def save_telemetry(collar, parsed, rssi, snr, raw):
    animal_id = upsert_animal_from_collar(collar)
    con = db()
    values = (
        animal_id, collar, parsed["packet_type"], parsed["boot_count"],
        parsed["temperature"], parsed["latitude"], parsed["longitude"],
        parsed["behavior_id"], parsed["behavior"],
        parsed["ax_mean"], parsed["ay_mean"], parsed["az_mean"],
        parsed["ax_sqsum"], parsed["ay_sqsum"], parsed["az_sqsum"],
        parsed["ax_std"], parsed["ay_std"], parsed["az_std"],
        rssi, snr, raw
    )
    with con:
        cur = con.execute("""
            INSERT INTO telemetry(
                animal_id, collar_address, packet_type, boot_count, temperature,
                latitude, longitude, behavior_id, behavior,
                ax_mean, ay_mean, az_mean,
                ax_sqsum, ay_sqsum, az_sqsum,
                ax_std, ay_std, az_std, rssi, snr, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, values)
    con.close()
    return animal_id, cur.lastrowid


async def broadcast(message):
    dead = []
    for ws in list(ws_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


def send_at(ser, command, wait=0.25):
    ser.write((command + "\r\n").encode("ascii"))
    ser.flush()
    time.sleep(wait)


def configure_rylr993(ser):
    # Same RYLR993 settings used by the supplied ESP32 receiver sketch.
    commands = [
        "AT",
        f"AT+OPMODE={LORA_OPMODE}",
        f"AT+BAND={LORA_BAND}",
        f"AT+ADDRESS={LORA_ADDRESS}",
    ]
    for command in commands:
        print("[RYLR993 TX]", command)
        send_at(ser, command)

    # Drain command responses so they cannot be mistaken for LoRa packets.
    end = time.time() + 0.5
    while time.time() < end:
        if ser.in_waiting:
            response = ser.readline().decode("utf-8", errors="replace").strip()
            if response:
                print("[RYLR993]", response)

    print("[RYLR993] Direct Pi gateway ready")


def serial_worker():
    try:
        import serial
    except ImportError:
        print("pyserial is not installed; LoRa receiver disabled.")
        return

    while True:
        try:
            print(f"[LORA] Opening {SERIAL_PORT} @ {SERIAL_BAUD} 8N1")
            ser = serial.Serial(
                port=SERIAL_PORT, baudrate=SERIAL_BAUD,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=1
            )
            configure_rylr993(ser)
            while True:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                packet = parse_rcv(line)
                if not packet:
                    continue

                sender, length, payload, rssi, snr = packet

                # ACK exactly like the ESP32 receiver.
                ack = f"AT+SEND={sender},3,ACK"
                print("[RYLR993 TX]", ack)
                send_at(ser, ack, wait=0.10)

                parsed = parse_payload(payload)
                if not parsed:
                    print("[LORA] Unparsed payload:", payload)
                    continue

                animal_id, row_id = save_telemetry(
                    sender, parsed, rssi, snr, payload
                )

                msg = {
                    "type": "telemetry",
                    "id": row_id,
                    "animal_id": animal_id,
                    "collar_address": sender,
                    "rssi": rssi,
                    "snr": snr,
                    **parsed,
                }

                if ws_loop:
                    asyncio.run_coroutine_threadsafe(
                        broadcast(msg), ws_loop
                    )

        except Exception as e:
            print("[LORA] Serial error:", e)
            time.sleep(3)


app = FastAPI(title="Livestock LoRa Monitor")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class AnimalIn(BaseModel):
    animal_id: str
    name: str = ""
    species: str = "Unknown"
    breed: str = ""
    sex: str = ""
    birth_date: str = ""
    collar_address: int | None = None
    notes: str = ""


class VaccinationIn(BaseModel):
    vaccine: str
    date: str = ""
    next_due: str = ""
    batch: str = ""
    notes: str = ""


class DiagnosisIn(BaseModel):
    date: str = ""
    disease: str
    diagnosis: str = ""
    treatment: str = ""
    veterinarian: str = ""
    notes: str = ""


@app.on_event("startup")
async def startup():
    global ws_loop
    init_db()
    ws_loop = asyncio.get_running_loop()
    threading.Thread(target=serial_worker, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/animals")
def animals():
    con = db()
    rows = con.execute("""
        SELECT a.*,
          (SELECT temperature FROM telemetry t WHERE t.animal_id=a.animal_id
             ORDER BY id DESC LIMIT 1) AS temperature,
          (SELECT latitude FROM telemetry t WHERE t.animal_id=a.animal_id
             ORDER BY id DESC LIMIT 1) AS latitude,
          (SELECT longitude FROM telemetry t WHERE t.animal_id=a.animal_id
             ORDER BY id DESC LIMIT 1) AS longitude,
          (SELECT behavior FROM telemetry t WHERE t.animal_id=a.animal_id
             ORDER BY id DESC LIMIT 1) AS behavior,
          (SELECT received_at FROM telemetry t WHERE t.animal_id=a.animal_id
             ORDER BY id DESC LIMIT 1) AS last_seen
        FROM animals a ORDER BY a.animal_id
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.post("/api/animals")
def add_animal(a: AnimalIn):
    con = db()
    try:
        with con:
            con.execute("""
                INSERT INTO animals(animal_id,name,species,breed,sex,birth_date,
                                     collar_address,notes)
                VALUES(?,?,?,?,?,?,?,?)
            """, tuple(a.model_dump().values()))
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, str(e))
    finally:
        con.close()
    return {"ok": True}


@app.get("/api/animals/{animal_id}")
def animal(animal_id: str):
    con = db()
    a = con.execute("SELECT * FROM animals WHERE animal_id=?", (animal_id,)).fetchone()
    if not a:
        raise HTTPException(404, "Animal not found")

    v = con.execute(
        "SELECT * FROM vaccinations WHERE animal_id=? ORDER BY date DESC",
        (animal_id,)
    ).fetchall()
    d = con.execute(
        "SELECT * FROM diagnoses WHERE animal_id=? ORDER BY date DESC",
        (animal_id,)
    ).fetchall()
    con.close()
    return {"animal": dict(a), "vaccinations": [dict(x) for x in v],
            "diagnoses": [dict(x) for x in d]}


@app.get("/api/animals/{animal_id}/telemetry")
def telemetry(animal_id: str, limit: int = 1000):
    limit = max(1, min(limit, 10000))
    con = db()
    rows = con.execute(
        "SELECT * FROM telemetry WHERE animal_id=? ORDER BY id DESC LIMIT ?",
        (animal_id, limit)
    ).fetchall()
    con.close()
    return [dict(r) for r in reversed(rows)]


@app.post("/api/animals/{animal_id}/vaccinations")
def add_vaccination(animal_id: str, v: VaccinationIn):
    con = db()
    with con:
        con.execute("""
            INSERT INTO vaccinations(animal_id,vaccine,date,next_due,batch,notes)
            VALUES(?,?,?,?,?,?)
        """, (animal_id, v.vaccine, v.date, v.next_due, v.batch, v.notes))
    con.close()
    return {"ok": True}


@app.post("/api/animals/{animal_id}/diagnoses")
def add_diagnosis(animal_id: str, d: DiagnosisIn):
    con = db()
    with con:
        con.execute("""
            INSERT INTO diagnoses(animal_id,date,disease,diagnosis,treatment,
                                   veterinarian,notes)
            VALUES(?,?,?,?,?,?,?)
        """, (animal_id, d.date, d.disease, d.diagnosis, d.treatment,
              d.veterinarian, d.notes))
    con.close()
    return {"ok": True}


@app.get("/api/map")
def map_data():
    con = db()
    rows = con.execute("""
        SELECT animal_id, latitude, longitude, behavior, temperature,
               received_at, collar_address
        FROM telemetry
        WHERE id IN (
          SELECT MAX(id) FROM telemetry
          WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          GROUP BY animal_id
        )
    """).fetchall()
    fence = con.execute("SELECT * FROM geofence WHERE id=1").fetchone()
    con.close()
    return {"animals": [dict(r) for r in rows],
            "geofence": dict(fence) if fence else None}


@app.get("/api/animals/{animal_id}/path")
def animal_path(animal_id: str, limit: int = 5000):
    limit = max(1, min(limit, 20000))
    con = db()
    rows = con.execute("""
        SELECT latitude,longitude,received_at
        FROM telemetry
        WHERE animal_id=? AND latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (animal_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/health")
def health():
    return {"ok": True, "lora_port": SERIAL_PORT, "baud": SERIAL_BAUD, "band": LORA_BAND, "address": LORA_ADDRESS, "opmode": LORA_OPMODE}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)
