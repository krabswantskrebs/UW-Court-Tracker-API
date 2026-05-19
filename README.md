# UW Court Tracker — Backend API

Headless-browser scraper that fetches live UW Madison gym schedules from the EMS site
and serves them as JSON to the court tracker app.

## Deploy to Render (free, ~5 min)

### Step 1 — Push to GitHub
1. Create a free account at github.com if you don't have one
2. Create a new repository called `uw-court-tracker-api` (public or private)
3. Upload these files (drag & drop on the GitHub website works fine):
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `render.yaml`

### Step 2 — Deploy on Render
1. Go to https://render.com and sign up (free)
2. Click **New → Web Service**
3. Connect your GitHub account and select the `uw-court-tracker-api` repo
4. Render auto-detects the `Dockerfile` — just click **Create Web Service**
5. Wait ~3 minutes for the first build

### Step 3 — Get your URL
Once deployed, Render gives you a URL like:
```
https://uw-court-tracker-api.onrender.com
```

### Step 4 — Update the app
Paste your Render URL into the court tracker app when prompted.

---

## API Endpoints

### GET /schedule/{facility}
Fetch court schedule for a facility.

**facility**: `bakke` or `nick`
**date** (optional): `YYYY-MM-DD` format — defaults to today

```
GET https://your-app.onrender.com/schedule/bakke?date=2026-05-19
GET https://your-app.onrender.com/schedule/nick
```

**Response:**
```json
{
  "facility": "bakke",
  "date": "2026-05-19",
  "cached": false,
  "events": [
    { "name": "Open Rec Basketball", "start": 255, "end": 1440, "courts": [1, 2] },
    { "name": "IM Basketball",       "start": 1080, "end": 1320, "courts": [5, 6, 7, 8] }
  ]
}
```
`start` and `end` are minutes since midnight (e.g. 480 = 8:00 AM, 1020 = 5:00 PM).

### DELETE /cache
Force a fresh scrape (clears in-memory cache).

### GET /health
Returns `{"status": "ok"}` — used by Render to confirm the service is running.

---

## Notes
- **Free tier cold starts**: Render's free tier spins down after 15 min of inactivity.
  First request after idle may take ~30 seconds while it wakes up. Subsequent requests are fast.
- **Caching**: Schedules are cached in memory per facility per date. Restart the service or
  call `DELETE /cache` to force a fresh scrape.
- **Local testing**: Run `pip install -r requirements.txt && playwright install chromium`
  then `uvicorn main:app --reload` and visit http://localhost:8000/schedule/nick
