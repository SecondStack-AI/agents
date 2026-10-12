# Deep research Agent

A named Agent that researches a question on the open web, or in documents you provide, and answers with a sourced report. Every factual claim is linked to the passage that supports it, checked against that passage, and cited. Nickname: *Citation Needed*.

The method lives in the Agent-owned [`deep-research` Skill](skills/deep-research/SKILL.md). The [instructions](instructions.md) set the persona, delivery rules and the overrides the Skill needs. The [profile manifest](agent.json) carries the portable settings and Skill selection.

## How it works

- **Depth.** The Agent picks `quick`, `standard` or `deep` from the wording of the request, and a level the user names always wins. Each depth sets the evidence the store must hold before research may stop: number of questions, independent publishers per key question, a counter-evidence question, fetch rounds, sources behind verified claims, and at `deep` a primary source per key question. "Go deeper" in a follow-up raises the depth on the same research without losing it.
- **Evidence store.** A SQLite store in the workspace (`/workspace/research/`) keeps questions, fetched sources split into numbered passages, claims linked to those passages, and verdicts. A verdict needs a quote copied from each supporting passage. Unverified, unsupported or contradicted claims cannot be cited.
- **Helpers.** Extractor helpers record claims from fetched sources and verifier helpers check them, in parallel.
- **Delivery.** `render` turns claim markers into numbered citations and writes `report.md` and `evidence.md`. `audit` scores the result before the reply. The reply is the rendered report.
- **Export.** After a `standard` or `deep` report the Agent offers a Site, a PDF or Markdown, and produces them only on request from one template. A Site carries PDF, Markdown, HTML and evidence downloads.
- **Provided documents.** Chat attachments and Agent Knowledge files are imported into the workspace and added as sources with `add-file`; they are cited as provided documents.

## Configuration

| Setting | Value |
|---|---|
| Chat profile | `agentic-claude-opus-5-5` / `claude-opus-5-5`, high reasoning |
| Tools | **Metered web search** with a search model selected; **Workspace**; **Send attachments** |
| Internet access | On (the workspace fetches pages over HTTPS) |
| Skills | `deep-research`, bundled as a Skill the Agent owns. The built-in **Browser** and **Sites** Skills are used automatically when available |
| Sites | Optional; needed only for the Site export |
| Instructions | The full text of [instructions.md](instructions.md) |

Search is the Agent's `web_search` tool. The Skill logs each search but cannot see the tool calls, so search counts in its status are reported by the Agent, not measured.

The initial manifest is seeded from this Agent's documented configuration and SecondStack's default profile definitions, not captured from a live Agent. Metered search uses `web-search`. A target must provide that search model, the configured Chat profile/model and SecondBox. Profile/model availability is checked by Chat's import preview.

Run `scripts/package-agent.sh deep-research` from the repository root and import `dist/deep-research.zip` through Chat's **Agents → Import**. Review the resulting draft, enable the Agent's internet access, run evaluations and publish. Internet policy is deployment state and does not travel in the package. Sites remains optional.

## Using it

- Ask a question; say "quick", "thorough" or "deep dive" to choose the depth.
- "Go deeper" continues the same research at a higher depth.
- Attach documents and say "using only the attached documents" to keep the research off the web.
- Answer the export offer with "Site", "PDF" or "Markdown".

## Testing

[`eval/eval-set.md`](eval/eval-set.md) holds twelve test prompts (E1–E12) with pass criteria, covering narrow facts, comparisons, historical and conflicting evidence, unanswerable questions, provided documents, interrupted runs, depth upgrades, ambiguous requests and export.
[`eval/assistant-batch-prompt.md`](eval/assistant-batch-prompt.md) is a prompt for the Personal Assistant that starts the whole set and sends the follow-ups.
[`eval/collect.py`](eval/collect.py) reads a deployment over SSH, read-only, and prints one scorecard row per test thread.

Avoid running the set while the deployment is being redeployed: a restart interrupts every running turn.

## Known limits

- Turns longer than the Agent Platform run limit (one hour) fail; the store survives, so "continue" resumes.
- An activated Skill's instructions can be lost when a long conversation is compacted; the Skill tells the Agent to run `status` first after a pause.
- Charts shown in chat with `show_html` are not carried into exports.
- The Agent sometimes answers a simple fact without the Skill, and sometimes retypes the report instead of delivering it unchanged.
- Workspace operations run one at a time, so the Skill downloads pages in parallel inside one command and gives helpers reading, not downloading.

## Version history

| Skill version | Change |
|---|---|
| v1 | Evidence store, claims linked to passages, numbered citations |
| v2–v4 | Quotes required for verdicts; checked stop; evidence file; per-source quotes; charset fix; heading-only quotes refused |
| v5–v7 | Batch search logging; every stop reason checked against the store; price-conflict hints; citation-stuffing and over-linking warnings |
| v8–v9 | Depth levels (`quick`, `standard`, `deep`) and depth upgrade; publishers counted by registered domain |
| v10 | Export to Site, PDF, HTML and Markdown from one template |
| v11 | Provided documents (`add-file`), snapshot integrity, `audit` scorecard, evaluation set |
| v12 | Audit treats a checked partial stop as partial; stricter unchanged-delivery rule |
