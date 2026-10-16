# seed.py — make the demo reproducible.
#
#   python seed.py history   # ~10 weekdays of normal life -> engine learns "leaves ~07:29"
#   python seed.py anomaly   # motion at 07:45 on a weekday + calls /evaluate (the alert)
#   python seed.py demo      # both, in order (use this on stage)
#
# History is written straight to SQLite. The anomaly goes through the real HTTP API
# so you see the exact same path a real device would take.

import sys, os, sqlite3, datetime as dt, random
import httpx

DB   = os.getenv("DB_PATH", "home.db")
BASE = os.getenv("BASE", "http://localhost:8000")

def conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, device TEXT, type TEXT, value REAL DEFAULT 1)""")
    return c

def add(c, t, device, typ="motion", value=1):
    c.execute("INSERT INTO events(ts,device,type,value) VALUES(?,?,?,?)",
              (t.isoformat(timespec="seconds"), device, typ, value))

def last_weekday(d):
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d

def history(days=10):
    c = conn()
    today, made, back = dt.date.today(), 0, 1
    while made < days:
        day = today - dt.timedelta(days=back); back += 1
        if day.weekday() >= 5:            # weekdays only
            continue
        made += 1
        # morning routine, then leave at ~07:29 (+/- 2 min) — the bus is at 07:31
        wake = dt.datetime.combine(day, dt.time(6, 50)) + dt.timedelta(minutes=random.randint(-5, 5))
        for m in (0, 8, 20, 33, 39):
            add(c, wake + dt.timedelta(minutes=m), random.choice(["bedroom_pir", "kitchen_pir", "hallway_pir"]))
        leave = dt.datetime.combine(day, dt.time(7, 29)) + dt.timedelta(minutes=random.randint(-2, 2))
        add(c, leave, "hallway_pir")
        # away 07:31 -> ~18:10, then evening motion until ~23:00
        home = dt.datetime.combine(day, dt.time(18, 10)) + dt.timedelta(minutes=random.randint(-15, 20))
        for m in range(0, 5 * 60, 25):
            add(c, home + dt.timedelta(minutes=m), random.choice(["living_pir", "kitchen_pir", "bedroom_pir"]))
        # electricity meter, hourly, with an evening cooking/heating peak
        for h in range(24):
            base = 0.2 if 8 <= h < 17 else 1.0
            peak = 3.5 if h in (19, 20) else 0.0
            add(c, dt.datetime.combine(day, dt.time(h, 0)), "power_meter", "electric",
                round(base + peak + random.random(), 2))
    c.commit(); c.close()
    print(f"seeded {made} weekdays of history into {DB}")

def anomaly():
    day = last_weekday(dt.date.today())
    now = dt.datetime.combine(day, dt.time(7, 45))     # you should be gone by now
    httpx.post(f"{BASE}/ingest", json={"device": "hallway_pir", "type": "motion", "ts": now.isoformat()})
    r = httpx.post(f"{BASE}/evaluate", json={"now": now.isoformat()})
    print("\n--- /evaluate ---")
    print(__import__("json").dumps(r.json(), indent=2, ensure_ascii=False))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd in ("history", "demo"): history()
    if cmd in ("anomaly", "demo"): anomaly()
