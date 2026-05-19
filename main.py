from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import asyncio, re, json
from datetime import datetime
from typing import Optional

app = FastAPI(title="UW Court Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # allow any frontend origin
    allow_methods=["GET"],
    allow_headers=["*"],
)

EMS_URLS = {
    "bakke": "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=meoZqrqZMvHKSLWaHS%2f4bjdroAMc1geNvtL12O1chw1fIP%2bOGy79Y1bkm2DPPKqmpSFHyPvFHX3LAJJHEfBPycyxctYlpcHD4rIwd%2byAtBNWXsKhJT9UDchzs%2bSc3Ze6JFHimlPlQrL2Jk7LFEkj3FoTWmA0BKzQQk0%2beDFO2IBZSiNnDXPGZQ%3d%3d",
    "nick":  "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=RtFXo1hK2Mh0UPlwkh3Aua7auJ66NvvBNBlUULUwM7vu4XjCwc5WoatHUWdz5pRofwluz9ZmHCNbHsgQ9uEDZjArIem0ShC%2fuM4gJbohNWkNGhzqKkAwrHDWzuEbcQxjHc8CzLweyL05oQ7ToCjKkM5TC%2b639V3qHwqgx1EhbWU%3d",
}

FAC_KEY = {
    "bakke": "Bakke Recreation",
    "nick":  "Nicholas Recreation Center",
}

# Simple in-memory cache: {facility: {date: [events]}}
_cache: dict = {}


def to_min(s: str) -> Optional[int]:
    """Convert '4:15 PM' -> minutes since midnight."""
    m = re.match(r"^(\d+):(\d+)\s*(AM|PM)$", s.strip(), re.I)
    if not m:
        return None
    h, mn, ap = int(m[1]), int(m[2]), m[3].upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h * 60 + mn


def to_end_min(s: str) -> int:
    t = to_min(s)
    return 1440 if t == 0 else t   # midnight end = 1440, not 0


def courts_from_loc(loc: str, fac: str) -> list[int]:
    l = loc.lower()
    if fac == "bakke":
        if re.search(r"courts?\s*1\s*[-–]\s*2", l): return [1, 2]
        if re.search(r"courts?\s*3\s*[-–]\s*4", l): return [3, 4]
        if re.search(r"courts?\s*5\s*[-–]\s*8", l): return [5, 6, 7, 8]
        if re.search(r"courts?\s*7\s*[-–]\s*8", l): return [7, 8]
    out = []
    for i in range(1, 9):
        if re.search(rf"\bcourt\s*{i}(?!\d)", l):
            out.append(i)
    return out


def parse_lines(lines: list[str], fac: str, date_filter: Optional[str]) -> list[dict]:
    """Parse raw text lines from the EMS page into court events."""
    events = []
    fac_key = FAC_KEY[fac]

    for line in lines:
        line = line.strip()
        if not line or not re.match(r"^\d+:\d+", line):
            continue
        b_idx = line.find(fac_key)
        if b_idx == -1:
            continue

        # Try tab-separated first, then space-separated
        parts = line.split("\t")
        if len(parts) >= 4 and re.match(r"\d+:\d+\s*[AP]M", parts[0], re.I):
            start_s, end_s, name = parts[0].strip(), parts[1].strip(), parts[3].strip()
        else:
            pm = re.match(
                r"^(\d+:\d+\s*[AP]M)\s+(\d+:\d+\s*[AP]M)\s+CT\s+(.*)",
                line[:b_idx].strip(), re.I
            )
            if not pm:
                continue
            start_s, end_s, name = pm[1], pm[2], pm[3].strip()

        suffix = line[b_idx:]
        dash = suffix.find(" - ")
        if dash == -1:
            continue
        loc_full = suffix[dash + 3:].strip()
        courts = courts_from_loc(loc_full, fac)
        if not courts:
            continue

        start = to_min(start_s)
        end   = to_end_min(end_s)
        if start is None:
            continue

        # IM events never on courts 1-4 at Bakke
        if fac == "bakke" and re.match(r"^im\s", name, re.I):
            courts = [c for c in courts if c >= 5]
        if not courts:
            continue

        events.append({"name": name, "start": start, "end": end, "courts": courts})

    return events


async def scrape_ems(fac: str, date_str: str) -> list[dict]:
    """Launch headless browser, navigate to EMS, select date, return parsed events."""
    url = EMS_URLS[fac]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        # Block images/fonts to load faster
        await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,svg}", lambda r: r.abort())

        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Click the target date if it exists on the calendar, else just grab whatever is shown
        try:
            # The EMS daily list shows one date at a time; navigate to the right date
            # Try clicking date link that matches our date
            target = datetime.strptime(date_str, "%Y-%m-%d")
            day_num = str(target.day)  # e.g. "19"

            # Find and click the correct day cell in the calendar widget
            day_links = page.locator(f"a.calendar-day, td.day a, a[title*='{target.strftime('%B')}']")
            count = await day_links.count()
            for i in range(count):
                link = day_links.nth(i)
                txt = (await link.inner_text()).strip()
                if txt == day_num:
                    await link.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    break
        except Exception:
            pass  # Use whatever date the page shows by default

        # Grab all visible text
        content = await page.inner_text("body")
        await browser.close()

    lines = content.splitlines()
    return parse_lines(lines, fac, date_str)


@app.get("/schedule/{facility}")
async def get_schedule(facility: str, date: Optional[str] = None):
    """
    GET /schedule/bakke?date=2026-05-19
    GET /schedule/nick?date=2026-05-19
    Returns list of {name, start, end, courts} for the given facility and date.
    """
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail=f"Unknown facility '{facility}'. Use 'bakke' or 'nick'.")

    date_str = date or datetime.now().strftime("%Y-%m-%d")

    # Check cache
    cached = _cache.get(facility, {}).get(date_str)
    if cached is not None:
        return {"facility": facility, "date": date_str, "events": cached, "cached": True}

    try:
        events = await scrape_ems(facility, date_str)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")

    # Store in cache
    _cache.setdefault(facility, {})[date_str] = events
    return {"facility": facility, "date": date_str, "events": events, "cached": False}


@app.delete("/cache")
async def clear_cache():
    """Clear the schedule cache (call this to force a fresh scrape)."""
    _cache.clear()
    return {"message": "Cache cleared"}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
