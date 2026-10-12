# SecondStack Agents

Configured named Agents for SecondStack, kept in Git for review, versioning and installation on another deployment.

## Layout

```text
agents/<agent>/
  agent.json             portable profile, Skill selection and integrity
  instructions.md        Agent instructions
  skills/<skill>/        Agent-owned Skill: SKILL.md and supporting files
  README.md              purpose, deployment requirements and version history
  eval/                  evaluation prompts and tools
scripts/                 add an export or build an import package
tools/                   pinned SecondStack package-format utility
dist/                    generated ZIPs, ignored by Git
```

Agent-owned Skills live with their Agent. A manifest can also explicitly source an owned Skill from root `skills/<skill>/` when several Agents reuse the same authored files. Packing bundles a separate owned copy for each Agent. Live shared Skills remain references in `agent.json`; their content is not bundled.

| Agent | Purpose | Owned Skill |
|---|---|---|
| [deep-research](agents/deep-research/README.md) | Research with claims linked to quoted evidence | [deep-research](agents/deep-research/skills/deep-research/SKILL.md) |

## Put an exported Agent in Git

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from this checkout:

```bash
scripts/add-export.sh ~/Downloads/agents.zip
git diff
git status --short
```

The command accepts single-Agent and bulk exports. It validates every package, hash, path and file mode before changing destinations. It replaces each exported Agent's `agent.json`, `instructions.md` and bundled `skills/`, including removing stale files and Skills omitted from the export. Agent READMEs, evaluations, other Agents and root Skill sources are preserved. Review the changes and commit them through your normal Git workflow.

Validation failures leave the checkout unchanged. A filesystem failure during installation can leave a partial update; inspect Git before retrying.

## Get a ZIP for Chat import

```bash
scripts/package-agent.sh deep-research
```

This writes `dist/deep-research.zip` and prints its package digest. Packing refreshes canonical serialization and integrity in `agent.json`, so review and commit that manifest with any instruction or Skill edits. Identical files produce identical ZIP bytes. The ZIP contains the profile, instructions and all owned Skills; Agent-level README and evaluation files stay in Git.

In Chat, choose **Agents → Import**, upload the ZIP, choose a new Agent or an existing target, and review the diff. Import saves a draft. Resolve target deployment requirements, test the Agent, then publish. Model/profile keys and search models must exist on the target; the preview reports missing dependencies. Live replacement of an existing owned Skill requires explicit acknowledgment.

To import one Agent from a bulk export, add the collection here, then pack the chosen Agent. Chat import accepts one Agent at a time.

## Change an Agent

- Edit `agent.json`, `instructions.md` or the Agent's Skill files; run `scripts/package-agent.sh <agent>` to refresh hashes and build the ZIP.
- Test on a deployment and run the Agent's evaluation set before production. Record behavior changes in its README's version history.
- Keep deployment identities, hosts, credentials and customer names out of Git. Packages omit funding, connections, Memory and other runtime state; document required deployment setup in the Agent README.

## Tooling maintenance

`tools/agent_package_format.py` is an unchanged standalone copy of SecondStack's schema-1 packer. Its upstream commit and SHA-256 are recorded in [tools/agent-package-source.json](tools/agent-package-source.json). It uses the Python standard library and requires no SecondStack checkout or running deployment. `uv` uses an available compatible Python or provisions one on first use; packing works offline once Python is available.

When updating the utility, copy it from an explicit SecondStack revision, refresh its source record, and run the command tests:

```bash
uv run --no-project python -I -m unittest discover -s tests -v
```
