# SD Open Play

Interactive weekly calendar of open play schedules across San Diego recreation centers.

**[View the live calendar →](https://myrastrong.github.io/sd-open-play)**

---

## What's in this repo

| File | Purpose |
|---|---|
| `index.html` | The calendar UI — loads `schedule.json` at runtime |
| `schedule.json` | All schedule data — edit this to update times or add centers |
| `scraper.py` | Fetches PDFs from sandiego.gov and updates `schedule.json` |
| `requirements.txt` | Python dependencies for the scraper |
| `.github/workflows/refresh-schedules.yml` | Runs the scraper every Saturday at 8am PT |

---

## Covered recreation centers

| Center | Address | Schedule source |
|---|---|---|
| Doyle Park | 8175 Regents Rd | [PDF](https://www.sandiego.gov/sites/default/files/doyleopenplay.pdf) |
| Ocean Air – Gym | 4770 Fairport Way | [PDF](https://www.sandiego.gov/sites/default/files/oceanairopenplaygym.pdf) |
| Ocean Air – Rooms | 4770 Fairport Way | [PDF](https://www.sandiego.gov/sites/default/files/oceanairopenplayrooms.pdf) |
| Balboa Park (BPAC) | 2145 Park Blvd | [PDF](https://www.sandiego.gov/sites/default/files/bpaccalendar.pdf) |
| Nobel Rec Center | 8810 Judicial Dr | [PDF](https://www.sandiego.gov/sites/default/files/nobelopenplay.pdf) |

---

## Adding a new recreation center

**Step 1 — Add the center to `schedule.json`**

In the `"centers"` array, add:
```json
{
  "id": "your_center_id",
  "label": "Full Center Name",
  "short": "Short Name",
  "addr": "Street Address",
  "col": "#HEX_COLOR",
  "bg": "#LIGHT_BG_HEX",
  "source_url": "https://www.sandiego.gov/sites/default/files/yourschedule.pdf"
}
```

**Step 2 — Add weekly events to `schedule.json`**

In the `"weekly_events"` array, add one entry per time block:
```json
{
  "center": "your_center_id",
  "dow": 1,
  "sport": "Basketball",
  "label": "Open Basketball",
  "sh": 14, "sm": 0,
  "eh": 17, "em": 0,
  "loc": "Gymnasium"
}
```
- `dow`: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
- `sh`/`sm`: start hour (24h) and minute
- `eh`/`em`: end hour (24h) and minute

**Step 3 — Optionally add a parser to `scraper.py`**

Add a `parse_your_center_id(text: str)` function and register it in the `PARSERS` dict. Without a parser the scraper will still fetch the PDF and print its text for manual review each Saturday.

**Step 4 — Commit** — the calendar and filter buttons update automatically.

---

## Running the scraper manually

```bash
# Install dependencies
pip install -r requirements.txt

# Dry run (fetch + parse, no file writes)
python3 scraper.py --dry-run

# Full run (updates schedule.json if changes detected)
python3 scraper.py
```

---

## How the auto-refresh works

Every **Saturday at 8:00 AM Pacific**, GitHub Actions:
1. Runs `scraper.py`
2. If `schedule.json` changed, commits and pushes the update
3. GitHub Pages redeploys automatically (~60 seconds)
4. The scraper log is saved as a workflow artifact (kept 30 days)

Centers without a parser (`ocean_rooms`, `balboa`, `nobel`) are fetched and their text is printed in the log for manual review. Check the Actions tab after each Saturday run.

To trigger a manual refresh anytime: **Actions → Refresh Open Play Schedules → Run workflow**.

---

## Notes

- All schedules are sourced from `sandiego.gov` PDFs and subject to change without notice.
- Confirm times with each center before visiting.
- Nobel Recreation Center's gym open play PDF is image-only; that schedule requires manual verification.
