#!/usr/bin/env python3
"""Count today's and yesterday's bookings on the Book-A-Call GHL calendars.

Writes COUNTS ONLY (no names, emails, or contact ids) to the path given as argv[1].
The dashboard reads that file for its "College Funding Calls" number, because GHL is the
source of truth for a booked call; Meta's Schedule event is the fallback.

The API key arrives as the GHL_API_KEY environment variable (an Actions secret).
It must never be written into this repo or the published page: it can read every
CFS contact.
"""
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

LOCATION_ID = "drk0ArGlymV297Q7swkl"
# The calendars the Book-A-Call funnel books into. The other calendars in the location
# are closer / specialist calendars, not this funnel.
# 2026-11-11 (Phil): GHL keeps renaming these, and bookings come from THREE funnel pages, so
# the list is read live off those pages every run (any embedded id that is a calendar in the
# location counts). KNOWN_CALENDARS stays as a floor so a swap does not drop past bookings,
# and a calendar once seen on a page keeps counting (SEEN file, local publisher only).
#   jz4PWYKlNfHjOEj0p1p5  original Book-A-Call calendar, on the Real Estate CFS home page
#   OktfSo1igkPAh08dRHHP  round robin since 2026-11-06, on college.legetty.com/meta + event.legetty.com/meta
FUNNEL_PAGES = {
    "College Legetty": "https://college.legetty.com/meta",
    "Event Legetty": "https://event.legetty.com/meta",
    "Real Estate CFS": "https://realestatecollegefundingsecrets.com/",
}
KNOWN_CALENDARS = {"jz4PWYKlNfHjOEj0p1p5", "OktfSo1igkPAh08dRHHP"}
GHL_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{20}(?![A-Za-z0-9])")
# Both Meta ad accounts report in America/Denver, so "today" here matches Meta's.
TZ = ZoneInfo("America/Denver")


def ghl_get(key, url, version="2021-04-15"):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "Version": version,
        "Accept": "application/json", "User-Agent": "curl/8.7.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def funnel_calendars(key, seen_path):
    """{calendar id: {"name": current GHL name, "pages": [funnel pages embedding it]}}."""
    names = {c["id"]: c["name"] for c in ghl_get(
        key, f"https://services.leadconnectorhq.com/calendars/?locationId={LOCATION_ID}").get("calendars", [])}
    try:
        with open(seen_path) as f:
            seen = set(json.load(f))
    except Exception:
        seen = set()
    found, notes = {}, []
    for label, url in FUNNEL_PAGES.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            notes.append(f"could not read {label} page: {exc}")
            continue
        hits = set(GHL_ID.findall(html)) & names.keys()
        if not hits:
            notes.append(f"no calendar found on {label} page")
        for cid in hits:
            found.setdefault(cid, []).append(label)
    ids = KNOWN_CALENDARS | seen | set(found)
    try:
        with open(seen_path, "w") as f:
            json.dump(sorted(ids), f)
    except Exception:
        pass
    for n in notes:
        print(n, file=sys.stderr)
    return {cid: {"name": names.get(cid, "(deleted calendar)"), "pages": found.get(cid, [])}
            for cid in sorted(ids)}, notes


def fetch_events(key, calendar_id, start, end):
    url = (
        "https://services.leadconnectorhq.com/calendars/events"
        f"?locationId={LOCATION_ID}&calendarId={calendar_id}"
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


def contact_source(key, contact_id, cache):
    """Classify a booking's first-touch source: "meta" or "other" (Google, YouTube, organic search).

    Added 2026-11-03: the calendar is shared by every funnel, so Google/YouTube bookings
    (event.legetty.com/youtube, /google) were inflating the Meta dashboard. Anything that
    is not clearly search/YouTube counts as Meta, the same as before this change.
    """
    if contact_id in cache:
        return cache[contact_id]
    kind = "meta"
    try:
        url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}", "Version": "2021-07-28",
            "Accept": "application/json", "User-Agent": "curl/8.7.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            c = json.load(resp).get("contact", {})
        # Check BOTH touches: a contact can first arrive via Meta and then book through the
        # Google/YouTube page (lastAttributionSource), which the first touch alone misses.
        for a in (c.get("attributionSource") or {}, c.get("lastAttributionSource") or {}):
            page = urlparse(a.get("url") or "")
            path = page.path.rstrip("/").lower()
            if ((a.get("sessionSource") or "") in ("Paid Search", "Organic Search")
                    or (a.get("utmSource") or "").lower() in ("google", "youtube")
                    or (page.netloc.endswith("event.legetty.com") and path in ("/youtube", "/google"))):
                kind = "other"
    except Exception as exc:  # a failed lookup must not drop a booking: count it as Meta
        print("source lookup failed:", exc, file=sys.stderr)
    cache[contact_id] = kind
    return kind


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
    # was MADE. The calendars book only a few days ahead, so every booking made
    # yesterday or today starts inside this window (14 days covers a wider setting).
    start = datetime.combine(yesterday, datetime.min.time(), TZ)
    end = datetime.combine(today + timedelta(days=14), datetime.min.time(), TZ)
    seen_path = os.environ.get("CALENDARS_SEEN_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(out_path)), "calendars_seen.json")
    calendars, notes = funnel_calendars(key, seen_path)
    events = []
    for cal_id, cal in calendars.items():
        for ev in fetch_events(key, cal_id, start, end):
            ev["_calendar"] = cal["name"]
            events.append(ev)

    cache = {}
    days = {d: {"total": 0, "status": Counter(), "source": Counter(), "by_hour": Counter(), "by_calendar": Counter(), "all_total": 0, "other_sources": 0}
            for d in sorted(wanted)}
    for ev in events:
        if ev.get("deleted") or not ev.get("dateAdded"):
            continue
        made = datetime.fromisoformat(ev["dateAdded"].replace("Z", "+00:00")).astimezone(TZ)
        day = made.date().isoformat()
        if day not in wanted:
            continue
        bucket = days[day]
        bucket["all_total"] += 1
        if contact_source(key, ev.get("contactId"), cache) == "other":
            bucket["other_sources"] += 1
            continue
        bucket["total"] += 1
        bucket["status"][ev.get("appointmentStatus") or "unknown"] += 1
        bucket["source"][(ev.get("createdBy") or {}).get("source") or "unknown"] += 1
        bucket["by_hour"][str(made.hour)] += 1
        bucket["by_calendar"][ev["_calendar"]] += 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": "America/Denver",
        "calendar_ids": list(calendars),
        "calendars": calendars,
        "calendar_notes": notes,
        "definition": "Appointments on the calendars embedded in the Book-A-Call funnel pages (College Legetty /meta, Event Legetty /meta, Real Estate CFS home; read live each run, matched by calendar id, a calendar once seen keeps counting), counted on the day they were booked (dateAdded), deleted excluded, Google/YouTube/organic-search sources excluded from total (see all_total / other_sources).",
        "days": {d: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in b.items()}
                 for d, b in days.items()},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(json.dumps({d: b["total"] for d, b in payload["days"].items()}), "->", out_path)


if __name__ == "__main__":
    main()
