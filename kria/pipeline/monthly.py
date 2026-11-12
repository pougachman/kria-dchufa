"""The monthly pipeline job, safe to run every day.

    python -m kria.pipeline.monthly            # the previous calendar month
    python -m kria.pipeline.monthly 2026-10    # a given month
    python -m kria.pipeline.monthly 2026-10 --wait

Each run does the steps that are missing for the month and exits:
1. no successful ingest run -> ingest the month from PubMed (M1), then go on to step 3;
2. a screening batch is running -> collect it if it has ended, otherwise wait for tomorrow;
3. articles still unscreened (new, or failed with attempts left) -> submit a screening batch;
4. otherwise -> nothing to do.

On Railway this runs as a cron service once a day (see README). A month is ingested once: its
record set never changes (records created in the month), and re-ingesting is done by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from sqlalchemy.orm import Session

from kria.config import settings
from kria.pipeline import ingest, screen


def previous_month(today: dt.date) -> str:
    first = today.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime("%Y-%m")


def step(session: Session, month: str, pubmed, batches, wait: bool = False) -> str:
    """Do the missing steps for the month and say what happened."""
    done = ""
    if screen.latest_ingest_run(session, month) is None:
        run = ingest.ingest_month(session, pubmed(), month)
        done = f"ingested {month}: {run.report['kept']} of {run.report['total_unique']} kept; "
    return done + _screening_step(session, month, batches, wait)


def _screening_step(session: Session, month: str, batches, wait: bool) -> str:
    pending = screen.pending_run(session, month)
    if pending is not None:
        if wait:
            screen.wait_and_collect(session, batches(), pending)
        elif not screen.collect(session, batches(), pending):
            return f"screening batch {pending.batch_id} for {month} is still running"
        return (
            f"collected screening run {pending.id} for {month}: {pending.succeeded} ok, "
            f"{pending.failed} failed, ${pending.cost_usd:.4f}"
        )
    run = screen.submit_month(session, batches(), month)
    if run is None:
        return f"{month} is fully screened; nothing to do"
    if wait:
        screen.wait_and_collect(session, batches(), run)
        return (
            f"screened {month} (run {run.id}): {run.succeeded} ok, {run.failed} failed, "
            f"${run.cost_usd:.4f}"
        )
    return f"submitted {run.requested} articles from {month} as batch {run.batch_id}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the next step of the monthly pipeline.")
    parser.add_argument("month", nargs="?", help="default: the previous calendar month")
    parser.add_argument("--wait", action="store_true", help="wait for the batch to finish")
    args = parser.parse_args(argv)
    month = args.month or previous_month(dt.datetime.now(dt.UTC).date())
    ingest.parse_month(month)

    from kria.db.base import SessionLocal
    from kria.pipeline.batches import ClaudeBatches
    from kria.pipeline.eutils import EUtils

    def pubmed():
        return EUtils(
            api_key=settings.ncbi_api_key, email=settings.ncbi_email, tool=settings.ncbi_tool
        )

    def batches():
        return ClaudeBatches(api_key=settings.anthropic_api_key)

    with SessionLocal() as session:
        print(step(session, month, pubmed, batches, wait=args.wait))
    return 0


if __name__ == "__main__":
    sys.exit(main())
