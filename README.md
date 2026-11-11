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
```

## Deploy (Railway)

The Dockerfile runs migrations and starts the server on `$PORT`. Attach a Postgres service so
Railway sets `DATABASE_URL`, and add `ANTHROPIC_API_KEY`, `NCBI_API_KEY` and `NCBI_EMAIL` as
service variables. Health check: `/healthz`.
