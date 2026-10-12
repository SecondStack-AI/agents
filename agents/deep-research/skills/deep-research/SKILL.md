---
name: deep-research
description: Method and evidence store for sourced research on the open web. Use for any answer that needs sources, from one checked fact to a full research report, comparison or buying guide. Keeps every claim linked to the passage that supports it and renders numbered citations.
---

Scripts are relative to this skill's root directory; Agent Platform returns the root during activation.
Run the store as `python3 <root>/scripts/research.py <command>`; `--help` on any command shows its arguments.
State lives in `/workspace/research/` and survives long runs, compaction and follow-up turns.
After a pause, a compaction or a follow-up question, run `status` first and continue from the store; never reconstruct sources or findings from memory.

The store enforces the method: an unverified claim cannot be cited, a verdict needs `claim show` first and a quote from each supporting source, and `stop sufficient` refuses while gaps remain. When it refuses, fix the gap it names; do not work around it.

## 1. Depth and brief

Choose the depth from the request and state it in your one-line interpretation, so the user can correct it:
- `quick`: one fact or a narrow question ("what is", "which version", "when"). One authoritative source can settle it; one fetch round; a short reply.
- `standard` (default): "research", "compare", "best", "pros and cons". Two independent publishers per priority-1 question, a counter-evidence question, two fetch rounds.
- `deep`: "deep dive", "thorough", "full report", "literature review", or a request with legal, financial, medical or safety stakes. Three independent publishers and a primary source per priority-1 question, three fetch rounds, at least ten sources behind verified claims, and a verifier helper for every cited claim.
A depth named by the user always wins over your reading of the request.

Run `init "<question>" --depth quick|standard|deep`; it prints the bar the store will enforce at `stop sufficient`.
If a store already exists, a follow-up continues it; a new topic uses `init --fresh`. When the user asks to go deeper, run `init --depth deep` on the existing store: every source and claim stays, and the higher bar applies.
Run `date -u` and write `/workspace/research/brief.md`:
- the question and what the answer will be used for;
- constraints as stated (dates, region, versions, audience, budget); mark anything not stated as unspecified, never assume it;
- the deliverable;
- what evidence would be sufficient for the conclusions that matter.

Then add the questions that must be answered, each with the evidence that would settle it:
`q add "Which grills perform best in independent tests?" --needs "hands-on comparative tests from at least two outlets" --priority 1`.
Use as many questions as the depth asks for (quick 1–2, standard 3–5, deep 5–8). Prioritize the ones whose answer could change the conclusion and avoid overlap.
At standard and deep, one priority-1 question looks for counter-evidence, added with `--counter`: criticism, failures, recalls, safety notices, durability and owner complaints, retractions, or the strongest opposing view. It gets its own searches; evidence gathered for other questions does not answer it.
Mirror the questions in `update_plan` so the user sees progress.

## 2. Discover

`web_search` finds candidate sources; its excerpts are leads, never evidence.
Run the searches for independent questions together, as several `web_search` calls in one step. Then log them in one command, one exact query per call, grouped by question:
`searched --q q1 --results 7 "<query 1>" "<query 2>"; searched --q q3 "<query 3>"`.
Every priority-1 question needs at least one search of its own. Log every call, including searches that returned nothing. Search counts per depth are a guide, not a limit: about 3 quick, 10 standard, 25 deep.
Each search must serve a named open question. Write specific queries, vary phrasing only when results are thin, and do not repeat a search whose result you did not see.
Use the excerpts to choose what to fetch: prefer results that are the original source, are dated, and name the exact product, version or document. When a page you need is blocked, its search excerpt can still be recorded with `snippet` (step 3).
`site:` can narrow a search to one domain but sometimes returns nothing; when it does, search again with the publication or organization named in plain words.

Choose sources for the claim, not by a fixed ranking: an official repository or regulator text is strong for what it states; a vendor page is strong for its own prices and specifications but not for comparisons; an old document can be right for a historical question.
Several sites repeating one announcement are one evidence chain, not several.
Meet the depth's publisher count per priority-1 question. When one is blocked, look for another rather than settling for one.
Mark original evidence with `source sN --primary`: the law or regulation itself, a dataset, a filing, an official specification, a study, or a tester's own measured results. Articles that report on these are secondary. Deep research needs a primary source behind each priority-1 question.

Research in rounds: search and fetch, read and record claims, then search again for what the first round left open or contradicted. Each `fetch` that adds new sources starts a round; the depth sets the minimum.

## 3. Read

Fetch the chosen pages in one call so they download in parallel:
`fetch <url> <url> ... --q q2 --why "independent hands-on tests"`.
Each result reports `source`, `fetch_status`, `content_kind`, `published` and the passage count.
- `ok`: the full text is stored as numbered passages in `sources/sN.txt`. Short labels are merged, so a product card or table row is usually one passage.
- `thin` or `blocked`: retry once with `fetch --browser <url>`. If it stays blocked, do not evade the challenge; find another source.
- PDFs are split by page; passages start with `(page N)`.

Do not print whole sources into the conversation. Locate what the question needs:
`find s3 "Spirit E-325|warranty"` (or `find all ...`), then `read s3:p40-44 --context 1`.
If no passage addresses the question, record nothing from that source. Absence in one document is not evidence of absence.
Documents the user provides are sources too. Bring them into the workspace first (`import_chat_file` for a Chat attachment, `download_collection_file` for Agent Knowledge), then add them in one call:
`add-file /workspace/imports/q3-memo.pdf /workspace/imports/plan.docx --publisher "Acme finance team" --q q1 --why "the user's internal figures"`.
PDF, Word, OpenDocument, RTF, EPUB, HTML, spreadsheets, presentations and plain text are supported. `--publisher` is required: it names who wrote the document, so independence counts stay honest. Add `--primary` when the document is the original record (the company's own filing, dataset or minutes). In the report these appear as provided documents, without a link. Research limited to provided documents follows the same method; say in the brief that the web is out of scope.
If `fetch` reports that a page changed or failed since claims cited it, the store keeps the snapshot those claims quote and saves the new text beside it. Compare the two, and when the change matters, say so in the report.

When a key page cannot be read at all and no alternative exists, record the search excerpt with `snippet <url> --excerpt "..." --reason "paywalled"`; it renders as "snippet only, page not read".

## 4. Record claims

Record each finding as a claim linked to the exact passages; several passages and ranges are allowed:
`claim add "CNET named the Spirit E-325 best overall" --q q1 --kind comparison --support s2:p96 s2:p124-128 --as-of 2026-10`.
- One claim, one fact: a test result, a price, a size and a warranty are four claims. A bundled claim is verified only as far as its quote reaches; `claim add` warns when a statement carries several figures.
- Keep numbers, dates, conditions and qualifiers exactly as the source states them, including the source's own date and whether a price is a sale price. Never strengthen a claim beyond the passage.
- Link only the passages that carry the claim, not the surrounding page; `claim add` warns past four passages of one source.
- `claim add` prints `check:` lines when the same source gives a different price for the same item nearby. Read each one; if it is the same item, record the second value as `--counter` and report both.
- When sources, or one source in two places, give different values for the same thing (price, specification, date, ranking), record both: a claim for one value with `--counter` on the other passage. Report the difference; do not pick one silently.
- `--kind` is free text (fact, number, date, comparison, specification, ...). `inference` and `opinion` mark your own reasoning; they may have no support, and the report must word them as such.
- Set each question's status with `q set` as evidence arrives, and add follow-up questions when a finding opens one. A question whose note names something you could not establish is `partial`, not `answered`, and your stated confidence must reflect it.

## 5. Use helpers for parallel work

Open the workspace and run `init` before starting helpers; they share it.
Workspace commands run one at a time, so give helpers reading and judgment, not downloading: fetch first, then hand each helper a few stored sources or one question.
Start independent helpers together in one step. Use the briefs in `references/helper-briefs.md` as written: the extractor records claims, including `--counter` for conflicting values, directly in the store; the verifier checks claims. A helper's text reply is not in the store, so a finding it only mentions (a second price, a caveat) never reaches the report unless it is recorded as a claim.
A helper sees nothing of this conversation; its brief must carry the overall question as well as its part.

## 6. Verify

Every claim you cite must have a verdict. Check each one on its own:
1. `claim show cN` and read every linked passage, with `read sN:pA --context 2` where the context matters.
2. Check the whole claim: quantities, qualifiers, dates, versions, model names and causal language. A feature in an unreleased branch does not support "available in the current release"; a test of one model does not support a claim about its sibling.
3. Set the verdict with the decisive words copied exactly from the linked passages, one quote per supporting source:
   `claim verdict cN supported --quote "named it our top pick for most backyard cooks" "Our winner is durable, reliable"`.
   The store refuses a verdict before `claim show`, a quote that is not in a linked passage, and a supporting source without a quote. If a source does not carry the claim, remove it from the claim instead of quoting around it.
   `partial` and `contradicted` also need `--quote`, plus `--note` with the mismatch and `--restate` with what the evidence does support. `unsupported` and `insufficient` need `--note`.
Never set verdicts in a loop or with a shared note; each verdict is a separate judgment.
At deep, every cited claim goes to a verifier helper; at standard, do so with more than about 8 claims, so the writer does not grade its own work.
If more research could resolve an `insufficient` claim, add a question and go back to step 2.

## 7. Decide when to stop

Stop when one of these holds, and record it with `stop <reason> --note "..."`:
- `sufficient`: the store checks the depth's bar: question count, counter-evidence question, fetch rounds, sources behind verified claims, and for every priority-1 question its status, own search, verified claims, independent publishers and, at deep, a primary source. It refuses otherwise and names the gaps.
- `diminishing`: a further search round for the open questions found nothing worth reading; the store requires the depth's fetch rounds and at least two searches logged after the last fetch;
- `access`: the remaining evidence is behind blocked pages, and alternatives were tried;
- `deadline`: the run is over 30 minutes old; keep at least a quarter of your effort for verification and writing.
Before any stop, every category or option the brief promised (for example budget, premium, compact) has a verified claim or is named in the report as not covered. A question is `answered` only with a verified claim; the store refuses it otherwise.
A stop other than `sufficient` makes the answer explicitly partial; say which gaps remain.

## 8. Write and render

Write the draft at `/workspace/research/draft.md` from the store, not from memory. Cite with claim markers `[c12]` or `[c12, c15]`; cite a whole source as `[s4]` only for background.
Keep established facts, your inferences and open questions distinguishable, and state gaps precisely instead of adding unsupported detail.
Do not write a source list; `render` builds it.

`render /workspace/research/draft.md` writes `/workspace/report.md` with numbered citations `[1]` and a Sources list, and `/workspace/evidence.md` with the quote behind every cited claim.
It refuses unknown, unverified, unsupported or contradicted claims and warns about partial claims and uncited figures or dates. Fix every error and review every warning, then render again. Answer an uncited-figure warning by citing the sentence with the claim that states it, never by deleting the fact and never by attaching unrelated claims. Your own statements (today's date, scope notes) need no citation.
`render` ends the report with a line offering evidence.md; do not write your own.
`render ... --check` validates without writing.
After rendering, run `audit`. It scores the evidence behind the report: every cited claim verified with a quote from each supporting source, figures and dates cited, the depth bar, snippet-only and stale sources, disagreements to state, and unchanged snapshots. Fix every `FAIL` before you reply; mention stale sources and stated disagreements in the report where they matter. It checks structure and quotes, not meaning.

The rendered `report.md` is the answer. Print it with `cat /workspace/report.md` and deliver that text exactly, down to its last line: keep the source list as rendered, the evidence line and the export offer. Never retype, reformat or shorten it in the reply, even for a short answer. To change anything, edit the draft and render again.

## 9. Export on request

At standard and deep, `render` ends the report with an offer of a Site, Markdown or PDF. Do nothing until the user picks one, then:
- **Site:** `export site` builds `/workspace/sites/<name>/` from the report template: the report page with its evidence section and a download bar for PDF, Markdown, HTML and the evidence file. Activate the sites Skill, then call `publish_site` with the site name and path that `export` prints, keeping it `private` unless the user asked to share it. Reply with the link once. Use `--name` for a name the user wants, and `--replace` to rebuild after the report changes.
- **PDF:** `export pdf`, then `send_attachment` with `/workspace/report.pdf` (`application/pdf`).
- **Markdown:** `export md`, then attach `/workspace/report.md` and, when the user wants the quotes, `/workspace/evidence.md` (`text/markdown`).
- **HTML:** `export html`, then attach `/workspace/report.html` (`text/html`).
Every format comes from the rendered report; after a correction, render again before exporting. Use the template unless the user asks for a different look; then build the page yourself with the sites Skill, from `report.md`.
