"""MySQL persistence with full-column inserts for founder profiles."""

from __future__ import annotations

import csv
import logging
import os

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config import FOUNDERS_TABLE
from database import engine
from parser import dedupe_key, normalize_name, normalize_website

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {FOUNDERS_TABLE} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    full_name VARCHAR(255) NOT NULL,
    linkedin_profile VARCHAR(512) NULL,
    personal_website VARCHAR(512) NULL,
    successful_startups INT DEFAULT 0,
    startup_1_name VARCHAR(255) NULL,
    startup_1_valuation VARCHAR(128) NULL,
    startup_2_name VARCHAR(255) NULL,
    startup_2_valuation VARCHAR(128) NULL,
    startup_3_name VARCHAR(255) NULL,
    startup_3_valuation VARCHAR(128) NULL,
    email VARCHAR(255) NULL,
    phone VARCHAR(64) NULL,
    willing_to_join_waitlist ENUM('yes', 'no') DEFAULT 'no',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_founder_identity (full_name, personal_website(255))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

INSERT_SQL = text(f"""
INSERT INTO {FOUNDERS_TABLE} (
    full_name,
    linkedin_profile,
    personal_website,
    successful_startups,
    startup_1_name,
    startup_1_valuation,
    startup_2_name,
    startup_2_valuation,
    startup_3_name,
    startup_3_valuation,
    email,
    phone,
    willing_to_join_waitlist
) VALUES (
    :full_name,
    :linkedin_profile,
    :personal_website,
    :successful_startups,
    :startup_1_name,
    :startup_1_valuation,
    :startup_2_name,
    :startup_2_valuation,
    :startup_3_name,
    :startup_3_valuation,
    :email,
    :phone,
    :willing_to_join_waitlist
)
""")


def build_insert_payload(profile: dict) -> dict:
    """Ensure every SQL column is explicitly set (NULL / 0 / 'no' defaults)."""
    return {
        "full_name": normalize_name(profile["full_name"]),
        "linkedin_profile": profile.get("linkedin_profile"),
        "personal_website": normalize_website(profile.get("personal_website")),
        "successful_startups": int(profile.get("successful_startups") or 0),
        "startup_1_name": profile.get("startup_1_name"),
        "startup_1_valuation": profile.get("startup_1_valuation"),
        "startup_2_name": None,
        "startup_2_valuation": None,
        "startup_3_name": None,
        "startup_3_valuation": None,
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "willing_to_join_waitlist": profile.get("willing_to_join_waitlist") or "no",
    }


def ensure_founders_table() -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
    logger.info("[SQL] Table '%s' is ready", FOUNDERS_TABLE)


CSV_FILE = "accepted_founders.csv"
CSV_COLUMNS = [
    "full_name",
    "linkedin_profile",
    "personal_website",
    "startup_1_name",
    "successful_startups",
    "email",
    "phone",
]


def append_to_csv(profile: dict) -> None:
    # Check if founder already exists in CSV to prevent duplicates
    exists = False
    if os.path.exists(CSV_FILE):
        try:
            with open(CSV_FILE, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("full_name", "").lower() == profile["full_name"].lower():
                        exists = True
                        break
        except Exception as e:
            logger.warning("[CSV] Error reading CSV to check duplicate: %s", e)
            
    if exists:
        return
        
    file_exists = os.path.exists(CSV_FILE)
    try:
        with open(CSV_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            
            row = {
                "full_name": profile.get("full_name"),
                "linkedin_profile": profile.get("linkedin_profile"),
                "personal_website": profile.get("personal_website"),
                "startup_1_name": profile.get("startup_1_name"),
                "successful_startups": profile.get("successful_startups"),
                "email": profile.get("email"),
                "phone": profile.get("phone"),
            }
            writer.writerow(row)
        logger.info("[CSV] Appended founder | %s", profile["full_name"])
    except Exception as e:
        logger.error("[CSV] Failed to append founder %s: %s", profile["full_name"], e)


def founder_exists(full_name: str) -> bool:
    name = normalize_name(full_name)
    query = text(f"""
        SELECT id FROM {FOUNDERS_TABLE}
        WHERE LOWER(full_name) = LOWER(:full_name)
        LIMIT 1
    """)
    try:
        with engine.connect() as conn:
            row = conn.execute(query, {"full_name": name}).fetchone()
        return row is not None
    except SQLAlchemyError as exc:
        logger.warning("[SQL] Duplicate check failed: %s", exc)
        return False


def insert_founder(profile: dict) -> str:
    """Returns: inserted | skipped | error"""
    payload = build_insert_payload(profile)

    if founder_exists(payload["full_name"]):
        return "skipped"

    try:
        with engine.begin() as conn:
            conn.execute(INSERT_SQL, payload)
        return "inserted"
    except SQLAlchemyError as exc:
        err = str(exc).lower()
        if "duplicate" in err or "uq_founder" in err:
            return "skipped"
        logger.error("[SQL] Insert failed for %s: %s", payload["full_name"], exc)
        return "error"


def persist_profiles(profiles: list[dict]) -> dict[str, int]:
    stats = {"inserted": 0, "skipped": 0, "error": 0}
    seen: set[str] = set()

    for profile in profiles:
        name = profile.get("full_name", "unknown")
        name_lower = name.lower()

        if name_lower in seen:
            stats["skipped"] += 1
            logger.info("[SQL] SKIPPED duplicate | %s", name)
            continue
        seen.add(name_lower)

        result = insert_founder(profile)
        stats[result] = stats.get(result, 0) + 1

        if result == "inserted":
            logger.info("[SQL] Inserted founder | %s", name)
            append_to_csv(profile)
        elif result == "skipped":
            logger.info("[SQL] SKIPPED duplicate | %s", name)
        else:
            logger.info("[SQL] ERROR | %s", name)

    return stats
