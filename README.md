# UW Court Tracker — Backend API

## IMPORTANT: Upload ALL 5 files to GitHub when updating
- main.py
- requirements.txt  
- Dockerfile
- render.yaml
- README.md

## Deploy to Render
1. Push all 5 files to a GitHub repo
2. New → Web Service → connect repo → Create Web Service
3. Wait ~3 min for build

## Test after deploy
Visit these URLs in your browser:
- https://your-app.onrender.com/health         ← should return {"status":"ok"}
- https://your-app.onrender.com/debug/nick     ← shows raw EMS page text (paste here if broken)
- https://your-app.onrender.com/schedule/nick  ← returns court events JSON

## Troubleshooting
If /schedule returns 0 events, visit /debug/nick and paste the "page_text" 
field here so we can see what the EMS site is returning to the scraper.
