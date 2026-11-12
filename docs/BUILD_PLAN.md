# Build plan

Live version (Hebrew): https://claude.ai/artifact/RdJj7cDDeMd3STX1Y9E45Z (tab "תוכנית בנייה").

Each milestone is a vertical slice that ends in something working end to end.

Status (2026-11-12): M0 deployed on Railway (https://kria-dchufa-production.up.railway.app).
M1 code done and run on October 2026 against live PubMed on a local Postgres twice, with
identical results. M2 code done and tested offline (no live model call yet). The daily job
(`kria.pipeline.monthly`, a Railway cron service) runs both on Railway: it ingests October 2026
and submits screening on its first run, and collects the batch the next day. M1 and M2 are done
when that real run is visible on /status. Next: the M2 sensitivity check (needs the gold
standard), then M3.
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
- Size of the national digest per month: open.
- Gold standard: 2-3 program directors to label ~200 articles; SGEM benchmark list of ~10
  articles: not started. Also open: which month(s) to sample from, and how (a plain random
  sample of 200 would contain only a few high-yield articles), and the sensitivity threshold
  for gate 1.
- Screening defaults (M2, in `prompts/screening.v1.md` and `kria/pipeline/`), until Yoav
  decides otherwise: when unsure about relevance, the model says relevant (sensitivity first);
  high yield = highest of the three scores, shortlist = relevant articles ranked by it, 40 per
  month; "score 4-5" is counted as high yield in the reports; an article whose screening fails
  is retried up to 3 times.

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
  the API's own timestamps. Estimate for October 2026 (2,398 articles): about 0.6 USD.
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
