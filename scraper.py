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
    """'2:30-3:30pm' | '10:15am-2:30pm' | '9-2:50pm' | '6-7:45pm' → (sh,sm,eh,em) or None"""
    text = text.strip().lower()
    m = re.match(r"(\d{1,2}(?::\d{2})?(?:am|pm)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?(?:am|pm)?)", text)
    if not m:
        return None
    start_raw, end_raw = m.group(1), m.group(2)

    # If start has no am/pm suffix, infer it from the end token.
    # Naive inheritance ("9-2:50pm" → start gets "pm" → 9pm=21) is wrong when
    # applying the end's suffix would make start LATER than end in 24h time.
    # In that case the start must be AM (morning open play sessions never start
    # in the evening and end before the next hour).
    if not re.search(r"(am|pm)", start_raw) and re.search(r"(am|pm)", end_raw):
        end_suffix = re.search(r"(am|pm)", end_raw).group(1)
        start_h = int(re.match(r"(\d{1,2})", start_raw).group(1))
        end_h_raw = int(re.match(r"(\d{1,2})", end_raw).group(1))

        # Convert end to 24h to compare
        end_h24 = end_h_raw + (12 if end_suffix == "pm" and end_h_raw != 12 else 0)
        start_h24_if_pm = start_h + (12 if start_h != 12 else 0)

        # If giving start the same suffix as end makes it >= end in 24h, use "am"
        if end_suffix == "pm" and start_h24_if_pm >= end_h24:
            start_raw = start_raw + "am"
        else:
            start_raw = start_raw + end_suffix

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
    current_sport        = None
    pending_continuation = None  # holds (dow, sport, partial_rest) for "&"-wrapped lines

    for raw_line in text.splitlines():
        line = raw_line.strip().lower()

        # Detect sport section header (skip blank lines)
        if not line:
            continue
        for kw, sport in SPORT_SECTION.items():
            if line == kw or line.startswith(kw + " "):
                current_sport = sport
                pending_continuation = None  # reset on new section
                break

        if not current_sport:
            continue

        # ── Handle line continuations ──────────────────────────────────────
        # The PDF sometimes wraps a single day's schedule across two lines:
        #   "Friday 10:15am-2:30pm & "   ← ends with "&", ranges continue below
        #   "2:30-6:45pm 3 courts only"  ← continuation line has no day name
        # Strategy: if the day-line ends with "&", stash it in pending_continuation.
        # On the next iteration, if no day name is found and we have a pending
        # stash, merge the two lines and parse the combined result.

        day_match = re.match(
            r"(?:\w+\s+)?(monday|tuesday|wednesday|thursday|friday[s]?|saturday[s]?)\s+(.*)",
            line
        )

        if day_match:
            day_str = day_match.group(1).rstrip("s")  # "fridays" → "friday"
            rest    = day_match.group(2)
            dow     = DOW_MAP.get(day_str) or DOW_MAP.get(day_str + "s")
            if dow is None:
                continue

            # If rest contains "& <day_name> <times>", split into multiple
            # (dow, rest) pairs and process each independently.
            # e.g. "6-7:45pm & saturdays 11-2:45pm" → Tue segment + Sat segment
            DAY_NAMES = r"monday|tuesday|wednesday|thursday|friday[s]?|saturday[s]?"
            sub_day_split = re.split(r"&\s*(" + DAY_NAMES + r")\s+", rest)
            # sub_day_split[0] = first segment's times, then alternating day/times
            # e.g. ["6-7:45pm ", "saturday", "11-2:45pm"]
            day_rest_pairs = [(dow, sub_day_split[0].strip())]
            for k in range(1, len(sub_day_split) - 1, 2):
                sub_day = sub_day_split[k].rstrip("s")
                sub_dow = DOW_MAP.get(sub_day) or DOW_MAP.get(sub_day + "s")
                sub_rest = sub_day_split[k + 1].strip() if k + 1 < len(sub_day_split) else ""
                if sub_dow is not None and sub_rest:
                    day_rest_pairs.append((sub_dow, sub_rest))

            if len(day_rest_pairs) > 1:
                # Multiple segments — queue them all for processing below
                # by iterating; set dow/rest to first and push the rest onto a
                # small local stack processed after this iteration.
                extra_segments = day_rest_pairs[1:]
                dow, rest = day_rest_pairs[0]
            else:
                extra_segments = []

            # If the single rest ends with "&", schedule continues on next line
            if not extra_segments and rest.rstrip().endswith("&"):
                pending_continuation = (dow, current_sport, rest.rstrip().rstrip("&").rstrip())
                continue
            # Otherwise clear any stale pending state
            pending_continuation = None

        elif pending_continuation is not None:
            # This line is a continuation of the previous day's schedule
            dow, current_sport, partial_rest = pending_continuation
            rest = partial_rest + " " + line
            pending_continuation = None
            extra_segments = []

        else:
            # No day name, no pending continuation — not a schedule line
            continue

        # ── Extract and append events for each (dow, rest) segment ───────────
        def _append_events_for_segment(seg_dow, seg_rest):
            ranges = re.findall(
                r"\d{1,2}(?::\d{2})?(?:am|pm)?\s*[-–]\s*\d{1,2}(?::\d{2})?(?:am|pm)?",
                seg_rest
            )
            for i, rng in enumerate(ranges):
                times = _time_range(rng)
                if not times:
                    continue
                sh, sm, eh, em = times

                label = current_sport
                if current_sport == "Volleyball":
                    label = "Volleyball (Adults)"

                # Pickleball Friday: merge all ranges into one continuous block
                if current_sport == "Pickleball" and seg_dow == 5 and len(ranges) > 1:
                    if i == 0:
                        label = "Pickleball"
                    else:
                        for ev in reversed(events):
                            if (ev["center"] == "ocean_gym"
                                    and ev["dow"] == 5
                                    and ev["sport"] == "Pickleball"):
                                ev["eh"]    = eh
                                ev["em"]    = em
                                ev["label"] = "Pickleball (3 courts from 2:30pm)"
                                break
                        return  # don't append second block
                    # fall through to append first block

                events.append({
                    "center": "ocean_gym",
                    "dow":    seg_dow,
                    "sport":  current_sport,
                    "label":  label,
                    "sh": sh, "sm": sm,
                    "eh": eh, "em": em,
                    "loc": "Gymnasium",
                })

        _append_events_for_segment(dow, rest)
        for seg_dow, seg_rest in extra_segments:
            _append_events_for_segment(seg_dow, seg_rest)

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


# ── DATE HELPERS ──────────────────────────────────────────────────────────────

def current_month_str() -> str:
    """Returns e.g. 'June 2026' for the current month."""
    today = date.today()
    return today.strftime("%B %Y")  # e.g. "June 2026"


def roll_date_overrides(overrides: list, old_month_str: str, new_month_str: str) -> list:
    """
    Re-date any date_overrides entries whose dates fall in old_month to new_month.

    Overrides with known recurring patterns (Memorial Day, holiday closures) are
    automatically re-computed for the new month. Overrides for events that are
    one-off and month-specific (tournaments, special events) are dropped and
    logged so the operator knows to re-add them manually.

    Returns the updated overrides list.
    """
    from calendar import monthcalendar, MONDAY

    # Parse old and new month
    old_dt  = datetime.strptime(old_month_str, "%B %Y")
    new_dt  = datetime.strptime(new_month_str, "%B %Y")
    new_y   = new_dt.year
    new_m   = new_dt.month
    days_in_new = (date(new_y, new_m % 12 + 1, 1) - date(new_y, new_m, 1)).days \
                  if new_m < 12 else 31

    if old_dt == new_dt:
        return overrides  # same month, nothing to do

    print(f"\n   ⟳  Rolling date_overrides from {old_month_str} → {new_month_str}")

    # Find federal holidays that fall in the new month
    new_holidays = {}  # day → label

    # Memorial Day: last Monday of May
    if new_m == 5:
        cal = monthcalendar(new_y, 5)
        last_monday = max(week[MONDAY] for week in cal if week[MONDAY] != 0)
        new_holidays[last_monday] = "Memorial Day – All Centers Closed"

    # Independence Day: July 4
    if new_m == 7:
        new_holidays[4] = "Independence Day – All Centers Closed"

    # Labor Day: first Monday of September
    if new_m == 9:
        cal = monthcalendar(new_y, 9)
        first_monday = next(week[MONDAY] for week in cal if week[MONDAY] != 0)
        new_holidays[first_monday] = "Labor Day – All Centers Closed"

    # Veterans Day: November 11
    if new_m == 11:
        new_holidays[11] = "Veterans Day – All Centers Closed"

    # Thanksgiving: fourth Thursday of November
    if new_m == 11:
        from calendar import THURSDAY
        cal = monthcalendar(new_y, 11)
        thursdays = [week[THURSDAY] for week in cal if week[THURSDAY] != 0]
        new_holidays[thursdays[3]] = "Thanksgiving – All Centers Closed"

    # Christmas Day: December 25
    if new_m == 12:
        new_holidays[25] = "Christmas Day – All Centers Closed"

    # New Year's Day: January 1
    if new_m == 1:
        new_holidays[1] = "New Year's Day – All Centers Closed"

    kept     = []
    dropped  = []
    added    = []

    for ov in overrides:
        d_str = ov.get("date", "")
        label = ov.get("label", "")

        # Keep non-dated overrides as-is
        if not d_str:
            kept.append(ov)
            continue

        # Check if this override is a known federal holiday → will be re-generated
        is_holiday = any(h in label for h in [
            "Memorial Day", "Independence Day", "Labor Day",
            "Veterans Day", "Thanksgiving", "Christmas", "New Year"
        ])
        if is_holiday:
            continue  # will be replaced below

        # Non-holiday one-off events (tournaments, special events) are dropped
        dropped.append(label)

    # Add auto-generated holidays for the new month
    for day, label in new_holidays.items():
        date_str = f"{new_y}-{str(new_m).padStart(2, '0')}-{str(day).padStart(2, '0')}" \
            if False else f"{new_y}-{new_m:02d}-{day:02d}"
        entry = {
            "date":    date_str,
            "center":  "all",
            "blocked": True,
            "all_day": True,
            "label":   label,
            "sh": 9, "sm": 0, "eh": 21, "em": 0,
            "sport":   "Other",
        }
        kept.append(entry)
        added.append(f"{date_str}: {label}")

    if added:
        print(f"   ✓  Auto-added holidays: {', '.join(added)}")
    if dropped:
        print(f"   ⚠  Dropped one-off overrides (add manually if still relevant):")
        for d in dropped:
            print(f"      • {d}")

    kept.sort(key=lambda x: x.get("date", ""))
    return kept


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

    # Determine whether _meta.month needs rolling to the current month
    today          = date.today()
    current_month  = today.strftime("%B %Y")     # e.g. "June 2026"
    existing_month = schedule["_meta"].get("month", "")
    month_changed  = current_month != existing_month

    if month_changed:
        print(f"\n📅 Month change detected: {existing_month!r} → {current_month!r}")

    # Treat a month change as a write-worthy change even if no events differ
    any_changed = month_changed or any(
        r.get("status") == "ok" and r.get("changed") for r in results.values()
    )

    if not any_changed:
        print("No schedule changes to write.\n")
    elif dry_run:
        print("DRY RUN — changes detected but not written to schedule.json\n")
        if month_changed:
            print(f"  • _meta.month would update: {existing_month!r} → {current_month!r}")
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
                c = center_map[cid]
                updated_entries.append({"_comment": f"── {c['label'].upper()} ─────────────────────"})
                updated_entries.extend(r["new_events"])

        schedule["weekly_events"] = comment_entries + unchanged_entries + updated_entries

        # Roll date_overrides forward if month changed
        if month_changed:
            schedule["date_overrides"] = roll_date_overrides(
                schedule.get("date_overrides", []),
                existing_month,
                current_month,
            )

        # Update _meta with current month and today's date
        schedule["_meta"]["month"]        = current_month
        schedule["_meta"]["last_updated"] = today.isoformat()

        with open(SCHEDULE_JSON, "w") as f:
            json.dump(schedule, f, indent=2)

        print(f"✅ schedule.json updated — month: {current_month}, last_updated: {today.isoformat()}\n")

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
