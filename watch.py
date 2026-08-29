#!/usr/bin/env python3
"""Watch a Cinemark showing for seat openings and newly added dates.

Everything Cinemark serves is plain server-rendered HTML, so a sweep is:
fetch the theater page, fetch each date's page for showtime IDs for the
configured movie, fetch each showtime's seat map, and diff availability
against the previous sweep.

Usage:
  python3 watch.py --once
  python3 watch.py
  python3 watch.py --report
  python3 watch.py --showtimes
  python3 watch.py --dates 2026-08-29
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
ALERT_LOG = HERE / "alerts.log"

_cfg = tomllib.loads((HERE / "config.toml").read_text())
TARGET, FILTERS, PACING = _cfg["target"], _cfg["filters"], _cfg.get("pacing", {})

THEATER = TARGET["theater"]
MOVIE_ID = str(TARGET["movie_id"])
MOVIE_NAME = TARGET.get("movie_name", f"movie {MOVIE_ID}")
TZ = ZoneInfo(TARGET.get("timezone", "UTC"))

EXCLUDED_ROWS = set(FILTERS.get("excluded_rows", []))
EARLIEST = FILTERS.get("earliest_showtime", "00:00")
LATEST = FILTERS.get("latest_showtime", "23:59")
PARTY_SIZE = int(FILTERS.get("party_size", 1))

REQUEST_GAP = float(PACING.get("request_gap_seconds", 8))
DATE_SCAN_EVERY = int(PACING.get("date_scan_every", 3))
POLL_MINUTES = float(PACING.get("poll_minutes", 5))

BASE = "https://www.cinemark.com"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

BACKOFF_SCHEDULE = [120, 300, 900]

DATE_VALUE = re.compile(
    r'data-datevalue="(\d{4}-\d{2}-\d{2})"'
)

SHOWTIME_LINK = re.compile(
    r'/TicketSeatMap/\?TheaterId=(\d+)'
    r'&(?:amp;)?ShowtimeId=(\d+)'
    r'&(?:amp;)?CinemarkMovieId=' + re.escape(MOVIE_ID) +
    r'&(?:amp;)?Showtime=([\d\-T:]+)'
)

AVAILABLE_SEAT = re.compile(
    r'<button[^>]*class="seatAvailable seatBlock"[^>]*'
    r'info="([A-Z]+),(\d+),\d+,(\d+),'
)


@dataclass
class Seat:
    row: str
    number: int
    col: int

    @property
    def label(self) -> str:
        return f"{self.row}{self.number}"


def log(msg: str) -> None:
    print(
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}",
        flush=True,
    )


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
        },
    )

    retry_after = 0

    for attempt, backoff in enumerate([0, *BACKOFF_SCHEDULE]):
        if backoff:
            wait = max(backoff, retry_after)
            log(
                f"rate-limited/blocked, backing off "
                f"{wait}s (attempt {attempt})"
            )
            time.sleep(wait)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()

                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)

            time.sleep(
                REQUEST_GAP
                + REQUEST_GAP / 2 * random.random()
            )

            return body.decode("utf-8", errors="replace")

        except urllib.error.HTTPError as e:
            if e.code not in (429, 403, 500, 502, 503):
                raise

            try:
                retry_after = min(
                    int(e.headers.get("Retry-After", "0")),
                    1800,
                )
            except ValueError:
                retry_after = 0

        except (urllib.error.URLError, TimeoutError):
            pass

    raise RuntimeError(
        f"gave up fetching {url} after "
        f"{len(BACKOFF_SCHEDULE)} backoffs"
    )


def notify(title: str, message: str) -> None:
    log(f"ALERT: {title}: {message}")

    with ALERT_LOG.open("a") as f:
        f.write(
            f"{datetime.now().isoformat()}  "
            f"{title}: {message}\n"
        )

    hook = HERE / "notify-hook"

    if hook.exists() and os.access(hook, os.X_OK):
        try:
            subprocess.run(
                [str(hook), title, message],
                capture_output=True,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            log(f"WARN: notify-hook failed: {e!r}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())

    return {
        "dates": {},
        "seats": {},
        "cycle": 0,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=1, sort_keys=True)
    )


def showtimes_for(
    date: str,
) -> tuple[str | None, dict[str, str]]:
    """Return theater ID and Showtime IDs for the configured movie/date."""

    url = f"{BASE}/theatres/{THEATER}?showDate={date}"
    html = fetch(url)

    links = SHOWTIME_LINK.findall(html)

    theater_id = links[0][0] if links else None

    shows = {
        sid: iso
        for _tid, sid, iso in links
    }

    return theater_id, shows


def dates_for_next_two_weeks() -> list[str]:
    """Get Cinemark's currently available dates, limited to 14 days."""

    html = fetch(f"{BASE}/theatres/{THEATER}")

    dates = sorted(set(DATE_VALUE.findall(html)))

    today = datetime.now(TZ).date()
    cutoff = today + timedelta(days=13)

    return [
        d for d in dates
        if today.isoformat() <= d <= cutoff.isoformat()
    ]


def showtimes_diagnostic() -> None:
    """Fetch fresh Seven Bridges showtime IDs without using state.json."""

    log(f"THEATER SLUG: {THEATER}")
    log(f"MOVIE ID: {MOVIE_ID}")
    log(f"MOVIE: {MOVIE_NAME}")
    log("Fetching fresh dates from Cinemark...")

    dates = dates_for_next_two_weeks()

    if not dates:
        print("No dates found.")
        return

    print()
    print("=" * 72)
    print("FRESH CINEMARK SHOWTIME IDS")
    print("=" * 72)
    print(f"Theater: {THEATER}")
    print(f"Movie ID: {MOVIE_ID}")
    print(f"Dates: {dates[0]} through {dates[-1]}")
    print()

    all_ids = 0
    theater_ids = set()

    for date in dates:
        try:
            theater_id, shows = showtimes_for(date)
        except Exception as e:  # noqa: BLE001
            print(f"{date} ERROR: {e!r}")
            continue

        if theater_id:
            theater_ids.add(theater_id)

        print(f"{date}")

        if not shows:
            print("  No showtimes found.")
            print()
            continue

        for sid, iso in sorted(
            shows.items(),
            key=lambda kv: kv[1],
        ):
            print(
                f"  {fmt_time(iso):>8}  "
                f"TheaterId={theater_id}  "
                f"ShowtimeId={sid}"
            )
            all_ids += 1

        print()

    print("=" * 72)
    print(f"TOTAL SHOWTIME IDS: {all_ids}")
    print(
        "THEATER IDS FOUND: "
        + (", ".join(sorted(theater_ids)) if theater_ids else "none")
    )
    print("=" * 72)


def qualifying(iso: str) -> bool:
    return EARLIEST <= iso[11:16] <= LATEST


def available_seats(
    theater_id: str,
    showtime_id: str,
    iso: str,
) -> list[Seat]:

    url = (
        f"{BASE}/TicketSeatMap/"
        f"?TheaterId={theater_id}"
        f"&ShowtimeId={showtime_id}"
        f"&CinemarkMovieId={MOVIE_ID}"
        f"&Showtime={iso}"
    )

    html = fetch(url)

    if "seatBlock" not in html:
        log(
            f"WARN: seat map {showtime_id} returned "
            f"no seat markup (page changed?)"
        )
        return []

    return [
        Seat(row, int(num), int(col))
        for row, num, col in AVAILABLE_SEAT.findall(html)
        if row not in EXCLUDED_ROWS
    ]


def seat_blocks(
    seats: list[Seat],
) -> list[list[Seat]]:
    """Group seats into physically adjacent runs."""

    blocks = []

    for row in sorted({s.row for s in seats}):
        run: list[Seat] = []

        for s in sorted(
            (s for s in seats if s.row == row),
            key=lambda s: s.col,
        ):
            if run and s.col != run[-1].col + 1:
                blocks.append(run)
                run = []

            run.append(s)

        if run:
            blocks.append(run)

    return blocks


def fmt_block(block: list[Seat]) -> str:
    if len(block) == 1:
        return block[0].label

    numbers = sorted(s.number for s in block)

    return (
        f"{block[0].row}"
        f"{numbers[0]}-"
        f"{block[0].row}"
        f"{numbers[-1]}"
    )


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M%p").lower()


def prune_past(state: dict) -> None:
    today = datetime.now(TZ).date().isoformat()

    for d in [
        d for d in state["dates"]
        if d < today
    ]:
        for sid in state["dates"][d]["showtimes"]:
            state["seats"].pop(sid, None)

        del state["dates"][d]


def sweep(
    state: dict,
    scan_dates: bool,
    only_dates: list[str] | None,
) -> None:

    first_run = not state["dates"]

    prune_past(state)

    if (
        scan_dates
        or first_run
        or only_dates
        or "theater_id" not in state
    ):

        strip = (
            only_dates
            or dates_for_next_two_weeks()
        )

        for date in sorted(set(strip)):

            if state["dates"].get(date, {}).get("showtimes"):
                continue

            try:
                theater_id, shows = showtimes_for(date)

            except Exception as e:  # noqa: BLE001
                log(
                    f"WARN: date probe {date} failed: {e!r}"
                )
                continue

            if theater_id:
                state["theater_id"] = theater_id

            state["dates"][date] = {
                "showtimes": shows
            }

            if shows and not first_run:
                notify(
                    f"New date on sale: {date}",
                    f"{MOVIE_NAME} added for {date}: "
                    + ", ".join(
                        sorted(
                            fmt_time(i)
                            for i in shows.values()
                        )
                    ),
                )

        log(
            "date scan: tracking "
            f"{sum(1 for d in state['dates'].values() if d['showtimes'])} "
            "dates"
        )

        save_state(state)

    today = datetime.now(TZ).date()
    cutoff_date = today + timedelta(days=13)

    watch = [
        (date, sid, iso)
        for date, info in sorted(
            state["dates"].items()
        )
        if today.isoformat()
        <= date
        <= cutoff_date.isoformat()
        for sid, iso in sorted(
            info["showtimes"].items(),
            key=lambda kv: kv[1],
        )
        if qualifying(iso)
        and (
            not only_dates
            or date in only_dates
        )
    ]

    if "theater_id" not in state:
        log("ERROR: no theater_id available; skipping seat scan")
        return

    total = 0

    for i, (date, sid, iso) in enumerate(watch):

        try:
            seats = available_seats(
                state["theater_id"],
                sid,
                iso,
            )

        except Exception as e:  # noqa: BLE001
            log(
                f"WARN: seat check {date} "
                f"{fmt_time(iso)} failed: {e!r}"
            )
            continue

        total += len(seats)

        prev = set(
            state["seats"].get(sid, [])
        )

        fresh = {
            s.label for s in seats
        } - prev

        state["seats"][sid] = sorted(
            s.label for s in seats
        )

        openings = [
            b
            for b in seat_blocks(seats)
            if len(b) >= PARTY_SIZE
            and any(
                s.label in fresh
                for s in b
            )
        ]

        if openings and not first_run:
            notify(
                f"Seats open {date} {fmt_time(iso)}",
                f"{MOVIE_NAME}: "
                + ", ".join(
                    fmt_block(b)
                    for b in openings
                ),
            )

        if i % 10 == 9:
            save_state(state)

    log(
        f"seat scan: {len(watch)} showtimes checked, "
        f"{total} qualifying seats"
    )

    if first_run:
        log(
            "first run: baseline recorded, "
            "no alerts fired"
        )


def report(state: dict) -> None:
    print(f"\n{MOVIE_NAME} @ {THEATER}")

    print(
        f"filters: rows "
        f"{''.join(sorted(EXCLUDED_ROWS)) or 'none'} "
        f"excluded, "
        f"shows {EARLIEST}-{LATEST}, "
        f"party of {PARTY_SIZE}\n"
    )

    tracked = {
        d: v
        for d, v in sorted(
            state["dates"].items()
        )
        if v["showtimes"]
    }

    if not tracked:
        print(
            "no dates tracked yet: "
            "run a sweep first"
        )
        return

    print(
        f"on sale: {min(tracked)} to "
        f"{max(tracked)} "
        f"({len(tracked)} dates)\n"
    )

    empty = True

    for d, info in tracked.items():

        for sid, iso in sorted(
            info["showtimes"].items(),
            key=lambda kv: kv[1],
        ):

            seats = state["seats"].get(
                sid,
                [],
            )

            if qualifying(iso) and seats:
                empty = False

                print(
                    f"  {d} "
                    f"{fmt_time(iso):>8} "
                    f"{len(seats):>3} seats: "
                    f"{', '.join(seats[:14])}"
                    f"{'...' if len(seats) > 14 else ''}"
                )

    if empty:
        print(
            "no qualifying seats right now: "
            "the watcher alerts when one opens"
        )


def main() -> None:
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--once",
        action="store_true",
        help="single sweep, then exit",
    )

    ap.add_argument(
        "--dates",
        nargs="*",
        help="restrict a sweep to specific YYYY-MM-DD dates",
    )

    ap.add_argument(
        "--report",
        action="store_true",
        help="print availability from state.json and exit",
    )

    ap.add_argument(
        "--showtimes",
        action="store_true",
        help="fetch and print fresh Showtime IDs for the next 14 days",
    )

    args = ap.parse_args()

    if args.report:
        report(load_state())
        return

    if args.showtimes:
        showtimes_diagnostic()
        return

    while True:

        state = load_state()

        cycle = state.get("cycle", 0)

        try:
            sweep(
                state,
                scan_dates=(
                    cycle % DATE_SCAN_EVERY == 0
                ),
                only_dates=args.dates,
            )

        except Exception as e:  # noqa: BLE001
            log(
                f"ERROR during sweep: {e!r}"
            )

        state["cycle"] = cycle + 1

        save_state(state)

        if args.once:
            return

        time.sleep(
            POLL_MINUTES * 60
        )


if __name__ == "__main__":
    main()