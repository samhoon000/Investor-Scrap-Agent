"""
YC-only founder-turned-angel discovery agent.

Pipeline: YC companies → company page → founders → validate → MySQL.
"""

from __future__ import annotations

import argparse
import logging
import sys

from config import MAX_PROFILES_PER_RUN, SOURCE_NAME
from parser import build_sql_profile, validate_founder_angel
from scraper import scrape_source
from sql_handler import ensure_founders_table, persist_profiles, founder_exists

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fundeable-agent")


def process_yc() -> tuple[list[dict], dict[str, int]]:
    accepted: list[dict] = []
    stats = {
        "checked": 0,
        "accepted": 0,
        "filtered": 0,
        "skipped_quality": 0,
    }

    logger.info(
        "[YC] Starting company-first discovery (max %d inserts)",
        MAX_PROFILES_PER_RUN,
    )

    for raw in scrape_source(SOURCE_NAME):
        if stats["accepted"] >= MAX_PROFILES_PER_RUN:
            logger.info(
                "[YC] Reached max %d profiles for this run — stopping",
                MAX_PROFILES_PER_RUN,
            )
            break

        stats["checked"] += 1
        name = raw.get("full_name") or "unknown"

        logger.info("[YC] Founder found: %s", name)

        # Early duplicate check
        if founder_exists(name):
            logger.info("[SQL] SKIPPED duplicate | %s", name)
            continue

        # Use person-centric founder_profile_text for validation
        founder_profile_text = raw.get("founder_profile_text") or raw.get("bio") or ""

        res = validate_founder_angel(name, founder_profile_text)
        
        # Log scores before filtering/acceptance
        logger.info("[YC] founder_score=%d investor_score=%d", res["founder_score"], res["investor_score"])

        if not res["valid"]:
            stats["filtered"] += 1
            logger.info("[YC] FILTERED → %s", res["reason"])
            continue

        profile = build_sql_profile(raw)
        if not profile:
            stats["skipped_quality"] += 1
            logger.info("[SKIPPED] Insufficient structured data")
            continue

        stats["accepted"] += 1
        accepted.append(profile)
        logger.info("[YC] ACCEPTED → %s", name)
        
        # Log why accepted with matched signals
        if res.get("matched_founder_signal"):
            logger.info('[YC] Founder signal = "%s"', res["matched_founder_signal"])
        if res.get("matched_investor_signal"):
            logger.info('[YC] Investor signal = "%s"', res["matched_investor_signal"])

    return accepted, stats


def run_agent() -> int:
    ensure_founders_table()

    profiles, stats = process_yc()

    logger.info(
        "[YC] Checked %d founders | Accepted %d | Filtered %d | Skipped (quality) %d",
        stats["checked"],
        stats["accepted"],
        stats["filtered"],
        stats["skipped_quality"],
    )

    if not profiles:
        logger.info("[SQL] No new founders to insert")
        return 0

    db_stats = persist_profiles(profiles)

    logger.info(
        "[SQL] Done — inserted: %d | skipped: %d | errors: %d",
        db_stats.get("inserted", 0),
        db_stats.get("skipped", 0),
        db_stats.get("error", 0),
    )

    return 0 if db_stats.get("error", 0) == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YC-only founder-angel discovery agent.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        exit_code = run_agent()
    except KeyboardInterrupt:
        logger.info("[YC] Interrupted by user")
        exit_code = 130
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
