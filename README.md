# Edge Home Assistant

An offline smart-home brain. IoT devices (motion sensors, water/gas/electricity meters) send events to a tiny local server. It logs them, **learns your habits** with plain statistics, and when something happens out of habit it asks a **local Gemma model (via Ollama)** to judge it and phrase a friendly notification, then pushes it to **Telegram** (or FCM).

Everything runs on one machine. No cloud required. This is the prototype for the talk **"Letting a language model touch the real world, safely."**

> Scaling this to Google Cloud (Gemini, Cloud Run, connecting real home sensors, and cost/latency numbers): see **[CLOUD.md](CLOUD.md)**.

## The story it tells

You leave for university every weekday at 07:29 (the bus is at 07:31). After a few days, the house has *learned* that. So it does two things:

1. When you leave, the house drops into **energy-saving mode** (nobody is home, so why heat and light it).
2. If one morning there is **motion at 07:45**, when you should already be gone, it sends you a push: **"Someone is at home?"**

That is the whole idea. Simple to understand, and it solves a real problem: dumb automation wastes energy and never watches your back.

## What it does

- Accepts any IoT event over HTTP (`POST /ingest`).
- Logs everything to SQLite with a timestamp.
- Learns typical weekday **departure** and **return** times from motion history (median, pure statistics, explainable).
- Detects **motion while you should be away** and flags meter **peaks** (mean + 2*std).
- Asks local Gemma only to *judge and phrase* the alert. Returns strict JSON.
- Pushes the message to Telegram.

## Architecture (local)

```
  PIR / meters                FastAPI + SQLite                   local Gemma (Ollama)
 ┌────────────┐   HTTP POST  ┌──────────────────────────┐  JSON  ┌───────────────────┐
 │ hallway_pir │ ───────────▶│ /ingest  -> log event    │ ─────▶ │ judge + phrase the │
 │ power_meter │             │ habit engine (stats)     │        │ notification (JSON)│
 └────────────┘             │ /evaluate -> decision    │ ◀───── └───────────────────┘
                             └───────────┬──────────────┘
                                         │ push
                                         ▼
                                   Telegram / FCM
```

**The senior idea (say it on stage):** the deterministic code decides *that* something is off; the LLM only decides *how to say it*. The model never fires an alert on its own and never touches hardware. Policy loop, not control loop. Fail-safe by design (same instinct as a barrier that fails *up*).

## Quick start

```bash
cd smart-home-edge
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# terminal 1: run the server
venv/bin/python app.py            # or: venv/bin/uvicorn app:app --port 8000

# terminal 2: seed 10 weekdays of history + fire the 07:45 anomaly
venv/bin/python seed.py demo
```

You do **not** need Ollama to see it work. If Ollama is not running, a built-in fallback returns a valid decision, so the whole pipeline still runs (also a great stage safety net). To use the real model:

```bash
ollama pull gemma3:4b            # laptop; on a Raspberry Pi 5 use: ollama pull gemma4:e2b
# then just run seed.py again, the server will call it automatically
```

## What you will see (verified run)

The habit engine learned `typical_departure 07:30`, `typical_return 18:13` from 10 weekdays, then the 07:45 motion produced:

```json
{
  "situation": "motion_while_away",
  "ai": {
    "status": "SECURITY_ALERT",
    "severity": "medium",
    "title": "Someone is at home?",
    "message": "Motion at 07:45, but you usually leave by 07:30. Is everything ok?"
  }
}
```

Other times of day return `away_idle -> ENERGY_SAVING` (nobody home) or `present -> NONE`.

## Endpoints

| Method + path | What it does |
|---|---|
| `POST /ingest` | store one event `{device, type, value?, ts?}` |
| `GET /events?limit=` | recent raw events (debug) |
| `POST /evaluate` | run the decision now (body `{now?}` lets you pretend a time) |
| `GET /report` | natural-language daily briefing (habits + meter peaks) |

## The prompt and structured output

We force the model to answer with a fixed JSON schema (`format` field), so the reply parses straight into `switch(status)`. See `ALERT_SCHEMA` in `app.py`. Output enum: `NORMAL | ENERGY_SAVING | SECURITY_ALERT`, plus `severity`, `title`, `message`, `reasoning`.

**Expert gotcha for a slide:** on `gemma4`, setting `think=false` makes the `format` (structured output) constraint get silently ignored (Ollama issue [#15260](https://github.com/ollama/ollama/issues/15260)). Keep thinking on, or pin `gemma3` for reliable JSON.

## Connect a real sensor

**Raspberry Pi + HC-SR501 PIR (gpiozero):**
```python
from gpiozero import MotionSensor
import httpx
pir = MotionSensor(4)
while True:
    pir.wait_for_motion()
    httpx.post("http://SERVER:8000/ingest", json={"device": "hallway_pir", "type": "motion"})
    pir.wait_for_no_motion()
```

**ESP32 + PIR (Arduino):**
```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#define PIR 13
void setup(){ WiFi.begin("SSID","PASS"); pinMode(PIR, INPUT); }
void loop(){
  if (digitalRead(PIR) == HIGH){
    HTTPClient h; h.begin("http://SERVER:8000/ingest");
    h.addHeader("Content-Type","application/json");
    h.POST("{\"device\":\"hallway_pir\",\"type\":\"motion\"}");
    h.end(); delay(30000);
  }
}
```

**A meter reading** is just another event:
```bash
curl -X POST localhost:8000/ingest -H 'content-type: application/json' \
  -d '{"device":"power_meter","type":"electric","value":3.4}'
```

## Telegram push (2 minutes)

1. Message **@BotFather**, create a bot, copy the token.
2. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read your `chat.id`.
3. `export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...` and restart the server.

FCM is Firebase-native and needs a service account, so for a live stage demo Telegram is much easier. FCM shape is noted in `app.py` and the cloud version in [CLOUD.md](CLOUD.md) makes it trivial.

## Stage demo script (3 beats)

1. **Learn:** run `seed.py history`, hit `GET /report`, "look, it figured out I leave at 07:30 on its own."
2. **The money moment:** fire the 07:45 motion (`seed.py anomaly`), the push appears: *"Someone is at home?"*
3. **The leash:** show that a rule, not the model, decided to alert; the model only wrote the sentence. "The model is allowed to be wrong. The system is not."

Fallback: everything runs without Ollama and without Telegram, so nothing on stage can hard-fail.

## Files

- `app.py` — the whole server (ingest, habit engine, Gemma call, Telegram).
- `seed.py` — reproducible demo data + the anomaly.
- `requirements.txt`, `.env.example`, `.gitignore`.
- `CLOUD.md` — scaling to Google Cloud with Gemini, deployment, and real numbers.
