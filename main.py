from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import re, asyncio
from datetime import datetime
from typing import Optional

app = FastAPI(title="UW Court Tracker API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","DELETE"], allow_headers=["*"])

EMS_URLS = {
    "bakke": "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=meoZqrqZMvHKSLWaHS%2f4bjdroAMc1geNvtL12O1chw1fIP%2bOGy79Y1bkm2DPPKqmpSFHyPvFHX3LAJJHEfBPycyxctYlpcHD4rIwd%2byAtBNWXsKhJT9UDchzs%2bSc3Ze6JFHimlPlQrL2Jk7LFEkj3FoTWmA0BKzQQk0%2beDFO2IBZSiNnDXPGZQ%3d%3d",
    "nick":  "https://uwmadison.emscloudservice.com/web/CustomBrowseEvents.aspx?data=RtFXo1hK2Mh0UPlwkh3Aua7auJ66NvvBNBlUULUwM7vu4XjCwc5WoatHUWdz5pRofwluz9ZmHCNbHsgQ9uEDZjArIem0ShC%2fuM4gJbohNWkNGhzqKkAwrHDWzuEbcQxjHc8CzLweyL05oQ7ToCjKkM5TC%2b639V3qHwqgx1EhbWU%3d",
}
FAC_KEY = {"bakke": "Bakke Recreation", "nick": "Nicholas Recreation Center"}
_cache: dict = {}

# --- stealth JS injected before every page load ---
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'permissions', {
  get: () => ({query: (p) => Promise.resolve({state: 'granted'})})
});
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
    for i in range(1,9):
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

async def make_browser(pw):
    return await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--window-size=1280,800",
        ]
    )

async def make_context(browser):
    return await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/Chicago",
        java_script_enabled=True,
    )

async def scrape_ems(fac: str, date_str: str) -> list:
    url = EMS_URLS[fac]
    print(f"[scrape] {fac}/{date_str}", flush=True)

    async with async_playwright() as pw:
        browser = await make_browser(pw)
        context = await make_context(browser)

        # Inject stealth script before page JS runs
        await context.add_init_script(STEALTH_JS)

        page = await context.new_page()

        # Intercept network responses — capture any HTML/text that contains schedule data
        captured_html = []
        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "html" in ct or "text" in ct:
                    body = await response.body()
                    text = body.decode("utf-8", errors="ignore")
                    if FAC_KEY[fac] in text or "Court" in text:
                        captured_html.append(text)
                        print(f"[scrape] Captured response from {response.url[:80]} ({len(text)} chars)", flush=True)
            except Exception:
                pass

        page.on("response", on_response)

        # Block only images/fonts to keep JS working
        await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ico,svg}", lambda r: r.abort())

        print(f"[scrape] Navigating...", flush=True)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[scrape] goto warning: {e}", flush=True)

        # Wait for JS to execute and render content
        await page.wait_for_timeout(6000)

        # Log raw page content for debugging
        body_text = await page.inner_text("body")
        print(f"[scrape] Body text length after wait: {len(body_text)}", flush=True)
        print(f"[scrape] Body preview: {body_text[:300]}", flush=True)

        # Try clicking the correct date
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d")
            day_num = str(target.day)
            # Try clicking a link whose full text is just the day number
            clicked = await page.evaluate(f"""
                () => {{
                    const links = document.querySelectorAll('a, td');
                    for (const el of links) {{
                        if (el.textContent.trim() === '{day_num}') {{
                            el.click();
                            return true;
                        }}
                    }}
                    return false;
                }}
            """)
            if clicked:
                print(f"[scrape] Clicked day {day_num} via JS", flush=True)
                await page.wait_for_timeout(4000)
                body_text = await page.inner_text("body")
                print(f"[scrape] Body after date click: {len(body_text)} chars", flush=True)
        except Exception as e:
            print(f"[scrape] Date click error: {e}", flush=True)

        # Get final page HTML too
        page_html = await page.content()
        await browser.close()

    # Try parsing from all sources: body text, captured responses, page HTML
    all_sources = [body_text, page_html] + captured_html
    best_events = []
    for src in all_sources:
        evts = parse_text(src, fac)
        if len(evts) > len(best_events):
            best_events = evts

    print(f"[scrape] Final event count: {len(best_events)}", flush=True)
    return best_events


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
async def debug(facility: str):
    """Returns raw page text and captured responses — for diagnosing scraper issues."""
    if facility not in EMS_URLS:
        raise HTTPException(status_code=404, detail="Unknown facility")
    url = EMS_URLS[facility]
    result = {"page_text": "", "captured": [], "page_html_snippet": ""}
    async with async_playwright() as pw:
        browser = await make_browser(pw)
        context = await make_context(browser)
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        captured = []
        async def on_resp(r):
            try:
                ct = r.headers.get("content-type","")
                if "html" in ct or "text" in ct:
                    body = await r.body()
                    text = body.decode("utf-8", errors="ignore")
                    captured.append({"url": r.url[:100], "len": len(text), "preview": text[:500]})
            except Exception: pass
        page.on("response", on_resp)

        await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ico}", lambda r: r.abort())
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception: pass
        await page.wait_for_timeout(6000)

        result["page_text"] = (await page.inner_text("body"))[:3000]
        result["page_html_snippet"] = (await page.content())[:2000]
        result["captured"] = captured
        await browser.close()
    return result


@app.delete("/cache")
async def clear_cache():
    _cache.clear()
    return {"message": "Cache cleared"}

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
