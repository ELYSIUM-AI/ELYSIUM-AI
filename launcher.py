"""
Elysium Bot Launcher
Starts a minimal FastAPI health endpoint in a background thread,
then runs the Telegram bot with its own asyncio event loop in the main thread.
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
    """Run FastAPI in its own thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8000, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())

# ── Main entry ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start health server in a background daemon thread (its own event loop)
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    # Run Telegram bot in the main thread with its own asyncio event loop
    import bot_ultimate
    bot_ultimate.main()
