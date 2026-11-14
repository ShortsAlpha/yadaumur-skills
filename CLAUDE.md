# claude-skills

Personal Claude Code skills, subagent definitions and hooks, published as a public repo.

## This repo is public

Never commit anything that identifies the author or their environment: real names, OS user names, machine names, email addresses, company, project, product or channel names, absolute personal paths, plan or account details, time zone or location, and secrets (tokens, keys, `.env` values). Use neutral wording ("the user", `~`, `my-app`) and keep personal settings in local config outside the repo (for example `~/.claude/vault-live/config.json`). Before each commit, check what `git add -n .` would publish. Commit with the GitHub noreply address as `user.email` and with `TZ=UTC`.

## Layout

- `skills/<name>/SKILL.md` (+ `scripts/`): one folder per skill; the folder name is the skill name.
- `agents/*.md`: subagent definitions, linked as `~/.claude/agents`.
- `install.sh`: links the skills and agents into `~/.claude` and installs the hooks. It is idempotent; run it again after adding a skill.

## Conventions

- Scripts must run on macOS bash 3.2 and on Windows Git Bash, and are called as `bash <path>`. Python scripts need Python 3.9+ and the standard library only.
- `SKILL.md` files and scripts are written in English.
- Tests live next to the scripts they cover (`skills/*/scripts/test_*.py`). Keep them passing and keep their fixtures synthetic: no real paths, names or data.
