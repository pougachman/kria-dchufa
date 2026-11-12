# Build plan

Live version (Hebrew): https://claude.ai/artifact/RdJj7cDDeMd3STX1Y9E45Z (tab "תוכנית בנייה").

Each milestone is a vertical slice that ends in something working end to end.

Status (2026-11-12): M0 deployed on Railway (https://kria-dchufa-production.up.railway.app).
M1 and M2 done: the daily job (`monthly-job`, a Railway cron service at 05:00 UTC) ingested
October 2026 on Railway (3,451 records, 2,398 kept, identical to the local runs) and screened
all 2,398 with Haiku 5.5 in one batch: 949 relevant, 291 scored 4-5, 14.3 minutes, 0.30 USD.
M3 code done (2026-11-12): editor screen at /editor, digest email through Resend. Done when
October's digest is approved and emailed from Railway. Then: the M2 sensitivity check (needs
the gold standard).
M4 does not depend on the pipeline and can be built in parallel with M1-M3.

| Milestone | What is built | Done when | Depends on |
| --- | --- | --- | --- |
| M0 | Infrastructure | Empty app runs on Railway, tests run in CI, keys only in env vars | none |
| M1 | PubMed ingestion | Re-running the same month returns the same set; per-topic count report | M0 |
| M2 | Screening and scoring | Every article has relevance, topics, design, three scored dimensions with reasons; cost and time logged | M1 |
| Check | Screening sensitivity | Sensitivity vs 200 hand-labeled articles meets the agreed threshold (gate 1) | M2 |
| M3 | Approval screen and digest | One month's digest approved and emailed; editor time measured | M2 |
| M4 | Appraisal engine, open-access lookup, PDF upload | 10 benchmark articles appraised from open access or an uploaded PDF (also from a phone), every number anchored to a page, PDF deleted after approval, calc unit tests pass | M0 |
| Check | Digest in 90 minutes | Editor approves a real digest within 90 minutes (gate 2) | M3 |
| M5 | Programs and roles | A second program's curator sees only their program and rates candidates | M3 |
| M6 | Reading lists and questions | A resident gets a list with a due date and answers questions; curator sees completion | M4, M5 |
| M7 | Shared library | An approved appraisal is visible to all programs and added to a list in one click | M4, M5 |
| Check | Full month at Shamir | One full month at Shamir through the app (gate 3), then a multi-site pilot | M6, M7 |

## Open decisions (Yoav)

Defaults are in effect until he decides; record each answer here and in the live doc.

- Case reports: kept, screening decides (default).
- JACEP Open (J Am Coll Emerg Physicians Open) as a core EM journal: not added yet.
- Code filter age limit: 6 months (default, `data/filters.yaml`).
- Journals marked "suggested" in `data/taxonomy.yaml` and `data/sources.yaml`: included, not
  yet approved.
- Size of the national digest per month: no fixed number; the digest is exactly what the
  editor approves (Yoav, 2026-11-12). Revisit after a few months.
- Editor access until M5: one editor password, `EDITOR_PASSWORD` (Yoav, 2026-11-12).
- Digest email: through Resend (Yoav, 2026-11-12). Pilot recipients: Yoav only. Until a domain
  is verified in Resend, the sender is onboarding@resend.dev, which can send only to the
  Resend account's own address.
- Gold standard: 2-3 program directors to label ~200 articles; SGEM benchmark list of ~10
  articles: not started. Also open: which month(s) to sample from, and how (a plain random
  sample of 200 would contain only a few high-yield articles), and the sensitivity threshold
  for gate 1.
- Screening defaults (M2, in `prompts/screening.v1.md` and `kria/pipeline/`), until Yoav
  decides otherwise: when unsure about relevance, the model says relevant (sensitivity first);
  high yield = highest of the three scores, shortlist = relevant articles ranked by it, 40 per
  month; "score 4-5" is counted as high yield in the reports; an article whose screening fails
  is retried up to 3 times.

- Articles without an abstract (M2 finding): 854 of October's 2,398 kept records (36%) had
  no abstract yet and were screened on title, journal and type only. Open: whether to re-check
  them in PubMed the next month and re-screen those that gained an abstract.
- Score calibration (M2 finding): 291 of 949 relevant October articles scored 4-5, more than
  the prompt intends ("reserve 4 and 5"). To review against the gold standard and in the M3
  approval screen before changing the prompt (any change becomes `screening.v2`).

## M2 as built (2026-11-12)

- Model: Claude Haiku 5.5 through the Message Batches API (half price, done within 24 hours),
  effort low, structured JSON output. Prompt `prompts/screening.v1.md` (33 topics with English
  labels from `data/taxonomy.yaml`); every run stores model, prompt version, prompt and schema
  hashes and settings; every article's result stores the served model and prompt version.
- Output per article: relevant, 1-3 topics, design (24 fixed categories), practice-changing,
  board-exam and journal-club scores 1-5 with a one-sentence reason each, and a summary of up
  to three sentences. Code validates every answer; invalid, refused, truncated or errored
  results are stored with the raw output or error and retried.
- Ranking (`kria/pipeline/rank.py`, code only): highest score, then design, journal tier,
  total score, PMID. FOAMed coverage and past ratings join when those signals exist.
- Cost and time: tokens and USD per article and per run (`data/pricing.yaml`), batch time from
  the API's own timestamps. October 2026 (2,398 articles): 0.30 USD, 14.3 minutes.
- Visibility: `python -m kria.pipeline.screen report <month>` and the Hebrew page /status
  (counts and costs only, no article content, since nothing is approved yet).

## Evaluation

- Unit tests for every statistic against known values.
- Screening gold standard: ~200 articles labeled by 2-3 program directors; sensitivity first.
- Appraisal benchmark: ~10 articles with SGEM episodes; compare checklist answers and bottom line;
  manually verify page anchors.
- Prompts are versioned; every model output stores model and prompt version.

## Rough API cost (assumptions, to be measured)

Screening 3,000 articles/month at 1,500 input + 300 output tokens each on Haiku 5.5 batch
(0.05 / 0.25 USD per million tokens): about 0.45 USD. Ten appraisals at 100k input + 15k output
tokens each: about 3.5 USD on Sonnet 5.5 or 7 USD on Opus 5.5. Hosting and email not included.

## M3 as built (2026-11-12)

- /editor (password `EDITOR_PASSWORD`, signed cookie for 30 days): months with a finished
  screening run; per month, the ranked relevant articles (40 at a time, "show 20 more"), each
  with rank, score, design, topics, a no-abstract warning, PubMed and DOI links, the English
  summary and the three scores with reasons, and one-click approve / reject / undo.
- Decisions are stored per month and article with the rank and screening shown at the time.
- Editor time: every page view and decision is logged; minutes = the sum of the gaps between
  actions, each gap capped at 5 minutes. Shown on the screens and stored with each digest.
- Digest: exactly the approved articles, grouped by first topic in taxonomy order, ranked
  within; Hebrew RTL frame, English content; title linked to PubMed, design, summary and the
  reason for the top score. Preview on /editor/<month>/digest, then "approve and send". Every
  sending is stored (articles, subject, HTML, text, recipients, Resend id, editor minutes).
- Settings (Railway variables on the site service): `EDITOR_PASSWORD`, `RESEND_API_KEY`,
  `DIGEST_RECIPIENTS` (comma-separated), optional `DIGEST_FROM`.
- Styles: Tailwind 3.4.16 compiled to `kria/web/static/editor.css` (no runtime CDN, which
  hospital networks may block); command in `tailwind.config.js`.
