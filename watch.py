#!/usr/bin/env python3
"""Watch Cinemark Seven Bridges for seat openings.

Showtime IDs are intentionally NOT discovered dynamically.

The only source of Showtime IDs is state.json.

GitHub Actions is responsible for scheduling runs.
Each run performs ONE bounded-by-state sweep and exits.

Every qualifying showtime currently stored in state.json
is checked on every run.
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
ALERT_LOG = HERE / "alerts.log"


_cfg = tomllib.loads(
    (HERE / "config.toml").read_text()
)

TARGET = _cfg["target"]
FILTERS = _cfg["filters"]
PACING = _cfg.get("pacing", {})


THEATER = TARGET["theater"]
MOVIE_ID = str(TARGET["movie_id"])
MOVIE_NAME = TARGET.get(
    "movie_name",
    f"movie {MOVIE_ID}",
)

TZ = ZoneInfo(
    TARGET.get(
        "timezone",
        "UTC",
    )
)


EXCLUDED_ROWS = set(
    FILTERS.get(
        "excluded_rows",
        [],
    )
)

EARLIEST = FILTERS.get(
    "earliest_showtime",
    "00:00",
)

LATEST = FILTERS.get(
    "latest_showtime",
    "23:59",
)

PARTY_SIZE = int(
    FILTERS.get(
        "party_size",
        1,
    )
)


# ---------------------------------------------------------
# REQUEST PACING
# ---------------------------------------------------------
#
# This is the delay between successful seat-map requests.
#
# GitHub Actions controls HOW OFTEN the workflow runs.
# watch.py does NOT contain a polling loop.
#
REQUEST_GAP = float(
    PACING.get(
        "request_gap_seconds",
        8,
    )
)


BASE = "https://www.cinemark.com"


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


AVAILABLE_SEAT = (
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
        f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] "
        f"{msg}",
        flush=True,
    )


def fetch(
    url: str,
    gap: float | None = None,
) -> str:
    """Fetch a page with one short retry.

    There is deliberately no long exponential/backoff loop.
    If Cinemark blocks the request, retry once after 30 seconds,
    then abandon that particular seat-map request.
    """

    if gap is None:
        gap = REQUEST_GAP

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": (
                "text/html,"
                "application/xhtml+xml"
            ),
            "Accept-Encoding": "gzip",
        },
    )

    for attempt in range(2):

        if attempt:
            wait = 30

            log(
                f"rate-limited/blocked, "
                f"backing off {wait}s "
                f"(retry {attempt})"
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
                    body = gzip.decompress(
                        body
                    )

            if gap > 0:
                time.sleep(
                    gap
                    + gap
                    / 2
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

            log(
                f"WARN: HTTP {e.code} "
                f"for {url}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as e:

            log(
                f"WARN: request failed "
                f"for {url}: {e!r}"
            )

    raise RuntimeError(
        f"gave up fetching {url} "
        f"after 1 retry"
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
            f"{datetime.now(TZ).isoformat()}  "
            f"{title}: {message}\n"
        )

    hook = HERE / "notify-hook"

    if (
        hook.exists()
        and os.access(
            hook,
            os.X_OK,
        )
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
                check=False,
            )

        except Exception as e:  # noqa: BLE001

            log(
                f"WARN: notify-hook failed: "
                f"{e!r}"
            )


def load_state() -> dict:

    if STATE_FILE.exists():

        return json.loads(
            STATE_FILE.read_text()
        )

    return {
        "cycle": 0,
        "dates": {},
        "seats": {},
        "theater_id": "276",
    }


def save_state(
    state: dict,
) -> None:

    # scan_cursor is no longer used.
    # Remove it if an old state file still contains it.
    state.pop(
        "scan_cursor",
        None,
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )


def qualifying(
    iso: str,
) -> bool:
    """Return True only if the showtime is eligible to scan.

    Applies:
      - configured daily time window
      - current-time cutoff for today's showtimes

    A showtime from an earlier time today is not scanned.
    Future dates are eligible normally.
    """

    showtime_date = iso[:10]
    showtime_time = iso[11:16]

    if not (
        EARLIEST
        <= showtime_time
        <= LATEST
    ):
        return False

    today = datetime.now(TZ).date().isoformat()

    if showtime_date < today:
        return False

    if showtime_date == today:
        now_time = datetime.now(TZ).strftime(
            "%H:%M"
        )

        if showtime_time < now_time:
            return False

    return True


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
            f"WARN: seat map "
            f"{showtime_id} "
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
        for row, num, col
        in re.findall(
            AVAILABLE_SEAT,
            html,
        )
        if row not in EXCLUDED_ROWS
    ]


def seat_blocks(
    seats: list[Seat],
) -> list[list[Seat]]:
    """Group physically adjacent available seats by row."""

    blocks: list[list[Seat]] = []

    for row in sorted(
        {
            s.row
            for s in seats
        }
    ):

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


def qualifying_blocks(
    seats: list[Seat],
) -> list[list[Seat]]:
    """Return only seat blocks large enough for the party."""

    return [
        block
        for block in seat_blocks(seats)
        if len(block) >= PARTY_SIZE
    ]


def fmt_block(
    block: list[Seat],
) -> str:

    if not block:
        return ""

    if len(block) == 1:
        return block[0].label

    numbers = sorted(
        s.number
        for s in block
    )

    if len(numbers) == 2:
        return (
            f"{block[0].row}"
            f"{numbers[0]}-"
            f"{block[0].row}"
            f"{numbers[-1]}"
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
    ).strftime(
        "%-I:%M%p"
    ).lower()


def prune_past(
    state: dict,
) -> None:

    today = (
        datetime.now(TZ)
        .date()
        .isoformat()
    )

    for date in [
        d
        for d in state["dates"]
        if d < today
    ]:

        for sid in state[
            "dates"
        ][date]["showtimes"]:

            state[
                "seats"
            ].pop(
                sid,
                None,
            )

        del state[
            "dates"
        ][date]


def build_watch_list(
    state: dict,
    only_dates: list[str] | None,
) -> list[
    tuple[str, str, str]
]:

    watch = [
        (
            date,
            sid,
            iso,
        )
        for date, info
        in sorted(
            state["dates"].items()
        )

        for sid, iso
        in sorted(
            info[
                "showtimes"
            ].items(),
            key=lambda kv: kv[1],
        )

        if qualifying(iso)

        and (
            not only_dates
            or date in only_dates
        )
    ]

    return watch


def sweep(
    state: dict,
    only_dates: list[str] | None,
) -> None:

    prune_past(state)

    theater_id = str(
        state.get(
            "theater_id",
            "276",
        )
    )

    if theater_id != "276":

        log(
            f"ERROR: refusing seat scan "
            f"because TheaterId is "
            f"{theater_id} instead of 276"
        )

        return

    watch = build_watch_list(
        state,
        only_dates,
    )

    if not watch:

        log(
            "seat scan: no qualifying "
            "showtimes in state.json"
        )

        return

    # ---------------------------------------------------------
    # FULL SWEEP
    # ---------------------------------------------------------
    #
    # Every qualifying Showtime ID in state.json is checked
    # on every scheduled run.
    #
    # There is intentionally:
    #
    #   - no discovery
    #   - no rotating shard
    #   - no scan cursor
    #   - no MAX_SEATMAPS_PER_RUN
    #
    selected = watch

    log(
        f"seat scan: checking "
        f"{len(selected)} of "
        f"{len(watch)} qualifying "
        f"showtimes at TheaterId=276"
    )

    qualifying_block_count = 0
    successful = 0
    failed = 0

    for date, sid, iso in selected:

        try:

            seats = available_seats(
                theater_id,
                sid,
                iso,
            )

            successful += 1

        except Exception as e:

            failed += 1

            log(
                f"WARN: seat check "
                f"{date} "
                f"{fmt_time(iso)} "
                f"ShowtimeId={sid} "
                f"failed: {e!r}"
            )

            continue

        current = sorted(
            {
                s.label
                for s in seats
            }
        )

        state[
            "seats"
        ][sid] = current

        openings = qualifying_blocks(
            seats
        )

        qualifying_block_count += len(
            openings
        )

        if openings:

            notify(
                (
                    f"Seats available "
                    f"{date} "
                    f"{fmt_time(iso)}"
                ),
                (
                    f"{MOVIE_NAME}: "
                    + ", ".join(
                        fmt_block(block)
                        for block in openings
                    )
                ),
            )

        # Save after every successful showtime.
        # This prevents a long sweep from losing all
        # collected availability if the runner is interrupted.
        save_state(state)

    log(
        f"seat scan: "
        f"{successful} showtimes checked, "
        f"{failed} failed, "
        f"{qualifying_block_count} qualifying "
        f"seat blocks"
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
        f"party of {PARTY_SIZE}"
    )

    tracked = {
        d: v
        for d, v
        in sorted(
            state["dates"].items()
        )
        if v["showtimes"]
    }

    if not tracked:

        print(
            "no dates tracked yet"
        )

        return

    print(
        f"on sale: "
        f"{min(tracked)} to "
        f"{max(tracked)} "
        f"({len(tracked)} dates)\n"
    )

    found = False

    for date, info in tracked.items():

        for sid, iso in sorted(
            info[
                "showtimes"
            ].items(),
            key=lambda kv: kv[1],
        ):

            seats = state[
                "seats"
            ].get(
                sid,
                [],
            )

            if not (
                qualifying(iso)
                and seats
            ):
                continue

            seat_objects = [
                Seat(
                    row=label.rstrip(
                        "0123456789"
                    ),
                    number=int(
                        re.search(
                            r"\d+$",
                            label,
                        ).group()
                    ),
                    col=0,
                )
                for label in seats
            ]

            # The saved state only contains labels, so report
            # the raw available seats here. Live sweep alerts
            # are based on true physical adjacency.
            found = True

            print(
                f"  {date} "
                f"{fmt_time(iso):>8} "
                f"{len(seats):>3} seats: "
                f"{', '.join(seats[:14])}"
                f"{'...' if len(seats) > 14 else ''}"
            )

    if not found:

        print(
            "no qualifying seats "
            "currently recorded"
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

    state[
        "cycle"
    ] = int(
        state.get(
            "cycle",
            0,
        )
    ) + 1

    save_state(state)


if __name__ == "__main__":
    main()