# Spec summary

Authoritative spec (Hebrew, live): https://claude.ai/artifact/RdJj7cDDeMd3STX1Y9E45Z
This file is an English working summary for development. If they disagree, the live doc wins.

## Goal

Each month, give every Israeli EM residency program a curated, topic-sorted list of new articles,
turn it into a reading list with due dates and questions for residents, and offer on-demand
SGEM-style critical appraisal of any article.

## Roles

| Role | Who | Does | Time budget |
| --- | --- | --- | --- |
| National editor | Yoav, later a small team | Approves the automatic digest, runs the shared library, maintains taxonomy and sources | up to 90 min/month |
| Program curator | Program director or chief resident | Picks mode and topics, rates and approves in curated mode, builds reading lists, requests appraisals | up to 1 hour/month (max 2) |
| Resident | Program residents | Sees only own program's reading list, reads, answers questions | per reading list |

## Modes (one engine, three configuration levels; a program can switch monthly)

- Automatic: the approved national digest, filtered by the program's topics.
- Customized: same feed with the program's topic weights and monthly volume.
- Fully curated: the raw candidate list before national approval; curator rates, approves,
  rejects, or adds articles by PMID/DOI.

Editorial model: the automatic digest = model proposal + national editor approval. Curated
programs' ratings stay private per program and are used only in aggregate, anonymously, to
calibrate scoring.

National editor time plan (pilot assumption): ~60 min on a shortlist of ~40 ranked candidates
(3-line summary, design, score reason, one-click approve/reject), ~20 min approving library
appraisals, ~10 min on topics/sources. Cycle: pipeline runs on the 1st; editor approves by the 5th.

## Taxonomy and sources

`data/taxonomy.yaml` (33 topics, 3 groups) and `data/sources.yaml`. Each topic gets a versioned
search definition (MeSH terms, keywords, journals). v1: programs choose a subset of base topics
and weights. Later: program-defined topics with their own query, always mapped to a base topic.

FOAMed sites and organizations are ranking signals and commentary links, never article sources.
EM:RAP requires a subscription and is not scanned.

## Monthly pipeline

Sources (PubMed, Crossref, FOAMed RSS) -> code filter (dedupe by PMID/DOI, publication type)
-> LLM screening and scoring -> ranked shortlist -> national editor (automatic/customized) or
program curator (curated) -> digest / program list -> reading lists and questions.

High yield = high score (1-5, one-sentence reason) on at least one of:
practice-changing in an Israeli ED; core board-exam (Shalav A/B) material; journal-club worthy
methodology (good or flawed). Ties broken by design, journal, FOAMed coverage, past ratings.

## Appraisal engine (the only agentic part)

English report in SGEM structure: Case, Background, Clinical Question, Reference, PICO,
Authors' Conclusions, Quality Checklist, Key Results, Talk Nerdy, Comment on Authors'
Conclusion vs Ours, Bottom Line, Clinical Application, What Do I Tell My Patient, Case
Resolution, plus Israeli ED applicability and journal club questions.

Steps: identify design and checklist (RoB 2, ROBINS-I, QUADAS-2, PROBAST, AMSTAR 2, AGREE II,
then GRADE) -> fetch context (trial registry, protocol, FOAMed commentary) -> structured
extraction with a quote and page/table for every number -> statistics in code -> first pass ->
separate Guest Skeptic pass (spin, outcome switching, composite/surrogate outcomes,
multiplicity, subgroup credibility, early stopping, funding/COI) -> reconcile and report
remaining disagreements.

Safety: abstract-only appraisals are labeled limited and stay out of the library; missing data
is "not reported"; no reference that was not retrieved; library entry only after editor approval.

## Full text

The monthly scan needs abstracts only. Full text is needed only for deep appraisal (~10/month):
1. Open access, fully automatic: PubMed Central, Europe PMC, Unpaywall.
2. Subscribed content via publisher TDM APIs (Elsevier, Wiley), which require the library's
   agreement and either an institutional token or requests from inside the Shamir network.
3. Otherwise: abstract-only, labeled limited.
Never: scraping the library portal or using anyone's credentials.

## Reading lists and questions

Curator builds a list from the digest or library and sets a due date; email reminders a week
before and on the day. Board-style MCQs (one best answer, explanation per option, anchored to
a finding) and open discussion questions from Talk Nerdy. Curator approves questions; approved
questions are reused. Tracking: opened, marked read, quiz score; visible only to the resident
and their program's curator.

## Shared library

Every editor-approved appraisal is visible to all programs, deduplicated, versioned, with
approver, date, full-text vs abstract, and sources. Flagged for review on comment, correction
or retraction. Searchable by topic, design, journal, date.

## Success metrics

Program director satisfaction (quarterly 3-5 question survey) and resident reading (share of
items marked read by due date) are primary. Also MCQ scores, national editor minutes (<= 90),
screening sensitivity vs gold standard, number of active programs.
