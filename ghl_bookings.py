#!/usr/bin/env python3
"""Count today's and yesterday's bookings on the Book-A-Call GHL calendar.

Writes COUNTS ONLY (no names, emails, or contact ids) to the path given as argv[1].
The dashboard reads that file for its "Scheduled Calls" number, because GHL is the
source of truth for a booked call; Meta's Schedule event is the fallback.

The API key arrives as the GHL_API_KEY environment variable (an Actions secret).
It must never be written into this repo or the published page: it can read every
CFS contact.
"""
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCATION_ID = "drk0ArGlymV297Q7swkl"
# "Real Estate College Funding Secrets 1 on 1 mtg": the calendar embedded on the
# Book-A-Call funnel. The other calendars in the location are not this funnel.
CALENDAR_ID = "jz4PWYKlNfHjOEj0p1p5"
# Both Meta ad accounts report in America/Denver, so "today" here matches Meta's.
TZ = ZoneInfo("America/Denver")


def fetch_events(key, start, end):
    url = (
        "https://services.leadconnectorhq.com/calendars/events"
        f"?locationId={LOCATION_ID}&calendarId={CALENDAR_ID}"
        f"&startTime={int(start.timestamp() * 1000)}&endTime={int(end.timestamp() * 1000)}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Version": "2021-04-15",
        "Accept": "application/json",
        # GHL's Cloudflare blocks the default python-urllib user agent with a 1010
        # that looks like an auth failure.
        "User-Agent": "curl/8.7.1",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("events", [])


def main():
    key = os.environ.get("GHL_API_KEY", "").strip()
    if not key:
        sys.exit("GHL_API_KEY is not set")
    out_path = sys.argv[1] if len(sys.argv) > 1 else "bookings.json"

    now = datetime.now(TZ)
    today = now.date()
    yesterday = today - timedelta(days=1)
    wanted = {today.isoformat(), yesterday.isoformat()}

    # The API filters on appointment START time, but a booking counts on the day it
    # was MADE. The calendar only books 4 hours to 4 days ahead, so every booking
    # made yesterday or today starts inside this window.
    start = datetime.combine(yesterday, datetime.min.time(), TZ)
    end = datetime.combine(today + timedelta(days=7), datetime.min.time(), TZ)
    events = fetch_events(key, start, end)

    days = {d: {"total": 0, "status": Counter(), "source": Counter(), "by_hour": Counter()}
            for d in sorted(wanted)}
    for ev in events:
        if ev.get("deleted") or not ev.get("dateAdded"):
            continue
        made = datetime.fromisoformat(ev["dateAdded"].replace("Z", "+00:00")).astimezone(TZ)
        day = made.date().isoformat()
        if day not in wanted:
            continue
        bucket = days[day]
        bucket["total"] += 1
        bucket["status"][ev.get("appointmentStatus") or "unknown"] += 1
        bucket["source"][(ev.get("createdBy") or {}).get("source") or "unknown"] += 1
        bucket["by_hour"][str(made.hour)] += 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": "America/Denver",
        "calendar_id": CALENDAR_ID,
        "definition": "Appointments on the Book-A-Call calendar, counted on the day they were booked (dateAdded), deleted excluded.",
        "days": {d: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in b.items()}
                 for d, b in days.items()},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(json.dumps({d: b["total"] for d, b in payload["days"].items()}), "->", out_path)


if __name__ == "__main__":
    main()
