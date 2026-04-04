# LinkedIn Connector

Automates LinkedIn connection requests to founders, co-founders, and leadership roles outside India. Built with Playwright and a persistent Chrome profile so it runs as you — no API keys, no headless fingerprint issues.

---

## Features

- Searches LinkedIn for target titles (Founder, Co-Founder, CTO, etc.)
- Filters by geography — only US, UK, Canada, Australia, Singapore
- Skips India-based profiles (location check only)
- Skips engineers, designers, analysts, and other non-leadership roles
- Handles both direct Connect buttons and the **More → Connect** dropdown
- Avoids sidebar/recommendation false positives using name-specific aria-label matching
- Skips already-connected and pending-request profiles automatically
- Sends without a note (LinkedIn Premium not required)
- Random delays between requests to avoid detection
- Logs every sent request to `linkedin_sent_requests.csv`
- Hard cap: 50 requests/day

---

## Requirements

```bash
pip install playwright
playwright install chrome
```

Requires **Google Chrome** installed (uses persistent profile via `--channel chrome`).

---

## First Run — Login

On first run the browser opens and waits for you to log in to LinkedIn manually. After that the session is saved to `~/.linkedin-harvester-profile` and reused automatically.

---

## Usage

```bash
# Default — search for founders/leadership, outside India, limit 50
python3 linkedin_connector.py

# Limit to 20 requests this session
python3 linkedin_connector.py --limit 20

# Dry run — preview profiles without sending any requests
python3 linkedin_connector.py --dry-run

# Connect with any role (bypass title filter)
python3 linkedin_connector.py --any-role

# Test the connect flow on a single profile (dry run)
python3 linkedin_connector.py --test-profile https://www.linkedin.com/in/username/

# Test + actually send the request
python3 linkedin_connector.py --test-profile https://www.linkedin.com/in/username/ --send

# Test + skip title filter + send
python3 linkedin_connector.py --test-profile https://www.linkedin.com/in/username/ --any-role --send
```

---

## Flags

| Flag | Description |
|------|-------------|
| `--limit N` | Max connection requests this session (default: 50) |
| `--dry-run` | Preview only — no requests sent |
| `--any-role` | Disable title filter — connect with any role |
| `--test-profile URL` | Test connect flow on one profile (dry-run by default) |
| `--send` | Used with `--test-profile` to actually send the request |

---

## How it Works

1. Searches LinkedIn people search with `geoUrn` filter (US/UK/Canada/AU/SG)
2. Extracts profile cards — filters India, non-leadership roles, already-sent
3. Visits each profile page to confirm title and location
4. Looks for Connect button in the **profile action bar** using name-specific `aria-label` (avoids sidebar people's Connect buttons)
5. If not found directly, opens the **More actions** dropdown and clicks Connect there
6. Sends without a note — no LinkedIn Premium required
7. Logs to CSV and enforces 50/day hard cap

---

## Output

Sent requests are logged to `linkedin_sent_requests.csv`:

```
profile_url,name,location,title,date_sent
https://www.linkedin.com/in/example/,Jane Smith,"San Francisco, CA",Founder at Acme,2025-01-15
```

---

## Safety Notes

- Keep `--limit` at or below 50/day to stay within LinkedIn's informal limits
- Random 10–20s delays are built in between each request
- Do not run multiple sessions simultaneously
- The persistent Chrome profile stores cookies — keep `~/.linkedin-harvester-profile` private
