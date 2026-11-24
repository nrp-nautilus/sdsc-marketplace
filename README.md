# SDSC Marketplace

Agent skills and plugins for systems run by the San Diego Supercomputer Center. A skill is a folder of
instructions, references and templates that a coding agent (Claude Code, OpenCode, Codex and others) loads when a
task needs it. The aim is twofold:

- **For users:** an agent that already knows the system's access steps, policies, storage and GPU names, so a plain
  request ("run this on a GPU") turns into a correct, policy-compliant workload.
- **For operators:** agents that follow the same rules the admins would teach a new user, so they stop creating the
  patterns that hurt a shared system (huge ConfigMaps, idle GPUs, floods of Jobs, `sleep infinity`).

Skills follow the [Agent Skills specification](https://agentskills.io/specification).

## Plugins

| Plugin | System | Skills |
|---|---|---|
| [`nrp-nautilus`](plugins/nrp-nautilus) | National Research Platform, Nautilus Kubernetes cluster | `nrp` (access, hard rules, policies, GPUs, storage, preflight check), `nrp-run` (your code to a running Job), `nrp-llm` (managed LLM endpoint), `kubernetes` (Jobs, Indexed Jobs, troubleshooting for Slurm/PBS users) |

Other SDSC systems (Expanse, Voyager, ...) are not covered yet.

## Install

### Claude Code

There is nothing to clone first: Claude Code clones the repository from GitHub itself, over SSH if you have a key
on GitHub and over HTTPS if not.

Register the marketplace:

```bash
claude plugin marketplace add nrp-nautilus/sdsc-marketplace
```

Install the plugin:

```bash
claude plugin install nrp-nautilus@sdsc-marketplace
```

Check it. `nrp-nautilus` should show version 0.3.0 and "enabled":

```bash
claude plugin list
```

Start a new conversation afterwards; skills load when a conversation starts. `/skills` lists them. Then ask for
what you want in plain words, for example "run this script on Nautilus" or "why is my pod Pending?".

Get a newer version later:

```bash
claude plugin marketplace update sdsc-marketplace
```

What you need on your machine:

- `kubectl` set up for Nautilus. The guard only checks commands sent to a context named `nautilus`. If yours has
  another name, tell it: `export NRP_PREFLIGHT_CONTEXTS=<your-context-name>` in your shell profile.
- Python 3 on your `PATH`, for the guard: it runs `python3`, or `python` when there is no `python3`. Standard
  library only, nothing else to install.

From a local clone instead: `claude plugin marketplace add /path/to/sdsc-marketplace`, then install as above.

### OpenCode

OpenCode reads skills from `.opencode/skills/` in a project, or `~/.config/opencode/skills/` for every project.
Link the plugin's skills in:

```bash
mkdir -p ~/.config/opencode/skills
ln -s /path/to/sdsc-marketplace/plugins/nrp-nautilus/skills/nrp        ~/.config/opencode/skills/nrp
ln -s /path/to/sdsc-marketplace/plugins/nrp-nautilus/skills/kubernetes ~/.config/opencode/skills/kubernetes
```

### Other agents

Symlink a plugin's `skills/` directory into the agent's skill path, for example
`ln -s $PWD/plugins/nrp-nautilus/skills ~/.agents/skills/nrp-nautilus`.

## Guardrails

Skill text is advice; two pieces make the important rules mechanical.

- `plugins/nrp-nautilus/skills/nrp/scripts/nrp_preflight.py` checks a manifest or a kubectl command line against
  NRP's rules (files in ConfigMaps, Jobs that never end, floods of pods, tolerations, priority classes, force
  deletes) and exits 2 on a blocking finding. Any agent can run it; it creates nothing.
- In Claude Code the plugin installs a `PreToolUse` hook that runs the same check on every `kubectl` command sent to
  the `nautilus` context and stops the blocked ones, whichever skill is loaded.

Neither replaces admission control on the cluster: an agent that talks to the API another way is not checked.

## Also in this repository

- [`demo/`](demo) — a presenter's script for showing the plugin, with a sample project and deliberately bad manifests.
- [`validation/nrp-admission/`](validation/nrp-admission) — dated transcripts of what Nautilus' admission does, from server-side dry runs.
- [`tests/`](tests) — unit tests for the preflight script.

## Check

```bash
python3 scripts/check.py                       # manifests and skill frontmatter; standard library only
python3 -m unittest discover -s tests          # the preflight rules
```

## Credits and license

Apache-2.0. See [CREDITS.md](CREDITS.md) for where each skill came from.
