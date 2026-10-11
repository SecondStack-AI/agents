# SecondStack Agents

Configured named Agents for SecondStack and the Skills they use, kept in Git so they can be reviewed, versioned and installed on any deployment.

A SecondStack named Agent is configuration, not code: a Chat profile, a set of tools, instructions and Skills. This repository holds that configuration in plain files. When Agent profile export and import arrive, the exported profile of each Agent will live next to its instructions here.

## Layout

```text
agents/<agent>/
  README.md          what the Agent does and how to configure it
  instructions.md    text for the Agent's Instructions field
  eval/              test prompts and tools for checking the Agent
skills/<skill>/      one Agent Skill: SKILL.md at the root, plus scripts/, references/, templates/
scripts/             repository tooling
```

Skills are separate from Agents because one Skill can serve several Agents. Each Agent README names the Skills it needs.

## Agents

| Agent | Purpose | Skills |
|---|---|---|
| [deep-research](agents/deep-research/README.md) | Sourced research on the open web and on documents you provide, with every claim linked to a quoted passage | [deep-research](skills/deep-research/SKILL.md) |

## Install an Agent

1. Build the Skill archives: `scripts/package-skill.sh <skill>` writes `dist/<skill>.zip` with `SKILL.md` at the archive root.
2. In Chat, open **Agents**, create the Agent, and on its **Profile** tab set the Chat profile and tools its README lists.
3. Under **Agentic Skills**, choose **Add skills → Upload skill** and upload the archive. To update a Skill the Agent already owns, open it with **Preview** and use **Replace content**; uploading a second Skill with the same name is refused.
4. Paste `instructions.md` into **Instructions**, then **Publish**.

## Change an Agent or Skill

- Change the files here first, then install them on a test deployment and run the Agent's evaluation set before production.
- Record each change that affects behaviour in the Agent README's version history.
- Keep deployment details out of this repository: no host names, Agent ids, keys or customer names. Pass them to tools as arguments.
