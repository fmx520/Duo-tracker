#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# ---------- 1. browser.py: launch args + UA ----------
python3 - <<'PY'
from pathlib import Path
p = Path("duo_tracker/browser.py")
s = p.read_text()

old = """    kwargs = dict(
        user_data_dir=str(cfg.profile_dir),
        headless=False,
        no_viewport=True,
    )"""
# Fall back to whatever the current dict looks like if formatting differs
if old not in s and "kwargs = dict(" in s:
    import re
    s = re.sub(
        r"kwargs = dict\([^)]*\)",
        """kwargs = dict(
        user_data_dir=str(cfg.profile_dir),
        headless=False,
        no_viewport=True,
        user_agent=REAL_UA,
        locale="en-US",
        timezone_id="America/New_York",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )""",
        s,
        count=1,
    )
else:
    s = s.replace(old, """kwargs = dict(
        user_data_dir=str(cfg.profile_dir),
        headless=False,
        no_viewport=True,
        user_agent=REAL_UA,
        locale="en-US",
        timezone_id="America/New_York",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )""")

if "REAL_UA =" not in s:
    s = 'REAL_UA = (\n    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "\n    "AppleWebKit/537.36 (KHTML, like Gecko) "\n    "Chrome/124.0.0.0 Safari/537.36"\n)\n\n' + s

p.write_text(s)
print("patched browser.py")
PY

# ---------- 2. browser.py: active in-page fetch ----------
python3 - <<'PY'
from pathlib import Path
p = Path("duo_tracker/browser.py")
s = p.read_text()

if "x-aos-ui-fetch-call-1" not in s:
    # Insert helper imports if missing
    if "import secrets" not in s:
        s = "import secrets\nimport time\n" + s

    # Append active fetch helper to the module
    s += """

def active_fetch(page, part: str):
    \"\"\"kelindi-style active in-page fetch.\"\"\"
    bust = f"_={int(time.time())}-{secrets.token_hex(4)}"
    endpoint = f"/shop/fulfillment-messages?parts.0={part}&{bust}"
    try:
        return page.evaluate(
            \"\"\"async (url) => {
                const res = await fetch(url, {
                    credentials: 'include',
                    headers: {'x-aos-ui-fetch-call-1': crypto.randomUUID()},
                });
                if (res.status === 541) throw new Error('541');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.json();
            }\"\"\",
            endpoint,
        )
    except Exception as e:
        if "541" in str(e):
            raise Blocked("HTTP 541")
        raise
"""
p.write_text(s)
print("appended active_fetch to browser.py")
PY

# ---------- 3. cli.py: retry loop + jitter + decoy + shuffle ----------
python3 - <<'PY'
from pathlib import Path
p = Path("duo_tracker/cli.py")
s = p.read_text()

if "check_round_with_retry" not in s:
    s = s.replace(
        "def cmd_watch(",
        """def check_round_with_retry(page, models, carrier, max_wait=75):
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        try:
            return list(check_round(page, models, carrier))
        except Blocked as e:
            say(f"541 during round: {e} — reloading and retrying", logging.WARNING)
            page.reload(wait_until="domcontentloaded")
            time.sleep(random.uniform(2, 5))
    raise Blocked("541 challenge not passed within 75s")


def maybe_send_decoy(page, decoy_part, counter):
    if counter % 10 != 0:
        return
    try:
        page.evaluate(
            \"\"\"async (u) => fetch(u, {
                credentials: 'include',
                headers: {'x-aos-ui-fetch-call-1': crypto.randomUUID()},
            })\"\"\",
            f"/shop/fulfillment-messages?parts.0={decoy_part}",
        )
    except Exception:
        pass
    time.sleep(random.uniform(1.5, 4.0))


def cmd_watch(",
        1,
    )

# Swap check_round call for retry wrapper, add shuffle + decoy + jitter
s = s.replace(
    "for m, av in check_round(page, cfg.models, cfg.carrier):",
    """shuffled = list(cfg.models)
        random.shuffle(shuffled)
        for m, av in check_round_with_retry(page, shuffled, cfg.carrier):""",
    1,
)

s = s.replace(
    "wait, label = sched.current()",
    "wait, label = sched.current()\n    wait = max(30, int(wait * random.uniform(0.7, 1.3)))",
    1,
)

p.write_text(s)
print("patched cli.py")
PY

echo "Done. Review with: git diff"
echo "Then: git checkout -b evasion && git add -A && git commit -m 'kelindi evasion stack' && git push -u origin evasion"
