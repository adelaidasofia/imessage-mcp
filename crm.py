"""crm.py — match an iMessage handle to a vault CRM note.

Strategy mirrors whatsapp-mcp:
  1. Phone digit match (handle is a phone, scan CRM frontmatter for the same
     digits — last 7 are the floor; full E.164 is preferred).
  2. Email match (case-insensitive, on email-shaped handles).
  3. Display name match (when contacts.py resolves the handle to a real name).

Returns a `CRMContext` or None. None when no CRM path is configured or no
match is found.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import frontmatter

VAULT_CRM_PATH = (
    os.environ.get("IMESSAGE_VAULT_CRM_PATH")
    or os.environ.get("WHATSAPP_VAULT_CRM_PATH")
    or ""
).strip()


@dataclass
class CRMContext:
    path: str
    name: str
    relationship: str | None = None
    company: str | None = None
    next_step: str | None = None
    first_paragraph: str | None = None
    preferred_language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


def _digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _frontmatter_phones(meta: dict[str, Any]) -> list[str]:
    """Pull phone candidates from CRM frontmatter."""
    out: list[str] = []
    for key in ("phone", "phones", "whatsapp", "imessage", "mobile"):
        v = meta.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            out.extend(str(x) for x in v if x)
        else:
            out.append(str(v))
    return out


def _frontmatter_emails(meta: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("email", "emails"):
        v = meta.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            out.extend(str(x).strip().lower() for x in v if x)
        else:
            out.append(str(v).strip().lower())
    return out


def _frontmatter_aliases(meta: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("aliases", "alias", "names"):
        v = meta.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            out.extend(str(x).lower() for x in v if x)
        else:
            out.append(str(v).lower())
    return out


def _first_paragraph(body: str) -> str:
    """Return the first non-empty paragraph from a CRM note body."""
    if not body:
        return ""
    parts = re.split(r"\n\s*\n", body.strip(), maxsplit=2)
    for p in parts:
        cleaned = p.strip()
        if cleaned and not cleaned.startswith("#"):
            return cleaned[:480]
    return ""


def lookup_crm_context(
    handle: str | None = None,
    display_name: str | None = None,
) -> CRMContext | None:
    """Match an iMessage handle and/or a resolved display name to a CRM note.

    Returns the first match. Phone match wins over name match.
    """
    if not VAULT_CRM_PATH:
        return None
    root = Path(VAULT_CRM_PATH)
    if not root.is_dir():
        return None

    target_email = ""
    target_phone_digits = ""
    if handle:
        h = handle.strip()
        if "@" in h:
            target_email = h.lower()
        else:
            target_phone_digits = _digits_only(h)

    target_name = (display_name or "").strip().lower()
    if not (target_email or target_phone_digits or target_name):
        return None

    phone_match: CRMContext | None = None
    email_match: CRMContext | None = None
    name_match: CRMContext | None = None

    for f in root.rglob("*.md"):
        try:
            post = frontmatter.load(str(f))
        except Exception:
            continue
        meta = post.metadata or {}
        body = post.content or ""
        name = (meta.get("name") or f.stem).strip()
        relationship = meta.get("relationship")
        company = meta.get("company") or meta.get("organization")
        next_step = meta.get("next_step") or meta.get("next-step")
        preferred_language = meta.get("preferred_language") or meta.get("preferred-language")

        ctx = CRMContext(
            path=str(f),
            name=name,
            relationship=str(relationship) if relationship else None,
            company=str(company) if company else None,
            next_step=str(next_step) if next_step else None,
            first_paragraph=_first_paragraph(body),
            preferred_language=str(preferred_language) if preferred_language else None,
        )

        if target_phone_digits:
            for p in _frontmatter_phones(meta):
                pdigits = _digits_only(p)
                if not pdigits:
                    continue
                if pdigits == target_phone_digits or (
                    len(pdigits) >= 7
                    and len(target_phone_digits) >= 7
                    and pdigits[-7:] == target_phone_digits[-7:]
                ):
                    phone_match = ctx
                    break
            if phone_match:
                break

        if target_email and email_match is None:
            for e in _frontmatter_emails(meta):
                if e == target_email:
                    email_match = ctx
                    break

        if target_name and name_match is None:
            n = name.lower()
            if n == target_name or target_name in n or n in target_name:
                name_match = ctx
                continue
            for alias in _frontmatter_aliases(meta):
                if alias == target_name or target_name in alias:
                    name_match = ctx
                    break

    return phone_match or email_match or name_match
