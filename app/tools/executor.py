"""Runs a tool the agent asked for: quote → autonomy decision → execute / ask elder / ask caretaker / refuse.

Every outcome returns a small dict for the model to phrase, plus a deterministic `say` fallback
used if the model can't be reached after side effects already happened.
"""
from __future__ import annotations

import logging

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from app import alerts, checkin_store, contacts
from app.alerts import EMERGENCY_NUMBER
from app.autonomy import AutonomyRules, decide, format_money
from app.contacts import Contact, HelpRequest, find_contact, pick_volunteer
from app import push
from app.agent import live
from app.sms import send_sms
from app.checkin_store import LogEntry
from app.agent import family, live
from app.agent.family import FamilyMessage
from app.tools import actions, providers
from app.tools.actions import Action
from app.routers import places_saved_api
from app.tools.geo import Place, geocode, km_between, maps_link, uber_link
from app.tools.weather import get_weather

log = logging.getLogger(__name__)


# Gemini function declarations (JSON Schema). `reply` is how the model talks; it must always call something.
REPLY_TOOL = {
    "name": "reply",
    "description": "Say something to the elder. Use for every answer that isn't an action. Always include 3 quick replies.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "1-3 short, warm sentences in the elder's language."},
            "quickReplies": {"type": "array", "items": {"type": "string"},
                             "description": "Exactly 3 things THE ELDER might tap to say to you next, in their own voice (first person, max 5 words, their language), e.g. 'I slept well', 'Tell me a joke', 'What's the weather?'. Never questions you would ask them, and never their own name."},
            "spoken": {"type": "string", "description": "ONLY for Hinglish elders: the same reply written in Devanagari script, for the voice. Leave out otherwise."},
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
    {"name": "book_ride", "description": "Book a taxi for them, from home. If it's unclear where to, ask first (offer their saved places).",
     "parameters": {"type": "object", "properties": {
         "destination": {"type": "string", "description": "One of their saved places' names if it fits, else the place they said."}, "when": {"type": "string", "description": "'now' or HH:MM today."}},
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
    {"name": "notify_volunteer", "description": "Ask a trusted volunteer from their contacts to come and help: "
     "repairs (plumbing, electrical), tech help, shopping, a walk, company. Use for anything at home that needs a person.",
     "parameters": {"type": "object", "properties": {
         "skill": {"type": "string", "description": "One or two words: plumbing, electrical, tech help, shopping, company…"},
         "title": {"type": "string", "description": "Short task, e.g. 'Fix leaking kitchen tap'."},
         "description": {"type": "string"}, "preferred_time": {"type": "string", "description": "e.g. 'today evening'."}},
         "required": ["skill", "title"]}},
    {"name": "call_contact", "description": "Show a button to phone someone from their contacts (family, friend, neighbour, doctor).",
     "parameters": {"type": "object", "properties": {
         "who": {"type": "string", "description": "Name or relationship, e.g. 'Ravi', 'my son', 'the doctor'."}},
         "required": ["who"]}},
    {"name": "escalate_emergency", "description": "Use IMMEDIATELY for a fall, chest pain, trouble breathing, bleeding, "
     "fainting or any emergency. Alerts their family.",
     "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "report_scam", "description": "Use when they mention someone (call, SMS, WhatsApp, visitor) asking for an OTP, PIN, "
     "password, bank or card details, a money transfer or UPI payment, a 'KYC update', a prize/lottery fee, or threatening "
     "them (police, arrest, account blocked). Only when SOMEONE ELSE contacted them and asked. NOT for their own plans "
     "(e.g. sending a grandchild a gift), ordinary bills, or their family. Warns their family.",
     "parameters": {"type": "object", "properties": {
         "what": {"type": "string", "description": "One line: who contacted them and what they asked for."}}, "required": ["what"]}},
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
    people: list[Contact] = field(default_factory=list)
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
        elif a.tool == "notify_volunteer":
            return await _ask_volunteer(a, elder)
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


async def _ask_volunteer(a: Action, elder: dict[str, Any]) -> dict[str, Any]:
    """Pick the best volunteer for the skill and text them. Creates a help request the family can follow."""
    people = await asyncio.to_thread(contacts.list_contacts, a.elderId)
    vol = pick_volunteer(people, a.args["skill"], bool(a.args.get("trustedOnly", True)))
    req = HelpRequest(skill=a.args["skill"], title=a.args["title"], description=a.args.get("description", ""),
                      preferredTime=a.args.get("preferred_time", ""))
    name = elder.get("name", "Your neighbour")
    if vol is None:
        await asyncio.to_thread(contacts.save_request, a.elderId, req)
        a.status = "failed"
        await _save(a)
        await asyncio.to_thread(checkin_store.add_log, a.elderId, LogEntry(
            message=f"Help needed — {a.args['title']}: no {'trusted ' if a.args.get('trustedOnly', True) else ''}"
                    f"volunteer with “{a.args['skill']}” skills. Please arrange help or add a volunteer in Contacts.",
            kind="warn"))
        return {"status": "no_volunteer_found", "say": "I couldn't find a volunteer for that, so I've asked your family to help.",
                "instruction": "Say no volunteer was available, and that their family has been told and will sort it out."}
    address = elder.get("address") or {}
    where = ", ".join(x for x in (address.get("line1"), address.get("landmark"), address.get("city")) if x)
    body = (f"Hi {vol.name}, this is Elderly for {name}. Could you help with: {a.args['title']}"
            f"{' (' + req.preferredTime + ')' if req.preferredTime else ''}? {('Address: ' + where + '. ') if where else ''}"
            f"Please call {name} on {elder.get('phone', '')} to confirm. Thank you!")
    via = await asyncio.to_thread(send_sms, vol.phone, body)
    req.volunteerId, req.volunteerName, req.status, req.notifiedVia = vol.id, vol.name, "notified", [via]
    await asyncio.to_thread(contacts.save_request, a.elderId, req)
    a.status = "done"
    a.args = {**a.args, "label": f"{a.args['title']} — asked {vol.name}"}
    await _save(a)
    how = "by text" if via == "sms" else ("(no texts in this demo)" if elder.get("sandbox") else "(not texted yet — SMS isn't switched on)")
    await asyncio.to_thread(checkin_store.add_log, a.elderId, LogEntry(
        message=f"Asked {vol.name}{' ⭐' if vol.trusted else ''} to help with “{a.args['title']}” {how}.", kind="ok"))
    return {"status": "done", "volunteer": vol.name, "trusted": vol.trusted,
            "say": f"I've asked {vol.name} to help. They'll call you.",
            "instruction": f"Tell them you've asked {vol.name} to help and they should get a call soon."}


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
                "price": format_money(quote.amountMinor, quote.currency) if quote else None,
                "currency": quote.currency if quote else None,
                "instruction": "Tell them what you'd order/do and the price, and ask them to tap Yes or No on the card."}
    if decision == "caretaker_approve":
        a.status = "awaiting_caretaker"
        await _save(a)
        why = "it's above the spending limit" if over_limit else "the family asked to approve these first"
        await _log(ctx, f"Approval needed — {a.summary()} ({why})", "warn")
        push.notify_family(ctx.elder, f"🔐 {ctx.name} needs your OK", a.summary(), "/caretaker/approvals")
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
        saved = places_saved_api.match(places_saved_api.read(ctx.elder), dest)
        place = None
        if saved and saved.address.lat is not None and saved.address.lng is not None:
            # A place the family saved: exact address, labelled the way the elder says it.
            place, dest = Place(lat=saved.address.lat, lng=saved.address.lng, name=saved.address.one_line()), saved.name
        else:
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
    if name == "notify_volunteer":
        skill = str(args.get("skill", "")).strip()[:40]
        title = str(args.get("title", "")).strip()[:120]
        if not skill or not title:
            return {"status": "need_more_info", "instruction": "Ask what they need help with."}
        return await _gated(ctx, name, {"skill": skill, "title": title, "description": str(args.get("description", ""))[:300],
                                        "preferred_time": str(args.get("preferred_time", ""))[:60],
                                        "trustedOnly": ctx.rules.trustedVolunteersOnly}, None)
    if name == "call_contact":
        person = find_contact(ctx.people, str(args.get("who", "")))
        if person is None:
            names = ", ".join(f"{p.name} ({p.relationship or p.kind})" for p in ctx.people[:8]) or "nobody yet"
            return {"status": "not_found", "contacts": names,
                    "instruction": "Say you couldn't find them and ask who they mean, using these names."}
        a = Action(elderId=ctx.elder_id, tool=name, args={"who": args.get("who"), "name": person.name,
                   "label": f"{person.name}{' · ' + person.relationship if person.relationship else ''}"},
                   status="handed_off", bookingUrl=f"tel:{person.phone.replace(' ', '')}", requestText=ctx.request_text)
        ctx.touched.append(a)
        await _save(a)
        return {"status": "ready_to_call", "name": person.name, "say": f"Tap the button to call {person.name}.",
                "instruction": f"Tell them to tap the green button to call {person.name}."}
    if name == "check_weather":
        # The model may name the city its own way ("Dilli"); fall back to the saved city before giving up.
        cities = [c for c in dict.fromkeys((str(args.get("city") or "").strip(), str(ctx.elder.get("city") or "").strip())) if c]
        if not cities:
            return {"status": "need_more_info", "instruction": "Ask which town or city they live in."}
        for city in cities:
            try:
                return {"status": "ok", **(await get_weather(city)).model_dump()}
            except Exception:  # noqa: BLE001
                log.warning("weather lookup failed for %r", city, exc_info=True)
        return {"status": "unavailable", "instruction": "Say you couldn't check the weather just now."}
    if name == "report_scam":
        what = str(args.get("what", "Someone asked for money or bank details"))[:300]
        await _log(ctx, f"⚠️ Possible scam — {ctx.name}: {what}. Please call them and check.", "alert")
        await live.broadcast(ctx.elder_id, {"type": "notice", "kind": "alert", "summary": f"⚠️ Possible scam: {what}"}, "family")
        push.notify_family(ctx.elder, f"⚠️ Possible scam call — {ctx.name}", what)
        return {"status": "family_warned",
                "instruction": "Calmly tell them: do NOT share any OTP, PIN, password or bank details, do not send money, "
                               "and hang up — real banks and police never ask for these on the phone. Say their family "
                               "has been told and will check with them. Be reassuring, not frightening."}
    if name == "escalate_emergency":
        desc = str(args.get("description", "Emergency"))[:300]
        # Same path as the SOS button: alert record, caretaker banners, texts to emergency contacts.
        await alerts.raise_alert(ctx.elder_id, ctx.elder, "agent", desc)
        return {"status": "family_alerted", "emergencyNumber": EMERGENCY_NUMBER.get(ctx.tz, "112"),
                "instruction": "Stay calm and kind. Tell them family has been alerted. If it's serious, tell them to call "
                               "the emergency number now or press the red SOS button. Ask if they are safe right now."}
    return {"status": "unknown_tool"}


def tool_declarations(pending_confirmation: bool) -> list[dict[str, Any]]:
    tools = [REPLY_TOOL, *ACTION_TOOLS]
    return [*tools, ANSWER_CONFIRMATION_TOOL] if pending_confirmation else tools

