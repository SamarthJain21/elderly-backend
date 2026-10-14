"""Runs a tool the agent asked for: quote → autonomy decision → execute / ask elder / ask caretaker / refuse.

Every outcome returns a small dict for the model to phrase, plus a deterministic `say` fallback
used if the model can't be reached after side effects already happened.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from app import checkin_store
from app.autonomy import AutonomyRules, decide, format_money
from app.checkin_store import LogEntry
from app.agent import family, live
from app.agent.family import FamilyMessage
from app.tools import actions, providers
from app.tools.actions import Action
from app.tools.geo import geocode, km_between, maps_link, uber_link
from app.tools.weather import get_weather

# Emergency numbers by country (via timezone). Shown to the elder, never dialled automatically.
EMERGENCY_NUMBER = {"Asia/Kolkata": "112", "Asia/Bangkok": "1669", "Asia/Singapore": "995"}

# Gemini function declarations (JSON Schema). `reply` is how the model talks; it must always call something.
REPLY_TOOL = {
    "name": "reply",
    "description": "Say something to the elder. Use for every answer that isn't an action. Always include 3 quick replies.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "1-3 short, warm sentences in the elder's language."},
            "quickReplies": {"type": "array", "items": {"type": "string"},
                             "description": "Exactly 3 short things they might say next (max 5 words each)."},
        },
        "required": ["text", "quickReplies"],
    },
}

ACTION_TOOLS: list[dict[str, Any]] = [
    {"name": "order_food", "description": "Order food or groceries delivered to their home. Use when they ask for food, are hungry, or need groceries.",
     "parameters": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "string"}, "description": "Each item with quantity, e.g. '2 rotis'."},
         "notes": {"type": "string", "description": "Optional: soft food, less spicy, etc."}}, "required": ["items"]}},
    {"name": "order_medicine", "description": "Reorder one of their medicines from the pharmacy.",
     "parameters": {"type": "object", "properties": {
         "medicine": {"type": "string"}, "quantity": {"type": "integer", "description": "Number of strips/packs, default 1."}},
         "required": ["medicine"]}},
    {"name": "book_ride", "description": "Book a taxi for them.",
     "parameters": {"type": "object", "properties": {
         "destination": {"type": "string"}, "when": {"type": "string", "description": "'now' or HH:MM today."}},
         "required": ["destination"]}},
    {"name": "set_reminder", "description": "Set a reminder for them at a time.",
     "parameters": {"type": "object", "properties": {
         "title": {"type": "string"}, "time": {"type": "string", "description": "HH:MM, 24h, their local time."},
         "date": {"type": "string", "description": "YYYY-MM-DD. Omit for the next time this clock time comes round."}},
         "required": ["title", "time"]}},
    {"name": "send_message_to_caretaker", "description": "Pass a message to their family/caretaker.",
     "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
    {"name": "check_weather", "description": "Today's weather where they live (or a named city), with heat and rain advice.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
    {"name": "escalate_emergency", "description": "Use IMMEDIATELY for a fall, chest pain, trouble breathing, bleeding, "
     "fainting or any emergency. Alerts their family.",
     "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
]

ANSWER_CONFIRMATION_TOOL = {
    "name": "answer_pending_confirmation",
    "description": "They answered yes or no to the action waiting for their confirmation (shown on screen).",
    "parameters": {"type": "object", "properties": {"approve": {"type": "boolean"}}, "required": ["approve"]},
}


@dataclass
class ToolContext:
    elder_id: str
    elder: dict[str, Any]
    rules: AutonomyRules
    request_text: str
    actor_uid: str
    # Filled in as tools run so the session can attach a card to the agent's reply.
    touched: list[Action] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.elder.get("name", "They")

    @property
    def tz(self) -> str:
        return self.elder.get("timezone") or "Asia/Kolkata"


async def _log(ctx: ToolContext, message: str, kind: str, actor: str = "agent") -> None:
    await asyncio.to_thread(checkin_store.add_log, ctx.elder_id, LogEntry(message=message, kind=kind, actor=actor))  # type: ignore[arg-type]


async def _save(a: Action) -> None:
    await asyncio.to_thread(actions.save, a)


# ---------- executing an approved action (shared by "do it now", elder Yes, caretaker Approve) ----------

def _next_occurrence(time_hhmm: str, tz: str) -> dt.date:
    now = dt.datetime.now(ZoneInfo(tz))
    return now.date() if time_hhmm > f"{now:%H:%M}" else now.date() + dt.timedelta(days=1)


async def perform(a: Action, elder: dict[str, Any]) -> dict[str, Any]:
    """Carry out an approved action. Marks it done (or failed) and returns facts for the model."""
    try:
        if a.tool == "book_ride" and a.bookingUrl:
            # Real handoff: a prefilled ride-app link. The elder (or family) taps it to finish booking.
            a.status = "handed_off"
            await _save(a)
            await asyncio.to_thread(checkin_store.add_log, a.elderId, LogEntry(
                message=f"Ride ready to book — {a.summary()} (estimate). Link sent to {elder.get('name', 'them')}.", kind="ok"))
            return {"status": "ready_to_book", "estimatedFare": format_money(a.quote.amountMinor, a.quote.currency) if a.quote else None,
                    "instruction": "Tell them to tap the button on the card to open the app and confirm the ride there. "
                                   "The fare shown is an estimate.",
                    "say": "Tap the button on the card to finish booking your ride."}
        if a.quote is not None:
            a.orderId = providers.place(a.quote)
            result = {"status": "done", "orderId": a.orderId, "provider": a.quote.provider,
                      "total": format_money(a.quote.amountMinor, a.quote.currency), "eta": a.quote.eta,
                      "deliverTo": a.deliverTo, "demo": a.quote.demo}
        elif a.tool == "set_reminder":
            from app.routers.reminders import save_reminder  # avoid import cycle at module load
            day = a.args.get("date") or _next_occurrence(a.args["time"], elder.get("timezone") or "Asia/Kolkata").isoformat()
            await asyncio.to_thread(save_reminder, a.elderId, a.args["title"], a.args["time"], dt.date.fromisoformat(day))
            result = {"status": "done", "reminder": f"{a.args['title']} at {a.args['time']} on {day}"}
        elif a.tool == "send_message_to_caretaker":
            msg = FamilyMessage(sender="elder", senderUid=a.elderId, senderName=elder.get("name", ""),
                                text=a.args["message"], viaAgent=True)
            await asyncio.to_thread(family.save, a.elderId, msg)
            await live.broadcast(a.elderId, {"type": "family_message", "message": msg.model_dump(mode="json")}, "family")
            result = {"status": "done", "delivered": True}
        else:
            raise ValueError(f"no executor for {a.tool}")
        a.status = "done"
    except Exception as exc:  # noqa: BLE001 — report the failure to the elder instead of crashing the chat
        a.status = "failed"
        result = {"status": "failed", "error": str(exc)[:200]}
    await _save(a)
    if a.status == "done":
        demo = " (demo — not really ordered)" if a.quote and a.quote.demo else ""
        await asyncio.to_thread(checkin_store.add_log, a.elderId, LogEntry(message=f"Done — {a.summary()}{demo}", kind="ok"))
        result["say"] = f"Done — {a.summary()}."
    else:
        result["say"] = "Sorry, that didn't work. Your family can help."
    return result


# ---------- the tools ----------

async def _gated(ctx: ToolContext, tool: str, args: dict[str, Any], quote: providers.Quote | None,
                 **extra: Any) -> dict[str, Any]:
    decision = decide(tool, ctx.rules, quote.amountMinor if quote else None)
    a = Action(elderId=ctx.elder_id, tool=tool, args=args, status="done", quote=quote, requestText=ctx.request_text, **extra)
    ctx.touched.append(a)
    over_limit = quote is not None and decision == "caretaker_approve" and ctx.rules.tools[tool].mode != "caretaker_approve"  # type: ignore[index]

    if decision == "execute":
        return await perform(a, ctx.elder)
    if decision == "ask_elder":
        a.status = "awaiting_elder"
        await _save(a)
        return {"status": "awaiting_elder_confirmation", "details": a.summary(), "say": f"{a.summary()}. Shall I go ahead?",
                "instruction": "Tell them what you'd order/do and the price, and ask them to tap Yes or No on the card."}
    if decision == "caretaker_approve":
        a.status = "awaiting_caretaker"
        await _save(a)
        why = "it's above the spending limit" if over_limit else "the family asked to approve these first"
        await _log(ctx, f"Approval needed — {a.summary()} ({why})", "warn")
        return {"status": "sent_to_family_for_approval", "details": a.summary(), "reason": why,
                "say": "I've asked your family to approve this. I'll let you know.",
                "instruction": "Tell them you've asked their family to approve it and you'll let them know."}
    a.status = "blocked"
    await _save(a)
    await _log(ctx, f"Blocked — {ctx.name} asked: “{ctx.request_text}” ({a.summary()})", "alert")
    return {"status": "not_allowed", "say": "I can't do that one, but I've told your family.", "instruction": "Kindly say you can't do this one, and that their family has been told so they can help."}


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    currency = ctx.rules.currency
    address = ctx.elder.get("address") or {}
    located = address.get("lat") is not None and address.get("lng") is not None
    home = (address["lat"], address["lng"]) if located else None
    # A city-level match is fine for "which hospital is near" but not as a pickup point.
    pickup = home if located and address.get("precision") in ("address", "street") else None
    deliver_to = ", ".join(x for x in (address.get("line1"), address.get("line2"), address.get("landmark"),
                                       address.get("city"), address.get("postcode")) if x) or None
    if name in ("order_food", "order_medicine") and deliver_to is None:
        await _log(ctx, f"{ctx.name} wanted to order ({name.replace('_', ' ')}) but there's no delivery address. "
                        "Add it in their Profile.", "warn")
        return {"status": "no_delivery_address", "say": "Your family needs to add your address first. I've told them.",
                "instruction": "Say you can't order yet because their family needs to add their home address; "
                               "you've let them know."}
    if name == "order_food":
        items = [str(i) for i in args.get("items") or [] if str(i).strip()][:10]
        if not items:
            return {"status": "need_more_info", "instruction": "Ask what they'd like to eat."}
        return await _gated(ctx, name, {"items": items, "notes": args.get("notes", "")},
                            providers.quote_food(items, currency), deliverTo=deliver_to)
    if name == "order_medicine":
        med = str(args.get("medicine", "")).strip()
        qty = int(args.get("quantity") or 1)
        return await _gated(ctx, name, {"medicine": med, "quantity": qty},
                            providers.quote_medicine(med, qty, currency), deliverTo=deliver_to)
    if name == "book_ride":
        dest = str(args.get("destination", "")).strip()
        try:
            place = await geocode(f"{dest}, {address.get('city', '')}".strip(", "), ctx.tz, home)
        except Exception:  # noqa: BLE001
            place = None
        if place is None:
            return {"status": "place_not_found",
                    "instruction": f"Say you couldn't find '{dest}'. Ask for the full name or address of the place."}
        km = km_between(home, (place.lat, place.lng)) if home else None
        if currency == "INR":
            url = uber_link(pickup, "Home", place, dest)  # pickup None → Uber uses their current location
        else:
            url = maps_link(pickup, place)
        return await _gated(ctx, name, {"destination": dest, "when": args.get("when", "now")},
                            providers.quote_ride(dest, km, currency), bookingUrl=url)
    if name == "set_reminder":
        time_s = str(args.get("time", ""))
        if len(time_s) != 5 or time_s[2] != ":":
            return {"status": "need_more_info", "instruction": "Ask what time (HH:MM)."}
        clean = {"title": str(args.get("title", "Reminder"))[:120], "time": time_s}
        if args.get("date"):
            clean["date"] = str(args["date"])
        return await _gated(ctx, name, clean, None)
    if name == "send_message_to_caretaker":
        return await _gated(ctx, name, {"message": str(args.get("message", ""))[:500]}, None)
    if name == "check_weather":
        city = str(args.get("city") or ctx.elder.get("city") or "").strip()
        if not city:
            return {"status": "need_more_info", "instruction": "Ask which town or city they live in."}
        try:
            return {"status": "ok", **(await get_weather(city)).model_dump()}
        except Exception:  # noqa: BLE001
            return {"status": "unavailable", "instruction": "Say you couldn't check the weather just now."}
    if name == "escalate_emergency":
        desc = str(args.get("description", "Emergency"))[:300]
        await _log(ctx, f"🚨 EMERGENCY — {ctx.name}: {desc}", "alert")
        return {"status": "family_alerted", "emergencyNumber": EMERGENCY_NUMBER.get(ctx.tz, "112"),
                "instruction": "Stay calm and kind. Tell them family has been alerted. If it's serious, tell them to call "
                               "the emergency number now or press the red SOS button. Ask if they are safe right now."}
    return {"status": "unknown_tool"}


def tool_declarations(pending_confirmation: bool) -> list[dict[str, Any]]:
    tools = [REPLY_TOOL, *ACTION_TOOLS]
    return [*tools, ANSWER_CONFIRMATION_TOOL] if pending_confirmation else tools

