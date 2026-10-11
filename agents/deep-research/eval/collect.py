#!/usr/bin/env python3
"""Collects a research Agent's threads from a SecondStack deployment (read-only, over SSH) and prints one scorecard row per thread."""
import argparse
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

THREADS_SQL = """\\pset format unaligned
\\pset tuples_only on
SELECT id FROM chainlit."Thread" WHERE "namedAgentId" = '{agent}' AND "createdAt" > '{since}' ORDER BY "createdAt";
"""
STEPS_SQL = """\\pset format unaligned
\\pset tuples_only on
SELECT '##### ' || s.type || ' | ' || s.name || ' | ' || s."createdAt" || E'\\n--- INPUT\\n' || coalesce(s.input,'') || E'\\n--- OUTPUT\\n' || coalesce(s.output,'')
FROM chainlit."Step" s WHERE s."threadId" = '{thread}'
 AND s.type IN ('tool','assistant_message','user_message','system_message')
ORDER BY s."createdAt";
"""


def remote_query(args, sql: str, name: str) -> str:
    subprocess.run(["ssh", args.host, f"cat > /tmp/{name}.sql"], input=sql, text=True, check=True)
    return subprocess.run(["ssh", args.host, f"cd {args.repo} && ./utils/pg-query.sh -f /tmp/{name}.sql"], capture_output=True, text=True, check=True).stdout


def last(pattern: str, text: str, default: str = "-") -> str:
    found = re.findall(pattern, text)
    return found[-1] if found else default


def score(thread: str, text: str) -> dict:
    text = text.replace('\\"', '"').replace("\\n", "\n")
    blocks = text.split("\n##### ")
    heads = [block.split("\n", 1)[0] for block in blocks]
    users = [block.split("--- OUTPUT\n", 1)[1].strip().splitlines()[0][:60] for block in blocks if block.startswith("user_message") and "--- OUTPUT\n" in block]
    stamps = re.findall(r"\| (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", "\n".join(heads))
    minutes = "-"
    if len(stamps) >= 2:
        import datetime
        start, end = (datetime.datetime.fromisoformat(stamp) for stamp in (stamps[0], stamps[-1]))
        minutes = f"{(end - start).total_seconds() / 60:.1f}"
    return {
        "thread": thread[:8],
        "prompt": " // ".join(users),
        "depth": last(r"--depth (quick|standard|deep)", text),
        "rounds": last(r"fetch rounds: (\d+/\d+)", text),
        "read": str(len(set(re.findall(r'"source": "(s\d+)", "url": "[^"]+", "fetch_status": "ok"', text)))),
        "verified": last(r"claims: [^\n]*?supported=(\d+)", text),
        "stop": last(r"stopped: ([a-z]+)", text),
        "audit": last(r"audit: (\d+) failing", text, "not run"),
        "helpers": str(sum("Helper:" in head for head in heads)),
        "searches": str(sum("Web search" in head for head in heads)),
        "minutes": minutes,
        "cost": last(r"thread total \*\*([^*]+)\*\*", text),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help='UTC timestamp, for example "2026-10-12 09:00"')
    parser.add_argument("--host", required=True, help="SSH target of the deployment, for example ubuntu@staging-host")
    parser.add_argument("--repo", required=True, help="monorepo checkout on that host, for example /opt/secondstack/SecondStack")
    parser.add_argument("--agent", required=True, help="the research Agent's id (agent-...)")
    args = parser.parse_args()
    os.makedirs(os.path.join(HERE, "runs"), exist_ok=True)
    ids = [line.strip() for line in remote_query(args, THREADS_SQL.format(agent=args.agent, since=args.since), "dr_eval_threads").splitlines() if re.fullmatch(r"[0-9a-f-]{36}", line.strip())]
    columns = ("thread", "prompt", "depth", "rounds", "read", "verified", "stop", "audit", "helpers", "searches", "minutes", "cost")
    print(" | ".join(columns))
    for thread in ids:
        text = remote_query(args, STEPS_SQL.format(thread=thread), f"dr_eval_{thread[:8]}")
        with open(os.path.join(HERE, "runs", f"{thread}.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
        row = score(thread, text)
        print(" | ".join(row[column] for column in columns))


if __name__ == "__main__":
    main()
