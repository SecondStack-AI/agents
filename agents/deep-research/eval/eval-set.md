# Deep-research evaluation set

Run every prompt in a new thread with the research Agent, unless the row says otherwise.
Then collect and score the batch with `collect.py` (see the end of this file).
Rerun the whole set after every Skill or instruction change, and compare with the previous scorecard.

## Criteria for every run

- The interpretation line names the depth (quick, standard or deep).
- `audit` ran after the last `render` and reports 0 failing checks, or the reply states why a check could not pass.
- The reply is the rendered `report.md` text, unchanged.
- No source is cited that the run did not read (snippets are marked as such).
- At standard and deep, the reply ends with the export offer, and no Site or file is produced unprompted.

## Prompts

| ID | Task type | Prompt | Expected depth | Passes when |
|---|---|---|---|---|
| E1 | Narrow fact | What is the current maximum context window of Anthropic's latest Claude model? | quick | One authoritative source, 1–4 searches, a short reply, no padding source. |
| E2 | Comparison | Which noise-cancelling headphones under $400 are best for frequent flyers in 2026? Compare noise cancellation, comfort, battery and call quality. | standard | Consistent comparison dimensions, price conflicts recorded with `--counter`, durability or complaint counter-evidence, a table. |
| E3 | Technical documentation | How does PostgreSQL 17 incremental backup work, and what limitations does the documentation state? | standard | PostgreSQL documentation marked primary; documented behaviour kept apart from blog opinion; version stated. |
| E4 | Historical | What did reviewers say about the original Apple Vision Pro at its February 2024 launch, and how did reviews a year later differ? | standard | Contemporaneous 2024 reviews dated as such; later reviews dated; no current documentation projected backward. |
| E5 | Conflicting sources and numbers | How have lithium-ion battery pack prices changed since 2015, and where do 2030 forecasts disagree, and why? | standard or deep | Real vs nominal dollars and pack vs cell kept apart; forecast vintages stated; disagreement explained, not averaged. |
| E6 | Evidence late in a long document | Using the text of the EU AI Act, which article sets fines for general-purpose AI model providers, and what are the maximum amounts? | quick | Article 101 quoted from the regulation text; amounts exactly as written; the regulation marked primary. |
| E7 | Not answerable from public evidence | What was the exact training compute, in FLOP, of GPT-4o? | quick or standard | States that no authoritative public figure exists; estimates, if any, are labelled as third-party estimates with their method; no invented number. |
| E8 | Provided document only | Attach a PDF (for example the BNEF 2024 battery price survey executive summary), then: Using only the attached document, what were the average pack prices by region and what drove the change? | quick | The file imported and added with `add-file` and a `--publisher`; no web search; citations show "provided document". |
| E9 | Interrupted run | Start: Deep dive: obligations of general-purpose AI model providers under the EU AI Act, upcoming dates and expert disagreement. Press Stop after about two minutes, then send: continue. | deep | The second turn runs `status` first, keeps the store (no `--fresh`) and reaches the deep bar. |
| E10 | Depth upgrade | How have lithium-ion battery pack prices changed since 2015? Then, in the same thread: Go deeper: include 2030 forecasts and why they disagree. | quick, then deep | `init --depth deep` on the same store; earlier sources and claims reused; the deep bar met. |
| E11 | Ambiguous request | Research the best vector database. Answer its question with: 1, and make it thorough. | deep after the answer | One round of clarifying questions, then deep. |
| E12 | Export | After any standard report, answer the offer with: Site. Then ask: also send the PDF. | — | `export site` then `publish_site` (private); one link; the PDF attached; Site downloads work. |

## With and without the Skill

Twice per release, run E2 and E5 with a second Agent that has the same instructions minus the Skill (deselect it in a copy of the Agent).
Compare: claims traceable to a quoted passage, conflicts reported, stale sources flagged, time and cost.
The Skill must win on traceability and conflicts; it may cost more time.

## Collecting results

`python3 collect.py --since "2026-10-12 09:00" --host <ssh target> --repo <monorepo path> --agent <agent id>` prints one row per thread of the research Agent created after that time:
prompt, depth, fetch rounds, sources read, verified claims, stop reason, audit result, helpers, duration and cost.
It reads the deployment over SSH (read-only) and keeps the raw dumps in `runs/<thread id>.txt` for review.
