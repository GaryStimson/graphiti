"""Advisory scan for things that should never be stored in memory.

Flags card numbers, passwords, one-time codes, API keys, bank details and similar. The
result never contains the matched text itself, only the kind and a masked hint, so the UI
does not copy secrets around. This only warns; it never deletes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Flag:
    kind: str
    hint: str

    def as_dict(self) -> dict[str, str]:
        return {'kind': self.kind, 'hint': self.hint}


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def _mask(text: str) -> str:
    compact = re.sub(r'\s+', '', text)
    return f'…{compact[-4:]}' if len(compact) > 6 else '…'


_CARD = re.compile(r'(?<![\d-])(?:\d[ -]?){13,19}(?!\d)')
_PASSWORD = re.compile(
    r'\b(?:pass(?:word|code|phrase)?|pwd|pin)\b\s*(?:is|was|=|:)\s*["\']?([^\s"\',;]{3,})', re.I
)
_OTP = re.compile(
    r'\b(?:otp|one[- ]time (?:code|passcode|password)|verification code|security code|2fa code)'
    r'\b[^.\n\d]{0,30}\b(\d{4,8})\b',
    re.I,
)
_KEY = re.compile(
    r'\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}'
    r'|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})'
)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_SORT_ACCOUNT = re.compile(r'\b\d{2}-\d{2}-\d{2}\b[^\d\n]{0,25}\b(\d{8})\b')
_IBAN = re.compile(r'\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\s?[A-Z0-9]{1,4})\b')
_NINO = re.compile(r'\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b')


def scan(text: str | None) -> list[Flag]:
    """Return the sensitive-data warnings for a piece of text (empty list when clean)."""
    if not text:
        return []
    flags: list[Flag] = []

    for match in _CARD.finditer(text):
        digits = re.sub(r'\D', '', match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            flags.append(Flag('card number', _mask(digits)))
    for match in _PASSWORD.finditer(text):
        value = match.group(1)
        # "password=$TOKEN", "<password>", "{{pw}}", "****": a reference, not a secret
        if value[0] in '$%<{[(*@' or re.fullmatch(r'[xX*•.]+', value):
            continue
        flags.append(Flag('password or PIN', _mask(value)))
    for match in _OTP.finditer(text):
        flags.append(Flag('one-time code', _mask(match.group(1))))
    for match in _KEY.finditer(text):
        flags.append(Flag('API key or token', _mask(match.group(1))))
    if _PRIVATE_KEY.search(text):
        flags.append(Flag('private key', '…'))
    for match in _SORT_ACCOUNT.finditer(text):
        flags.append(Flag('bank account details', _mask(match.group(1))))
    for match in _IBAN.finditer(text):
        iban = re.sub(r'\s', '', match.group(1))
        if 15 <= len(iban) <= 34:
            flags.append(Flag('IBAN', _mask(iban)))
    for match in _NINO.finditer(text):
        flags.append(Flag('National Insurance number', _mask(match.group(0))))

    unique: dict[tuple[str, str], Flag] = {(f.kind, f.hint): f for f in flags}
    return list(unique.values())
