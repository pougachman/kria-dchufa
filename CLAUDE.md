# Kria Dchufa (קריאה דחופה, "Urgent Read")

Literature curation and critical appraisal for Israeli emergency medicine residency programs.
Owner: Yoav (EM residency program director, Shamir Medical Center). He is not a software
developer: explain changes in plain Hebrew, and keep him out of git and terminal details.

Read `docs/SPEC.md` (product spec) and `docs/BUILD_PLAN.md` (milestones, open decisions) before
changing anything. They are kept in sync with Yoav's live Hebrew doc (linked there), which a
Claude Code session may not be able to open; the repo files are enough to work from. When a
product decision is missing, ask Yoav instead of guessing, and record the answer in `docs/`.

## Non-negotiable rules

1. Every article enters through an API with a PMID or DOI. The language model never invents a reference.
2. All statistics (ARR, NNT, fragility index, LR, post-test probability) are computed in
   `kria/appraisal/calc.py`, never by the model. Add a unit test for every new formula.
3. Nothing reaches residents without human approval (national editor or program curator).
4. Every model output is stored with the model name and prompt version.
5. UI text is Hebrew, right-to-left. Generated content (summaries, appraisals, questions) is English.
6. Full text only through authorized routes: open access (PMC, Europe PMC, Unpaywall) first,
   otherwise a PDF that the requesting editor or curator downloaded with their own access and
   uploaded. Publisher TDM APIs only if a library agrees later (Shamir's will not). Never scrape
   library portals, never automate downloads, never use anyone's library credentials.
   An uploaded PDF is visible only to its uploader, is deleted when the appraisal is approved
   (30 days at most), and is never shared between programs. The library keeps the appraisal
   and the link only.
7. Secrets only in environment variables. Never commit keys.

## Stack

Python 3.12, FastAPI, Jinja templates (Tailwind from M3, compiled to static/editor.css), SQLAlchemy 2 + Alembic, Postgres
on Railway, Claude API (Haiku for screening via Message Batches, Sonnet/Opus for appraisal).

## Commands

```bash
pip install -r requirements-dev.txt
pytest                 # all tests
ruff check .           # lint
alembic upgrade head   # apply migrations (uses DATABASE_URL)
uvicorn kria.web.main:app --reload
python -m kria.pipeline.ingest 2026-10   # ingest one month from PubMed, print count report
python -m kria.pipeline.journals         # re-map journal names after editing data/*.yaml
python -m kria.pipeline.screen run 2026-10 --limit 20  # screen with Claude (paid, needs the key)
python -m kria.pipeline.screen report 2026-10          # counts, cost, time, ranked shortlist
python -m kria.pipeline.monthly          # daily job: next missing step for the previous month
npx tailwindcss@3.4.16 -c tailwind.config.js -i kria/web/static/editor.src.css -o kria/web/static/editor.css --minify
```

Tests never call the network; live checks are run by hand. A local Postgres 16 is available at
/usr/lib/postgresql/16/bin (run it as the postgres user) for checking migrations.

## Layout

- `kria/web/` FastAPI app and RTL templates
- `kria/pipeline/` monthly ingest, screen (Claude Batches), rank, monthly job, digest
- `kria/appraisal/` SGEM-style appraisal engine; `calc.py` holds the statistics
- `kria/questions/` MCQ and discussion questions
- `kria/db/` models; `migrations/` Alembic
- `data/` taxonomy (33 topics), sources, `journals.yaml` (generated NLM mapping, with
  hand-checked `overrides`), `filters.yaml` (versioned code filter), `pricing.yaml` (API prices)
- `prompts/` versioned prompts; `eval/` gold standard and SGEM benchmark

## Milestone status

- [x] M0 infrastructure: deployed on Railway at https://kria-dchufa-production.up.railway.app
      (home page verified 2026-11-12). Yoav can open /healthz to confirm database_kind is
      postgresql; if it says sqlite, DATABASE_URL in Railway needs fixing
- [x] M1 PubMed ingestion: October 2026 run twice on local Postgres and once on Railway
      (2026-11-12), identical each time: 3,451 records, 2,398 kept
- [x] M2 screening and scoring (2026-11-12): Haiku 5.5 via Message Batches, prompt
      `screening.v1`. First real run on Railway, October 2026: 2,398 of 2,398 screened,
      949 relevant, 291 scored 4-5, 14.3 minutes, 0.30 USD. Daily job `monthly-job` on Railway
      (cron 05:00 UTC); progress and failures on /status. Next: sensitivity check (gate 1).
      Prompt `screening.v2` (adds ed_relevance) is the default from November 2026; a month
      keeps the prompt version it was first screened with (`screen.month_version`)
- [x] M3 approval screen and digest (2026-11-12): /editor with password, ranked shortlist with
      one-click approve/reject and ★ top articles, editor minutes, digest email through Resend
      (design v2). October 2026 digest approved and emailed to Yoav: 21 approved, 21 editor
      minutes without reading the articles (Yoav's estimate for a real month: about 45).
      Resend sends only to Yoav until a domain is verified (deferred; Dr. Zaki got a forward)
- [x] M4 appraisal engine (2026-11-14): /editor/appraisals, open access (PMC via NCBI, Europe
      PMC, Unpaywall) or uploaded PDF, Opus 5.5 extract/report/Guest Skeptic/reconcile (prompts
      v2/v3: one page, about 600 words, at most 1 USD; one appraisal a month), stats in calc.py,
      quote and number checks, approve -> PDF deleted and public /a/<token>, digest link.
      Benchmark (reduced, Yoav's decision): CLOVERS, PREVENT, SALT-ED, CRASH-3, HALT-IT run on
      Railway and reviewed by Yoav against SGEM: "good"
- [ ] M5 programs and roles
- [ ] M6 reading lists and questions
- [ ] M7 shared library
