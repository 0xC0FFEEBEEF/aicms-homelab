# AICMS Repository Instructions

This repository is for AICMS.

AICMS is a local service that inventories projects over SSH and exposes a local web UI.

The current milestone includes:
- host management
- SSH connectivity testing
- full scans
- incremental scans
- project discovery
- scan history
- settings UI
- LM Studio connection, model listing, and model selection
- local dashboard pages

Do not implement CMS publishing yet.

Do not implement auto-posting yet.

Never scan the entire filesystem by default.

Only scan allowlisted roots.

Never ingest or store secret-bearing files such as `.env`, private keys, token files, SSH keys, browser profiles, wallet files, or database contents.

Prefer small modules and readable code.

For large tasks, always plan first, then implement, then run tests, then summarize the result and run instructions.
