#!/usr/bin/env python3
"""Watch a webpage for changes, or wait for a missing (404) page to appear.

Examples:
    ./watch.py https://example.com/page
    ./watch.py https://example.com/coming-soon --interval 30 --exit-on-change
    ./watch.py https://example.com --ignore 'Updated \\d+ minutes ago' --command 'say changed'

Pushover: if PUSHOVER_TOKEN and PUSHOVER_USER are set (in the environment or in
pushover.env next to this script), every change message is also pushed.
To set up: cp pushover.env.example pushover.env, then fill in your keys.

Uses only the Python standard library.
"""

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MISSING_STATUSES = {404, 410}
PUSHOVER_API = "https://api.pushover.net/1"
PUSHOVER_ENV_FILE = Path(__file__).resolve().parent / "pushover.env"


class TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style and similar non-content tags."""

    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "header", "footer", "table", "ul", "ol", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


class TitleExtractor(HTMLParser):
    """Grab the text of the first <title> element."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.done:
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title" and self.in_title:
            self.in_title = False
            self.done = True

    def handle_data(self, data):
        if self.in_title:
            self.parts.append(data)

    def title(self):
        return " ".join("".join(self.parts).split())


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url, timeout):
    """Return (status, body_text). Status is None on network errors."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        log(f"Network error: {reason}")
        return None, ""


def normalize(body, title, raw, ignore_patterns):
    # Text mode skips <head>, so add the title back in to catch title-only changes.
    content = body if raw else "\n".join(filter(None, [title, extract_text(body)]))
    for pattern in ignore_patterns:
        content = pattern.sub("", content)
    return content


def extract_text(html):
    parser = TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def extract_title(html):
    parser = TitleExtractor()
    parser.feed(html)
    parser.close()
    return parser.title()


def fmt_title(title):
    return f'"{title}"' if title else "(no title)"


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def make_diff(old, new):
    return list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=1))


def show_diff(diff, max_lines):
    for line in diff[:max_lines]:
        print("    " + line)
    if len(diff) > max_lines:
        print(f"    ... ({len(diff) - max_lines} more diff lines)")


def diff_summary(diff):
    """Just the added/removed lines, for push notifications."""
    return "\n".join(line for line in diff
                     if line[:1] in "+-" and not line.startswith(("+++", "---")))


def notify(title, message, args, details=""):
    print("\a", end="", flush=True)  # terminal bell
    if not args.no_notify and sys.platform == "darwin":
        script = f"display notification {applescript_str(message)} with title {applescript_str(title)}"
        subprocess.run(["osascript", "-e", script], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.pushover:
        body = f"{message}\n\n{details}" if details else message
        send_pushover(args.pushover, title, body, args.url, args.pushover_priority)
    if args.command:
        subprocess.run(args.command, shell=True, check=False)


def truncate(s, limit):
    return s if len(s) <= limit else s[:limit - 1] + "…"


def load_pushover_credentials():
    """Return (token, user) from the environment, falling back to pushover.env."""
    values = {}
    if PUSHOVER_ENV_FILE.exists():
        for line in PUSHOVER_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    token = os.environ.get("PUSHOVER_TOKEN") or values.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER") or values.get("PUSHOVER_USER")
    return (token, user) if token and user else None


def pushover_post(endpoint, fields, timeout=15):
    """POST to the Pushover API. Returns (http_status, json) or (None, None) on network error."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"{PUSHOVER_API}/{endpoint}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        log(f"Pushover network error: {getattr(e, 'reason', e)}")
        return None, None


def validate_pushover(creds):
    """Check credentials without sending a message. Returns False only if Pushover rejects them."""
    token, user = creds
    status, resp = pushover_post("users/validate.json", {"token": token, "user": user})
    if status is None:
        log("Pushover: couldn't reach the API to validate credentials; will try anyway")
        return True
    if resp.get("status") == 1:
        devices = ", ".join(resp.get("devices", [])) or "no named devices"
        log(f"Pushover: enabled ({devices})")
        return True
    log(f"Pushover: credentials rejected: {'; '.join(resp.get('errors', [])) or f'HTTP {status}'}")
    return False


def send_pushover(creds, title, message, url, priority, attempts=3):
    token, user = creds
    fields = {
        "token": token,
        "user": user,
        "title": truncate(title, 250),
        "message": truncate(message, 1024),
        "priority": priority,
    }
    if len(url) <= 512:
        fields["url"] = url
        fields["url_title"] = "Open page"
    for attempt in range(1, attempts + 1):
        status, resp = pushover_post("messages.json", fields)
        if status == 200 and resp.get("status") == 1:
            log("Pushover: sent")
            return True
        if status is not None and 400 <= status < 500:
            # Bad input, bad credentials, or monthly quota hit; retrying won't help.
            log(f"Pushover: rejected (HTTP {status}): {'; '.join(resp.get('errors', []))}")
            return False
        if attempt < attempts:
            time.sleep(5 * attempt)  # Pushover asks for at least 5s between retries
    log("Pushover: giving up after repeated failures")
    return False


def applescript_str(s):
    # AppleScript string literal: escape backslashes and quotes.
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_args():
    p = argparse.ArgumentParser(
        description="Watch a webpage for changes, or wait for a 404 page to appear.")
    p.add_argument("url")
    p.add_argument("-i", "--interval", type=float, default=60,
                   help="seconds between checks (default: 60)")
    p.add_argument("--raw", action="store_true",
                   help="compare raw HTML instead of visible text "
                        "(noisier: catches nonces, tokens, script changes)")
    p.add_argument("--ignore", action="append", default=[], metavar="REGEX",
                   help="regex to strip before comparing (repeatable), "
                        "e.g. timestamps or visitor counters")
    p.add_argument("--exit-on-change", action="store_true",
                   help="exit after the first change or appearance is detected")
    p.add_argument("--command", metavar="CMD",
                   help="shell command to run when a change is detected")
    p.add_argument("--no-notify", action="store_true",
                   help="disable macOS desktop notifications")
    p.add_argument("--no-pushover", action="store_true",
                   help="disable Pushover even if credentials are configured")
    p.add_argument("--pushover-priority", type=int, default=0, choices=[-2, -1, 0, 1],
                   help="Pushover priority: -2 silent, -1 quiet, 0 normal, "
                        "1 high/bypass quiet hours (default: 0)")
    p.add_argument("--test-push", action="store_true",
                   help="send a test Pushover message and exit")
    p.add_argument("--timeout", type=float, default=30,
                   help="request timeout in seconds (default: 30)")
    p.add_argument("--diff-lines", type=int, default=40,
                   help="max diff lines to print on change (default: 40)")
    return p.parse_args()


def main():
    args = parse_args()
    ignore = [re.compile(pattern) for pattern in args.ignore]

    args.pushover = None if args.no_pushover else load_pushover_credentials()
    if args.test_push:
        if not args.pushover:
            log(f"Pushover: no credentials (set PUSHOVER_TOKEN/PUSHOVER_USER or {PUSHOVER_ENV_FILE})")
            return 1
        ok = send_pushover(args.pushover, "Website watcher test",
                           f"Test message for {args.url}", args.url, args.pushover_priority)
        return 0 if ok else 1
    if args.pushover and not validate_pushover(args.pushover):
        return 1
    if not args.pushover and not args.no_pushover:
        log("Pushover: not configured")

    log(f"Watching {args.url} every {args.interval:g}s "
        f"({'raw HTML' if args.raw else 'visible text'})")
    if args.command:
        log(f"On change will run: {shlex.quote(args.command)}")

    last_state = None     # "present", "missing", or None (unknown)
    last_content = None
    last_hash = None
    last_title = None
    checks = 0

    while True:
        status, body = fetch(args.url, args.timeout)
        checks += 1
        event = None
        details = ""

        if status is None or status >= 500 or status == 429:
            # Transient problem; don't treat as a change, just retry.
            if status is not None:
                log(f"HTTP {status} (server error/rate limited), retrying")
        elif status in MISSING_STATUSES:
            if last_state is None:
                log(f"HTTP {status}: page not there yet, waiting for it to appear...")
            elif last_state == "present":
                event = ("Page disappeared", f"{args.url} now returns {status}")
            last_state = "missing"
        elif 200 <= status < 300:
            title = extract_title(body)
            content = normalize(body, title, args.raw, ignore)
            h = digest(content)
            if last_state is None:
                log(f"HTTP {status}: page is up: {fmt_title(title)} "
                    f"({len(content)} chars). Watching for changes...")
            elif last_state == "missing":
                event = ("Page appeared!",
                         f"{args.url} is now live (HTTP {status}): {fmt_title(title)}")
            elif h != last_hash:
                event = ("Page changed", f"{args.url}: {fmt_title(title)}")
                log("Change detected:")
                if title != last_title:
                    log(f"Title changed: {fmt_title(last_title)} -> {fmt_title(title)}")
                diff = make_diff(last_content, content)
                show_diff(diff, args.diff_lines)
                details = diff_summary(diff)
            last_state, last_content, last_hash = "present", content, h
            last_title = title
        else:
            log(f"HTTP {status}: unexpected status, ignoring")

        if event:
            title, message = event
            log(f"*** {title}: {message}")
            notify(title, message, args, details)
            if args.exit_on_change and title != "Page disappeared":
                return 0
        elif checks % 10 == 0:
            log(f"Still watching ({checks} checks, no change)")

        # Small jitter so we're not perfectly periodic.
        time.sleep(args.interval * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        log("Stopped.")
