from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import re
from datetime import datetime, date, timedelta
from typing import Optional

app = FastAPI(title="UW Court Tracker API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","DELETE"], allow_headers=["*"])

EMS_URLS = {
    "bakke": "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=meoZqrqZMvHKSLWaHS%2f4bjdroAMc1geNvtL12O1chw1fIP%2bOGy79Y1bkm2DPPKqmpSFHyPvFHX3LAJJHEfBPycyxctYlpcHD4rIwd%2byAtBNWXsKhJT9UDchzs%2bSc3Ze6JFHimlPlQrL2Jk7LFEkj3FoTWmA0BKzQQk0%2beDFO2IBZSiNnDXPGZQ%3d%3d",
    "nick":  "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=RtFXo1hK2Mh0UPlwkh3Aua7auJ66NvvBNBlUULUwM7vu4XjCwc5WoatHUWdz5pRofwluz9ZmHCNbHsgQ9uEDZjArIem0ShC%2fuM4gJbohNWkNGhzqKkAwrHDWzuEbcQxjHc8CzLweyL05oQ7ToCjKkM5TC%2b639V3qHwqgx1EhbWU%3d",
}
FAC_KEY = {"bakke": "Bakke Recreation", "nick": "Nicholas Recreation Center"}
_cache: dict = {}

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
"""

def to_min(s):
    m = re.match(r"^(\d+):(\d+)\s*(AM|PM)$", s.strip(), re.I)
    if not m: return None
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
        if re.search(r"courts?\s*1\s*[-–]\s*2", l): return [1,2]
        if re.search(r"courts?\s*3\s*[-–]\s*4", l): return [3,4]
        if re.search(r"courts?\s*5\s*[-–]\s*8", l): return [5,6,7,8]
        if re.search(r"courts?\s*7\s*[-–]\s*8", l): return [7,8]
    out = []
    for i in range(1, 9):
        if re.search(rf"\bcourt\s*{i}(?!\d)", l): out.append(i)
    return out

def parse_text(text, fac):
    events = []
    fac_key = FAC_KEY[fac]
    for line in text.splitlines():
        line = line.strip()
        if not line or not re.match(r"^\d+:\d+", line): continue
        b_idx = line.find(fac_key)
        if b_idx == -1: continue
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
        courts = courts_from_loc(suffix[dash+3:].strip(), fac)
        if not courts: continue
        start = to_min(start_s); end = to_end_min(end_s)
        if start is None: continue
        if fac == "bakke" and re.match(r"^im\s", name, re.I):
            courts = [c for c in courts if c >= 5]
        if not courts: continue
        events.append({"name": name, "start": start, "end": end, "courts": courts})
    return events


def parse_page_date(text: str) -> Optional[date]:
    """
    Try to read the currently-displayed date from the EMS page text.
    EMS shows something like 'Monday, May 19th 2026' or 'Monday, May 19, 2026'.
    Returns a date object, or None if not found.
    """
    # Match "Monday, May 19th 2026" or "Monday, May 19, 2026"
    m = re.search(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        text, re.I
    )
    if m:
        try:
            return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%b %d %Y").date()
        except Exception:
            pass
    return None


async def navigate_to_date(page, target_date: date, fac: str):
    """
    Navigate EMS Daily List to target_date by clicking the
    [◄ Mon] (prev) or [Wed ►] (next) day buttons visible in the UI.
    """

    async def get_current_date() -> date:
        text = await page.inner_text("body")
        d = parse_page_date(text)
        print(f"[nav] Parsed date from page: {d!r}", flush=True)
        return d if d else date.today()

    async def click_nav(direction: str) -> bool:
        """Click prev (◄) or next (►) day button using multiple strategies."""
        # Arrow chars used by EMS: ◄ ► and fallbacks
        chars = ["◄", "◀", "‹", "«"] if direction == "prev" else ["►", "▶", "›", "»"]

        # Strategy 1: find any clickable element containing the arrow char
        for ch in chars:
            try:
                clicked = await page.evaluate(f"""() => {{
                    const els = [...document.querySelectorAll('a, button, input[type=button], input[type=submit]')];
                    const btn = els.find(e => (e.textContent || e.value || "").includes("{ch}"));
                    if (btn) {{ btn.click(); return true; }}
                    return false;
                }}""")
                if clicked:
                    print(f"[nav] Clicked \'{direction}\' via char \'{ch}\'", flush=True)
                    return True
            except Exception:
                pass

        # Strategy 2: find sibling of date header element
        sibling = "previousElementSibling" if direction == "prev" else "nextElementSibling"
        try:
            clicked = await page.evaluate(f"""() => {{
                const months = ['January','February','March','April','May','June',
                                'July','August','September','October','November','December'];
                for (const el of document.querySelectorAll('*')) {{
                    const t = el.textContent || '';
                    if (months.some(m => t.includes(m)) && t.length < 80) {{
                        const nav = el.{sibling} || (el.parentElement && el.parentElement.{sibling});
                        if (nav) {{ nav.click(); return true; }}
                    }}
                }}
                return false;
            }}""")
            if clicked:
                print(f"[nav] Clicked \'{direction}\' via sibling strategy", flush=True)
                return True
        except Exception as e:
            print(f"[nav] Sibling failed: {{e}}", flush=True)

        print(f"[nav] WARNING: could not find \'{direction}\' button", flush=True)
        return False

    current = await get_current_date()
    delta = (target_date - current).days
    print(f"[nav] page={current}, target={target_date}, delta={delta:+d}", flush=True)

    if delta == 0:
        print("[nav] Already on target date", flush=True)
        return

    direction = "next" if delta > 0 else "prev"
    steps = abs(delta)
    print(f"[nav] Clicking \'{direction}\' {steps}x", flush=True)

    for step in range(steps):
        ok = await click_nav(direction)
        if not ok:
            break
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            await page.wait_for_timeout(2500)
        new = await get_current_date()
        print(f"[nav] Step {step+1}/{steps}: now showing {new}", flush=True)
        if new == target_date:
            break

    final = await get_current_date()
    print(f"[nav] Final date: {final}", flush=True)


async def make_page(pw):
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox",
              "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage", "--disable-gpu",
              "--window-size=1280,800"]
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/Chicago",
    )
    await context.add_init_script(STEALTH_JS)
    page = await context.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ico,svg}", lambda r: r.abort())
    return browser, page


async def scrape_ems(fac: str, date_str: str) -> list:
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    print(f"[scrape] {fac} → {date_str}", flush=True)

    captured = []

    async with async_playwright() as pw:
        browser, page = await make_page(pw)

        # Capture AJAX/HTML responses that contain schedule data
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "html" in ct or "text" in ct:
                    body = await response.body()
                    text = body.decode("utf-8", errors="ignore")
                    if FAC_KEY[fac] in text:
                        captured.append(text)
                        print(f"[scrape] Captured {len(text)} chars from {response.url[:80]}", flush=True)
            except Exception:
                pass
        page.on("response", on_response)

        # Load the EMS page (defaults to today)
        try:
            await page.goto(EMS_URLS[fac], wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[scrape] goto warning: {e}", flush=True)
        await page.wait_for_timeout(4000)

        body_after_load = await page.inner_text("body")
        print(f"[scrape] After load: {len(body_after_load)} chars", flush=True)

        # Navigate to target date by clicking prev/next day arrows
        await navigate_to_date(page, target, fac)

        # Grab final page content
        final_text = await page.inner_text("body")
        final_html = await page.content()
        print(f"[scrape] Final body: {len(final_text)} chars", flush=True)

        await browser.close()

    # Parse from all sources, pick the one with most events
    all_sources = [final_text, final_html] + captured
    best = []
    for src in all_sources:
        evts = parse_text(src, fac)
        if len(evts) > len(best):
            best = evts

    print(f"[scrape] Events found: {len(best)}", flush=True)
    return best


@app.get("/schedule/{facility}")
async def get_schedule(facility: str, date: Optional[str] = None):
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail="Unknown facility. Use 'bakke' or 'nick'.")
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
async def debug(facility: str, date: Optional[str] = None):
    """
    Returns raw page text after navigating to the given date.
    Visit /debug/nick or /debug/nick?date=2026-05-21 to inspect what the scraper sees.
    """
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail="Unknown facility")
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    captured = []

    async with async_playwright() as pw:
        browser, page = await make_page(pw)

        async def on_resp(r):
            try:
                ct = r.headers.get("content-type","")
                if "html" in ct or "text" in ct:
                    body = await r.body()
                    text = body.decode("utf-8", errors="ignore")
                    captured.append({"url": r.url[:100], "len": len(text), "preview": text[:400]})
            except Exception: pass
        page.on("response", on_resp)

        await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ico}", lambda r: r.abort())
        try:
            await page.goto(EMS_URLS[facility], wait_until="domcontentloaded", timeout=45000)
        except Exception: pass
        await page.wait_for_timeout(4000)

        page_date_before = parse_page_date(await page.inner_text("body"))
        await navigate_to_date(page, target, facility)

        body = await page.inner_text("body")
        page_date_after = parse_page_date(body)
        await browser.close()

    return {
        "requested_date": date_str,
        "page_date_before_nav": str(page_date_before),
        "page_date_after_nav": str(page_date_after),
        "body_length": len(body),
        "body_preview": body[:3000],
        "captured_responses": captured[:5],
    }


@app.delete("/cache")
async def clear_cache():
    _cache.clear()
    return {"message": "Cache cleared"}

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
