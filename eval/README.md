# Evaluation sets

- `gold_standard/`: ~200 articles from past months, labeled by 2-3 program directors
  (relevant yes/no, topic, high-yield dimensions). Used to measure screening sensitivity
  and precision for every prompt or model change.
- `sgem_benchmark/`: ~10 articles that have an SGEM episode. The appraisal engine's
  checklist answers and bottom line are compared with the episode, and every number's page
  anchor is checked by hand.

Run both sets before any prompt or model change goes live.
