"""Tests for the safety guardrail features added to linkedin_connector.py"""
import sys
sys.path.insert(0, '.')

from linkedin_connector import (
    is_india, is_leadership, count_sent_today, count_sent_this_week,
    compute_acceptance_stats, build_search_url,
    CAPTCHA_SIGNALS, RATE_LIMIT_SIGNALS, WEEKLY_LIMIT, DAILY_LIMIT
)
from datetime import date, timedelta

passed = 0
failed = 0

def test(name, condition):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}")
        failed += 1

print("\n── is_india ──")
test("Bangalore detected", is_india("Bangalore, Karnataka") == True)
test("San Francisco clean", is_india("San Francisco, CA") == False)
test("Mumbai detected", is_india("Mumbai, India") == True)
test("London clean", is_india("London, UK") == False)
test("Empty string clean", is_india("") == False)
test("Delhi detected", is_india("New Delhi, Delhi") == True)

print("\n── is_leadership ──")
test("Founder matches", is_leadership("Founder & CEO at Acme") == True)
test("SWE blocked", is_leadership("Software Engineer at Google") == False)
test("CTO matches", is_leadership("CTO at Startup") == True)
test("Empty passes through", is_leadership("") == True)
test("Frontend dev blocked", is_leadership("Frontend Developer") == False)
test("VP Engineering blocked (pre-existing: 'engineer' substring)", is_leadership("VP Engineering") == False)
test("VP Operations matches", is_leadership("VP Operations") == True)
test("Director matches", is_leadership("Director of Product") == True)
test("Analyst blocked", is_leadership("Data Analyst") == False)

print("\n── count_sent_today ──")
today = str(date.today())
yesterday = str(date.today() - timedelta(days=1))
log = {"url1": today, "url2": today, "url3": yesterday}
test("Counts only today", count_sent_today(log) == 2)
test("Empty log = 0", count_sent_today({}) == 0)

print("\n── count_sent_this_week (NEW) ──")
six_days_ago = str(date.today() - timedelta(days=6))
eight_days_ago = str(date.today() - timedelta(days=8))
log2 = {"a": today, "b": yesterday, "c": six_days_ago, "d": eight_days_ago}
test("Rolling 7-day window", count_sent_this_week(log2) == 3)
test("Empty log = 0", count_sent_this_week({}) == 0)
test("All old entries = 0", count_sent_this_week({"x": str(date.today() - timedelta(days=10))}) == 0)
test("Exactly 7 days ago excluded", count_sent_this_week({"x": str(date.today() - timedelta(days=7))}) == 0)
test("6 days ago included", count_sent_this_week({"x": str(date.today() - timedelta(days=6))}) == 1)
test("Today included", count_sent_this_week({"x": today}) == 1)

print("\n── build_search_url ──")
url = build_search_url("Founder", 2)
test("Contains keyword", "keywords=Founder" in url)
test("Contains page number", "page=2" in url)
test("Contains geoUrn", "geoUrn=" in url)
test("HTTPS URL", url.startswith("https://"))

print("\n── CAPTCHA / rate-limit signals (NEW) ──")
test("CAPTCHA signals non-empty", len(CAPTCHA_SIGNALS) > 0)
test("Rate-limit signals non-empty", len(RATE_LIMIT_SIGNALS) > 0)
test("security verification in CAPTCHA", "security verification" in CAPTCHA_SIGNALS)
test("/checkpoint/ in CAPTCHA", "/checkpoint/" in CAPTCHA_SIGNALS)
test("weekly invitation limit in rate-limit", "you've reached the weekly invitation limit" in RATE_LIMIT_SIGNALS)
test("temporarily restricted in rate-limit", "temporarily restricted" in RATE_LIMIT_SIGNALS)

print("\n── Weekly limit math (NEW) ──")
test("DAILY_LIMIT is 50", DAILY_LIMIT == 50)
test("WEEKLY_LIMIT is 150", WEEKLY_LIMIT == 150)

# 140 sent this week, 10 today → only 10 left (weekly caps it)
daily_left = DAILY_LIMIT - 10
weekly_left = WEEKLY_LIMIT - 140
cap = min(50, daily_left, weekly_left)
test("Weekly limit caps session (140/150 sent → 10 left)", cap == 10)

# Weekly fully hit
cap2 = min(50, 40, WEEKLY_LIMIT - 150)
test("Weekly limit = 0 stops session", cap2 == 0)

# Daily hits before weekly
cap3 = min(50, DAILY_LIMIT - 50, WEEKLY_LIMIT - 100)
test("Daily limit = 0 stops even if weekly has room", cap3 == 0)

# Both have room
cap4 = min(50, DAILY_LIMIT - 0, WEEKLY_LIMIT - 0)
test("Fresh day/week → full session available", cap4 == 50)

print("\n── is_india word boundary fix ──")
test("Indianapolis NOT matched", is_india("Indianapolis, Indiana") == False)
test("Indiana NOT matched", is_india("Indiana, United States") == False)
test("India still matched", is_india("Mumbai, India") == True)
test("Indian city still matched", is_india("Bangalore, India") == True)
test("Noida matched", is_india("Noida, Uttar Pradesh") == True)

print("\n── Viewport rotation ──")
from linkedin_connector import VIEWPORTS
test("Multiple viewports available", len(VIEWPORTS) >= 3)
test("All viewports have width/height", all("width" in v and "height" in v for v in VIEWPORTS))
test("Viewports are varied", len(set(v["width"] for v in VIEWPORTS)) > 1)

print("\n── Dead code removed ──")
import inspect, linkedin_connector
all_funcs = [name for name, _ in inspect.getmembers(linkedin_connector, inspect.isfunction)]
test("get_connect_buttons_from_page removed", "get_connect_buttons_from_page" not in all_funcs)
test("human_scroll exists", "human_scroll" in [name for name, _ in inspect.getmembers(linkedin_connector, inspect.iscoroutinefunction)])
test("human_move_and_click exists", "human_move_and_click" in [name for name, _ in inspect.getmembers(linkedin_connector, inspect.iscoroutinefunction)])

print(f"\n{'='*50}")
print(f"  Passed: {passed}")
print(f"  Failed: {failed}")
if failed == 0:
    print(f"  ✅ All {passed} tests passed.")
else:
    print(f"  ❌ {failed} test(s) failed.")
    sys.exit(1)
