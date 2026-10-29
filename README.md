# purser

Purser is the finance backend for the personal agent stack: AI-native,
self-hosted personal finance with a double-entry ledger, a SimpleFIN bank feed,
deterministic matching and forecasting, and MCP and REST as its primary
interfaces. Your AI agent operates it for you with scoped, audited and
reversible access.

**Documentation:** https://mlamoure.github.io/purser/

## Set up with your AI

Purser is meant to be installed by your personal AI agent (Claude Code, Codex,
an OpenClaw agent or any agent that can run shell commands). Give it this
prompt:

```text
Set up Purser for me by following https://mlamoure.github.io/purser/tutorials/ai-setup/
```

The agent installs Purser with Docker, stops at the steps only you can take,
and connects itself over MCP. Agents that load skills can also use
[`skills/purser/SKILL.md`](skills/purser/SKILL.md).

## License

AGPL-3.0-only — see [LICENSE](LICENSE).
