"""Delete everything stored about an elder. Used to expire sandboxes; the same path will serve 'delete my data'."""
from __future__ import annotations

import logging
from collections.abc import Sequence

from firebase_admin import auth as fb_auth
from google.cloud.firestore_v1.base_query import FieldFilter

from app import firebase

log = logging.getLogger(__name__)

# Top-level collections keyed by an `elderId` field (everything else lives under elders/{id}/…).
_BY_ELDER_ID = ("check_ins", "check_in_transcripts", "reminders")


def _delete_auth_user(uid: str) -> None:
    try:
        fb_auth.delete_user(uid, app=firebase.get_app())
    except fb_auth.UserNotFoundError:
        pass


def erase_elder(elder_id: str, *, also_caretakers: Sequence[str] = ()) -> None:
    """Elder doc + every subcollection (messages, logs, actions, alerts, contacts…), their check-ins, reminders,
    rules, user doc and sign-in account. `also_caretakers`: caretaker accounts to remove entirely (sandbox only)."""
    db = firebase.get_db()
    for col in _BY_ELDER_ID:
        for snap in db.collection(col).where(filter=FieldFilter("elderId", "==", elder_id)).stream():
            snap.reference.delete()
    db.collection("rules").document(elder_id).delete()
    db.recursive_delete(db.collection("elders").document(elder_id))
    db.collection("users").document(elder_id).delete()
    _delete_auth_user(elder_id)
    for uid in also_caretakers:
        db.collection("caretakers").document(uid).delete()
        db.collection("users").document(uid).delete()
        _delete_auth_user(uid)
    log.info("erased elder %s (+%d caretaker accounts)", elder_id, len(also_caretakers))
