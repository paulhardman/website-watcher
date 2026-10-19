# website-watcher

Watch a webpage for changes, or wait for a page that currently returns 404 to appear.
It alerts you in the terminal, with macOS notifications, through [Pushover](https://pushover.net) on your phone, or by running any shell command you choose.

It's a single Python script that uses only the standard library, so there's nothing to install.

## Usage

Requires Python 3.8 or newer.

```bash
# Watch a page for changes, checking every 60 seconds (the default)
./watch.py https://example.com/page

# Wait for a page that's currently a 404, check every 30s, stop once it's live
./watch.py https://example.com/coming-soon -i 30 --exit-on-change

# Ignore parts of the page that always change, and run a command on change
./watch.py https://example.com --ignore 'Updated \d+ minutes ago' --command 'say "page changed"'
```

Example output:

```
[2026-10-19 12:28:18] Pushover: enabled (iphone)
[2026-10-19 12:28:18] Watching https://example.com/tickets every 60s (visible text)
[2026-10-19 12:28:18] HTTP 404: page not there yet, waiting for it to appear...
[2026-10-19 12:31:21] *** Page appeared!: https://example.com/tickets is now live (HTTP 200): "Tickets"
[2026-10-19 12:31:21] Pushover: sent
[2026-10-19 12:40:23] Change detected:
    --- before
    +++ after
    @@ -1,2 +1,2 @@
     Tickets
    -Sold out
    +Available now
[2026-10-19 12:40:23] *** Page changed: https://example.com/tickets: "Tickets"
```

Press Ctrl-C to stop.

## How it works

- **The page returns 404 or 410:** the script keeps checking and alerts you when the page appears. If a page that was up starts returning 404, it alerts you that the page has disappeared.
- **The page is up:** by default the script compares only the page's visible text and title, so changing scripts, security tokens and markup don't trigger false alarms. Use `--raw` to compare the full HTML instead. On each change it prints a diff and the page title.
- **Network errors, 5xx responses and 429 (rate limited)** are logged and retried. They never count as a change.
- Each wait between checks varies by ±10%, so requests don't arrive at exactly the same moment each time.

## Pushover notifications

To get every alert pushed to your phone:

1. Create an application at <https://pushover.net/apps/build> to get an app token. Your user key is on the Pushover dashboard.
2. Copy the template and fill in your keys:

   ```bash
   cp pushover.env.example pushover.env
   chmod 600 pushover.env
   ```

3. Send a test notification:

   ```bash
   ./watch.py https://example.com --test-push
   ```

You can set the `PUSHOVER_TOKEN` and `PUSHOVER_USER` environment variables instead; they override `pushover.env`. The script checks the credentials once at startup without sending a notification, and stops if Pushover rejects them.

Pushes include the page title and, for changes, the lines that were added and removed. Tapping a notification opens the page. `pushover.env` is git-ignored, so your keys stay out of version control.

## Options

| Option | Description |
|---|---|
| `-i`, `--interval SECONDS` | Seconds between checks (default: 60) |
| `--raw` | Compare raw HTML instead of visible text |
| `--ignore REGEX` | Regex to strip before comparing, e.g. timestamps or visitor counters. Can be repeated. |
| `--exit-on-change` | Exit after the first change or appearance is detected |
| `--command CMD` | Shell command to run when a change is detected |
| `--no-notify` | Turn off macOS desktop notifications |
| `--no-pushover` | Turn off Pushover even if credentials are configured |
| `--pushover-priority N` | `-2` silent, `-1` quiet, `0` normal, `1` high priority, which gets through quiet hours (default: 0) |
| `--test-push` | Send a test Pushover message and exit |
| `--timeout SECONDS` | Request timeout (default: 30) |
| `--diff-lines N` | Maximum number of diff lines to print on change (default: 40) |

## Limitations

The script fetches the HTML directly and doesn't run JavaScript. For pages that build their content in the browser, watch the underlying data or API URL instead, or use a headless browser such as Playwright.

Be considerate with `--interval`. Checking a site every few seconds is rarely necessary and may get you rate limited or blocked.
