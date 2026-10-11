# Personal Assistant batch prompt

Paste this into the Personal Assistant to run the evaluation set. Replace `<agent id>` with the research Agent's id.
For E8, attach a PDF to the same message and wait until its upload finishes before sending; otherwise the Assistant stops and asks for it.
Avoid running the batch while the deployment is being redeployed.

````text
Run a test batch for my research Agent and report back. Do not judge the answers; only run the steps exactly and tell me what happened.

Agent: named_agent_id = <agent id>

For each test below: create a new conversation with that Agent (thread_create with named_agent_id), name it with the test ID (for example "E3 - Postgres incremental backup"), and send the first message exactly as written. Start all of them first, then wait for their turns with thread_turn_wait, repeating the wait while a turn is still running (runs can take up to 20 minutes). Send a follow-up only after the turn before it has finished. If the Agent asks a clarifying question in any test other than E11, reply: "Use your best judgment and state your assumptions."

Before starting, confirm that this message has a PDF attached. If it has none, run every test except E8 and say so.

E1: What is the current maximum context window of Anthropic's latest Claude model?
E2: Which noise-cancelling headphones under $400 are best for frequent flyers in 2026? Compare noise cancellation, comfort, battery and call quality.
E3: How does PostgreSQL 17 incremental backup work, and what limitations does the documentation state?
E4: What did reviewers say about the original Apple Vision Pro at its February 2024 launch, and how did reviews a year later differ?
E5: How have lithium-ion battery pack prices changed since 2015, and where do 2030 forecasts disagree, and why?
E6: Using the text of the EU AI Act, which article sets fines for general-purpose AI model providers, and what are the maximum amounts?
E7: What was the exact training compute, in FLOP, of GPT-4o?
E8: Send the PDF attached to this message with the first message, using its file id in file_ids. Message: "Using only the attached document, what were the average battery pack prices by region and what drove the change?"
E9: Send: "Deep dive: obligations of general-purpose AI model providers under the EU AI Act, upcoming dates and expert disagreement." After about 2 minutes, cancel that turn with thread_turn_cancel. Then send: "continue" and wait for it to finish.
E10: Send: "How have lithium-ion battery pack prices changed since 2015?" When it finishes, send: "Go deeper: include 2030 forecasts and why they disagree."
E11: Send: "Research the best vector database." When it asks its question, reply: "1, and make it thorough."
E12: Send: "Compare Postgres logical replication and physical streaming replication for a 2 TB OLTP database that needs a read replica in another region." When it finishes, reply: "Site". When that finishes, send: "Also send the PDF."

When everything has finished (or after 45 minutes, whichever comes first), reply with:
- the UTC time you created the first conversation;
- one line per test: ID, conversation link, final status (finished, still running, failed or cancelled), and whether every follow-up was sent;
- for E8, the file name and file id you attached;
- anything that went wrong, with the exact error text.
````

Then run `collect.py` with `--since` set to that UTC time.
