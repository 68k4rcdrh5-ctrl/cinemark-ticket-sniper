#!/usr/bin/env python3
"""Watch Cinemark Seven Bridges for seat openings using saved Showtime IDs."""

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

# Delay between seat-map requests.
REQUEST_GAP = float(
    PACING.get("request_gap_seconds", 8)
)

# Only relevant if watch.py is run without --once.
POLL_MINUTES = float(
    PACING.get("poll_minutes", 10)
)

BASE = "https://www.cinemark.com"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

BACKOFF_SCHEDULE = [120, 300, 900]

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


def fetch(url: str) -> str:
    """Fetch a Cinemark seat-map page with retry/backoff protection."""

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
        },
    )

    retry_after = 0

    for attempt, backoff in enumerate(
        [0, *BACKOFF_SCHEDULE]
    ):
        if backoff:
            wait = max(
                backoff,
                retry_after,
            )

            log(
                f"rate-limited/blocked, backing off "
                f"{wait}s (attempt {attempt})"
            )

            time.sleep(wait)

        try:
            with urllib.request.urlopen(
                req,
                timeout=30,
            ) as resp:

                body = resp.read()

                if (
                    resp.headers.get(
                        "Content-Encoding"
                    )
                    == "gzip"
                ):
                    body = gzip.decompress(body)

            if REQUEST_GAP > 0:
                time.sleep(
                    REQUEST_GAP
                    + REQUEST_GAP / 2
                    * random.random()
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

            try:
                retry_after = min(
                    int(
                        e.headers.get(
                            "Retry-After",
                            "0",
                        )
                    ),
                    1800,
                )
            except ValueError:
                retry_after = 0

        except (
            urllib.error.URLError,
            TimeoutError,
        ):
            pass

    raise RuntimeError(
        f"gave up fetching {url} after "
        f"{len(BACKOFF_SCHEDULE)} backoffs"
    )


def notify(
    title: str,
    message: str,
) -> None:

    log(
        f"ALERT: {title}: {message}"
    )

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


def save_state(
    state: dict,
) -> None:

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=1,
            sort_keys=True,
        )
    )


def qualifying(
    iso: str,
) -> bool:

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

    html = fetch(url)

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

    today = datetime.now(TZ).date()

    cutoff_date = (
        today
        + timedelta(days=13)
    )

    today_str = today.isoformat()
    cutoff_str = cutoff_date.isoformat()

    # ---------------------------------------------------------
    # IMPORTANT:
    #
    # There is NO Showtime-ID discovery here.
    #
    # The watcher only uses Showtime IDs already stored
    # in state.json.
    # ---------------------------------------------------------

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

    if state["theater_id"] != "276":
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

        current = {
            s.label
            for s in seats
        }

        state[
            "seats"
        ][sid] = sorted(
            current
        )

        # Alert on ANY currently available
        # qualifying block.
        #
        # We intentionally do not compare against
        # the previous scan.

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
            "no dates/showtimes stored: "
            "state.json needs Showtime IDs"
        )
        return

    print(
        f"stored showtimes: "
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

    args = ap.parse_args()

    if args.report:

        report(
            load_state()
        )

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