# קריאה דחופה (Kria Dchufa)

כלי לאיסוף וניתוח ספרות לתוכניות התמחות ברפואה דחופה בישראל: סריקה חודשית לפי נושאים,
אישור של עורך אנושי, רשימות קריאה עם שאלות למתמחים, וניתוח ביקורתי בסגנון SGEM.

- אפיון: [docs/SPEC.md](docs/SPEC.md)
- תוכנית בנייה: [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md)
- הנחיות לעבודה עם Claude Code: [CLAUDE.md](CLAUDE.md)

## Development

```bash
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn kria.web.main:app --reload
pytest
python -m kria.pipeline.ingest 2026-10   # one month from PubMed
python -m kria.pipeline.screen run 2026-10 --limit 20   # screen a few with Claude (paid)
python -m kria.pipeline.monthly          # next missing step for the previous month
```

## Deploy (Railway)

The Dockerfile runs migrations and starts the server on `$PORT`. Attach a Postgres service so
Railway sets `DATABASE_URL`, and add `ANTHROPIC_API_KEY`, `NCBI_API_KEY` and `NCBI_EMAIL` as
service variables. Health check: `/healthz`. Pipeline progress (Hebrew, counts and costs only):
`/status`.

Monthly pipeline: add a second service from the same repository, with
- Custom Start Command: `sh -c "alembic upgrade head && python -m kria.pipeline.monthly"`
- Cron Schedule: `0 5 * * *` (every day at 05:00 UTC)
- the same variables (`DATABASE_URL` from the Postgres service, `ANTHROPIC_API_KEY`,
  `NCBI_API_KEY`, `NCBI_EMAIL`).

Every run does the next missing step for the previous month and exits: ingest from PubMed,
submit the screening batch, collect it the next day (batches finish within 24 hours), retry
failed articles, then nothing until the next month. It is safe to run any number of times.
