"""
linkedin_connector.py
─────────────────────
Searches LinkedIn for Founders, Co-founders, and CTOs outside India,
then sends connection requests.

Strategy:
  1. Paginate search results, extract profile URLs + card text
  2. Skip India-based profiles (detected from card location text)
  3. Visit each eligible profile page and click "Invite to connect"
  4. Confirm send (without note)
  5. Log to CSV to avoid duplicate requests

Usage:
    # Default search (Founder/Co-founder/CTO, outside India)
    python3 linkedin-connector/linkedin_connector.py

    # Limit requests this session
    python3 linkedin-connector/linkedin_connector.py --limit 15

    # Dry run — preview profiles without sending
    python3 linkedin-connector/linkedin_connector.py --dry-run

    # Check which connections were accepted
    python3 linkedin-connector/linkedin_connector.py --check-acceptance

    # Quick stats from CSV (no browser needed)
    python3 linkedin-connector/linkedin_connector.py --stats

Safety:
    - Hard cap: 50 requests/day, 150 requests/week (rolling 7 days)
    - CAPTCHA detection — auto-stops if LinkedIn shows a security challenge
    - Rate-limit detection — auto-stops if LinkedIn shows invitation limit banner
    - Acceptance rate tracking — warns if rate drops below 30%
    - Random 10-20s delay between requests
    - Sent requests logged to linkedin_sent_requests.csv
"""

import asyncio
import argparse
import csv
import random
import re
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page

# ── Config ────────────────────────────────────────────────────────────────────

USER_DATA       = Path.home() / ".linkedin-harvester-profile"
SENT_LOG        = Path(__file__).parent / "linkedin_sent_requests.csv"
DAILY_LIMIT     = 50
WEEKLY_LIMIT    = 150
SESSION_DEFAULT = 50

# ── CAPTCHA / restriction detection keywords ─────────────────────────────────
CAPTCHA_SIGNALS = [
    "security verification",
    "let's do a quick security check",
    "verify you're a real person",
    "unusual activity",
    "challenge",
    "/checkpoint/",
]

RATE_LIMIT_SIGNALS = [
    "you've reached the weekly invitation limit",
    "invitation limit",
    "too many pending invitations",
    "you can't send invitations",
    "withdraw pending invitations",
    "temporarily restricted",
    "your account is restricted",
]

TARGET_TITLES = [
    "Founder",
    "Co-Founder",
    "Cofounder",
    "CTO",
]

INDIA_TOKENS = [
    # Country
    "india", "indian",
    # Major cities
    "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "new delhi",
    "chennai", "pune", "kolkata", "noida", "gurugram", "gurgaon",
    "ahmedabad", "jaipur", "indore", "chandigarh", "kochi", "cochin",
    "surat", "lucknow", "bhopal", "nagpur", "vadodara", "visakhapatnam",
    "coimbatore", "madurai", "thiruvananthapuram", "bhubaneswar",
    # States / regions
    "karnataka", "maharashtra", "tamil nadu", "telangana", "andhra",
    "kerala", "rajasthan", "gujarat", "uttar pradesh", "west bengal",
    "haryana", "punjab", "madhya pradesh",
]

# NEVER connect with people whose title contains these — even if no leadership match
BLOCKED_TITLE_KEYWORDS = [
    "engineer", "engineering manager",   # catches SWE, backend, frontend, ML, etc.
    "developer", "programmer",
    "software", "swe", "sde",
    "backend", "frontend", "fullstack", "full-stack", "full stack",
    "devops", "platform", "infrastructure",
    "data scientist", "machine learning", "ml ", "ai engineer",
    "designer", "ux ", "ui ",
    "analyst",
    "intern", "student",
    "recruiter", "hr ",
    "consultant",
    "sales", "account executive", "account manager",
    "marketing",
]

# Only connect with people whose title contains one of these leadership keywords
LEADERSHIP_KEYWORDS = [
    "founder", "co-founder", "cofounder",
    "ceo", "cto", "coo", "cpo", "cmo", "cfo",
    "vp ", "vice president",
    "head of",
    "chief ",
    "director",
    "president",
    "managing director",
    "general manager",
    "partner",
    "principal",
    "entrepreneur",
    "building",   # "Building XYZ" — common founder signal in headline
]


def is_leadership(title: str) -> bool:
    """
    Return True only if title is a leadership/founder role AND not blocked.
    If title is empty: return True (let it through to profile for confirmation).
    """
    if not title:
        return True   # No card title — visit profile to check
    t = title.lower()
    # Hard block — engineers, designers, etc. always skipped
    if any(kw in t for kw in BLOCKED_TITLE_KEYWORDS):
        return False
    # Must match at least one leadership keyword
    return any(kw in t for kw in LEADERSHIP_KEYWORDS)

# ── Sent-request log ──────────────────────────────────────────────────────────

def load_sent_log() -> dict:
    sent = {}
    if not SENT_LOG.exists():
        return sent
    with open(SENT_LOG, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sent[row["profile_url"]] = row["date_sent"]
    return sent


def count_sent_today(sent_log: dict) -> int:
    today = str(date.today())
    return sum(1 for d in sent_log.values() if d == today)


def count_sent_this_week(sent_log: dict) -> int:
    """Count requests sent in the last 7 days (rolling window)."""
    today = date.today()
    count = 0
    for d in sent_log.values():
        try:
            sent_date = date.fromisoformat(d)
            if (today - sent_date).days < 7:
                count += 1
        except (ValueError, TypeError):
            pass
    return count


def append_sent_log(profile_url: str, name: str, location: str, title: str):
    is_new = not SENT_LOG.exists()
    fieldnames = ["profile_url", "name", "location", "title", "date_sent", "accepted"]
    with open(SENT_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "profile_url": profile_url,
            "name": name,
            "location": location,
            "title": title,
            "date_sent": str(date.today()),
            "accepted": "",  # filled in by --check-acceptance
        })


def compute_acceptance_stats(sent_log: dict) -> dict:
    """Compute acceptance rate stats from the CSV log."""
    if not SENT_LOG.exists():
        return {"total": 0, "accepted": 0, "pending": 0, "rate": 0.0}

    total = accepted = 0
    with open(SENT_LOG, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            if row.get("accepted", "").lower() == "yes":
                accepted += 1

    pending = total - accepted
    rate = (accepted / total * 100) if total > 0 else 0.0
    return {"total": total, "accepted": accepted, "pending": pending, "rate": rate}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_india(*fields: str) -> bool:
    """Check any number of text fields (location, title, headline) for India signals."""
    combined = " ".join(f.lower() for f in fields if f)
    return any(t in combined for t in INDIA_TOKENS)


async def check_for_captcha(page: Page) -> bool:
    """
    Detect if LinkedIn is showing a CAPTCHA or security challenge.
    Returns True if a CAPTCHA/challenge is detected.
    """
    try:
        url = page.url.lower()
        if "/checkpoint/" in url or "challenge" in url:
            return True
        page_text = await page.evaluate("document.body ? document.body.innerText.toLowerCase() : ''")
        return any(signal in page_text for signal in CAPTCHA_SIGNALS)
    except Exception:
        return False


async def check_for_rate_limit(page: Page) -> bool:
    """
    Detect if LinkedIn is showing a rate-limit or restriction banner.
    Returns True if a restriction is detected.
    """
    try:
        page_text = await page.evaluate("document.body ? document.body.innerText.toLowerCase() : ''")
        return any(signal in page_text for signal in RATE_LIMIT_SIGNALS)
    except Exception:
        return False


async def safety_check(page: Page) -> str | None:
    """
    Run CAPTCHA + rate-limit checks. Returns a reason string if we must stop,
    or None if safe to continue.
    """
    if await check_for_captcha(page):
        return "CAPTCHA_DETECTED"
    if await check_for_rate_limit(page):
        return "RATE_LIMIT_REACHED"
    return None


def build_search_url(keyword: str, page_num: int = 1) -> str:
    import urllib.parse
    # geoUrn must be raw (not double-encoded) — LinkedIn parses it as a JSON array in the URL
    # US=103644278, UK=101165590, Canada=101174742, Australia=101452733, Singapore=102454443
    GEO_URNS = "%5B%22103644278%22%2C%22101165590%22%2C%22101174742%22%2C%22101452733%22%2C%22102454443%22%5D"
    kw = urllib.parse.quote_plus(keyword)
    return (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={kw}&origin=FACETED_SEARCH&page={page_num}&geoUrn={GEO_URNS}"
    )


# ── Browser setup ─────────────────────────────────────────────────────────────

async def launch_browser(playwright):
    ctx = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA),
        headless=False,
        channel="chrome",
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return ctx


async def ensure_logged_in(page: Page):
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    if "login" in page.url or "authwall" in page.url:
        print("\n  Not logged in — log in manually in Chrome, then wait...\n")
        while "login" in page.url or "authwall" in page.url or "checkpoint" in page.url:
            await asyncio.sleep(2)
        print("  Logged in!\n")


async def recover_page(ctx, page: Page) -> Page:
    """Return the existing page if alive, otherwise open a fresh one."""
    try:
        await page.title()   # cheap liveness check
        return page
    except Exception:
        print("  [page closed — opening new tab]")
        new_page = await ctx.new_page()
        await asyncio.sleep(1)
        return new_page


# ── Search page: collect profile cards ───────────────────────────────────────

async def get_cards_from_page(page: Page, search_url: str) -> list[dict]:
    """
    Navigate to search URL and extract profile cards.
    Returns list of {name, profile_url, location, title}.
    Falls back to URL-only if card text parsing fails.
    """
    await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
    await asyncio.sleep(random.uniform(3, 5))

    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(random.uniform(0.4, 0.7))
    await asyncio.sleep(1.5)

    cards = await page.evaluate("""
    () => {
        const results = [];
        const seenUrls = new Set();

        // Identify the main search results container to avoid navbar/sidebar links
        const mainContainer = (
            document.querySelector('main') ||
            document.querySelector('.scaffold-layout__main') ||
            document.body
        );

        mainContainer.querySelectorAll('a[href*="/in/"]').forEach(anchor => {
            const m = anchor.href.match(/linkedin\\.com\\/in\\/([^/?#]+)/);
            if (!m || m[1] === 'undefined') return;

            const slug = m[1];
            // Skip pure-number slugs (unlikely real profiles)
            if (/^\\d+$/.test(slug)) return;
            if (seenUrls.has(slug)) return;
            seenUrls.add(slug);

            // Walk up to find card container (height 100-350px, wider than 400px)
            let card = anchor;
            for (let i = 0; i < 15; i++) {
                card = card.parentElement;
                if (!card) break;
                const h = card.clientHeight;
                const w = card.clientWidth;
                if (h >= 100 && h <= 350 && w >= 400) break;
            }

            const rawText = card ? card.innerText : '';
            // Split into non-empty lines, skip UI noise
            const SKIP = new Set(['Skip to main content', 'Skip to search', 'Home', 'Jobs', 'Me', 'Messaging', 'Notifications']);
            const lines = rawText.split('\\n').map(l => l.trim()).filter(l =>
                l && l !== '•' && l.length > 1 && !SKIP.has(l) && !/^\\d+ notification/.test(l)
            );

            // First real line is the person's name
            const name = lines[0] || '';
            // Skip if name looks like a UI element
            if (!name || /notification|skip|home|jobs/i.test(name)) return;

            // Find location: "City, Country" or "City, State, Country"
            let location = '';
            for (const line of lines) {
                if (line.includes(',') && line.length < 60 &&
                    !line.includes(' at ') && !line.toLowerCase().includes('connect') &&
                    !/^\\d/.test(line) && !line.includes('mutual') &&
                    !line.includes('Current:') && !line.includes('notification')) {
                    location = line;
                    break;
                }
            }

            // Find title: "X at Y" pattern
            let title = '';
            for (const line of lines) {
                if (line.includes(' at ') && line.length < 120 && !line.includes('Current:')) {
                    title = line;
                    break;
                }
            }

            results.push({
                name,
                profile_url: 'https://www.linkedin.com/in/' + slug + '/',
                location,
                title,
            });
        });

        return results;
    }
    """)
    return cards


async def has_next_page(page: Page) -> bool:
    # Look for Next button by text content (aria-label may vary)
    try:
        btn = await page.query_selector(
            'button:has-text("Next"), [aria-label="Next"]'
        )
        return btn is not None
    except Exception:
        return False


async def click_next_page(page: Page):
    try:
        btn = await page.query_selector('button:has-text("Next"), [aria-label="Next"]')
        if btn:
            await btn.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            await btn.click()
            await asyncio.sleep(random.uniform(3, 5))
    except Exception:
        pass



async def handle_connect_modal(page: Page) -> bool:
    """
    Handle the connection modal.
    If note is provided: click 'Add a note', type it, send.
    If no note: click 'Send without a note'.
    If no modal appears, LinkedIn auto-sent — treat as success.
    """
    MODAL_SELS = ['[role="dialog"]', '.artdeco-modal', '.send-invite', '.invitation-modal']

    modal = None
    for sel in MODAL_SELS:
        try:
            modal = await page.wait_for_selector(sel, timeout=4000)
            if modal:
                break
        except Exception:
            continue

    if not modal:
        try:
            toast = await page.wait_for_selector(
                '[data-test-artdeco-toast-item], .artdeco-toast-item, [role="alert"]',
                timeout=3000,
            )
            if toast:
                text = await toast.inner_text()
                if any(w in text.lower() for w in ["sent", "invited", "invitation"]):
                    return True
        except Exception:
            pass
        return True  # Assume auto-sent

    await asyncio.sleep(0.8)

    # Always send without a note (adding a note requires LinkedIn Premium)
    send_btn = None
    for sel in [
        'button:has-text("Send without a note")',
        'button[aria-label*="Send now" i]',
        'button[aria-label*="Send invitation" i]',
        'button:has-text("Send")',
    ]:
        send_btn = await page.query_selector(sel)
        if send_btn:
            break

    if send_btn:
        await send_btn.click()
        await asyncio.sleep(random.uniform(1.5, 2.5))
        return True

    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        return True
    except Exception:
        pass

    return False


# ── Profile page: visit + send connection request ─────────────────────────────

async def send_connection_on_profile(
    page: Page,
    profile_url: str,
    dry_run: bool,
    skip_title_filter: bool = False,
) -> tuple[bool, str, str]:
    """
    Visit the profile page, find the Connect button, send with note.
    Returns (success, confirmed_name, confirmed_location).
    """
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        print(f"    – Page load failed: {e}")
        return False, "", ""

    await asyncio.sleep(random.uniform(2.5, 4))

    # Safety check: confirm we landed on the right profile (not redirected elsewhere)
    if profile_url.rstrip("/") not in page.url:
        print(f"    – Redirected away from profile ({page.url[:60]}), skipping")
        return False, "", ""

    try:
        await page.evaluate("window.scrollBy(0, 300)")
    except Exception:
        pass
    await asyncio.sleep(1)

    try:
        profile_info = await page.evaluate("""
        () => {
            const h1 = document.querySelector('h1');
            const locEl = document.querySelector(
                '.pb2 span.text-body-small, .pv-text-details__left-panel span.text-body-small'
            );
            const titleEl = document.querySelector('.text-body-medium');
            return {
                name:     h1      ? h1.innerText.trim()      : '',
                location: locEl   ? locEl.innerText.trim()   : '',
                title:    titleEl ? titleEl.innerText.trim() : '',
            };
        }
        """)
    except Exception:
        profile_info = {}
    name     = profile_info.get("name", "")
    location = profile_info.get("location", "")
    title    = profile_info.get("title", "")

    # India check — location only on profile page (title may mention IIT/India in education history)
    if location and is_india(location):
        print(f"    – India on profile ({location}), skipping")
        return False, name, location

    # Double-check title on profile page — skip engineers even if card title was empty
    if not skip_title_filter and title and not is_leadership(title):
        print(f"    – Not leadership on profile ({title[:50]}), skipping")
        return False, name, location

    # Skip if already connected (1st degree — Message button visible, no Connect)
    try:
        msg_btn = await page.query_selector('button:has-text("Message"), a:has-text("Message")')
        if msg_btn and await msg_btn.is_visible():
            print(f"    – Already connected, skipping")
            return False, name, location
    except Exception:
        pass

    # Skip if connection request already pending
    try:
        pending_btn = await page.query_selector(
            'button:has-text("Pending"), button[aria-label*="Pending" i]'
        )
        if pending_btn and await pending_btn.is_visible():
            print(f"    – Request already pending, skipping")
            return False, name, location
    except Exception:
        pass

    # Step 1: Look for Connect button ONLY for this profile owner (not sidebar/recommendations).
    # Sidebar cards also have "Invite X to connect" buttons — we must filter them out.
    connect_btn = None
    first_name  = (name.split()[0] if name else "").lower()

    # Priority 1: name-specific aria-label  (e.g. "Invite Ankur Goel to connect")
    for sel in (
        [f'button[aria-label*="Invite {name}" i]'] if name else []
    ) + (
        [f'button[aria-label*="Invite {first_name}" i]'] if first_name else []
    ) + [
        'button[aria-label="Connect"]',          # exact generic label (no name)
        'button[aria-label*="connect" i]',        # generic aria-label fallback
    ]:
        try:
            for btn in await page.query_selector_all(sel):
                if not await btn.is_visible():
                    continue
                aria = (await btn.get_attribute("aria-label") or "").lower()
                # If it says "invite X" but X is NOT our person → sidebar button, skip
                if "invite" in aria and first_name and first_name not in aria:
                    continue
                connect_btn = btn
                print(f"    [Connect found directly: {sel[:70]}]")
                break
        except Exception:
            pass
        if connect_btn:
            break

    # Priority 2: plain "Connect" text button — but filter out sidebar "Invite X" buttons
    if not connect_btn:
        try:
            for btn in await page.query_selector_all('button:has-text("Connect")'):
                if not await btn.is_visible():
                    continue
                aria     = (await btn.get_attribute("aria-label") or "").lower()
                btn_text = (await btn.inner_text()).strip().lower()
                if "connect" not in btn_text:
                    continue
                # Skip if button belongs to a different person in sidebar
                if "invite" in aria and first_name and first_name not in aria:
                    continue
                connect_btn = btn
                print(f"    [Connect found via text filter]")
                break
        except Exception:
            pass

    # Step 2: If not found directly, ALWAYS try the More button
    if not connect_btn:
        print(f"    [no direct Connect — opening More menu]")
        more_btn = None

        # Try every reasonable selector for the More button.
        # LinkedIn uses button OR div[role=button], and aria-label varies by version.
        for sel in [
            'button[aria-label="More actions"]',
            'button[aria-label*="More actions" i]',
            '[role="button"][aria-label="More actions"]',
            '[role="button"][aria-label*="More actions" i]',
            'button:text-is("More")',
            '[role="button"]:text-is("More")',
            '[data-control-name="overflow_actions"]',
        ]:
            try:
                candidates = await page.query_selector_all(sel)
                for btn in candidates:
                    if await btn.is_visible():
                        more_btn = btn
                        print(f"    [More button found: {sel}]")
                        break
            except Exception:
                pass
            if more_btn:
                break

        if not more_btn:
            print(f"    [More button not found — Connect unavailable]")
            return False, name, location

        # Click More and wait for dropdown
        await more_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.5, 1))
        await more_btn.click()
        await asyncio.sleep(2.5)   # give dropdown time to fully open

        # Search for Connect inside the dropdown — click immediately while it's open
        dropdown_clicked = False
        for sel in [
            '[role="menuitem"]:has-text("Connect")',
            'li.artdeco-dropdown__item:has-text("Connect")',
            'div.artdeco-dropdown__item:has-text("Connect")',
            '.artdeco-dropdown__content [role="menuitem"]:has-text("Connect")',
            '[data-control-name="connect"]',
            'a:has-text("Connect")',
        ]:
            try:
                item = await page.query_selector(sel)
                if not item:
                    continue
                print(f"    [Connect found in More dropdown: {sel}]")
                if dry_run:
                    print(f"    [dry-run] Would connect: {name} | {location}")
                    return True, name, location
                # Click immediately — don't scroll, don't delay; dropdown closes if we wait
                try:
                    await item.click(timeout=5000)
                except Exception:
                    # force=True bypasses visibility check in case dropdown partially collapsed
                    await item.click(timeout=5000, force=True)
                dropdown_clicked = True
                break
            except Exception:
                pass

        if not dropdown_clicked:
            print(f"    [Connect not in More dropdown either — Follow-only profile]")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False, name, location

        await asyncio.sleep(random.uniform(1.5, 2.5))
        success = await handle_connect_modal(page)
        return success, name, location

    # ── Direct connect button (not from dropdown) ──────────────────────────────
    if dry_run:
        print(f"    [dry-run] Would connect: {name} | {location}")
        return True, name, location

    await connect_btn.scroll_into_view_if_needed()
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await connect_btn.click(timeout=10000)
    await asyncio.sleep(random.uniform(1.5, 2.5))

    success = await handle_connect_modal(page)
    return success, name, location


# ── Search-page connect buttons ───────────────────────────────────────────────

async def get_connect_buttons_from_page(page: Page) -> list[tuple]:
    """
    Scan the current search results page for visible Connect buttons.
    For each one, walk up the DOM to find the profile URL, name, location, title.
    Returns list of (playwright_button_handle, info_dict).
    Only returns buttons that are visible — profiles without a visible Connect
    button on the search card are skipped entirely (no profile page visit).
    """
    # Scroll to load all cards
    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 500)")
        await asyncio.sleep(random.uniform(0.3, 0.5))
    await asyncio.sleep(1.5)

    # Cast a wide net — LinkedIn uses aria-label OR plain text depending on UI version
    buttons = await page.query_selector_all(
        'button[aria-label*="connect" i], '
        'button[aria-label*="Invite" i], '
        'button:has-text("Connect")'
    )
    print(f"    [debug] raw buttons found by selector: {len(buttons)}")

    results = []
    seen_btns: set[int] = set()
    for btn in buttons:
        btn_id = id(btn)
        if btn_id in seen_btns:
            continue
        seen_btns.add(btn_id)

        if not await btn.is_visible():
            continue

        # Accept any button whose text contains "connect" (handles "+ Connect", "Connect", etc.)
        btn_text = (await btn.inner_text()).strip()
        if "connect" not in btn_text.lower():
            continue

        info = await page.evaluate("""
        (btn) => {
            let el = btn;
            for (let i = 0; i < 15; i++) {
                el = el.parentElement;
                if (!el) break;
                const links = el.querySelectorAll('a[href*="/in/"]');
                for (const link of links) {
                    const m = link.href.match(/linkedin\\.com\\/in\\/([^/?#]+)/);
                    if (!m || /^\\d+$/.test(m[1])) continue;

                    const lines = (el.innerText || '')
                        .split('\\n').map(l => l.trim()).filter(Boolean);
                    const name = lines[0] || '';
                    if (!name || /skip|home|notification/i.test(name)) continue;

                    let location = '';
                    for (const l of lines) {
                        if (l.includes(',') && l.length < 60 &&
                            !l.includes(' at ') && !l.includes('mutual')) {
                            location = l; break;
                        }
                    }
                    let title = '';
                    for (const l of lines) {
                        if (l.includes(' at ') && l.length < 120) { title = l; break; }
                    }

                    return {
                        profile_url: 'https://www.linkedin.com/in/' + m[1] + '/',
                        name,
                        location,
                        title,
                    };
                }
            }
            return null;
        }
        """, btn)

        if info and info.get("profile_url"):
            results.append((btn, info))

    return results


# ── Main runner ───────────────────────────────────────────────────────────────

async def run(session_limit: int, dry_run: bool, skip_title_filter: bool = False):
    sent_log     = load_sent_log()
    sent_today   = count_sent_today(sent_log)
    sent_week    = count_sent_this_week(sent_log)
    daily_left   = DAILY_LIMIT - sent_today
    weekly_left  = WEEKLY_LIMIT - sent_week
    session_cap  = min(session_limit, daily_left, weekly_left)

    # Acceptance rate warning
    stats = compute_acceptance_stats(sent_log)

    print("\nLinkedIn Connector")
    print(f"  Daily limit  : {DAILY_LIMIT}/day  (sent today: {sent_today})")
    print(f"  Weekly limit : {WEEKLY_LIMIT}/week (sent this week: {sent_week})")
    print(f"  This session : {session_cap} requests available")
    print(f"  Title filter : {'OFF — any role' if skip_title_filter else 'ON — founders/leadership only'}")
    if stats["total"] >= 20:
        print(f"  Accept rate  : {stats['rate']:.1f}% ({stats['accepted']}/{stats['total']})")
        if stats["rate"] < 30:
            print(f"  ⚠️  WARNING: Low acceptance rate — account may be at risk")
    if dry_run:
        print(f"  Mode         : DRY RUN (no requests will be sent)\n")
    else:
        print()

    if session_cap <= 0 and not dry_run:
        if weekly_left <= 0:
            print("  Weekly limit reached. Try again next week or use --check-acceptance to review stats.")
        else:
            print("  Daily limit reached. Run again tomorrow.")
        return

    sent_count      = 0
    skip_india      = 0
    skip_sent       = 0
    skip_no_connect = 0
    seen_this_run: set[str] = set()

    async with async_playwright() as pw:
        ctx  = await launch_browser(pw)
        page = await ctx.new_page()
        await ensure_logged_in(page)

        for keyword in TARGET_TITLES:
            if sent_count >= session_cap and not dry_run:
                break

            print(f"\n── '{keyword}' ──────────────────────────────────────")
            page_num = 1

            while True:
                if sent_count >= session_cap and not dry_run:
                    break

                search_url = build_search_url(keyword, page_num)
                print(f"  Page {page_num}: {search_url[:120]}")

                page = await recover_page(ctx, page)

                # Safety check before search
                stop_reason = await safety_check(page)
                if stop_reason:
                    print(f"\n  🛑 {stop_reason} — stopping immediately.")
                    if stop_reason == "CAPTCHA_DETECTED":
                        print("  LinkedIn is showing a CAPTCHA. Solve it manually and try again later.")
                    elif stop_reason == "RATE_LIMIT_REACHED":
                        print("  LinkedIn has rate-limited your account. Wait 24-48 hours before retrying.")
                    await ctx.close()
                    return

                cards = await get_cards_from_page(page, search_url)

                if not cards:
                    print(f"  No results.")
                    break

                for card in cards:
                    if sent_count >= session_cap and not dry_run:
                        break

                    purl     = card["profile_url"]
                    name     = card["name"]
                    location = card["location"]
                    title    = card["title"]

                    if purl in seen_this_run:
                        continue
                    seen_this_run.add(purl)

                    # Skip already connected (1st degree)
                    if "• 1st" in name:
                        print(f"  [connected] {name} — already in your network")
                        continue

                    if is_india(location):
                        print(f"  [india]   {name} ({location})")
                        skip_india += 1
                        continue

                    if purl in sent_log:
                        print(f"  [sent]    {name} ({sent_log[purl]})")
                        skip_sent += 1
                        continue

                    # Title filter: skip non-leadership roles (engineers, designers, etc.)
                    if not skip_title_filter and title and not is_leadership(title):
                        print(f"  [skip]    {name} — not leadership ({title[:50]})")
                        continue

                    print(f"  → {name} | {title or '?'} | {location or '?'}")

                    page = await recover_page(ctx, page)
                    try:
                        ok, confirmed_name, confirmed_loc = await send_connection_on_profile(
                            page, purl, dry_run, skip_title_filter=skip_title_filter
                        )
                    except Exception as e:
                        print(f"    – Unexpected error: {e}")
                        skip_no_connect += 1
                        page = await recover_page(ctx, page)
                        continue

                    # Safety check after profile interaction
                    stop_reason = await safety_check(page)
                    if stop_reason:
                        print(f"\n  🛑 {stop_reason} — stopping immediately.")
                        if stop_reason == "CAPTCHA_DETECTED":
                            print("  LinkedIn is showing a CAPTCHA. Solve it manually and try again later.")
                        elif stop_reason == "RATE_LIMIT_REACHED":
                            print("  LinkedIn has rate-limited your account. Wait 24-48 hours before retrying.")
                        await ctx.close()
                        return

                    if not name and confirmed_name:
                        name = confirmed_name
                    if not location and confirmed_loc:
                        location = confirmed_loc

                    if not ok and is_india(confirmed_loc):
                        print(f"    [india on profile] skipped")
                        skip_india += 1
                        continue

                    if ok:
                        sent_count += 1
                        if not dry_run:
                            append_sent_log(purl, name, location, title)
                            sent_log[purl] = str(date.today())
                        print(f"    ✓ Sent ({sent_count}/{session_cap})")
                    else:
                        skip_no_connect += 1
                        print(f"    – Connect unavailable (Follow-only / already connected / pending)")
                        continue   # no wait needed — nothing was sent

                    delay = random.uniform(10, 20)
                    print(f"    Waiting {delay:.0f}s …")
                    await asyncio.sleep(delay)

                # Paginate — recover page first in case it closed
                page = await recover_page(ctx, page)
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(2)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)
                except Exception:
                    break

                if await has_next_page(page):
                    await click_next_page(page)
                    page_num += 1
                else:
                    break

        await ctx.close()

    print(f"\n{'='*55}")
    print(f"  Sent         : {sent_count}")
    print(f"  India skip   : {skip_india}")
    print(f"  Already sent : {skip_sent}")
    print(f"  No Connect   : {skip_no_connect}")
    print(f"  Log          : {SENT_LOG}")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def check_acceptance():
    """
    Visit profiles in the sent log and check if they accepted the connection.
    Updates the CSV with acceptance status.
    """
    if not SENT_LOG.exists():
        print("No sent log found.")
        return

    rows = []
    with open(SENT_LOG, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "accepted" not in fieldnames:
            fieldnames.append("accepted")
        for row in reader:
            rows.append(row)

    # Only check rows that haven't been marked yet
    unchecked = [r for r in rows if r.get("accepted", "") == ""]
    if not unchecked:
        print("All connections already checked.")
        stats = compute_acceptance_stats({})
        print(f"\n  Total sent   : {stats['total']}")
        print(f"  Accepted     : {stats['accepted']}")
        print(f"  Pending      : {stats['pending']}")
        print(f"  Accept rate  : {stats['rate']:.1f}%")
        return

    print(f"\nChecking {len(unchecked)} pending connections...")

    async with async_playwright() as pw:
        ctx = await launch_browser(pw)
        page = await ctx.new_page()
        await ensure_logged_in(page)

        checked = 0
        for row in unchecked:
            url = row["profile_url"]
            name = row.get("name", "?")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(random.uniform(2, 3.5))

                # Check for "Message" button (= connected / 1st degree)
                msg_btn = await page.query_selector(
                    'button:has-text("Message"), a:has-text("Message")'
                )
                if msg_btn and await msg_btn.is_visible():
                    row["accepted"] = "yes"
                    print(f"  ✓ {name} — accepted")
                else:
                    # Check if still pending
                    pending_btn = await page.query_selector(
                        'button:has-text("Pending"), button[aria-label*="Pending" i]'
                    )
                    if pending_btn and await pending_btn.is_visible():
                        row["accepted"] = "pending"
                        print(f"  ⏳ {name} — still pending")
                    else:
                        row["accepted"] = "no"
                        print(f"  ✗ {name} — not connected (declined/withdrawn)")

                checked += 1
                await asyncio.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"  ? {name} — error checking: {e}")

        await ctx.close()

    # Rewrite CSV with updated acceptance status
    with open(SENT_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    stats = compute_acceptance_stats({})
    print(f"\n{'='*50}")
    print(f"  Checked      : {checked}")
    print(f"  Total sent   : {stats['total']}")
    print(f"  Accepted     : {stats['accepted']}")
    print(f"  Pending      : {stats['pending']}")
    print(f"  Accept rate  : {stats['rate']:.1f}%")

    if stats['rate'] < 30 and stats['total'] >= 20:
        print(f"\n  ⚠️  WARNING: Acceptance rate is below 30%.")
        print(f"  LinkedIn may restrict your account. Consider:")
        print(f"    - Reducing daily volume")
        print(f"    - Improving your profile/headline")
        print(f"    - Targeting more relevant connections")


async def test_profile(url: str, skip_title_filter: bool = False, send: bool = False):
    """Test the connect flow on a single profile URL — useful for debugging."""
    async with async_playwright() as pw:
        ctx  = await launch_browser(pw)
        page = await ctx.new_page()
        await ensure_logged_in(page)
        print(f"\nTesting: {url}")
        print(f"  Title filter: {'OFF (--any-role)' if skip_title_filter else 'ON (founders/leadership only)'}")
        print(f"  Mode        : {'SEND (will actually connect)' if send else 'DRY RUN (preview only)'}\n")
        ok, name, location = await send_connection_on_profile(
            page, url, dry_run=not send, skip_title_filter=skip_title_filter
        )
        print(f"\nResult: ok={ok}  name={name}  location={location}")
        await ctx.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit", type=int, default=SESSION_DEFAULT,
        help=f"Max requests this session (default {SESSION_DEFAULT}, cap {DAILY_LIMIT}/day)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only — no requests sent"
    )
    parser.add_argument(
        "--test-profile", metavar="URL",
        help="Test the connect flow on a single LinkedIn profile URL (always dry-run)"
    )
    parser.add_argument(
        "--any-role", action="store_true",
        help="Disable title filter — connect with any role, not just founders/leadership"
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Used with --test-profile: actually send the connection request (default is dry-run)"
    )
    parser.add_argument(
        "--check-acceptance", action="store_true",
        help="Check which sent connections were accepted and show acceptance rate stats"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show quick stats from the CSV without visiting LinkedIn"
    )
    args = parser.parse_args()

    if args.stats:
        stats = compute_acceptance_stats({})
        sent_log = load_sent_log()
        print(f"\nLinkedIn Connector — Stats")
        print(f"  Total sent     : {stats['total']}")
        print(f"  Accepted       : {stats['accepted']}")
        print(f"  Pending        : {stats['pending']}")
        print(f"  Accept rate    : {stats['rate']:.1f}%")
        print(f"  Sent today     : {count_sent_today(sent_log)}")
        print(f"  Sent this week : {count_sent_this_week(sent_log)}")
    elif args.check_acceptance:
        asyncio.run(check_acceptance())
    elif args.test_profile:
        asyncio.run(test_profile(args.test_profile, skip_title_filter=args.any_role, send=args.send))
    else:
        asyncio.run(run(args.limit, args.dry_run, skip_title_filter=args.any_role))


if __name__ == "__main__":
    main()
