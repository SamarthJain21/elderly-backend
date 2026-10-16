from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import voice_api  # natural read-aloud voice
from app.routers import places_api  # address search
from app.routers import medicines_api  # medicine-name check
from app.routers import device_api  # the elder app's settings, managed by family too
from app.routers import sandbox_api  # 'Try with real AI' (own line: keeps feature branches from colliding)
from app.config import get_settings
from app.routers import alerts_api, approvals, insights_api, chat, checkins, elders, me, people, reminders, settings

# Our own INFO logs (e.g. "turn timings …") go to stdout → Cloud Run logs. Uvicorn configures only its own loggers.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per outbound request is noise

app = FastAPI(title="Elderly API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(me.router)
app.include_router(sandbox_api.router)
app.include_router(elders.router)
app.include_router(reminders.router)
app.include_router(chat.router)
app.include_router(voice_api.router)
app.include_router(places_api.router)
app.include_router(medicines_api.router)
app.include_router(device_api.router)
app.include_router(checkins.router)
app.include_router(settings.router)
app.include_router(approvals.router)
app.include_router(people.router)
app.include_router(alerts_api.router)
app.include_router(insights_api.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
