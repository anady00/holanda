#!/usr/bin/env python3

"""
Netherlands VFS appointment monitor.

- Opens the Netherlands appointment scheduling URL.
- Discovers all Application Categories automatically.
- Checks every category independently.
- Detects the "no slots available" message.
- Detects possible appointment availability.
- Sends an email only when availability is detected.
- Does NOT book appointments automatically.
- Saves screenshots/HTML when an unexpected state or error occurs.

Required GitHub Secrets:
    NL_APPOINTMENT_URL
    GMAIL_USER
    GMAIL_APP_PASSWORD
    NOTIFY_EMAIL

Optional environment variables:
    APPLICANTS=1
    POLL_SECONDS=300
    CATEGORY_DELAY_SECONDS=2
    PAGE_TIMEOUT_MS=60000
    HEADLESS=true
    DEBUG=1
    ARTIFACT_DIR=debug
    STATE_FILE=state.json
"""

import json
import os
import re
import smtplib
import sys
import time

from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Tuple

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.environ.get("NL_APPOINTMENT_URL", "").strip()

APPLICANTS = os.environ.get("APPLICANTS", "1").strip()

POLL_SECONDS = int(
    os.environ.get("POLL_SECONDS", "300")
)

CATEGORY_DELAY_SECONDS = float(
    os.environ.get("CATEGORY_DELAY_SECONDS", "2")
)

PAGE_TIMEOUT_MS = int(
    os.environ.get("PAGE_TIMEOUT_MS", "60000")
)

HEADLESS = (
    os.environ.get("HEADLESS", "true").lower()
    not in {"0", "false", "no"}
)

DEBUG = (
    os.environ.get("DEBUG", "0").lower()
    in {"1", "true", "yes"}
)

ARTIFACT_DIR = Path(
    os.environ.get("ARTIFACT_DIR", "debug")
)

STATE_FILE = Path(
    os.environ.get("STATE_FILE", "state.json")
)


# ============================================================
# TEXT PATTERNS
# ============================================================

NO_SLOT_PATTERNS = [
    r"there are currently no slots available",
    r"no slots available",
    r"no appointments available",
    r"no appointment slots",
    r"not possible to make an appointment at this time",
]


POSITIVE_PATTERNS = [
    r"select (?:a|an) date",
    r"choose (?:a|an) date",
    r"appointment date",
    r"available appointments",
    r"available time",
    r"select a time",
    r"choose a time",
    r"appointment time",
    r"calendar",
]


CATEGORY_HINTS = (
    "passport",
    "identity",
    "legalisation",
    "legalization",
    "mvv",
    "certificate",
    "copy conform",
    "signature",
)


# ============================================================
# HELPERS
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()

    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value
    )

    return value[:120] or "unknown"


def normalize(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        text or ""
    ).strip().lower()


def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {
            "categories": {},
            "last_run": None
        }

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {
            "categories": {},
            "last_run": None
        }


def save_state(state: Dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# DEBUG / ARTIFACTS
# ============================================================

def save_debug(
    page,
    prefix: str,
    category: str = ""
) -> Tuple[Path, Path]:

    ARTIFACT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    stem = (
        f"{stamp}_"
        f"{safe_name(prefix)}_"
        f"{safe_name(category)}"
    ).strip("_")

    png = ARTIFACT_DIR / f"{stem}.png"

    html = ARTIFACT_DIR / f"{stem}.html"

    try:
        page.screenshot(
            path=str(png),
            full_page=True
        )

    except Exception as exc:
        print(
            f"[warn] screenshot failed: {exc}",
            flush=True
        )

    try:
        html.write_text(
            page.content(),
            encoding="utf-8"
        )

    except Exception as exc:
        print(
            f"[warn] HTML save failed: {exc}",
            flush=True
        )

    return png, html


def page_text(page) -> str:

    try:
        return normalize(
            page.locator("body").inner_text(
                timeout=5000
            )
        )

    except Exception:
        return normalize(
            page.content()
        )


# ============================================================
# FIND APPLICATION CATEGORY DROPDOWN
# ============================================================

def find_category_select(page):

    """
    Find Application Category <select>
    without depending on unstable ASP.NET IDs.
    """

    selects = page.locator(
        "select:visible"
    )

    count = selects.count()

    candidates = []

    for i in range(count):

        sel = selects.nth(i)

        try:

            options = sel.locator("option")

            texts = [
                normalize(
                    options.nth(j).inner_text()
                )
                for j in range(options.count())
            ]

            joined = " | ".join(texts)

            score = sum(
                1
                for hint in CATEGORY_HINTS
                if hint in joined
            )

            if score:
                candidates.append(
                    (
                        score,
                        i,
                        texts
                    )
                )

        except Exception:
            continue

    if not candidates:
        raise RuntimeError(
            "Could not find the Application Category dropdown."
        )

    candidates.sort(
        reverse=True
    )

    return selects.nth(
        candidates[0][1]
    )


# ============================================================
# DISCOVER ALL CATEGORIES
# ============================================================

def discover_categories(page) -> List[Dict[str, str]]:

    sel = find_category_select(page)

    options = sel.locator("option")

    categories = []

    for i in range(options.count()):

        opt = options.nth(i)

        text = opt.inner_text().strip()

        value = (
            opt.get_attribute("value")
            or ""
        )

        disabled = opt.is_disabled()

        if not text or disabled:
            continue

        # Skip placeholder
        if normalize(text) in {
            "-select-",
            "select",
            "--select--",
            "- select -",
        }:
            continue

        categories.append(
            {
                "text": text,
                "value": value,
            }
        )

    # Remove duplicates
    seen = set()

    result = []

    for item in categories:

        key = (
            item["text"],
            item["value"]
        )

        if key not in seen:

            seen.add(key)

            result.append(item)

    if not result:

        raise RuntimeError(
            "Application Category dropdown was found, "
            "but no usable options were discovered."
        )

    return result


# ============================================================
# APPLICANT COUNT
# ============================================================

def set_applicants(page) -> None:

    """
    The screenshot shows a normal text input
    containing the number of applicants.
    """

    inputs = page.locator(
        "input:visible"
    )

    for i in range(inputs.count()):

        inp = inputs.nth(i)

        try:

            typ = (
                inp.get_attribute("type")
                or "text"
            ).lower()

            value = (
                inp.input_value()
                or ""
            ).strip()

            if (
                typ in {"text", "number"}
                and value in {
                    "",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                }
            ):

                inp.fill(
                    APPLICANTS
                )

                return

        except Exception:
            continue

    # Fallback if applicants is a select
    selects = page.locator(
        "select:visible"
    )

    for i in range(selects.count()):

        sel = selects.nth(i)

        try:

            texts = [
                normalize(x)
                for x in sel
                .locator("option")
                .all_inner_texts()
            ]

            if APPLICANTS in texts:

                sel.select_option(
                    label=APPLICANTS
                )

                return

        except Exception:
            continue

    print(
        "[warn] Applicant count field "
        "was not identified; "
        "leaving site default.",
        flush=True
    )


# ============================================================
# CLICK CONTINUE
# ============================================================

def click_continue(page) -> None:

    """
    Finds Continue without relying on
    unstable ASP.NET control IDs.
    """

    locators = [

        page.get_by_role(
            "button",
            name=re.compile(
                r"^\s*continue\s*$",
                re.I
            )
        ),

        page.get_by_role(
            "link",
            name=re.compile(
                r"^\s*continue\s*$",
                re.I
            )
        ),

    ]

    for locator in locators:

        try:

            if locator.count():

                locator.first.click(
                    timeout=10000
                )

                return

        except Exception:
            pass

    # Fallback
    buttons = page.locator(
        "input:visible, "
        "button:visible, "
        "a:visible"
    )

    for i in range(buttons.count()):

        el = buttons.nth(i)

        try:

            txt = normalize(
                el.inner_text()
                or el.get_attribute("value")
                or ""
            )

            if txt == "continue":

                el.click(
                    timeout=10000
                )

                return

        except Exception:
            continue

    raise RuntimeError(
        "Could not find the Continue control."
    )


# ============================================================
# CLASSIFY RESULT
# ============================================================

def classify_result(page) -> Tuple[str, str]:

    """
    Returns:

        unavailable
        available
        unknown
    """

    text = page_text(page)

    # Explicit no-slot message
    for pattern in NO_SLOT_PATTERNS:

        if re.search(
            pattern,
            text,
            re.I
        ):

            return (
                "unavailable",
                f"matched: {pattern}"
            )

    # Appointment info page
    current_url = page.url.lower()

    if (
        "appschedulinggetinfo.aspx"
        in current_url
    ):

        return (
            "available",
            f"appointment info page: {page.url}"
        )

    # Positive signals
    for pattern in POSITIVE_PATTERNS:

        if re.search(
            pattern,
            text,
            re.I
        ):

            return (
                "available",
                f"matched positive signal: {pattern}"
            )

    return (
        "unknown",
        "Neither explicit no-slot message "
        "nor positive appointment signal was found"
    )


# ============================================================
# CHECK ONE CATEGORY
# ============================================================

def inspect_category(
    context,
    category: Dict[str, str],
    index: int,
    total: int
) -> Dict:

    page = context.new_page()

    page.set_default_timeout(
        PAGE_TIMEOUT_MS
    )

    try:

        print(
            f"[{index}/{total}] "
            f"Checking: {category['text']}",
            flush=True
        )

        # ----------------------------------------------------
        # Open fresh session/page
        # ----------------------------------------------------

        page.goto(
            BASE_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=15000
            )

        except PlaywrightTimeoutError:
            pass

        # ----------------------------------------------------
        # Set applicants
        # ----------------------------------------------------

        set_applicants(page)

        # ----------------------------------------------------
        # Find category dropdown
        # ----------------------------------------------------

        sel = find_category_select(
            page
        )

        # ----------------------------------------------------
        # Select category
        # ----------------------------------------------------

        if category["value"]:

            sel.select_option(
                value=category["value"]
            )

        else:

            sel.select_option(
                label=category["text"]
            )

        # ----------------------------------------------------
        # Continue
        # ----------------------------------------------------

        click_continue(page)

        # ----------------------------------------------------
        # Wait for ASP.NET postback
        # ----------------------------------------------------

        try:

            page.wait_for_load_state(
                "domcontentloaded",
                timeout=20000
            )

        except PlaywrightTimeoutError:
            pass

        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=10000
            )

        except PlaywrightTimeoutError:
            pass

        # Short stabilization delay
        page.wait_for_timeout(
            1500
        )

        # ----------------------------------------------------
        # Detect appointment state
        # ----------------------------------------------------

        result, reason = classify_result(
            page
        )

        # ----------------------------------------------------
        # Unknown state
        # ----------------------------------------------------

        if result == "unknown":

            png, html = save_debug(
                page,
                "unknown_state",
                category["text"]
            )

            print(
                f"[warn] UNKNOWN state for "
                f"{category['text']}",
                flush=True
            )

            print(
                f"[warn] Screenshot: {png}",
                flush=True
            )

            print(
                f"[warn] HTML: {html}",
                flush=True
            )

        # ----------------------------------------------------
        # Possible availability
        # ----------------------------------------------------

        elif result == "available":

            png, html = save_debug(
                page,
                "AVAILABLE",
                category["text"]
            )

            print(
                f"[ALERT] POSSIBLE AVAILABILITY: "
                f"{category['text']}",
                flush=True
            )

            print(
                f"[ALERT] Reason: {reason}",
                flush=True
            )

            print(
                f"[ALERT] Screenshot: {png}",
                flush=True
            )

        # ----------------------------------------------------
        # No appointment
        # ----------------------------------------------------

        else:

            print(
                f"[ok] No availability: "
                f"{category['text']} | {reason}",
                flush=True
            )

        return {
            "status": result,
            "reason": reason,
            "url": page.url,
            "checked_at": now_iso(),
        }

    except PlaywrightTimeoutError as exc:

        png, html = save_debug(
            page,
            "timeout",
            category["text"]
        )

        return {
            "status": "error",
            "reason": f"timeout: {exc}",
            "url": page.url,
            "checked_at": now_iso(),
            "png": str(png),
            "html": str(html),
        }

    except Exception as exc:

        png, html = save_debug(
            page,
            "error",
            category["text"]
        )

        return {
            "status": "error",
            "reason": (
                f"{type(exc).__name__}: {exc}"
            ),
            "url": page.url,
            "checked_at": now_iso(),
            "png": str(png),
            "html": str(html),
        }

    finally:

        try:
            page.close()

        except Exception:
            pass


# ============================================================
# EMAIL
# ============================================================

def send_email(
    subject: str,
    body: str
) -> None:

    user = os.environ.get(
        "GMAIL_USER",
        ""
    ).strip()

    password = os.environ.get(
        "GMAIL_APP_PASSWORD",
        ""
    ).strip()

    recipient = os.environ.get(
        "NOTIFY_EMAIL",
        ""
    ).strip()

    if (
        not user
        or not password
        or not recipient
    ):

        raise RuntimeError(
            "Email secrets missing. "
            "Set GMAIL_USER, "
            "GMAIL_APP_PASSWORD and "
            "NOTIFY_EMAIL."
        )

    msg = EmailMessage()

    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject

    msg.set_content(body)

    with smtplib.SMTP(
        "smtp.gmail.com",
        587,
        timeout=30
    ) as smtp:

        smtp.starttls()

        smtp.login(
            user,
            password
        )

        smtp.send_message(
            msg
        )


# ============================================================
# ONE MONITORING CYCLE
# ============================================================

def run_once() -> bool:

    if not BASE_URL:

        raise RuntimeError(
            "NL_APPOINTMENT_URL is not set."
        )

    state = load_state()

    state.setdefault(
        "categories",
        {}
    )

    with sync_playwright() as p:

        # ----------------------------------------------------
        # Launch Chromium
        # ----------------------------------------------------

        browser = p.chromium.launch(

            headless=HEADLESS,

            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(

            viewport={
                "width": 1440,
                "height": 1000,
            },

            locale="en-US",

            timezone_id="Africa/Cairo",

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151.0.0.0 "
                "Safari/537.36"
            ),
        )

        page = context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT_MS
        )

        # ----------------------------------------------------
        # Discover categories
        # ----------------------------------------------------

        try:

            print(
                f"[info] Opening: {BASE_URL}",
                flush=True
            )

            page.goto(
                BASE_URL,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS
            )

            try:

                page.wait_for_load_state(
                    "networkidle",
                    timeout=15000
                )

            except PlaywrightTimeoutError:
                pass

            # ------------------------------------------------
            # Anti-bot / CAPTCHA detection
            # ------------------------------------------------

            initial_text = page_text(
                page
            )

            challenge_terms = [
                "captcha",
                "verify you are human",
                "checking your browser",
                "access denied",
            ]

            if any(
                term in initial_text
                for term in challenge_terms
            ):

                save_debug(
                    page,
                    "challenge"
                )

                raise RuntimeError(
                    "The site presented a CAPTCHA/"
                    "anti-bot/challenge page. "
                    "The monitor stops instead of "
                    "attempting to bypass it."
                )

            # ------------------------------------------------
            # Discover all categories
            # ------------------------------------------------

            categories = (
                discover_categories(
                    page
                )
            )

            print(
                f"[info] Discovered "
                f"{len(categories)} categories.",
                flush=True
            )

            # ------------------------------------------------
            # Save category list
            # ------------------------------------------------

            ARTIFACT_DIR.mkdir(
                parents=True,
                exist_ok=True
            )

            (
                ARTIFACT_DIR
                / "categories.json"
            ).write_text(

                json.dumps(
                    categories,
                    ensure_ascii=False,
                    indent=2
                ),

                encoding="utf-8"
            )

        except Exception:

            save_debug(
                page,
                "startup_error"
            )

            raise

        finally:

            try:
                page.close()

            except Exception:
                pass

        # ----------------------------------------------------
        # Check every category
        # ----------------------------------------------------

        alerts = []

        for idx, category in enumerate(
            categories,
            start=1
        ):

            result = inspect_category(
                context,
                category,
                idx,
                len(categories)
            )

            key = (
                category["value"]
                or category["text"]
            )

            previous = (
                state["categories"]
                .get(key, {})
            )

            previous_status = (
                previous.get("status")
            )

            state["categories"][key] = {
                **category,
                **result,
            }

            # ------------------------------------------------
            # Alert only when status becomes available
            # ------------------------------------------------

            if (
                result["status"]
                == "available"
                and previous_status
                != "available"
            ):

                alerts.append(
                    (
                        category,
                        result
                    )
                )

            save_state(
                state
            )

            time.sleep(
                CATEGORY_DELAY_SECONDS
            )

        browser.close()

    # --------------------------------------------------------
    # Save cycle timestamp
    # --------------------------------------------------------

    state["last_run"] = now_iso()

    save_state(
        state
    )

    # --------------------------------------------------------
    # Send alert
    # --------------------------------------------------------

    if alerts:

        lines = [
            "Netherlands VFS appointment "
            "monitor detected possible availability.",
            "",
        ]

        for category, result in alerts:

            lines.extend(
                [
                    f"Category: "
                    f"{category['text']}",

                    f"URL: "
                    f"{result.get('url', BASE_URL)}",

                    f"Reason: "
                    f"{result.get('reason', '')}",

                    f"Detected: "
                    f"{result.get('checked_at', now_iso())}",

                    "",
                ]
            )

        lines.append(
            "No appointment was booked automatically."
        )

        send_email(

            subject=(
                "🇳🇱 Netherlands VFS — "
                "Possible appointment available"
            ),

            body="\n".join(lines),
        )

        print(
            f"[alert] Email sent for "
            f"{len(alerts)} category/categories.",
            flush=True
        )

    return bool(alerts)


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    try:

        # Single cycle
        if "--once" in sys.argv:

            run_once()

            return 0

        # Continuous mode
        while True:

            started = time.time()

            try:

                run_once()

            except Exception as exc:

                print(
                    f"[fatal] "
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

                return 1

            elapsed = (
                time.time()
                - started
            )

            wait_for = max(
                10,
                POLL_SECONDS
                - int(elapsed)
            )

            print(
                f"[info] Cycle finished "
                f"in {elapsed:.1f}s. "
                f"Next cycle in "
                f"{wait_for}s.",
                flush=True
            )

            time.sleep(
                wait_for
            )

    except KeyboardInterrupt:

        print(
            "[info] Stopped.",
            flush=True
        )

        return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    raise SystemExit(
        main()
    )
