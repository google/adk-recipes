# ✈️ Travel Concierge — Beginner-Friendly ADK Tokenomics Example

This directory provides a clean, easy-to-follow **3-agent Travel Concierge** built with Google ADK (`concierge_router`, `flight_hotel_finder`, and `itinerary_planner`) in two side-by-side versions so you can test **`adk-tokenomics-plugin`** interactively in `adk web` or offline via CLI:

| Agent Package | Model Setup | ADK FinOps Optimizations |
| :--- | :--- | :--- |
| **`examples/travel_unoptimized/`** | `gemini-4-argon` on all 3 agents | Baseline anti-pattern: ~5,500-token inline manual, no `ContextCacheConfig`, no `EventsCompactionConfig`, and `ThinkingLevel.HIGH` everywhere |
| **`examples/travel_optimized/`** | `gemini-3.8-flash` (router + lookup) & `gemini-4-argon` (`itinerary_planner` only) | Applies all 5 profiler fixes: sub-agent model tiering, `ContextCacheConfig`, `EventsCompactionConfig`, on-demand `SkillToolset` (`japan-guide`, `europe-guide`), and right-sized `ThinkingLevel` tiers |

---

## 💬 Option 1: Chat Interactively in `adk web` + Live Token Control Tower

1. **Start the Token Control Tower Observatory** (Terminal 1):
   ```bash
   adk-tokenomics web --traces examples/artifacts/traces --port 8080
   ```
2. **Start Google ADK Web UI** (Terminal 2 — with `.env` set to your GCP project):
   ```bash
   adk web examples --port 8000
   ```
3. Pick **`travel_optimized`** (or **`travel_unoptimized`**) in `adk web` and ask any normal travel question:
   - *"Plan a 3-day trip to Tokyo from SFO under $2,000."*
   - *"Find flights, hotels under $200/night, and weather for Paris."*
   - *"Can we add a Kyoto day trip by bullet train and stay on budget?"*

Every turn you run in `adk web` automatically streams live into **Token Control Tower** (`http://127.0.0.1:8080`).

---

## ⚡ Option 2: Run the 1-Second Offline Demo (Zero Cloud Credentials Needed)

```bash
# 1. Generate Before vs. After session traces & run the profiler + CI budget gate
python3 examples/run_travel_demo.py --simulate

# 2. Inspect individual CLI commands on the generated traces
adk-tokenomics profile -t examples/artifacts/traces/travel_unoptimized.json
adk-tokenomics profile -t examples/artifacts/traces/travel_optimized.json

adk-tokenomics assert \
  -t examples/artifacts/traces/travel_optimized.json \
  --max-cost-per-turn 0.004 \
  --min-cache-rate 0.60 \
  --max-tokens-per-turn 3500

# 3. Open the visual observatory to compare both sessions side-by-side
adk-tokenomics web --traces examples/artifacts/traces --port 8080
```
