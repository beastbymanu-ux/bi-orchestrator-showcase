"""
Autonomous Business Intelligence Orchestrator
Multi-Agent System: Scraper → Analyst → Reporter
Author: Manu (openclawlabs-multiagent.duckdns.org)
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import redis
from celery import Celery
import anthropic
import requests

# ── Configuration (all secrets via environment variables) ────
REDIS_URL        = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")          # required
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL")          # required
ALERT_THRESHOLD  = int(os.getenv("ALERT_THRESHOLD", "70"))   # severity floor

# ── Celery app ────────────────────────────────────────────────
app = Celery("bi_orchestrator", broker=REDIS_URL)
app.conf.task_routes = {
    "orchestrator.scrape_competitor": {"queue": "scraping"},
    "orchestrator.analyze_batch":     {"queue": "analysis"},
    "orchestrator.dispatch_report":   {"queue": "reporting"},
}

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  AGENT A — SCRAPER
#  Responsibility: crawl competitor pages, push only deltas
# ══════════════════════════════════════════════════════════════

@app.task(queue="scraping", rate_limit="10/m", bind=True, max_retries=3)
def scrape_competitor(self, domain: str, selectors: dict) -> Optional[dict]:
    """
    Crawl a competitor domain and extract structured signals.
    Uses SHA-256 delta detection: only changed content reaches Agent B.

    Args:
        domain:    Competitor domain (e.g. "competitor.com")
        selectors: CSS/XPath selectors for prices, products, blog, jobs

    Returns:
        Extracted payload dict, or None if no changes detected.
    """
    try:
        # In production: swap with PlaywrightScraper(domain).extract(selectors)
        raw_data = _mock_scrape(domain, selectors)

        current_hash = hashlib.sha256(
            json.dumps(raw_data, sort_keys=True).encode()
        ).hexdigest()

        last_hash = redis_client.get(f"hash:{domain}")
        if last_hash == current_hash:
            log.info("[Agent A] No change detected for %s — skipping.", domain)
            return None  # Silence is signal integrity.

        redis_client.set(f"hash:{domain}", current_hash)
        payload = {
            "domain":    domain,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data":      raw_data,
        }
        redis_client.lpush("scraped_data", json.dumps(payload))
        log.info("[Agent A] Delta detected for %s — pushed to queue.", domain)
        return payload

    except Exception as exc:
        log.error("[Agent A] Scrape failed for %s: %s", domain, exc)
        raise self.retry(exc=exc, countdown=60)


def _mock_scrape(domain: str, selectors: dict) -> dict:
    """Placeholder — replace with Scrapy/Playwright in production."""
    return {
        "prices":   {"starter": 299, "pro": 599},
        "products": ["Feature A", "Feature B"],
        "jobs":     [],
        "blog":     [],
    }


# ══════════════════════════════════════════════════════════════
#  AGENT B — ANALYST
#  Responsibility: reason over deltas, score signals 0-100
# ══════════════════════════════════════════════════════════════

@app.task(queue="analysis", bind=True, max_retries=2)
def analyze_batch(self, scraped_items: list) -> list:
    """
    Send a delta batch to Claude API with extended_thinking enabled.
    Returns a list of scored market signals.

    Extended thinking is critical here: it catches compound signals
    that single-pass LLMs miss — e.g. a competitor hiring 3 ML engineers
    while cutting prices is not a margin squeeze, it's a product launch
    incoming in ~90 days.

    Args:
        scraped_items: List of delta payloads from Agent A

    Returns:
        List of signal dicts with severity scores.
    """
    if not scraped_items:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a B2B competitive intelligence analyst.
Below is a batch of competitor changes detected in the last cycle.
Analyze ONLY the delta (what changed), not the full dataset.

DELTA BATCH ({len(scraped_items)} sources):
{json.dumps(scraped_items, indent=2)}

Analyze for:
1. PRICING_THREAT   — competitor dropped price >8%
2. PRODUCT_GAP      — they launched something we do not have
3. HIRING_SIGNAL    — job postings that predict future product direction
4. POSITIONING_SHIFT — messaging or target segment changed

Output: JSON array. Each item must include:
  source, signal_type, severity (0-100), summary, recommended_action.

Rules:
- Only include signals with severity >= 60.
- Be silent on everything below that threshold.
- severity 85-100 = critical (immediate competitive threat)
- severity 70-84  = alert (action needed this week)
- severity 60-69  = watch (log, no action required yet)
"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 10000},
            messages=[{"role": "user", "content": prompt}],
        )
        signals = _extract_signals(response.content)
        for signal in signals:
            redis_client.lpush("analysis_ready", json.dumps(signal))

        log.info("[Agent B] Analysis complete — %d signals scored ≥60.", len(signals))
        return signals

    except Exception as exc:
        log.error("[Agent B] Analysis failed: %s", exc)
        raise self.retry(exc=exc, countdown=120)


def _extract_signals(content: list) -> list:
    """Parse Claude's response — handle both text and thinking blocks."""
    for block in content:
        if block.type == "text":
            text = block.text.strip()
            start = text.find("[")
            end   = text.rfind("]") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
    return []


# ══════════════════════════════════════════════════════════════
#  AGENT C — REPORTER
#  Responsibility: close the loop — alert or silence
# ══════════════════════════════════════════════════════════════

@app.task(queue="reporting")
def dispatch_report(signal: dict) -> str:
    """
    Route a scored signal to the appropriate output channel.

    Severity >= ALERT_THRESHOLD (default 70) → Discord/Slack webhook.
    Below threshold → silent append to dashboard log only.

    Args:
        signal: Scored signal dict from Agent B

    Returns:
        Routing decision string for logging.
    """
    severity = signal.get("severity", 0)

    if severity >= ALERT_THRESHOLD:
        _send_discord_alert(signal)
        outcome = f"ALERTED (severity {severity})"
    else:
        outcome = f"LOGGED silently (severity {severity} < threshold {ALERT_THRESHOLD})"

    log.info("[Agent C] %s — %s", signal.get("source", "unknown"), outcome)
    return outcome


def _send_discord_alert(signal: dict) -> None:
    """Post a rich embed to Discord. Zero noise below threshold."""
    if not DISCORD_WEBHOOK:
        log.warning("[Agent C] DISCORD_WEBHOOK_URL not set — skipping alert.")
        return

    severity = signal["severity"]
    color    = 0xFF4444 if severity >= 85 else 0xFF9900

    payload = {
        "embeds": [{
            "title":       f"MARKET SIGNAL [{severity}/100] — {signal.get('signal_type', 'UNKNOWN')}",
            "description": signal.get("summary", ""),
            "color":       color,
            "fields": [
                {"name": "Source",   "value": signal.get("source", "-"),              "inline": True},
                {"name": "Severity", "value": str(severity),                           "inline": True},
                {"name": "Action",   "value": signal.get("recommended_action", "-"),  "inline": False},
            ],
            "footer": {"text": f"BI Orchestrator · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
        }]
    }
    resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    resp.raise_for_status()


# ══════════════════════════════════════════════════════════════
#  SCHEDULER ENTRY POINT
#  Called by Celery Beat every 6 hours
# ══════════════════════════════════════════════════════════════

COMPETITOR_CONFIG = [
    {
        "domain": "competitor-a.com",
        "selectors": {
            "price":   ".pricing-card .price",
            "product": ".product-title",
            "blog":    "article h2",
            "jobs":    ".job-listing h3",
        }
    },
    # Add more competitors here — system scales horizontally
]

@app.task
def run_full_cycle() -> None:
    """
    Master orchestration task fired by celery-beat every 6 hours.
    Fans out scraping tasks, then chains analysis → reporting.
    """
    log.info("[Scheduler] Starting full intelligence cycle — %d competitors.", len(COMPETITOR_CONFIG))

    for competitor in COMPETITOR_CONFIG:
        # Agent A: scrape (async, rate-limited)
        result = scrape_competitor.delay(
            competitor["domain"],
            competitor["selectors"],
        )

    # Agent B + C are triggered by queue consumers — no explicit chaining needed.
    # Redis queues decouple the agents for resilience and independent scaling.
    log.info("[Scheduler] Scraping tasks dispatched. Agents B and C will process asynchronously.")
