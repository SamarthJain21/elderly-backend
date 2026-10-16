"""The few things the server writes in the elder's voice (not the model's), in their language.
Hindi uses gender-neutral phrasing ("आज मूड अच्छा है"): "I feel…" needs a gendered verb."""
from __future__ import annotations

from app.checkin import Mood

_MOOD: dict[str, dict[str, str]] = {
    "en-US": {"great": "I'm feeling great", "good": "I'm feeling good", "okay": "I'm feeling okay",
              "low": "I'm feeling a bit low", "bad": "I'm not feeling good"},
    "hi-IN": {"great": "आज मूड बहुत अच्छा है", "good": "आज मूड अच्छा है", "okay": "आज मूड ठीक-ठाक है",
              "low": "आज मन थोड़ा उदास है", "bad": "आज तबीयत ठीक नहीं है"},
    "hi-Latn": {"great": "Aaj mood bahut accha hai", "good": "Aaj mood accha hai", "okay": "Aaj mood theek-thaak hai",
                "low": "Aaj mann thoda udaas hai", "bad": "Aaj tabiyat theek nahi hai"},
    "th-TH": {"great": "วันนี้รู้สึกดีมาก", "good": "วันนี้รู้สึกดี", "okay": "วันนี้รู้สึกเฉยๆ",
              "low": "วันนี้รู้สึกเศร้านิดหน่อย", "bad": "วันนี้รู้สึกไม่ค่อยสบาย"},
}
_START = {"en-US": "Let's do my check-in", "hi-IN": "आज का हाल बताना है", "hi-Latn": "Aaj ka haal batana hai",
          "th-TH": "ขอบอกอาการวันนี้"}


def checkin_opener(language: str, mood: Mood | None) -> str:
    """What the elder 'says' when they tap a mood face (or start the check-in)."""
    if mood is None:
        return _START.get(language, _START["en-US"])
    return _MOOD.get(language, _MOOD["en-US"])[mood]
