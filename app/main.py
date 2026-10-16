from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import alerts_api, approvals, chat, checkins, elders, me, people, reminders, settings

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
app.include_router(elders.router)
app.include_router(reminders.router)
app.include_router(chat.router)
app.include_router(checkins.router)
app.include_router(settings.router)
app.include_router(approvals.router)
app.include_router(people.router)
app.include_router(alerts_api.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
