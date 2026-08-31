#!/usr/bin/env python3

import json
import os
import re
import smtplib
import sys
import time

from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIG
# ============================================================

URL = os.getenv("NL_APPOINTMENT_URL", "").strip()

APPLICANTS = os.getenv("APPLICANTS", "1").strip()

PAGE_TIMEOUT = int(
    os.getenv("PAGE_TIMEOUT_MS", "60000")
)

CATEGORY_DELAY = float(
    os.getenv("CATEGORY_DELAY_SECONDS", "2")
)

HEADLESS = os.getenv(
    "HEADLESS",
    "true"
).lower() not in {
    "0",
    "false",
    "no",
}

DEBUG = os.getenv(
    "DEBUG",
    "1"
).lower() in {
    "1",
    "true",
    "yes",
}

DEBUG_DIR = Path(
    os.getenv("ARTIFACT_DIR", "debug")
)

STATE_FILE = Path(
    os.getenv("STATE_FILE", "state.json")
)


# ============================================================
# TEXT
# ============================================================

NO_SLOT_TEXT = [
    "there are currently no slots available",
    "no slots available",
    "no appointments available",
    "no appointment slots",
    "not possible to make an appointment at this time",
]

AVAILABLE_TEXT = [
    "select a date",
    "choose a date",
    "appointment date",
    "available appointments",
    "available time",
    "select a time",
    "choose a time",
    "appointment time",
]


# ============================================================
# BASIC HELPERS
# ============================================================

def timestamp():
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize(text):
    return re.sub(
        r"\s+",
        " ",
        str(text or "")
    ).strip()


def lower(text):
    return normalize(text).lower()


def safe_filename(text):
    text = normalize(text)

    text = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        text
    )

    return text[:100] or "unknown"


def save_debug(page, name):
    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    filename = (
        f"{stamp}_{safe_filename(name)}"
    )

    png = DEBUG_DIR / (
        filename + ".png"
    )

    html = DEBUG_DIR / (
        filename + ".html"
    )

    try:
        page.screenshot(
            path=str(png),
            full_page=True
        )
    except Exception as e:
        print(
            f"[debug] screenshot error: {e}"
        )

    try:
        html.write_text(
            page.content(),
            encoding="utf-8"
        )
    except Exception as e:
        print(
            f"[debug] html error: {e}"
        )

    print(
        f"[debug] saved: {png}"
    )

    print(
        f"[debug] saved: {html}"
    )

    return png, html


def get_body_text(page):
    try:
        return normalize(
            page.locator(
                "body"
            ).inner_text(
                timeout=10000
            )
        )
    except Exception:
        return normalize(
            page.content()
        )


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {
            "categories": {},
            "last_run": None,
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
            "last_run": None,
        }


def save_state(state):

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# PAGE LOADING
# ============================================================

def wait_page(page):

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000
        )
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=15000
        )
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(
        1500
    )


# ============================================================
# OPEN START PAGE
# ============================================================

def open_start_page(page):

    print(
        "[info] Opening Netherlands VFS..."
    )

    page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT
    )

    wait_page(page)

    print(
        "[info] Current URL:",
        page.url
    )

    if DEBUG:
        save_debug(
            page,
            "01_welcome"
        )


# ============================================================
# CLICK MAKE APPOINTMENT
# ============================================================

def click_make_appointment(page):

    print(
        "[info] Looking for "
        "'Make an appointment'..."
    )

    # Exact visible text
    candidates = [
        page.get_by_text(
            "Make an appointment",
            exact=True
        ),

        page.get_by_role(
            "link",
            name=re.compile(
                r"make an appointment",
                re.I
            )
        ),

        page.get_by_role(
            "button",
            name=re.compile(
                r"make an appointment",
                re.I
            )
        ),
    ]

    for locator in candidates:

        try:

            count = locator.count()

            if count == 0:
                continue

            for i in range(count):

                element = locator.nth(i)

                if not element.is_visible():
                    continue

                print(
                    "[info] Clicking "
                    "'Make an appointment'"
                )

                element.click(
                    timeout=15000
                )

                wait_page(page)

                print(
                    "[info] After click URL:",
                    page.url
                )

                if DEBUG:
                    save_debug(
                        page,
                        "02_after_make_appointment"
                    )

                return True

        except Exception as e:

            print(
                "[debug] click attempt failed:",
                e
            )

    # --------------------------------------------------------
    # Fallback: search all links/buttons by text
    # --------------------------------------------------------

    elements = page.locator(
        "a, button, input"
    )

    for i in range(
        elements.count()
    ):

        element = elements.nth(i)

        try:

            if not element.is_visible():
                continue

            text = lower(
                element.inner_text()
                or element.get_attribute(
                    "value"
                )
                or element.get_attribute(
                    "aria-label"
                )
            )

            if (
                "make an appointment"
                in text
            ):

                print(
                    "[info] Found fallback "
                    "appointment button"
                )

                element.click(
                    timeout=15000
                )

                wait_page(page)

                return True

        except Exception:
            continue

    save_debug(
        page,
        "ERROR_make_appointment_not_found"
    )

    raise RuntimeError(
        "Could not find 'Make an appointment'."
    )


# ============================================================
# FIND APPLICANT FIELD
# ============================================================

def set_applicants(page):

    print(
        f"[info] Setting applicants = {APPLICANTS}"
    )

    # Inputs
    inputs = page.locator(
        "input:visible"
    )

    for i in range(
        inputs.count()
    ):

        element = inputs.nth(i)

        try:

            typ = lower(
                element.get_attribute(
                    "type"
                )
            )

            if typ not in {
                "",
                "text",
                "number",
            }:
                continue

            name = lower(
                element.get_attribute(
                    "name"
                )
            )

            element_id = lower(
                element.get_attribute(
                    "id"
                )
            )

            placeholder = lower(
                element.get_attribute(
                    "placeholder"
                )
            )

            value = normalize(
                element.input_value()
            )

            combined = " ".join([
                name,
                element_id,
                placeholder,
                value,
            ])

            if (
                "applicant" in combined
                or value in {
                    "",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                }
            ):

                element.fill(
                    APPLICANTS
                )

                return

        except Exception:
            continue

    # Select fallback
    selects = page.locator(
        "select:visible"
    )

    for i in range(
        selects.count()
    ):

        select = selects.nth(i)

        try:

            options = [
                normalize(x)
                for x in select.locator(
                    "option"
                ).all_inner_texts()
            ]

            if APPLICANTS in options:

                select.select_option(
                    label=APPLICANTS
                )

                return

        except Exception:
            continue

    print(
        "[warn] Applicant field not found; "
        "site default will be used."
    )


# ============================================================
# DISCOVER APPLICATION CATEGORY
# ============================================================

def find_category_controls(page):

    print(
        "[info] Searching for "
        "Application Category..."
    )

    # First try normal <select>
    selects = page.locator(
        "select:visible"
    )

    for i in range(
        selects.count()
    ):

        select = selects.nth(i)

        try:

            options = [
                normalize(x)
                for x in select.locator(
                    "option"
                ).all_inner_texts()
            ]

            joined = " | ".join(
                options
            ).lower()

            # Application categories visible
            # in the screenshot/user page
            indicators = [
                "passport",
                "mvv",
                "legalisation",
                "certificate of life",
                "identity card",
                "copy conform original",
                "signature",
            ]

            score = sum(
                x in joined
                for x in indicators
            )

            if score >= 1:

                print(
                    "[info] Application Category "
                    "SELECT found."
                )

                return {
                    "type": "select",
                    "locator": select,
                }

        except Exception:
            continue

    # --------------------------------------------------------
    # ASP.NET / custom dropdown fallback
    # --------------------------------------------------------

    candidates = page.locator(
        "input:visible, "
        "button:visible, "
        "[role='combobox']:visible, "
        "[role='listbox']:visible"
    )

    for i in range(
        candidates.count()
    ):

        element = candidates.nth(i)

        try:

            text = lower(
                element.inner_text()
                or element.get_attribute(
                    "aria-label"
                )
                or element.get_attribute(
                    "title"
                )
                or element.get_attribute(
                    "name"
                )
                or element.get_attribute(
                    "id"
                )
            )

            if (
                "application category"
                in text
                or "category"
                in text
            ):

                print(
                    "[info] Custom "
                    "Application Category "
                    "control found."
                )

                return {
                    "type": "custom",
                    "locator": element,
                }

        except Exception:
            continue

    # Save complete page
    save_debug(
        page,
        "ERROR_application_category_not_found"
    )

    raise RuntimeError(
        "Application Category control "
        "was not found after "
        "Make an appointment."
    )


# ============================================================
# GET CATEGORIES
# ============================================================

def get_categories(page):

    control = find_category_controls(
        page
    )

    if control["type"] != "select":

        raise RuntimeError(
            "The Application Category is "
            "a custom control. "
            "A screenshot/HTML was saved "
            "for selector adjustment."
        )

    select = control["locator"]

    options = select.locator(
        "option"
    )

    categories = []

    for i in range(
        options.count()
    ):

        option = options.nth(i)

        try:

            text = normalize(
                option.inner_text()
            )

            value = (
                option.get_attribute(
                    "value"
                )
                or ""
            )

            disabled = (
                option.is_disabled()
            )

            if disabled:
                continue

            if not text:
                continue

            if lower(text) in {
                "select",
                "-select-",
                "--select--",
                "- select -",
            }:
                continue

            categories.append({
                "text": text,
                "value": value,
            })

        except Exception:
            continue

    # De-duplicate
    result = []
    seen = set()

    for category in categories:

        key = (
            category["text"],
            category["value"]
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(
            category
        )

    print(
        f"[info] Found "
        f"{len(result)} Application Categories."
    )

    for i, category in enumerate(
        result,
        1
    ):
        print(
            f"[category {i}] "
            f"{category['text']}"
        )

    if not result:

        raise RuntimeError(
            "Application Category exists "
            "but contains no usable options."
        )

    return result


# ============================================================
# CONTINUE
# ============================================================

def click_continue(page):

    print(
        "[info] Looking for Continue..."
    )

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

        page.get_by_text(
            "Continue",
            exact=True
        ),
    ]

    for locator in locators:

        try:

            count = locator.count()

            for i in range(count):

                element = locator.nth(i)

                if not element.is_visible():
                    continue

                element.click(
                    timeout=15000
                )

                wait_page(page)

                return

        except Exception:
            continue

    # Input fallback
    elements = page.locator(
        "input:visible, "
        "button:visible, "
        "a:visible"
    )

    for i in range(
        elements.count()
    ):

        element = elements.nth(i)

        try:

            text = lower(
                element.inner_text()
                or element.get_attribute(
                    "value"
                )
                or element.get_attribute(
                    "aria-label"
                )
            )

            if text == "continue":

                element.click(
                    timeout=15000
                )

                wait_page(page)

                return

        except Exception:
            continue

    save_debug(
        page,
        "ERROR_continue_not_found"
    )

    raise RuntimeError(
        "Continue button was not found."
    )


# ============================================================
# CHECK APPOINTMENT RESULT
# ============================================================

def check_result(page):

    text = lower(
        get_body_text(page)
    )

    # Explicit no slots
    for pattern in NO_SLOT_TEXT:

        if pattern in text:

            return (
                "unavailable",
                pattern
            )

    # Appointment information page
    if (
        "appschedulinggetinfo"
        in page.url.lower()
    ):

        return (
            "available",
            "appointment information page"
        )

    # Positive indicators
    for pattern in AVAILABLE_TEXT:

        if pattern in text:

            return (
                "available",
                pattern
            )

    return (
        "unknown",
        "No known appointment state detected"
    )


# ============================================================
# CHECK ONE CATEGORY
# ============================================================

def check_category(
    context,
    category,
    index,
    total
):

    page = context.new_page()

    page.set_default_timeout(
        PAGE_TIMEOUT
    )

    try:

        print()
        print(
            "=" * 60
        )

        print(
            f"[{index}/{total}] "
            f"{category['text']}"
        )

        print(
            "=" * 60
        )

        # Fresh page
        page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        wait_page(page)

        # IMPORTANT:
        # The original code failed because it searched
        # for Application Category on the welcome page.
        click_make_appointment(
            page
        )

        set_applicants(
            page
        )

        control = find_category_controls(
            page
        )

        if control["type"] != "select":

            save_debug(
                page,
                "CUSTOM_CATEGORY",
                category["text"]
            )

            raise RuntimeError(
                "Category control is custom."
            )

        select = control["locator"]

        # Select by VALUE whenever possible
        if category["value"]:

            select.select_option(
                value=category["value"]
            )

        else:

            select.select_option(
                label=category["text"]
            )

        print(
            f"[info] Selected: "
            f"{category['text']}"
        )

        click_continue(
            page
        )

        result, reason = check_result(
            page
        )

        if result == "unavailable":

            print(
                f"[OK] NO SLOT: "
                f"{category['text']}"
            )

        elif result == "available":

            print(
                f"[!!!] POSSIBLE SLOT: "
                f"{category['text']}"
            )

            print(
                f"[!!!] Reason: {reason}"
            )

            save_debug(
                page,
                "AVAILABLE",
                category["text"]
            )

        else:

            print(
                f"[UNKNOWN] "
                f"{category['text']}"
            )

            print(
                f"[UNKNOWN] {reason}"
            )

            save_debug(
                page,
                "UNKNOWN",
                category["text"]
            )

        return {
            "status": result,
            "reason": reason,
            "url": page.url,
            "checked_at": timestamp(),
        }

    except Exception as e:

        print(
            f"[ERROR] "
            f"{category['text']}: {e}"
        )

        save_debug(
            page,
            "ERROR",
            category["text"]
        )

        return {
            "status": "error",
            "reason": str(e),
            "url": page.url,
            "checked_at": timestamp(),
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
    categories
):

    gmail_user = os.getenv(
        "GMAIL_USER",
        ""
    ).strip()

    gmail_password = os.getenv(
        "GMAIL_APP_PASSWORD",
        ""
    ).strip()

    notify_email = os.getenv(
        "NOTIFY_EMAIL",
        ""
    ).strip()

    if not all([
        gmail_user,
        gmail_password,
        notify_email,
    ]):

        print(
            "[warn] Email secrets are missing."
        )

        return

    message = EmailMessage()

    message["From"] = gmail_user
    message["To"] = notify_email

    message["Subject"] = (
        "🇳🇱 Netherlands VFS "
        "Appointment Available"
    )

    body = [
        "Netherlands VFS appointment "
        "monitor detected possible availability.",
        "",
    ]

    for item in categories:

        body.extend([
            f"Category: "
            f"{item['category']}",

            f"Reason: "
            f"{item['reason']}",

            f"URL: "
            f"{item['url']}",

            "",
        ])

    body.append(
        "The monitor did NOT book an appointment."
    )

    message.set_content(
        "\n".join(body)
    )

    with smtplib.SMTP(
        "smtp.gmail.com",
        587,
        timeout=30
    ) as smtp:

        smtp.starttls()

        smtp.login(
            gmail_user,
            gmail_password
        )

        smtp.send_message(
            message
        )

    print(
        "[ALERT] Email sent."
    )


# ============================================================
# RUN ONE COMPLETE CYCLE
# ============================================================

def run_once():

    if not URL:

        raise RuntimeError(
            "NL_APPOINTMENT_URL is not set."
        )

    state = load_state()

    state.setdefault(
        "categories",
        {}
    )

    available = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        context = browser.new_context(

            viewport={
                "width": 1440,
                "height": 1000,
            },

            locale="en-US",

            timezone_id="Africa/Cairo",
        )

        # ----------------------------------------------------
        # FIRST PAGE:
        # Discover categories after clicking Make appointment
        # ----------------------------------------------------

        page = context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT
        )

        try:

            open_start_page(
                page
            )

            click_make_appointment(
                page
            )

            set_applicants(
                page
            )

            categories = get_categories(
                page
            )

        finally:

            try:
                page.close()
            except Exception:
                pass

        # Save categories
        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        (
            DEBUG_DIR
            / "categories.json"
        ).write_text(

            json.dumps(
                categories,
                ensure_ascii=False,
                indent=2
            ),

            encoding="utf-8"
        )

        # ----------------------------------------------------
        # Check every category
        # ----------------------------------------------------

        total = len(
            categories
        )

        for index, category in enumerate(
            categories,
            1
        ):

            result = check_category(
                context,
                category,
                index,
                total
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
                previous.get(
                    "status"
                )
            )

            state["categories"][key] = {
                **category,
                **result,
            }

            # Alert only when changing into
            # available state.
            if (
                result["status"]
                == "available"
                and previous_status
                != "available"
            ):

                available.append({
                    "category":
                        category["text"],

                    "reason":
                        result["reason"],

                    "url":
                        result["url"],
                })

            save_state(
                state
            )

            time.sleep(
                CATEGORY_DELAY
            )

        browser.close()

    state["last_run"] = timestamp()

    save_state(
        state
    )

    # --------------------------------------------------------
    # Email
    # --------------------------------------------------------

    if available:

        send_email(
            available
        )

    print()
    print(
        "=" * 60
    )

    print(
        "[DONE] Netherlands monitoring cycle completed."
    )

    print(
        f"[DONE] Categories checked: {len(categories)}"
    )

    print(
        f"[DONE] Possible available: {len(available)}"
    )

    print(
        "=" * 60
    )


# ============================================================
# MAIN
# ============================================================

def main():

    try:

        if "--once" in sys.argv:

            run_once()

            return 0

        while True:

            started = time.time()

            try:

                run_once()

            except Exception as e:

                print(
                    f"[FATAL] {type(e).__name__}: {e}"
                )

                return 1

            elapsed = (
                time.time()
                - started
            )

            wait = max(
                30,
                300 - int(elapsed)
            )

            print(
                f"[INFO] Next check "
                f"in {wait} seconds."
            )

            time.sleep(
                wait
            )

    except KeyboardInterrupt:

        print(
            "[INFO] Stopped."
        )

        return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )
