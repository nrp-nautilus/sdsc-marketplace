# Contributing

## Layout

```
.claude-plugin/marketplace.json          # the list of plugins
plugins/<plugin>/.claude-plugin/plugin.json
plugins/<plugin>/skills/<skill>/SKILL.md # frontmatter: name, description (+ optional compatibility, metadata, allowed-tools)
plugins/<plugin>/skills/<skill>/references/*.md
plugins/<plugin>/skills/<skill>/assets/...
plugins/<plugin>/skills/<skill>/scripts/...   # tools the agent runs (--help, exit codes)
plugins/<plugin>/hooks/hooks.json            # Claude Code only; must call a script that also works standalone
tests/                                       # unit tests for scripts
validation/<name>/                           # live checks with their dated transcripts
```

One plugin per system (`nrp-nautilus`, later `expanse`, ...). Generic skills that several systems share
(`kubernetes`, `slurm`) live in the plugin of the first system that needs them until a second system does.

## What a good skill has

- **Tested commands.** Every command and manifest was run on the system, with the date it was checked.
- **The rules an admin would teach.** Policies, limits and the patterns that hurt the shared system, stated as
  rules the agent can follow, with the safe alternative next to each one.
- **A short SKILL.md.** Under ~300 lines; details go in `references/`, ready-to-use manifests in `assets/`.
- **A description that says when to use it.** The agent decides from the description alone whether to load the skill.
- **No credentials.** Never a token, key or kubeconfig value, not even an example one.

## Before you commit

```bash
python3 scripts/check.py
python3 -m unittest discover -s tests
```

A rule about what the cluster does (an admission denial, a default it adds) needs a dated transcript under
`validation/`: see `validation/nrp-admission/verify.sh`, which uses only server-side dry runs.

Bump the plugin's `version` in both `plugin.json` and `.claude-plugin/marketplace.json` when its skills change.
