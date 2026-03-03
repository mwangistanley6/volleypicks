"""
Volleyball Under 4.5 - Web App Backend
=======================================
Flask server that runs the Playwright agent and streams
live progress to the frontend via Server-Sent Events (SSE).

Install & Run:
    pip install flask playwright google-genai
    playwright install chromium
    python app.py

Then open: http://localhost:5000
"""

import os
import asyncio
import json
import re
import queue
import threading
from datetime import datetime
from flask import Flask, Response, render_template, jsonify, request
from playwright.async_api import async_playwright
from google import genai

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyDP2ApAdwzLefOh15sHGqUMzBlLc4BqLsE"
GEMINI_MODEL   = "gemini-2.5-flash"
SPORTYBET_URL  = "https://www.sportybet.com/ke/sport/volleyball"
TARGET         = 21
MAX_ATTEMPTS   = 70
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
client = genai.Client(api_key=GEMINI_API_KEY)

# Global state
agent_state = {
    "running": False,
    "done": False,
    "booking_code": None,
    "selections": [],
    "skipped": 0,
    "failed": 0,
    "log_queue": queue.Queue()
}


def log(msg: str, type: str = "info"):
    """Push a log message to the queue for SSE streaming."""
    agent_state["log_queue"].put({"msg": msg, "type": type, "ts": datetime.now().strftime("%H:%M:%S")})
    print(msg)


# ══════════════════════════════════════════════════════════════════════════════
# AGENT LOGIC
# ══════════════════════════════════════════════════════════════════════════════

async def run_agent():
    log("🏐 Starting Volleyball Under 4.5 Agent...", "start")
    log(f"🎯 Target: {TARGET} selections", "info")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900}
        )
        page = await context.new_page()

        # STEP 1: Load page
        log("📡 Loading Sportybet Kenya volleyball...", "info")
        await page.goto(SPORTYBET_URL, wait_until="commit", timeout=60000)
        try:
            await page.wait_for_selector("div.match-row", timeout=30000)
            log("✅ Page loaded", "success")
        except:
            log("⚠️ Page load slow — continuing", "warn")
        await page.wait_for_timeout(2000)

        # STEP 2: Scrape
        log("📜 Scraping all matches...", "info")
        today, upcoming = await scrape_matches(page)
        log(f"✅ Found {len(today)+len(upcoming)} matches: {len(today)} today, {len(upcoming)} upcoming", "success")

        if not today and not upcoming:
            log("❌ No matches found!", "error")
            await browser.close()
            return

        # STEP 3: Gemini ranks
        log("🤖 Gemini AI analyzing matches...", "info")
        ranked = gemini_rank(today, upcoming)
        log(f"✅ AI selected {len(ranked)} candidates", "success")

        # STEP 4: Click Under 4.5 for each
        added, skipped_list, failed_list = [], [], []

        for i, sel in enumerate(ranked[:MAX_ATTEMPTS]):
            if len(added) >= TARGET:
                log(f"🎯 Reached {TARGET} selections!", "success")
                break

            eid = sel.get("event_id", "")
            name = sel.get("match") or sel.get("raw","")[:50]
            flag = "📅 TODAY" if sel.get("is_today") else "🗓️ UPCOMING"
            log(f"[{i+1}] {flag} — {name}", "match")

            if not eid:
                failed_list.append(sel)
                continue

            result = await process_match(page, eid, name)
            sel['status'] = result

            if result == 'added':
                added.append(sel)
                log(f"   ✅ Added! ({len(added)}/{TARGET})", "success")
            elif result == 'skipped':
                skipped_list.append(sel)
                log(f"   ⏭️ No Under 4.5 market", "skip")
            else:
                failed_list.append(sel)
                log(f"   ❌ Failed", "error")

        agent_state["skipped"] = len(skipped_list)
        agent_state["failed"] = len(failed_list)

        # STEP 5: Book Bet
        if added:
            log("📋 Clicking Book Bet...", "info")
            code = await do_book_bet(page)
            agent_state["booking_code"] = code
            agent_state["selections"] = added
            if code and code not in ("ERROR", "SEE_SCREENSHOT"):
                log(f"🎉 BOOKING CODE: {code}", "code")
            else:
                log("⚠️ Could not auto-read code — check screenshot", "warn")
        else:
            log("❌ No selections added", "error")

        await browser.close()
        log("✅ Agent finished!", "done")
        agent_state["done"] = True
        agent_state["running"] = False


async def scrape_matches(page):
    prev_h = 0
    for i in range(20):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        h = await page.evaluate("() => document.body.scrollHeight")
        if h == prev_h:
            break
        prev_h = h
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)

    today_str = datetime.now().strftime("%d/%m")
    matches = await page.evaluate(f"""
        () => {{
            const todayStr = "{today_str}";
            const results = [];
            const seen = new Set();
            document.querySelectorAll('div.match-row').forEach(row => {{
                const text = row.innerText || '';
                const idM = text.match(/ID[:\\s]*(\\d+)/);
                if (!idM) return;
                const id = idM[1];
                if (seen.has(id)) return;
                seen.add(id);
                let dateStr = '';
                let node = row;
                for (let depth = 0; depth < 15 && !dateStr; depth++) {{
                    node = node.parentElement;
                    if (!node || node === document.body) break;
                    let sib = node.previousElementSibling;
                    for (let s = 0; s < 8 && sib; s++, sib = sib.previousElementSibling) {{
                        const t = (sib.innerText || '').trim();
                        if (/\\d{{2}}\\/\\d{{2}}/.test(t)) {{ dateStr = t.split('\\n')[0].trim(); break; }}
                    }}
                }}
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                const timeLine = lines.find(l => /^\\d{{2}}:\\d{{2}}/.test(l)) || '';
                const nums = text.match(/\\b\\d+\\.\\d{{2}}\\b/g) || [];
                results.push({{
                    event_id: id, raw: text.substring(0,200),
                    date: dateStr, time: timeLine,
                    odds: nums.slice(0,4),
                    is_today: dateStr.includes(todayStr)
                }});
            }});
            return results;
        }}
    """)
    today = [m for m in matches if m.get('is_today')]
    upcoming = [m for m in matches if not m.get('is_today')]
    return today, upcoming


def gemini_rank(today, upcoming):
    from collections import OrderedDict
    days = OrderedDict()
    for m in upcoming:
        d = m.get('date', 'Unknown')
        days.setdefault(d, []).append(m)

    prompt = f"""Volleyball betting analyst. Rank by Under 4.5 Sets likelihood.
Under 4.5 = match ends 3-0. Best when favorite odds 1.05-1.45.

STRICT DAY ORDER:
1. TODAY: {json.dumps(today, indent=2)}
2. DAY BY DAY: {json.dumps(dict(days), indent=2)}

Complete all of one day before next. Within each day rank by confidence.
Return 50+ matches. ONLY JSON. No markdown.

{{"ranked":[{{"event_id":"31030","match":"Team A vs Team B","competition":"League","date":"04/03","time":"19:00","is_today":false,"favorite_odds":1.30,"confidence":"High","reasoning":"Strong favorite"}}]}}"""

    try:
        r = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = re.sub(r'```json|```', '', r.text).strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(m.group(0) if m else raw)
        return data.get("ranked", [])
    except:
        return today + upcoming


async def process_match(page, event_id, name):
    try:
        # Ensure on list page
        if 'sr:match' in page.url:
            await page.go_back()
            try:
                await page.wait_for_selector("div.match-row", timeout=15000)
            except:
                pass
            await page.wait_for_timeout(1500)

        # Scroll row into view + click +N
        await page.evaluate(f"""
            () => {{
                for (const row of document.querySelectorAll('div.match-row')) {{
                    if (row.innerText?.includes('ID: {event_id}') || row.innerText?.includes('ID:{event_id}')) {{
                        row.scrollIntoView({{block:'center'}});
                        return;
                    }}
                }}
            }}
        """)
        await page.wait_for_timeout(600)

        label = await page.evaluate(f"""
            () => {{
                for (const row of document.querySelectorAll('div.match-row')) {{
                    if (row.innerText?.includes('ID: {event_id}') || row.innerText?.includes('ID:{event_id}')) {{
                        const btn = row.querySelector('div.market-size');
                        if (btn) {{ btn.click(); return btn.innerText?.trim(); }}
                        return null;
                    }}
                }}
                return null;
            }}
        """)

        if not label:
            return 'failed'

        # Wait for navigation
        try:
            await page.wait_for_function("() => window.location.href.includes('sr:match')", timeout=15000)
        except:
            await page.wait_for_timeout(3000)

        if 'sr:match' not in page.url:
            return 'failed'

        # Click Under 4.5
        result = await click_under_45(page)

        # Go back
        await page.go_back()
        try:
            await page.wait_for_selector("div.match-row", timeout=15000)
        except:
            pass
        await page.wait_for_timeout(1500)
        return result

    except Exception as e:
        try:
            await page.go_back()
            await page.wait_for_timeout(2000)
        except:
            try:
                await page.goto(SPORTYBET_URL, wait_until="commit", timeout=60000)
                await page.wait_for_timeout(2000)
            except:
                pass
        return 'failed'


async def click_under_45(page):
    await page.wait_for_timeout(3000)
    try:
        await page.wait_for_selector("div.m-table-cell.m-table-cell--responsive", timeout=10000)
    except:
        body = await page.evaluate("() => document.body.innerText")
        return 'skipped' if 'Under 4.5' not in body else 'failed'

    page_h = await page.evaluate("() => document.body.scrollHeight")
    for y in range(0, page_h + 500, 250):
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.wait_for_timeout(120)
        result = await page.evaluate("""
            () => {
                const cells = document.querySelectorAll('div.m-table-cell.m-table-cell--responsive');
                for (const cell of cells) {
                    const spans = cell.querySelectorAll('span.m-table-cell-item');
                    if (!spans.length) continue;
                    if (spans[0].innerText?.trim() !== 'Under 4.5') continue;
                    const row = cell.parentElement;
                    if (!row || !(row.innerText||'').includes('Over 4.5')) continue;
                    cell.click();
                    return { ok: true, odds: spans[1]?.innerText?.trim() };
                }
                return { ok: false };
            }
        """)
        if result.get('ok'):
            await page.wait_for_timeout(1500)
            return 'added'

    body = await page.evaluate("() => document.body.innerText")
    if 'Under 4.5' not in body:
        return 'skipped'

    try:
        cells = page.locator('div.m-table-cell.m-table-cell--responsive').filter(has_text='Under 4.5')
        n = await cells.count()
        for i in range(n):
            cell = cells.nth(i)
            parent = await cell.evaluate("el => el.parentElement?.innerText || ''")
            if 'Over 4.5' in parent:
                await cell.scroll_into_view_if_needed()
                await page.wait_for_timeout(400)
                await cell.click()
                await page.wait_for_timeout(1500)
                return 'added'
    except:
        pass
    return 'failed'


async def do_book_bet(page):
    if 'sr:match' in page.url:
        await page.go_back()
        await page.wait_for_timeout(2000)

    await page.evaluate("window.scrollTo(0,0)")
    await page.wait_for_timeout(1500)

    method = await page.evaluate("""
        () => {
            const el = document.querySelector('[data-cms-key="book_bet"]');
            if (el) { el.click(); return 'data-attr'; }
            for (const el of document.querySelectorAll('*')) {
                if ((el.innerText||'').trim() === 'Book Bet') { el.click(); return 'text'; }
            }
            return null;
        }
    """)
    if not method:
        try:
            await page.locator('[data-cms-key="book_bet"]').click(force=True)
        except:
            pass

    await page.wait_for_timeout(4000)
    text = await page.evaluate("() => document.body.innerText")

    bad = {'REAL','SIM','BOOK','SPORT','KENYA','LOGIN','LOGOUT','PLACE',
           'PRINT','CASHOUT','BETSLIP','BOOKING','SPORTYBET'}
    for pat in [
        r'Booking Code\s*\n+\s*([A-Z0-9]{4,10})',
        r'Booking Code[^\n]*\n[^\n]*\n\s*([A-Z0-9]{4,10})',
        r'\n([A-Z0-9]{6})\n',
        r'\n([A-Z0-9]{5})\n',
    ]:
        m = re.search(pat, text, re.MULTILINE)
        if m:
            c = m.group(m.lastindex).strip()
            if c not in bad and not c.isdigit():
                return c

    try:
        for el in await page.locator('*').all():
            t = (await el.text_content() or '').strip()
            if re.fullmatch(r'[A-Z0-9]{4,8}', t) and t not in bad and not t.isdigit():
                return t
    except:
        pass
    return "SEE_APP"


# ══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/start', methods=['POST'])
def start_agent():
    if agent_state["running"]:
        return jsonify({"error": "Agent already running"}), 400

    # Reset state
    agent_state.update({
        "running": True, "done": False,
        "booking_code": None, "selections": [],
        "skipped": 0, "failed": 0,
        "log_queue": queue.Queue()
    })

    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_agent())
        loop.close()

    t = threading.Thread(target=run_in_thread, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route('/stream')
def stream():
    """Server-Sent Events endpoint for live log streaming."""
    def generate():
        while True:
            try:
                item = agent_state["log_queue"].get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "done":
                    break
            except queue.Empty:
                yield "data: {\"msg\": \"...\", \"type\": \"ping\"}\n\n"
                if agent_state["done"]:
                    break

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/status')
def status():
    return jsonify({
        "running": agent_state["running"],
        "done": agent_state["done"],
        "booking_code": agent_state["booking_code"],
        "total_added": len(agent_state["selections"]),
        "target": TARGET,
        "skipped": agent_state["skipped"],
        "failed": agent_state["failed"],
        "selections": agent_state["selections"]
    })


if __name__ == '__main__':
    print("\n🏐 Volleyball Under 4.5 — Web App")
    print("=" * 40)
    print("Open in browser: http://localhost:5000")
    print("Share on network: http://YOUR_IP:5000")
    print("=" * 40 + "\n")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False, threaded=True)
