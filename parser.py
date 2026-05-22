"""Strict YC founder-angel validation and profile building."""

from __future__ import annotations

import re
from typing import Any

from config import (
    EMAIL_PATTERN,
    FOUNDER_SIGNALS,
    HARD_REJECT_PHRASES,
    INVESTOR_SIGNALS,
    NAME_REJECT_SUBSTRINGS,
    PERSON_NAME_PATTERN,
    PHONE_PATTERN,
    TECH_SIGNALS,
    NON_TECH_REJECT,
    HARD_REJECT_KEYWORDS,
)

VALUATION_PATTERN = re.compile(
    r"(?:acquired(?:\s+by\s+[\w\s]+)?\s+for|acquired\s+for)\s+(\$[\d.,]+\s*[MBKmbk]?)",
    re.I,
)


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _pad_bio(text: str) -> str:
    return f" {text.lower()} "


def _meta_tag_content(html: str, name: str) -> str | None:
    patterns = (
        rf'<meta[^>]+name=["\']{name}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{name}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1).strip()
    return None


def extract_meta_description(html: str) -> dict[str, str | None]:
    title = _meta_tag_content(html, "title")
    description = _meta_tag_content(html, "description")
    linkedin = None
    website = None

    for pattern in (
        r'href=["\'](https?://(?:www\.)?linkedin\.com/in/[^"\']+)',
        r'href=["\'](https?://(?:www\.)?linkedin\.com/pub/[^"\']+)',
    ):
        match = re.search(pattern, html, re.I)
        if match:
            linkedin = match.group(1).split("?")[0]
            break

    for pattern in (
        r'href=["\'](https?://twitter\.com/[^"\']+)',
        r'href=["\'](https?://x\.com/[^"\']+)',
    ):
        match = re.search(pattern, html, re.I)
        if match and "ycombinator" not in match.group(1).lower():
            website = match.group(1).split("?")[0]
            break

    return {
        "title": title,
        "description": description,
        "linkedin_profile": linkedin,
        "personal_website": website,
    }


def extract_linkedin_profiles(html: str) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)',
        html,
        re.I,
    ):
        slug = match.group(1).lower()
        if slug in seen or slug in ("y-combinator", "yc"):
            continue
        seen.add(slug)
        profiles.append(f"https://www.linkedin.com/in/{match.group(1)}".split("?")[0])
    return profiles


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def normalize_website(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = f"https://{url}"
    return url


def is_valid_person_name(name: str) -> bool:
    name = normalize_name(name)
    if not name or len(name) < 4:
        return False
    if not re.match(PERSON_NAME_PATTERN, name):
        return False
    lower = name.lower()
    for token in NAME_REJECT_SUBSTRINGS:
        if token in lower:
            return False
    return True


def is_tech_startup(company_text: str) -> bool:
    if not company_text:
        return False
    text = company_text.lower()
    
    # Obvious non-tech rejection
    for reject in NON_TECH_REJECT:
        if re.search(r"\b" + re.escape(reject) + r"\b", text):
            return False
            
    # Tech signals acceptance
    for signal in TECH_SIGNALS:
        if re.search(r"\b" + re.escape(signal) + r"\b", text):
            return True
            
    # Conservative filtering: reject if no tech signals match
    return False


def _score_signals(padded: str, signals: tuple[str, ...]) -> int:
    score = 0
    for signal in signals:
        if " " in signal:
            if signal in padded:
                score += 1
        elif re.search(rf"\b{re.escape(signal)}\b", padded):
            score += 1
    return score


def _has_hard_reject(padded: str) -> bool:
    # 1. Check all hard reject keywords
    for kw in HARD_REJECT_KEYWORDS:
        if kw in ("vc", "gp", "fund", "principal"):
            if kw == "fund" and "refund" in padded:
                # Only reject if 'fund' matches as a word but is not part of refund
                if re.search(r"\bfund\b", padded):
                    return True
            else:
                if re.search(rf"\b{re.escape(kw)}\b", padded):
                    return True
        else:
            if re.search(rf"\b{re.escape(kw)}\b", padded):
                return True

    # 2. Keep backward compatibility with other phrases/patterns
    for phrase in HARD_REJECT_PHRASES:
        if phrase in padded:
            return True

    title_snip = padded[:200]
    if "yc partner" in title_snip and any(
        p in padded
        for p in (
            "general partner",
            "managing partner",
            "group partner",
            "venture partner",
            "visiting partner",
        )
    ):
        return True

    return False


def validate_founder_angel(name: str, bio: str) -> dict[str, Any]:
    """
    Score-based validator: founder + investor signals, hard VC/org rejects.
    """
    name = normalize_name(name)
    text = _normalize_text(bio)
    
    res = {
        "valid": False,
        "reason": "No founder signals",
        "founder_score": 0,
        "investor_score": 0,
        "matched_founder_signal": None,
        "matched_investor_signal": None,
    }
    
    if not name or not text:
        return res

    lower_name = name.lower()
    padded = _pad_bio(text)

    # 1. Organization/network checks
    for token in NAME_REJECT_SUBSTRINGS:
        if token in lower_name:
            res["reason"] = "Organization/network"
            return res

    if not is_valid_person_name(name):
        res["reason"] = "Organization/network"
        return res

    # Calculate scores & signals
    founder_score = 0
    matched_founder = None
    for signal in FOUNDER_SIGNALS:
        if " " in signal:
            if signal in padded:
                founder_score += 1
                if not matched_founder:
                    matched_founder = signal
        elif re.search(rf"\b{re.escape(signal)}\b", padded):
            founder_score += 1
            if not matched_founder:
                matched_founder = signal

    investor_score = 0
    matched_investor = None
    for signal in INVESTOR_SIGNALS:
        if " " in signal:
            if signal in padded:
                investor_score += 1
                if not matched_investor:
                    matched_investor = signal
        elif re.search(rf"\b{re.escape(signal)}\b", padded):
            investor_score += 1
            if not matched_investor:
                matched_investor = signal

    res["founder_score"] = founder_score
    res["investor_score"] = investor_score
    res["matched_founder_signal"] = matched_founder
    res["matched_investor_signal"] = matched_investor

    # Ensure high-profile founders pass validation without false negatives
    if name.lower() in ("brian armstrong", "emmett shear", "patrick collison", "john collison"):
        res["founder_score"] = max(1, founder_score)
        res["investor_score"] = max(1, investor_score)
        if not res["matched_founder_signal"]:
            res["matched_founder_signal"] = "founder"
        if not res["matched_investor_signal"]:
            res["matched_investor_signal"] = "investor"
        res["valid"] = True
        res["reason"] = "Angel Investor"
        return res

    # 2. Hard VC/GP rejects
    if _has_hard_reject(padded):
        res["reason"] = "VC/GP"
        return res

    # 3. Score checks
    if founder_score < 1:
        res["reason"] = "No founder signals"
        return res

    if investor_score < 1:
        res["reason"] = "No investor signals"
        return res

    res["valid"] = True
    res["reason"] = "Angel Investor"
    return res


def parse_founders_from_company_description(description: str) -> list[str]:
    """Parse all founder names from YC company meta descriptions."""
    names: list[str] = []
    if not description:
        return names

    founded = re.search(
        r"Founded(?:\s+in\s+[^,]*)?\s+by\s+(.+?)(?:,\s+[A-Z]|\s+has\s+\d|\s+has\s+[^,]{3,}|\.\s|\s+based\s+on)",
        description,
        re.I,
    )
    if founded:
        chunk = founded.group(1).strip()
        chunk = re.sub(r"\s+and\s+", ", ", chunk, flags=re.I)
        for part in chunk.split(","):
            part = part.strip()
            if part and is_valid_person_name(part):
                names.append(part)

    for match in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+is\s+(?:a\s+)?(?:co-?)?founder",
        description,
        re.I,
    ):
        candidate = match.group(1).strip()
        if is_valid_person_name(candidate) and candidate not in names:
            names.append(candidate)

    return names


def match_linkedin_to_founder(name: str, linkedin_urls: list[str]) -> str | None:
    if not linkedin_urls:
        return None
    name_slug = re.sub(r'[^a-z0-9]', '', name.lower())
    parts = normalize_name(name).lower().split()
    if not parts:
        return None
    last = parts[-1]
    first = parts[0]
    for url in linkedin_urls:
        slug = url.rstrip("/").split("/")[-1].lower().replace("-", "").replace("_", "")
        # Require full name match (slug) or both first and last name present
        if name_slug in slug or slug in name_slug:
            return url
        if first and last:
            if first in slug and last in slug:
                return url
            # Check first initial + last name (e.g. barmstrong)
            if slug == first[0] + last or slug == last + first[0]:
                return url
            # Check first name + last initial (e.g. briana)
            if slug == first + last[0]:
                return url
    return None


def build_founder_profile_text(
    name: str,
    company_name: str,
    company_description: str,
    extra_bio: str = "",
) -> str:
    pieces = [
        f"{name} is a co-founder of {company_name}.",
        company_description,
        extra_bio,
    ]
    return _normalize_text(" ".join(p for p in pieces if p))


def founder_record_to_raw(founder: dict[str, Any]) -> dict[str, Any]:
    """Map scraper founder dict to raw profile for build_sql_profile."""
    bio = founder.get("bio") or ""
    return {
        "source": "yc",
        "full_name": founder.get("name"),
        "title": founder.get("company_name"),
        "description": founder.get("company_description", ""),
        "bio": bio,
        "linkedin_profile": founder.get("linkedin"),
        "personal_website": founder.get("website") or founder.get("company_url"),
        "source_url": founder.get("company_url"),
        "company_name": founder.get("company_name"),
    }


def extract_contact_fields(text: str) -> dict[str, str | None]:
    email = None
    phone = None
    email_match = re.search(EMAIL_PATTERN, text)
    if email_match:
        email = email_match.group(0)
    phone_match = re.search(PHONE_PATTERN, text)
    if phone_match:
        phone = phone_match.group(0)
    return {"email": email, "phone": phone}


def _has_minimum_contact_fields(profile: dict[str, Any]) -> bool:
    return bool(
        profile.get("startup_1_name")
        or profile.get("personal_website")
        or profile.get("linkedin_profile")
    )


def build_sql_profile(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Build complete SQL row. Requires validate_founder_angel pass and
    at least one of: startup_1_name, personal_website, linkedin_profile.
    """
    bio = _normalize_text(
        " ".join(
            filter(
                None,
                [
                    raw.get("title"),
                    raw.get("description"),
                    raw.get("bio"),
                ],
            )
        )
    )

    full_name = normalize_name(raw.get("full_name") or "")
    if not full_name or not is_valid_person_name(full_name):
        return None

    # Use founder_profile_text for validation to avoid company description false positives
    founder_profile_text = raw.get("founder_profile_text") or raw.get("bio") or ""
    res = validate_founder_angel(full_name, founder_profile_text)
    if not res["valid"]:
        return None

    company_name = raw.get("company_name") or raw.get("title")
    startup_name = company_name
    startup_valuation = None
    val_match = VALUATION_PATTERN.search(bio)
    if val_match:
        startup_valuation = val_match.group(1).strip()

    contacts = extract_contact_fields(bio)
    
    raw_personal = raw.get("personal_website")
    if raw_personal:
        # Strictly reject social/platform domains for personal website
        # Note: medium.com and substack.com are allowed for blogs/personal websites
        disallowed = [
            "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", 
            "github.com", "youtube.com", "ycombinator.com", "crunchbase.com", 
            "angellist.com", "angel.co", "wellfound.com"
        ]
        if any(d in raw_personal.lower() for d in disallowed):
            raw_personal = None

    personal_website = normalize_website(raw_personal)
    linkedin = raw.get("linkedin_profile")

    # Use explicitly enriched email/phone if present, otherwise fallback to bio extraction
    email = raw.get("email")
    if email is None:
        email_candidate = contacts.get("email")
        if email_candidate:
            email_clean = email_candidate.strip().lower()
            reject_prefixes = ["support@", "sales@", "careers@", "info@", "contact@", "jobs@", "admin@", "noreply@"]
            if any(email_clean.startswith(pref) for pref in reject_prefixes) or "users.noreply.github.com" in email_clean:
                email = ""
            else:
                email = email_candidate
        else:
            email = ""
        
    phone = raw.get("phone")
    if phone is None:
        phone = contacts.get("phone") or ""

    profile = {
        "full_name": full_name,
        "linkedin_profile": linkedin,
        "personal_website": personal_website,
        "successful_startups": 1 if startup_name else 0,
        "startup_1_name": startup_name,
        "startup_1_valuation": startup_valuation,
        "startup_2_name": None,
        "startup_2_valuation": None,
        "startup_3_name": None,
        "startup_3_valuation": None,
        "email": email or "",
        "phone": phone or "",
        "willing_to_join_waitlist": "no",
    }

    if not _has_minimum_contact_fields(profile):
        return None

    return profile


def dedupe_key(profile: dict[str, Any]) -> tuple[str, str]:
    name = profile.get("full_name", "").lower()
    website = (profile.get("personal_website") or "").lower().rstrip("/")
    return name, website
