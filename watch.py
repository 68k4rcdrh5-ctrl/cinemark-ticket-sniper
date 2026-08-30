#!/usr/bin/env python3
"""Watch Cinemark Seven Bridges for seat openings."""

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
TARGET = _cfg["target"]
FILTERS = _cfg["filters"]
PACING = _cfg.get("pacing", {})

THEATER = TARGET["theater"]
MOVIE_ID = str(TARGET["movie_id"])
MOVIE_NAME = TARGET.get("movie_name", f"movie {MOVIE_ID}")
TZ = ZoneInfo(TARGET.get("timezone", "UTC"))

EXCLUDED_ROWS = set(FILTERS.get("excluded_rows", []))
EARLIEST = FILTERS.get("earliest_showtime", "00:00")
LATEST = FILTERS.get("latest_showtime", "23:59")
PARTY_SIZE = int(FILTERS.get("party_size", 1))

# Seat-map requests are the expensive/rate-limited requests.
REQUEST_GAP = float(PACING.get("request_gap_seconds", 8))

# Polling interval.
POLL_MINUTES = float(PACING.get("poll_minutes", 10))

BASE = "https://www.cinemark.com"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DATE_VALUE = re.compile(
    r'data-datevalue="(\d{4}-\d{2}-\d{2})"'
)

SHOWTIME_LINK = re.compile(
    r'/TicketSeatMap/\?TheaterId=(\d+)'
    r'&(?:amp;)?ShowtimeId=(\d+)'
    r'&(?:amp;)?CinemarkMovieId='
    + re.escape(MOVIE_ID)
    + r'&(?:amp;)?Showtime=([\d\-T:]+)'
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
        f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}",
        flush=True,
    )


def fetch(url: str, gap: float | None = None) -> str:
    """Fetch a Cinemark page with a short retry strategy."""

    if gap is None:
        gap = REQUEST_GAP

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
        },
    )

    # Only one retry after a short delay.
    # This prevents a blocked request from consuming
    # 20+ minutes of the GitHub Actions run.
    for attempt in range(2):

        if attempt:
            wait = 30
            log(
                f"rate-limited/blocked, backing off "
                f"{wait}s (retry {attempt})"
            )
            time.sleep(wait)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()

                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)

            if gap > 0:
                time.sleep(
                    gap + gap / 2 * random.random()
                )

            return body.decode(
                "utf-8",
                errors="replace",
            )

        except urllib.error.HTTPError as e:

            if e.code not in (
                429,
                403,
                500,
                502,
                503,
            ):
                raise

        except (
            urllib.error.URLError,
            TimeoutError,
        ):
            pass

    raise RuntimeError(
        f"gave up fetching {url} after 1 retry"
    )


def notify(title: str, message: str) -> None:
    log(f"ALERT: {title}: {message}")

    with ALERT_LOG.open("a") as f:
        f.write(
            f"{datetime.now().isoformat()}  "
            f"{title}: {message}\n"
        )

    hook = HERE / "notify-hook"

    if hook.exists() and os.access(
        hook,
        os.X_OK,
    ):
        try:
            subprocess.run(
                [
                    str(hook),
                    title,
                    message,
                ],
                capture_output=True,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            log(
                f"WARN: notify-hook failed: {e!r}"
            )


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(
            STATE_FILE.read_text()
        )

    return {
        "dates": {},
        "seats": {},
        "cycle": 0,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=1,
            sort_keys=True,
        )
    )


def showtimes_for(
    date: str,
) -> tuple[str | None, dict[str, str]]:
    """Get Seven Bridges theater ID and showtimes for one date."""

    url = (
        f"{BASE}/theatres/"
        f"{THEATER}?showDate={date}"
    )

    html = fetch(
        url,
        gap=1.0,
    )

    links = SHOWTIME_LINK.findall(html)

    theater_ids = {
        theater_id
        for theater_id, _sid, _iso in links
    }

    if theater_ids and theater_ids != {"276"}:
        log(
            f"WARN: unexpected theater IDs "
            f"for {date}: {sorted(theater_ids)}"
        )

    theater_id = (
        "276"
        if "276" in theater_ids
        else (
            next(iter(theater_ids))
            if theater_ids
            else None
        )
    )

    shows = {
        sid: iso
        for _tid, sid, iso in links
    }

    return theater_id, shows


def dates_for_next_two_weeks() -> list[str]:
    """Get Cinemark dates currently available for the next 14 days."""

    html = fetch(
        f"{BASE}/theatres/{THEATER}",
        gap=1.0,
    )

    dates = sorted(
        set(
            DATE_VALUE.findall(html)
        )
    )

    today = datetime.now(TZ).date()
    cutoff = today + timedelta(days=13)

    return [
        d
        for d in dates
        if today.isoformat()
        <= d
        <= cutoff.isoformat()
    ]


def showtimes_diagnostic() -> None:
    """Print fresh Showtime IDs without modifying state.json."""

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
    print(
        f"Dates: {dates[0]} through {dates[-1]}"
    )
    print()

    all_ids = 0
    theater_ids = set()

    for date in dates:

        try:
            theater_id, shows = showtimes_for(
                date
            )

        except Exception as e:  # noqa: BLE001
            print(
                f"{date} ERROR: {e!r}"
            )
            continue

        if theater_id:
            theater_ids.add(
                theater_id
            )

        print(date)

        if not shows:
            print(
                "  No showtimes found."
            )
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
    print(
        f"TOTAL SHOWTIME IDS: {all_ids}"
    )
    print(
        "THEATER IDS FOUND: "
        + (
            ", ".join(
                sorted(theater_ids)
            )
            if theater_ids
            else "none"
        )
    )
    print("=" * 72)


def qualifying(iso: str) -> bool:
    return (
        EARLIEST
        <= iso[11:16]
        <= LATEST
    )


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

    html = fetch(
        url,
        gap=REQUEST_GAP,
    )

    if "seatBlock" not in html:
        log(
            f"WARN: seat map {showtime_id} "
            f"returned no seat markup "
            f"(page changed?)"
        )
        return []

    return [
        Seat(
            row,
            int(num),
            int(col),
        )
        for row, num, col in
        AVAILABLE_SEAT.findall(html)
        if row not in EXCLUDED_ROWS
    ]


def seat_blocks(
    seats: list[Seat],
) -> list[list[Seat]]:
    """Group physically adjacent seats by row."""

    blocks = []

    rows = sorted(
        {
            s.row
            for s in seats
        }
    )

    for row in rows:

        run: list[Seat] = []

        row_seats = sorted(
            (
                s
                for s in seats
                if s.row == row
            ),
            key=lambda s: s.col,
        )

        for s in row_seats:

            if (
                run
                and s.col
                != run[-1].col + 1
            ):
                blocks.append(run)
                run = []

            run.append(s)

        if run:
            blocks.append(run)

    return blocks


def fmt_block(
    block: list[Seat],
) -> str:

    if len(block) == 1:
        return block[0].label

    numbers = sorted(
        s.number
        for s in block
    )

    return (
        f"{block[0].row}"
        f"{numbers[0]}-"
        f"{block[0].row}"
        f"{numbers[-1]}"
    )


def fmt_time(
    iso: str,
) -> str:
    return datetime.fromisoformat(
        iso
    ).strftime("%-I:%M%p").lower()


def prune_past(
    state: dict,
) -> None:

    today = (
        datetime.now(TZ)
        .date()
        .isoformat()
    )

    for d in [
        d
        for d in state["dates"]
        if d < today
    ]:

        for sid in state[
            "dates"
        ][d]["showtimes"]:
            state[
                "seats"
            ].pop(
                sid,
                None,
            )

        del state[
            "dates"
        ][d]


def sweep(
    state: dict,
    only_dates: list[str] | None,
) -> None:

    prune_past(state)

    # ---------------------------------------------------------
    # NO SHOWTIME DISCOVERY HERE.
    #
    # The watcher uses the Showtime IDs already stored in
    # state.json. This avoids repeatedly requesting Cinemark's
    # theater pages and reduces the chance of being blocked.
    #
    # New Showtime IDs must be added manually if Cinemark
    # changes/adds them.
    # ---------------------------------------------------------

    today = datetime.now(TZ).date()

    cutoff_date = (
        today
        + timedelta(days=13)
    )

    today_str = today.isoformat()
    cutoff_str = cutoff_date.isoformat()

    watch = [
        (
            date,
            sid,
            iso,
        )
        for date, info in sorted(
            state["dates"].items()
        )
        if today_str
        <= date
        <= cutoff_str
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
        log(
            "ERROR: no theater_id available; "
            "skipping seat scan"
        )
        return

    if (
        state["theater_id"]
        != "276"
    ):
        log(
            "ERROR: refusing seat scan "
            f"because TheaterId is "
            f"{state['theater_id']} "
            f"instead of 276"
        )
        return

    log(
        f"seat scan: checking "
        f"{len(watch)} qualifying showtimes "
        f"at TheaterId=276"
    )

    total = 0

    for i, (
        date,
        sid,
        iso,
    ) in enumerate(watch):

        try:
            seats = available_seats(
                "276",
                sid,
                iso,
            )

        except Exception as e:
            log(
                f"WARN: seat check "
                f"{date} "
                f"{fmt_time(iso)} "
                f"failed: {e!r}"
            )
            continue

        total += len(seats)

        # Record the current seat map.
        current = {
            s.label
            for s in seats
        }

        state[
            "seats"
        ][sid] = sorted(
            current
        )

        # Alert on ANY currently available qualifying block.
        #
        # We intentionally do not compare against the previous
        # scan because seats can appear and disappear between
        # scans. A currently available block is actionable.
        openings = [
            block
            for block in seat_blocks(
                seats
            )
            if len(block) >= PARTY_SIZE
        ]

        if openings:
            notify(
                f"Seats available "
                f"{date} "
                f"{fmt_time(iso)}",
                f"{MOVIE_NAME}: "
                + ", ".join(
                    fmt_block(b)
                    for b in openings
                ),
            )

        if i % 10 == 9:
            save_state(state)

    log(
        f"seat scan: "
        f"{len(watch)} showtimes checked, "
        f"{total} qualifying seats"
    )


def report(
    state: dict,
) -> None:

    print(
        f"\n{MOVIE_NAME} @ {THEATER}"
    )

    print(
        f"theater_id: "
        f"{state.get('theater_id', 'unknown')}"
    )

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
        f"on sale: "
        f"{min(tracked)} to "
        f"{max(tracked)} "
        f"({len(tracked)} dates)\n"
    )

    empty = True

    for d, info in tracked.items():

        for sid, iso in sorted(
            info["showtimes"].items(),
            key=lambda kv: kv[1],
        ):

            seats = state[
                "seats"
            ].get(
                sid,
                [],
            )

            if (
                qualifying(iso)
                and seats
            ):
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
            "the watcher alerts when one is available"
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
        help=(
            "restrict a sweep to "
            "specific YYYY-MM-DD dates"
        ),
    )

    ap.add_argument(
        "--report",
        action="store_true",
        help=(
            "print availability from "
            "state.json and exit"
        ),
    )

    ap.add_argument(
        "--showtimes",
        action="store_true",
        help=(
            "fetch and print fresh "
            "Showtime IDs for the "
            "next 14 days"
        ),
    )

    args = ap.parse_args()

    if args.report:
        report(
            load_state()
        )
        return

    if args.showtimes:
        showtimes_diagnostic()
        return

    while True:

        state = load_state()

        try:
            sweep(
                state,
                only_dates=args.dates,
            )

        except Exception as e:
            log(
                f"ERROR during sweep: "
                f"{e!r}"
            )

        state["cycle"] = (
            state.get("cycle", 0)
            + 1
        )

        save_state(state)

        if args.once:
            return

        time.sleep(
            POLL_MINUTES * 60
        )


if __name__ == "__main__":
    main()