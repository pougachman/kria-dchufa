# Kria Dchufa (קריאה דחופה, "Urgent Read")

Literature curation and critical appraisal for Israeli emergency medicine residency programs.
Owner: Yoav (EM residency program director, Shamir Medical Center). He is not a software
developer: explain changes in plain Hebrew, and keep him out of git and terminal details.

Read `docs/SPEC.md` (product spec) and `docs/BUILD_PLAN.md` (milestones) before changing anything.
The live, authoritative versions are in the Claude doc linked from those files.

## Non-negotiable rules

1. Every article enters through an API with a PMID or DOI. The language model never invents a reference.
2. All statistics (ARR, NNT, fragility index, LR, post-test probability) are computed in
   `kria/appraisal/calc.py`, never by the model. Add a unit test for every new formula.
3. Nothing reaches residents without human approval (national editor or program curator).
4. Every model output is stored with the model name and prompt version.
5. UI text is Hebrew, right-to-left. Generated content (summaries, appraisals, questions) is English.
6. Full text only through authorized routes: open access (PMC, Europe PMC, Unpaywall) or
   publisher TDM APIs with the library's agreement. Never scrape library portals or use
   anyone's library credentials. Never store or share article PDFs between programs.
7. Secrets only in environment variables. Never commit keys.

## Stack

Python 3.12, FastAPI, Jinja templates (Tailwind from M3), SQLAlchemy 2 + Alembic, Postgres
on Railway, Claude API (Haiku for screening via Message Batches, Sonnet/Opus for appraisal).

## Commands

```bash
pip install -r requirements-dev.txt
pytest                 # all tests
ruff check .           # lint
alembic upgrade head   # apply migrations (uses DATABASE_URL)
uvicorn kria.web.main:app --reload
```

## Layout

- `kria/web/` FastAPI app and RTL templates
- `kria/pipeline/` monthly ingest, screen, rank, digest
- `kria/appraisal/` SGEM-style appraisal engine; `calc.py` holds the statistics
- `kria/questions/` MCQ and discussion questions
- `kria/db/` models; `migrations/` Alembic
- `data/` taxonomy (33 topics) and sources
- `prompts/` versioned prompts; `eval/` gold standard and SGEM benchmark

## Milestone status

- [x] M0 infrastructure (app skeleton, health check, migrations, CI, Dockerfile, calc module)
- [ ] M1 PubMed ingestion
- [ ] M2 screening and scoring
- [ ] M3 approval screen and digest
- [ ] M4 appraisal engine
- [ ] M5 programs and roles
- [ ] M6 reading lists and questions
- [ ] M7 shared library
