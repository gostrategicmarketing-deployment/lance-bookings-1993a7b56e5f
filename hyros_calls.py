"""Meta-attributed Tax Scholarship calls from Hyros, per Mountain-time day. COUNTS ONLY.

Phil 2026-11-22: the dashboard's College Funding Calls number is Hyros's count of
"Tax Scholarship 1 on 1 Meeting" calls whose LAST-CLICK source is a Meta (Facebook) ad,
counted on the day Hyros created the call (= the day it was booked), Mountain time.

Hyros `/calls` returns each booked call with lastSource.adSource.platform. The key arrives
as HYROS_API_KEY (Actions secret / local key file); it can read every Lance lead, so it
never goes in this repo or the published file, and nothing but counts is written.
"""
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = "https://api.hyros.com/v1/api/v1.0/calls"
CALL_NAME_PREFIX = "tax scholarship"  # GHL calendar "Tax Scholarship 1 on 1 Meeting"


def _get(key, params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"API-Key": key, "Accept": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:  # transient Hyros/API errors: back off and retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Hyros /calls failed: {last}")


def _created(v):
    try:
        return datetime.strptime(v, "%a %b %d %H:%M:%S UTC %Y").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _platform(src):
    if not src:
        return "none"
    p = ((src.get("adSource") or {}).get("platform") or "").upper()
    if p:
        return p
    t = ((src.get("trafficSource") or {}).get("name") or "").lower()
    return "FACEBOOK" if t == "facebook" else (t or "organic").upper()


def tax_scholarship_counts(key, wanted, tz):
    """{day: {meta_calls, all_calls, by_platform, by_state}} for each day in `wanted`."""
    days = sorted(wanted)
    first = datetime.fromisoformat(days[0]).replace(tzinfo=tz)
    # Query a day wider on each side (Hyros reads the window in the account's own zone),
    # then bucket by creationDate in Mountain time. toDate carries the current time so the
    # request differs every run: Hyros caches an identical query for ~4 minutes.
    from_d = (first - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    to_d = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    calls, page = {}, None
    while True:
        p = {"fromDate": from_d, "toDate": to_d, "pageSize": 250}
        if page:
            p["pageId"] = page
        data = _get(key, p)
        rows = data.get("result") or []
        for c in rows:
            calls[c["id"]] = c
        page = data.get("nextPageId")
        if not page or not rows:
            break

    out = {d: {"meta_calls": 0, "all_calls": 0, "by_platform": Counter(), "by_state": Counter()} for d in days}
    for c in calls.values():
        if not (c.get("name") or "").lower().startswith(CALL_NAME_PREFIX):
            continue
        day = _created(c["creationDate"]).astimezone(tz).date().isoformat()
        if day not in out:
            continue
        b = out[day]
        plat = _platform(c.get("lastSource"))
        b["all_calls"] += 1
        b["by_platform"][plat] += 1
        b["by_state"][c.get("state") or "unknown"] += 1
        if plat == "FACEBOOK":
            b["meta_calls"] += 1
    return {d: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in b.items()} for d, b in out.items()}
