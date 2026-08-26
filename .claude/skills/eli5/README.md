# eli5 (vendored skill)

`SKILL.md` is vendored verbatim from the `eli5` plugin in
[anthropics/claude-plugins-community](https://github.com/anthropics/claude-plugins-community/tree/main/eli5)
(author: Thariq Shihipar; the plugin manifest declares MIT).

## Why vendored instead of installed as a plugin

Claude Code on the web runs each session in a fresh container, so anything
installed with `claude plugin install` is gone when the container is reclaimed.
Declaring the plugin in `.claude/settings.json` does not help either: since
Claude Code v2.1.195 a plugin whose source is external (a GitHub repo) is *not*
auto-installed from project settings — it is only reported as "not installed".

A skill checked into `.claude/skills/` is loaded straight from the repo at
session start, with no marketplace clone and no install step, so it is
available in every session — local and cloud.

## Usage

```
/eli5 how does DNS work
```

## Updating

```bash
git clone --depth 1 https://github.com/anthropics/claude-plugins-community.git /tmp/cpc
cp /tmp/cpc/eli5/skills/eli5/SKILL.md .claude/skills/eli5/SKILL.md
```

## Alternative: keep it a real plugin everywhere

Set an environment setup script on the Claude Code web environment
(https://code.claude.com/docs/en/claude-code-on-the-web) that runs:

```bash
claude plugin marketplace add anthropics/claude-plugins-community
claude plugin install eli5@claude-community
```

That reinstalls the plugin on every container build, keeps the `/eli5:eli5`
namespace and marketplace auto-updates, but costs a clone at each session start.
