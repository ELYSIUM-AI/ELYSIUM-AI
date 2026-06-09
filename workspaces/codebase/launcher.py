"""
Elysium Bot Launcher
Starts the Telegram bot (polling) alongside a minimal FastAPI health endpoint
so the platform can verify the container is alive.
"""
import asyncio
import threading
import uvicorn
from fastapi import FastAPI

# ── Health API ──────────────────────────────────────────────────────────────
health_app = FastAPI()

@health_app.get("/health")
async def health():
    return {"status": "ok", "bot": "elysium-v5"}

@health_app.get("/")
async def root():
    return {"status": "ok", "service": "Elysium AI Telegram Bot"}

def run_health_server():
    uvicorn.run(health_app, host="0.0.0.0", port=8000, log_level="warning")

# ── Bot entry ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start FastAPI health server in background thread
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    # Start the Telegram bot (blocking — runs forever)
    import bot_ultimate
    bot_ultimate.main()
