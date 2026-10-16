# Scaling to Google Cloud: Gemini, deployment, sensors, and the numbers

The local prototype (see [README.md](README.md)) runs entirely on the edge. This is the talk's second half: **when and how to bring Google Cloud in**, and what it actually costs and how fast it is. All figures are dated and sourced at the bottom (verified Sept 2026).

## Edge or cloud? (do not pick one, split the work)

| | Edge (Gemma on a Pi) | Cloud (Gemini on Vertex AI) |
|---|---|---|
| Reasoning latency | slow, ~5 to 15 s (small model, ~6 tok/s) | fast, ~1 to 1.5 s (TTFT ~0.4 s, ~200 tok/s) + network |
| Works with no internet | yes | no |
| Data leaves the home | no | yes (mitigate with EU region + no-training) |
| Cost per decision | ~$0 (electricity) | fraction of a cent |
| Model quality / multimodal | limited by the Pi | effectively unlimited |
| Fleet-wide learning across homes | weak | this is where cloud wins |

**The rule:** edge runs the fast, private, always-on loop and cheaply filters the 99% of events that are normal. Cloud does the hard reasoning, the multimodal heavy lifting, fleet-wide anomaly detection, remote access, and the push (FCM). Hybrid by design. Notice the twist: cloud Gemini is actually *faster and smarter* than the Pi; the Pi wins only on privacy, offline, and cost-at-high-frequency. Say that honestly on stage.

## Three ways to connect home sensors to Google

**A. Simplest: device -> HTTPS -> Cloud Run.** The exact same FastAPI app, containerized, scales to zero. Good for low event rates (a home). Device does an HTTPS POST to your Cloud Run URL instead of `localhost`.

```
sensors ──HTTPS POST──▶ Cloud Run (this app) ──▶ Firestore / BigQuery ──▶ Gemini ──▶ FCM
```

**B. Proper IoT fleet: device -> MQTT broker -> Pub/Sub -> BigQuery.** Google retired IoT Core in 2023, so you bring your own MQTT broker (EMQX, HiveMQ, or self-hosted Mosquitto on a small VM), which then forwards to Pub/Sub. Pub/Sub buffers, a Cloud Run push-subscriber or Dataflow processes, BigQuery stores.

```
sensors ──MQTT──▶ EMQX/HiveMQ ──▶ Pub/Sub ──▶ Cloud Run / Dataflow ──▶ BigQuery
```

**C. Firebase-first (nice for FCM): device -> Firestore -> Cloud Function -> Gemini -> FCM.** Because FCM is part of Firebase, the push you already wanted becomes one Admin SDK call. Good if you also want a mobile app.

## Gemini in Google Cloud

Two front doors:
- **Gemini API (Google AI Studio):** an API key, fastest to prototype.
- **Vertex AI Gemini:** IAM, VPC Service Controls, **EU data residency** (`europe-west*` regions), and inputs are **not used to train** the models. For a German smart-home privacy story this is the right answer, mention it, it is the GDPR-aware, senior detail.

**Same structured-output pattern, one env var changes.** This is already wired into `app.py`: there is one `decide(context)` interface and two backends, `ask_ollama()` (edge) and `ask_gemini()` (cloud). Gemini supports controlled generation (`response_mime_type="application/json"` + `response_schema`), so the exact same `ALERT_SCHEMA` is reused on both sides. Flip the backend with one variable, no code change:

```bash
# edge (default): local Gemma via Ollama
LLM_BACKEND=ollama venv/bin/python app.py

# cloud: Gemini on Vertex AI, EU region
LLM_BACKEND=gemini GCP_PROJECT=my-proj GCP_LOCATION=europe-west4 venv/bin/python app.py
```

The cloud backend, verbatim from `app.py` (needs `pip install -r requirements-cloud.txt`):

```python
def ask_gemini(context: dict) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)
    r = client.models.generate_content(
        model=GEMINI_MODEL,                          # gemini-2.5-flash-lite
        contents=json.dumps(context, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_ALERT,
            response_mime_type="application/json",
            response_schema=ALERT_SCHEMA,            # the SAME schema as the edge model
            temperature=0,
        ),
    )
    return json.loads(r.text)
```

If either backend is unreachable, `decide()` falls back to a deterministic message, so a demo never hard-fails.

## Deploy the app to Cloud Run

A `Dockerfile` is already included (it defaults the container to `LLM_BACKEND=gemini`). Deploy straight from source:

```bash
gcloud run deploy edge-home-assistant \
  --source . \
  --region europe-west4 \
  --allow-unauthenticated \
  --set-env-vars LLM_BACKEND=gemini,GCP_PROJECT=my-proj,GCP_LOCATION=europe-west4
```

Cloud Run scales to zero, so an idle home costs nothing. Swap SQLite for Firestore or Cloud SQL when you go multi-home (SQLite does not fit a stateless, scale-to-zero container).

## FCM push (closes your original loop)

```python
# firebase-admin: send the AI-written message straight to the phone
import firebase_admin
from firebase_admin import messaging
firebase_admin.initialize_app()

def push_fcm(device_token, title, body):
    messaging.send(messaging.Message(
        token=device_token,
        notification=messaging.Notification(title=title, body=body),
    ))
```

## The numbers (this is what the audience remembers)

**Assumptions:** one alert prompt is ~500 input + ~120 output tokens; a daily briefing ~800 + ~200. A home emits on the order of ~2,000 sensor events/day. Prices below are Gemini 2.5 Flash-Lite ($0.10 / 1M input, $0.40 / 1M output).

### Cost

| Scenario | Math | Cost |
|---|---|---|
| One Gemini decision | 500 in + 120 out | **~$0.0001** (a hundredth of a cent) |
| Smart pattern (edge filters, cloud only on ~3 anomalies + 1 report/day) | ~4 calls/day | **~$0.02 / home / month** |
| Smart pattern, 1,000 homes | | **~$17 / month** |
| Anti-pattern (send *every* event to Gemini) | ~2,000 calls/day | **~$6 / home / month** (~$6,000/month at 1,000 homes) |

That last row is the whole argument for the edge in one number: putting the LLM in the hot loop is ~300x more expensive. Let the cheap deterministic edge handle the 99% that is normal.

**Everything else is effectively free at home scale:**
- **Cloud Run** free tier: 2M requests + 180k vCPU-seconds + 360k GiB-seconds per month. One home is nowhere near it, so **$0**.
- **Pub/Sub:** first 10 GiB/month free. Home telemetry is tiny, **$0**.
- **BigQuery:** 10 GiB storage + 1 TiB queries/month free, **$0** for a home.
- Note: Gemini 2.5 Flash-Lite retires **Oct 16, 2026**; the next cheapest is Gemini 3.1 Flash-Lite at $0.25 / $1.50, roughly 3x, so ~$0.05/home/month on the smart pattern. Still a rounding error.

### Speed

| Step | Time |
|---|---|
| Edge decision (Gemma E2B on a Pi 5) | ~5 to 15 s, no network |
| Cloud decision (Gemini 2.5 Flash): time to first token | ~0.4 to 0.5 s |
| Cloud decision: full short JSON (~120 tokens @ ~200 tok/s) | ~0.6 to 1 s |
| Home to `europe-west` round trip + TLS | ~20 to 50 ms |
| **Cloud total** | **~1 to 1.5 s** (needs connectivity) |

So the honest takeaway: **cloud Gemini is roughly 10x faster and much smarter than the Pi, but it needs the internet and the data leaves the house.** Use the edge for privacy, offline resilience, and cheap high-frequency filtering; call Gemini for the hard, occasional reasoning and for learning across a whole fleet.

## One-slide takeaway

> Run the boring 99% on a $80 box at the edge: private, offline, ~$0. Escalate the interesting 1% to Gemini in the EU: ~1 second, a fraction of a cent, and it learns across every home. Same JSON schema on both sides. That is the pattern, the garden and the smart home are just where you saw it.

---

### Sources (verified Sept 2026)
- Gemini pricing: [Google Gemini API pricing 2026 (BenchLM)](https://benchlm.ai/google/api-pricing) · [Flash-Lite pricing & retirement (pricepertoken)](https://pricepertoken.com/pricing-page/model/google-gemini-2.5-flash-lite)
- Gemini latency/throughput: [Artificial Analysis: Gemini 2.5 Flash providers](https://artificialanalysis.ai/models/gemini-2-5-flash/providers) · [2026 LLM API latency benchmarks](https://www.kunalganglani.com/blog/llm-api-latency-benchmarks-2026)
- Cloud Run pricing/free tier: [Cloud Run pricing (Google)](https://cloud.google.com/run/pricing)
- IoT ingestion after IoT Core: [IoT telemetry on GCP with Pub/Sub + Dataflow (OneUptime)](https://oneuptime.com/blog/post/2026-02-17-how-to-build-an-iot-telemetry-pipeline-on-google-cloud-using-pubsub-and-dataflow-after-iot-core-retirement/view) · [MQTT broker bridge to Pub/Sub (HiveMQ)](https://www.hivemq.com/blog/best-alternative-google-cloud-iot-core-integrating-mqtt-data-with-gcp/)
- Edge model throughput on Pi 5: [Gemma on Raspberry Pi 5 benchmark](https://dev.to/kunal_d6a8fea2309e1571ee7/gemma-3-on-a-raspberry-pi-5-i-benchmarked-googles-open-model-on-a-80-computer-2026-3c0e)
