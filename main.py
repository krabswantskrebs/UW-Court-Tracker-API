from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import re
from datetime import datetime
from typing import Optional

app = FastAPI(title="UW Court Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "DELETE"],
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

_cache: dict = {}


def to_min(s):
    m = re.match(r"^(\d+):(\d+)\s*(AM|PM)$", s.strip(), re.I)
    if not m:
        return None
    h, mn, ap = int(m[1]), int(m[2]), m[3].upper()
    if ap == "PM" and h != 12: h += 12
    if ap == "AM" and h == 12: h = 0
    return h * 60 + mn

def to_end_min(s):
    t = to_min(s)
    return 1440 if t == 0 else t

def courts_from_loc(loc, fac):
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

def parse_lines(lines, fac):
    events = []
    fac_key = FAC_KEY[fac]
    for line in lines:
        line = line.strip()
        if not line or not re.match(r"^\d+:\d+", line):
            continue
        b_idx = line.find(fac_key)
        if b_idx == -1:
            continue
        parts = line.split("\t")
        if len(parts) >= 4 and re.match(r"\d+:\d+\s*[AP]M", parts[0], re.I):
            start_s, end_s, name = parts[0].strip(), parts[1].strip(), parts[3].strip()
        else:
            pm = re.match(r"^(\d+:\d+\s*[AP]M)\s+(\d+:\d+\s*[AP]M)\s+CT\s+(.*)", line[:b_idx].strip(), re.I)
            if not pm: continue
            start_s, end_s, name = pm[1], pm[2], pm[3].strip()
        suffix = line[b_idx:]
        dash = suffix.find(" - ")
        if dash == -1: continue
        courts = courts_from_loc(suffix[dash + 3:].strip(), fac)
        if not courts: continue
        start = to_min(start_s)
        end = to_end_min(end_s)
        if start is None: continue
        if fac == "bakke" and re.match(r"^im\s", name, re.I):
            courts = [c for c in courts if c >= 5]
        if not courts: continue
        events.append({"name": name, "start": start, "end": end, "courts": courts})
    return events


async def scrape_ems(fac, date_str):
    url = EMS_URLS[fac]
    print(f"[scrape] {fac}/{date_str} — launching browser", flush=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ico}", lambda r: r.abort())

        print(f"[scrape] Navigating...", flush=True)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[scrape] goto warning: {e}", flush=True)

        await page.wait_for_timeout(4000)

        # Try to click correct date
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d")
            day_num = str(target.day)
            print(f"[scrape] Clicking day {day_num}", flush=True)
            cells = page.locator("td, a")
            count = await cells.count()
            for i in range(min(count, 300)):
                try:
                    txt = (await cells.nth(i).inner_text()).strip()
                    if txt == day_num:
                        await cells.nth(i).click()
                        await page.wait_for_timeout(2000)
                        print(f"[scrape] Clicked day {day_num} at index {i}", flush=True)
                        break
                except Exception:
                    continue
        except Exception as e:
            print(f"[scrape] Date nav skipped: {e}", flush=True)

        content = await page.inner_text("body")
        await browser.close()

    print(f"[scrape] Content length: {len(content)}", flush=True)
    court_lines = [l.strip() for l in content.splitlines()
                   if l.strip() and "Court" in l and ("AM" in l or "PM" in l)]
    print(f"[scrape] Court lines: {len(court_lines)} — sample: {court_lines[:2]}", flush=True)

    events = parse_lines(content.splitlines(), fac)
    print(f"[scrape] Events parsed: {len(events)}", flush=True)
    return events


@app.get("/schedule/{facility}")
async def get_schedule(facility: str, date: Optional[str] = None):
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail=f"Unknown facility. Use 'bakke' or 'nick'.")
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    cached = _cache.get(facility, {}).get(date_str)
    if cached is not None:
        return {"facility": facility, "date": date_str, "events": cached, "cached": True}
    try:
        events = await scrape_ems(facility, date_str)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")
    _cache.setdefault(facility, {})[date_str] = events
    return {"facility": facility, "date": date_str, "events": events, "cached": False}


@app.get("/debug/{facility}")
async def debug(facility: str):
    """Returns raw page text so you can see what the scraper actually fetches."""
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail="Unknown facility")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        await page.goto(EMS_URLS[facility], wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        content = await page.inner_text("body")
        await browser.close()
    return {"length": len(content), "preview": content[:3000]}


@app.delete("/cache")
async def clear_cache():
    _cache.clear()
    return {"message": "Cache cleared"}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
