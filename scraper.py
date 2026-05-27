#!/usr/bin/env python3
"""
SD Open Play — Schedule Scraper
================================
Fetches open play PDF schedules from sandiego.gov, extracts text,
and updates schedule.json with any detected changes.

Run manually:
    python3 scraper.py

Run with dry-run (no file writes):
    python3 scraper.py --dry-run

Designed to be called by GitHub Actions every Saturday morning.
"""

import argparse
import io
import json
import re
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("Missing dependency: pip install pypdf")


# ── CONFIG ────────────────────────────────────────────────────────────────────

SCHEDULE_JSON = Path(__file__).parent / "schedule.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sandiego.gov/",
}

REQUEST_TIMEOUT = 20  # seconds


# ── PARSER REGISTRY ──────────────────────────────────────────────────────────
#
# Each center has an optional parse function:
#   parse_<center_id>(text: str) -> list[dict] | None
#
# Returns:
#   list[dict]  — new weekly_events rows to replace the center's existing ones
#   None        — parsing failed or schedule unchanged; keep existing data
#
# If a center has no parser, the scraper still fetches the PDF and reports
# its text so you can write a parser for it.
#
# TIME FORMAT: all times are integers. "2:30pm" → sh=14, sm=30
# DOW:  0=Sun  1=Mon  2=Tue  3=Wed  4=Thu  5=Fri  6=Sat

def _parse_time(token: str):
    """'2:30pm' | '10:15am' | '9am' → (hour_24, minute)"""
    token = token.strip().lower()
    m = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)", token)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, mn


def _time_range(text: str):
    """'2:30-3:30pm' | '10:15am-2:30pm' → (sh,sm,eh,em) or None"""
    text = text.strip().lower()
    # Handle ranges like '10:15am-2:30pm' or '2:30-3:30pm' (shared am/pm suffix)
    m = re.match(r"(\d{1,2}(?::\d{2})?(?:am|pm)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?(?:am|pm)?)", text)
    if not m:
        return None
    start_raw, end_raw = m.group(1), m.group(2)
    # If start has no am/pm but end does, infer from end
    if not re.search(r"(am|pm)", start_raw) and re.search(r"(am|pm)", end_raw):
        suffix = re.search(r"(am|pm)", end_raw).group(1)
        start_raw = start_raw + suffix
    t1 = _parse_time(start_raw)
    t2 = _parse_time(end_raw)
    if not t1 or not t2:
        return None
    return t1[0], t1[1], t2[0], t2[1]


# Ocean Air Gym ─────────────────────────────────────────────────────────────
def parse_ocean_gym(text: str):
    """
    Parse oceanairopenplaygym.pdf
    Expected sections: Basketball, Badminton, Pickleball, Volleyball
    Each followed by day+time lines.
    """
    DOW_MAP = {
        "monday": 1, "tuesday": 2, "wednesday": 3,
        "thursday": 4, "friday": 5, "friday s": 5, "saturdays": 6, "saturday": 6,
    }
    SPORT_SECTION = {
        "basketball": "Basketball",
        "badminton":  "Badminton",
        "pickleball": "Pickleball",
        "volleyball": "Volleyball",
    }

    events = []
    current_sport = None

    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue

        # Detect sport section header
        for kw, sport in SPORT_SECTION.items():
            if line == kw or line.startswith(kw + " "):
                current_sport = sport
                break

        if not current_sport:
            continue

        # Try to parse a "Day HH:MM-HH:MMam/pm" line
        # e.g. "Monday 2:30-3:30pm" or "Friday 10:15am-2:30pm & 2:30-6:45pm 3 courts only"
        day_match = re.match(
            r"(monday|tuesday|wednesday|thursday|friday[s]?|saturday[s]?)\s+(.*)",
            line
        )
        if not day_match:
            continue

        day_str = day_match.group(1).rstrip("s")  # normalize "fridays" → "friday"
        rest    = day_match.group(2)

        dow = DOW_MAP.get(day_str) or DOW_MAP.get(day_str + "s")
        if dow is None:
            continue

        # Extract all time ranges on this line (handles "x & y" style)
        ranges = re.findall(
            r"\d{1,2}(?::\d{2})?(?:am|pm)?\s*[-–]\s*\d{1,2}(?::\d{2})?(?:am|pm)?",
            rest
        )

        for i, rng in enumerate(ranges):
            times = _time_range(rng)
            if not times:
                continue
            sh, sm, eh, em = times

            # Build label — note 3 courts annotation for Fri Pickleball session 2
            label = current_sport
            if current_sport == "Pickleball" and dow == 5:
                if i == 0:
                    label = "Pickleball"
                else:
                    label = "Pickleball (3 courts)"
            elif current_sport == "Volleyball":
                label = "Volleyball (Adults)"

            # Merge back-to-back Pickleball Friday sessions into one block
            if current_sport == "Pickleball" and dow == 5 and len(ranges) == 2:
                if i == 1:
                    # Extend the previous event's end time
                    for ev in reversed(events):
                        if ev["center"] == "ocean_gym" and ev["dow"] == 5 and ev["sport"] == "Pickleball":
                            ev["eh"] = eh
                            ev["em"] = em
                            ev["label"] = "Pickleball (3 courts from 2:30pm)"
                            break
                    continue  # don't add a second event

            events.append({
                "center": "ocean_gym",
                "dow":    dow,
                "sport":  current_sport,
                "label":  label,
                "sh": sh, "sm": sm,
                "eh": eh, "em": em,
                "loc": "Gymnasium",
            })

    return events if events else None


# Doyle Park ─────────────────────────────────────────────────────────────────
def parse_doyle(text: str):
    """
    Parse doyleopenplay.pdf — gym open play schedule.
    Looks for Basketball, Volleyball, Badminton sections.
    """
    DOW_MAP = {"monday":1,"tuesday":2,"wednesday":3,"thursday":4,"friday":5,"saturday":6}
    SPORT_MAP = {"basketball":"Basketball","volleyball":"Volleyball","badminton":"Badminton"}
    LOC_MAP   = {"basketball":"Gym","volleyball":"Gym","badminton":"Gym"}

    events = []
    current_sport = None

    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue
        for kw, sport in SPORT_MAP.items():
            if line == kw or line.startswith(kw + " "):
                current_sport = sport
                break
        if not current_sport:
            continue
        day_match = re.match(r"(monday|tuesday|wednesday|thursday|friday|saturday)\s+(.*)", line)
        if not day_match:
            continue
        dow  = DOW_MAP.get(day_match.group(1))
        rest = day_match.group(2)
        if dow is None:
            continue
        ranges = re.findall(
            r"\d{1,2}(?::\d{2})?(?:am|pm)?\s*[-–]\s*\d{1,2}(?::\d{2})?(?:am|pm)?",
            rest
        )
        for rng in ranges:
            times = _time_range(rng)
            if not times:
                continue
            sh, sm, eh, em = times
            events.append({
                "center": "doyle",
                "dow":    dow,
                "sport":  current_sport,
                "label":  f"Open {current_sport}",
                "sh": sh, "sm": sm,
                "eh": eh, "em": em,
                "loc": LOC_MAP.get(current_sport.lower(), "Gym"),
            })

    return events if events else None


# ── CENTERS THAT NEED MANUAL REVIEW ────────────────────────────────────────
# ocean_rooms, balboa, and nobel PDFs are either image-based or use a
# calendar-grid layout that doesn't parse reliably with text extraction.
# The scraper will still fetch and print their text so you can manually
# verify nothing has changed.

PARSERS = {
    "doyle":     parse_doyle,
    "ocean_gym": parse_ocean_gym,
    # These centers are flagged for manual review (see above):
    "ocean_rooms": None,
    "balboa":      None,
    "nobel":       None,
}


# ── FETCH ─────────────────────────────────────────────────────────────────────

def fetch_pdf_text(url: str) -> tuple[str | None, str | None]:
    """
    Download PDF from url and return (extracted_text, error_message).
    Returns (None, error) on failure.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return None, f"HTTP error: {e}"

    try:
        reader = PdfReader(io.BytesIO(resp.content))
        pages = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        text = "\n".join(pages).strip()
        if not text:
            return None, "PDF extracted but contained no readable text (may be image-only)"
        return text, None
    except Exception as e:
        return None, f"PDF parse error: {e}"


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    print(f"\n{'='*60}")
    print(f"SD Open Play Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # Load existing schedule
    if not SCHEDULE_JSON.exists():
        sys.exit(f"ERROR: {SCHEDULE_JSON} not found. Run from the repo root.")
    with open(SCHEDULE_JSON) as f:
        schedule = json.load(f)

    center_map = {c["id"]: c for c in schedule["centers"]}
    results = {}  # center_id → {status, message, new_events}

    for center in schedule["centers"]:
        cid   = center["id"]
        label = center["label"]
        url   = center.get("source_url", "")

        print(f"── {label} ({cid})")

        if not url:
            print("   ⚠  No source_url — skipping\n")
            results[cid] = {"status": "skipped", "message": "No source_url configured"}
            continue

        # Fetch PDF text
        text, err = fetch_pdf_text(url)
        if err:
            print(f"   ✗  Fetch failed: {err}")
            print(f"   ⚠  Keeping existing schedule data\n")
            results[cid] = {"status": "fetch_failed", "message": err}
            continue

        print(f"   ✓  Fetched PDF ({len(text)} chars extracted)")

        # Run parser if available
        parser = PARSERS.get(cid)
        if parser is None:
            print(f"   ℹ  No auto-parser — MANUAL REVIEW REQUIRED")
            print(f"   📄 Extracted text preview:\n")
            for line in text.splitlines()[:30]:
                print(f"      {line}")
            if len(text.splitlines()) > 30:
                print(f"      ... ({len(text.splitlines())-30} more lines)")
            print()
            results[cid] = {"status": "manual_review", "message": "No parser; text extracted for manual review", "text": text}
            continue

        try:
            new_events = parser(text)
        except Exception:
            tb = traceback.format_exc()
            print(f"   ✗  Parser crashed:\n{tb}")
            results[cid] = {"status": "parse_failed", "message": tb}
            continue

        if not new_events:
            print(f"   ⚠  Parser returned no events — keeping existing data\n")
            results[cid] = {"status": "parse_empty", "message": "Parser returned no results"}
            continue

        print(f"   ✓  Parsed {len(new_events)} events")
        results[cid] = {"status": "ok", "new_events": new_events}

        # Show diff
        existing = [e for e in schedule["weekly_events"]
                    if not e.get("_comment") and e.get("center") == cid]
        if existing == new_events:
            print(f"   ✓  No changes detected\n")
            results[cid]["changed"] = False
        else:
            print(f"   🔄 Changes detected: {len(existing)} → {len(new_events)} events")
            results[cid]["changed"] = True
        print()

    # Apply updates
    any_changed = any(
        r.get("status") == "ok" and r.get("changed") for r in results.values()
    )

    if not any_changed:
        print("No schedule changes to write.\n")
    elif dry_run:
        print("DRY RUN — changes detected but not written to schedule.json\n")
        print("Centers with changes:")
        for cid, r in results.items():
            if r.get("changed"):
                print(f"  • {cid}: {len(r['new_events'])} new events")
    else:
        # Rebuild weekly_events: preserve comment entries, replace parsed center data
        comment_entries   = [e for e in schedule["weekly_events"] if e.get("_comment") is not None]
        unchanged_entries = [
            e for e in schedule["weekly_events"]
            if e.get("_comment") is None and e.get("center") not in
            {cid for cid, r in results.items() if r.get("status") == "ok" and r.get("changed")}
        ]

        updated_entries = []
        for cid, r in results.items():
            if r.get("status") == "ok" and r.get("changed"):
                # Insert a comment marker before the center's block
                c = center_map[cid]
                updated_entries.append({"_comment": f"── {c['label'].upper()} ─────────────────────"})
                updated_entries.extend(r["new_events"])

        schedule["weekly_events"] = comment_entries + unchanged_entries + updated_entries
        schedule["_meta"]["last_updated"] = date.today().isoformat()

        with open(SCHEDULE_JSON, "w") as f:
            json.dump(schedule, f, indent=2)

        print(f"✅ schedule.json updated (last_updated: {schedule['_meta']['last_updated']})\n")

    # Print summary
    print("─" * 60)
    print("Summary:")
    for cid, r in results.items():
        status  = r["status"]
        changed = r.get("changed")
        if status == "ok":
            icon = "🔄" if changed else "✓ "
            msg  = "updated" if changed else "no changes"
        elif status == "manual_review":
            icon = "👁 "
            msg  = "manual review required"
        elif status == "fetch_failed":
            icon = "✗ "
            msg  = f"fetch failed: {r['message']}"
        elif status == "parse_failed":
            icon = "✗ "
            msg  = "parser crashed"
        elif status == "parse_empty":
            icon = "⚠ "
            msg  = "parser returned nothing"
        else:
            icon = "–  "
            msg  = status
        print(f"  {icon} {cid:15s} {msg}")

    manual = [cid for cid, r in results.items() if r["status"] == "manual_review"]
    failed = [cid for cid, r in results.items() if r["status"] in ("fetch_failed","parse_failed","parse_empty")]

    print()
    if manual:
        print(f"⚠  Manual review needed for: {', '.join(manual)}")
        print("   Open schedule.json and compare the printed text above against the current event data.")
    if failed:
        print(f"✗  Failed centers: {', '.join(failed)} — existing data preserved")
    if not manual and not failed:
        print("✅ All centers processed successfully.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and update SD open play schedules")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but don't write changes to schedule.json")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
