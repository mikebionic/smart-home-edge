# app.py — Edge Home Assistant (an offline smart-home brain)
#
# One file. IoT devices POST events here -> we log them to SQLite ->
# a deterministic "habit engine" learns when you are usually away ->
# if something happens out of habit (motion while you should be gone),
# we ask a LOCAL Gemma model (via Ollama) to judge it and phrase a
# friendly notification -> we push it to Telegram (or FCM).
#
# THE SENIOR IDEA (say this on stage):
#   The deterministic code decides *that* something is off.
#   The LLM only decides *how to say it*.
#   The model never fires an alert on its own and never touches hardware.
#   -> policy loop, not control loop. Fail-safe by design.

import os, json, sqlite3, statistics, datetime as dt
from contextlib import closing
from typing import Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------- config (all via env, with sane defaults) ----------------
DB_PATH        = os.getenv("DB_PATH", "home.db")
OLLAMA_URL     = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "gemma3:4b")     # edge: gemma4:e2b on a Raspberry Pi 5
LLM_BACKEND    = os.getenv("LLM_BACKEND", "ollama")        # "ollama" (edge) | "gemini" (cloud)
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GCP_PROJECT    = os.getenv("GCP_PROJECT", "")
GCP_LOCATION   = os.getenv("GCP_LOCATION", "europe-west4")  # EU region for GDPR / data residency
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
LOOKBACK_DAYS  = int(os.getenv("LOOKBACK_DAYS", "14"))
MIN_DAYS       = int(os.getenv("MIN_DAYS", "3"))           # need N weekdays before we trust a habit

app = FastAPI(title="Edge Home Assistant")

# ---------------- database ----------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT NOT NULL,      -- ISO8601 timestamp
            device TEXT NOT NULL,      -- hallway_pir, kitchen_pir, power_meter ...
            type   TEXT NOT NULL,      -- motion | electric | water | gas
            value  REAL DEFAULT 1      -- motion = 1, meters = the reading
        )""")
        c.commit()
init_db()

# ---------------- request model ----------------
class Event(BaseModel):
    device: str
    type: str = "motion"
    value: float = 1
    ts: Optional[str] = None          # optional explicit timestamp (handy for demos)

# ---------------- tiny time helpers ----------------
def parse(ts):            return dt.datetime.fromisoformat(ts)
def minute_of_day(t):     return t.hour * 60 + t.minute
def hhmm(m):              return f"{m // 60:02d}:{m % 60:02d}"
def to_min(s):            return int(s[:2]) * 60 + int(s[3:])   # "07:29" -> 449

# ---------------- endpoints: ingest + debug ----------------
@app.post("/ingest")
def ingest(e: Event):
    ts = e.ts or dt.datetime.now().isoformat(timespec="seconds")
    with closing(db()) as c:
        c.execute("INSERT INTO events(ts,device,type,value) VALUES(?,?,?,?)",
                  (ts, e.device, e.type, e.value))
        c.commit()
    return {"ok": True, "stored": {"ts": ts, "device": e.device, "type": e.type, "value": e.value}}

@app.get("/events")
def events(limit: int = 50):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# ---------------- deterministic habit engine ----------------
def learn_profile():
    """Learn typical weekday departure and return time from motion history. Pure statistics."""
    since = (dt.datetime.now() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    with closing(db()) as c:
        rows = c.execute("SELECT ts FROM events WHERE type='motion' AND ts>=? ORDER BY ts",
                         (since,)).fetchall()
    by_day = {}
    for r in rows:
        t = parse(r["ts"])
        if t.weekday() >= 5:          # skip Sat/Sun
            continue
        by_day.setdefault(t.date(), []).append(t)

    departures, returns = [], []
    for _, times in by_day.items():
        morning = [t for t in times if 4 <= t.hour < 12]
        evening = [t for t in times if t.hour >= 12]
        if morning: departures.append(minute_of_day(max(morning)))  # last motion before leaving
        if evening: returns.append(minute_of_day(min(evening)))     # first motion back

    n = len(by_day)
    return {
        "weekdays_observed": n,
        "confident":         n >= MIN_DAYS and len(departures) >= MIN_DAYS,
        "typical_departure": hhmm(int(statistics.median(departures))) if departures else None,
        "typical_return":    hhmm(int(statistics.median(returns)))    if returns    else None,
    }

def is_away(now, profile):
    """Are we inside the learned 'nobody home' window on a weekday?"""
    if not (profile["confident"] and profile["typical_departure"] and profile["typical_return"]):
        return False
    if now.weekday() >= 5:
        return False
    dep, ret, m = to_min(profile["typical_departure"]), to_min(profile["typical_return"]), minute_of_day(now)
    return dep + 10 <= m <= ret - 10        # 10-min buffer so we don't alert on the way out/in

def meter_peaks():
    """Flag readings above mean + 2*std for each meter type. Simple, explainable peak detection."""
    since = (dt.datetime.now() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    out = {}
    with closing(db()) as c:
        for typ in ("electric", "water", "gas"):
            rows = c.execute("SELECT ts,value FROM events WHERE type=? AND ts>=? ORDER BY ts",
                             (typ, since)).fetchall()
            vals = [r["value"] for r in rows]
            if len(vals) < 5:
                continue
            mean, sd = statistics.mean(vals), (statistics.pstdev(vals) or 1)
            peaks = [{"ts": r["ts"], "value": r["value"]} for r in rows if r["value"] > mean + 2 * sd]
            out[typ] = {"mean": round(mean, 2), "peak_threshold": round(mean + 2 * sd, 2),
                        "recent_peaks": peaks[-3:]}
    return out

# ---------------- the local LLM (Ollama + Gemma) ----------------
# We force a JSON schema so the reply parses straight into code (constrained decoding).
ALERT_SCHEMA = {
    "type": "object",
    "properties": {
        "status":    {"type": "string", "enum": ["NORMAL", "ENERGY_SAVING", "SECURITY_ALERT"]},
        "severity":  {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "title":     {"type": "string"},
        "message":   {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["status", "severity", "title", "message", "reasoning"],
}

SYSTEM_ALERT = (
    "You are the on-device security and efficiency assistant of a private smart home. "
    "You are given the residents' learned habit profile and the current situation. "
    "Decide the status and write a short, friendly owner notification. Use ONLY facts "
    "from the data. Keep 'message' under 180 characters. Return JSON only."
)

def fallback_decision(context: dict) -> dict:
    # DEAD-DEMO FALLBACK: if no LLM is reachable, still return a valid decision so the
    # whole pipeline works with zero dependencies (also a great stage safety net).
    away = context.get("situation") == "motion_while_away"
    return {
        "status":   "SECURITY_ALERT" if away else "NORMAL",
        "severity": "medium" if away else "none",
        "title":    "Someone is at home?" if away else "All normal",
        "message":  (f"Motion at {context.get('trigger', {}).get('time', '?')}, but you usually "
                     f"leave by {context.get('profile', {}).get('typical_departure', '?')}. "
                     f"Is everything ok?") if away else "No anomalies detected.",
        "reasoning": "deterministic fallback",
    }

def ask_ollama(context: dict) -> dict:
    """EDGE backend: local Gemma via Ollama, JSON-schema constrained decoding."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_ALERT},
            {"role": "user",   "content": json.dumps(context, ensure_ascii=False)},
        ],
        "format": ALERT_SCHEMA,          # <- guarantees valid, parseable JSON
        "stream": False,
        "options": {"temperature": 0},
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])

def ask_gemini(context: dict) -> dict:
    """CLOUD backend: Gemini on Vertex AI (EU region). SAME ALERT_SCHEMA, controlled generation."""
    from google import genai               # pip install google-genai  (see requirements-cloud.txt)
    from google.genai import types
    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)
    r = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=json.dumps(context, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_ALERT,
            response_mime_type="application/json",
            response_schema=ALERT_SCHEMA,    # <- the SAME schema as the edge model
            temperature=0,
        ),
    )
    return json.loads(r.text)

def decide(context: dict) -> dict:
    """One interface, two backends: edge Ollama or cloud Gemini. Falls back offline."""
    try:
        return ask_gemini(context) if LLM_BACKEND == "gemini" else ask_ollama(context)
    except Exception as ex:
        d = fallback_decision(context)
        d["reasoning"] = f"{d['reasoning']} [backend={LLM_BACKEND} unavailable: {ex}]"
        return d

# ---------------- notifications ----------------
def push_telegram(title, message):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return {"sent": False, "reason": "telegram not configured (set TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)"}
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      params={"chat_id": TELEGRAM_CHAT, "text": f"\U0001F3E0 {title}\n{message}"}, timeout=15)
        return {"sent": r.status_code == 200}
    except Exception as ex:
        return {"sent": False, "reason": str(ex)}

# FCM (Firebase Cloud Messaging) — example only. HTTP v1 needs an OAuth2 access token from a
# service account. For a live stage demo, Telegram above is far easier. Shape for reference:
#   POST https://fcm.googleapis.com/v1/projects/<PROJECT_ID>/messages:send
#   Authorization: Bearer <access_token>
#   {"message": {"token": "<device_token>", "notification": {"title": ..., "body": ...}}}

# ---------------- the decision endpoint ----------------
class EvalReq(BaseModel):
    now: Optional[str] = None            # pretend it is this time (for demos, e.g. "...T07:45")

@app.post("/evaluate")
def evaluate(req: Optional[EvalReq] = None):
    req = req or EvalReq()
    now = parse(req.now) if req.now else dt.datetime.now()
    profile = learn_profile()
    away = is_away(now, profile)

    with closing(db()) as c:
        row = c.execute("SELECT * FROM events WHERE type='motion' AND ts<=? ORDER BY ts DESC LIMIT 1",
                        (now.isoformat(),)).fetchone()
    trigger = dict(row) if row else None
    recent_motion = bool(trigger) and (now - parse(trigger["ts"])).total_seconds() < 600  # last 10 min

    if away and recent_motion:
        situation = "motion_while_away"
    elif away:
        situation = "away_idle"
    else:
        situation = "present"

    context = {
        "home": "demo-flat-Hamburg",
        "now": now.isoformat(timespec="minutes"),
        "weekday": now.strftime("%A"),
        "profile": profile,
        "situation": situation,
        "trigger": ({"device": trigger["device"], "time": parse(trigger["ts"]).strftime("%H:%M")}
                    if trigger else None),
        "meter_peaks": meter_peaks(),
    }

    # Deterministic gate: we only escalate to the LLM when the rule says "this is off".
    if situation == "motion_while_away":
        ai = decide(context)                         # LLM only phrases it (edge or cloud)
        note = push_telegram(ai["title"], ai["message"])
        return {"situation": situation, "notified": note, "ai": ai, "context": context}

    if situation == "away_idle":
        return {"situation": situation, "action": "ENERGY_SAVING",
                "note": "Nobody home during the learned away-window -> switch to energy saving mode.",
                "context": context}

    return {"situation": situation, "action": "NONE", "context": context}

# ---------------- daily briefing (natural language) ----------------
@app.get("/report")
def report():
    profile, peaks = learn_profile(), meter_peaks()
    context = {"profile": profile, "meter_peaks": peaks}
    system = ("You are a home efficiency assistant. In 3 short, friendly bullet points, summarize "
              "the household's daily habits and any energy/water/gas peaks for the owner.")
    payload = {"model": OLLAMA_MODEL,
               "messages": [{"role": "system", "content": system},
                            {"role": "user",   "content": json.dumps(context, ensure_ascii=False)}],
               "stream": False, "options": {"temperature": 0.3}}
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60); r.raise_for_status()
        text = r.json()["message"]["content"]
    except Exception as ex:
        text = (f"(offline) Habit: leaves ~{profile.get('typical_departure')}, "
                f"returns ~{profile.get('typical_return')}. Meter peaks: {list(peaks)}. ({ex})")
    return {"briefing": text, "profile": profile, "peaks": peaks}

# ---------------- run directly: python app.py ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
