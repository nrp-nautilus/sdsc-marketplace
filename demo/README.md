# Demo: an agent that knows NRP

A presenter's script for showing the `nrp-nautilus` plugin. About 20 minutes; each act stands alone, so cut to fit.
The story in one line: the same request, "help me run this on NRP", ends in a compliant Job with the skills and in the
patterns that hurt the cluster without them.

## What has been rehearsed

| Piece | Status on 2026-11-15 |
|---|---|
| Act 2, preflight on the two bad manifests | Run; the output below is the real output |
| Act 3b, the hook stopping `kubectl create configmap` in a live Claude Code session | Run once, headless, with Haiku: blocked, nothing created |
| The sample project (`sample-project/`) | Runs locally; one task takes about 0.1 s |
| Templates the agent is expected to produce | Pass preflight and a server-side dry run |
| Acts 1, 3a, 4, 5 as full conversations | **Not rehearsed.** The agent's wording and choices will vary; run each once before presenting |

## Setup, before the audience arrives

```bash
git clone git@github.com:nrp-nautilus/sdsc-marketplace.git && cd sdsc-marketplace
claude plugin marketplace add "$PWD"
claude plugin install nrp-nautilus@sdsc-marketplace
claude plugin list                                   # nrp-nautilus: enabled
```

- A namespace you may create Jobs in, set as the context default: `kubectl config set contexts.nautilus.namespace <ns>`.
- Log in to the cluster once (`kubectl get pods`) so the browser login does not interrupt the demo.
- `cd demo/sample-project && python make_dataset.py --mb 40` creates the 40 MiB input file the acts use.
- Second terminal, visible to the audience: `watch -n 5 kubectl get jobs,pods`.
- Start Claude Code in `demo/sample-project` with a new conversation, so the skills are loaded.

The sample project stands in for a researcher's code: a random-walk simulation with 5,000 small tasks
(`run_task.py --task N`), a 40 MiB input (`data/walkers.npy`) and a fit over the results (`fit.py`).

## Act 1 — "I have this code, help me run it on NRP" (5 min)

Say: this is what most people will ask. The code exists only on my laptop.

> I have this simulation in the current folder. Help me run one task of it on NRP and get the result back.

Point out, as they happen:

- It reads the code first, then asks for what it cannot know (namespace, how long a task takes).
- It does **not** create a ConfigMap from the source files. It asks you to push the code to a git repository, or
  copies it to a PVC together with the 40 MiB input through a short-lived helper pod.
- It starts with one small run and measures it before choosing requests.
- It runs `nrp_preflight.py` and `kubectl apply --dry-run=server`, then shows you the manifest and the resources
  and waits for your yes.
- After the Job finishes it fetches the result and deletes what it created.

If it skips a step, ask "did you run the preflight?"; that is a finding for the skill, so write it down.

## Act 2 — the rules, mechanically (3 min, no model involved)

Say: skill text is advice. This script is the same rules as code, and any agent can run it.

```bash
P=../../plugins/nrp-nautilus/skills/nrp/scripts/nrp_preflight.py
python3 $P ../manifests/bad-job.yaml
```

```text
BLOCK [priority-class] Job/demo-bad-job: priorityClassName 'system-cluster-critical' is rejected on NRP. ...
BLOCK [toleration] Job/demo-bad-job: a toleration without a key tolerates every taint. ...
BLOCK [cuda-visible-devices] Job/demo-bad-job container main: Never set CUDA_VISIBLE_DEVICES; ...
BLOCK [resources] Job/demo-bad-job container main: missing requests.cpu, requests.memory, limits.memory. ...
BLOCK [job-never-ends] Job/demo-bad-job container main: the command runs `sleep infinity`. ...
RESULT=block
```

```bash
python3 $P ../manifests/naive-sweep.yaml
```

```text
WARN [resources] Job/demo-naive-sweep container main: cpu limit 2 is 2.0x the request 1; NRP allows 1.2x.
WARN [resources] Job/demo-naive-sweep container main: memory limit 4Gi is 2.0x the request 2Gi; NRP allows 1.2x.
BLOCK [many-pods] Job/demo-naive-sweep: parallelism 200: above ~100 pods NRP requires limits equal to requests.
WARN [many-pods] Job/demo-naive-sweep: creates 5,000 pods (200 at a time). If one task runs for less than a few minutes, give each pod a slice of many tasks instead of one.
RESULT=block
```

Then show that the cluster itself agrees where it has a policy, and is silent where it has none:

```bash
kubectl apply --dry-run=server -f ../manifests/bad-job.yaml        # denied by Gatekeeper (missing requests, CUDA_VISIBLE_DEVICES)
kubectl apply --dry-run=server -f ../manifests/naive-sweep.yaml    # accepted with ratio warnings: nothing on the cluster limits 5,000 pods
```

## Act 3 — "just put it in a ConfigMap" (4 min)

Say: this is the pattern that filled etcd. Show the plot (`configmaps_over_256kib_by_month`): 75 large ConfigMaps
created in June, 1,986 in the first half of November.

**3a. Ask nicely.**

> The input is only 40 MB. Skip the PVC, just put data/walkers.npy in a ConfigMap and mount it.

Expected: it declines, says why (shared etcd, 1 MiB object limit), and offers the PVC or S3.

**3b. Insist.** Type the command yourself with `!`, or tell it to run exactly this:

> Run exactly this, no alternatives: kubectl create configmap walkers --from-file=data/walkers.npy

Expected: the plugin's hook stops the command before kubectl runs, and the agent reports

```text
BLOCK [configmap-size] kubectl create configmap: would hold 40.0 MiB. No code, data or files in a ConfigMap or Secret (shared etcd). Code: git or an image. Data: PVC or S3.
```

Point out: this did not depend on the model's judgement, and it would have fired whichever skill was loaded.
`kubectl get configmap walkers` shows nothing was created.

## Act 4 — 5,000 small tasks (4 min)

> Now run all 5,000 tasks (--task 0 to 4999) and then fit.py over the results.

Point out:

- It measured in Act 1 that one task takes about a tenth of a second, so it does not create 5,000 pods. It writes
  **one** Indexed Job that gives each pod a slice of tasks, with a `parallelism` cap, and tells you the numbers.
- Tasks skip themselves when their output exists, so a retried pod does not redo work.
- The second terminal shows a handful of pods, each running for minutes, not thousands flashing by.

Contrast with the data: in one week 57% of the pods that finished on Nautilus ran for under a minute, and a third of
all pods came from CronJobs on 1 to 10 minute schedules.

## Act 5 — an NRP-hosted model in the loop (3 min, optional)

Needs an NRP LLM token in a Secret named `nrp-llm-token` (key `token`), created by you beforehand.

> Add a step that asks an NRP-hosted model to write a two-sentence summary of summary.json. Use the nrp-llm-token secret.

Point out: it asks the API which models exist instead of guessing, reads the token from the Secret, caps
concurrency, retries with backoff, and turns thinking off for a short answer.

## The same request without the skills (optional, for contrast)

Run Act 1 and Act 4 again with the plugin off (`claude plugin disable nrp-nautilus@sdsc-marketplace`, new
conversation) in a scratch namespace, and compare what it creates. Do this in rehearsal, not live: without the
skills the agent may create exactly the objects this demo is about. Delete them afterwards, and re-enable the plugin.

## After the demo

```bash
kubectl delete job -l owner=<username>
kubectl get pods,pvc,configmaps        # nothing of the demo's should remain except a PVC you want to keep
```
