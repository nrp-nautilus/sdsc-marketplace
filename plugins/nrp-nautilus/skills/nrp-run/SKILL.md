---
name: nrp-run
description: >-
  Take a user's own code to a running, policy-compliant Job on NRP Nautilus.
  Use when the user says "run this on NRP", "submit this to the cluster", "I
  need a GPU for this script", "run these N experiments", or hands over a
  script, repository, notebook or training code to execute on NRP. Covers
  getting the code and its environment into a container, getting data in and
  results out, sizing the request from a small test run, sweeps of many
  tasks, monitoring and cleanup.
compatibility: Requires kubectl with access to an NRP namespace, and the nrp skill installed next to this one. Building images uses a git host with CI (NRP GitLab) or local docker.
metadata:
  version: "1.0"
  system: nautilus
  facility: nrp
  scheduler: kubernetes
  checked: "2026-11-15"
allowed-tools: Bash(kubectl get *) Bash(kubectl describe *) Bash(kubectl logs *) Bash(kubectl config *) Bash(kubectl auth can-i *) Bash(kubectl top *) Bash(kubectl wait *) Read(/${CLAUDE_SKILL_DIR}/**)
---

# Run a user's code on NRP

The path is always the same: understand the work, get the **code** and its
**environment** into a container, put the **data** where pods can reach it,
**size** the request from a small run, submit a **Job**, watch it, collect
the results and clean up. Load the `nrp` skill first: its hard rules apply to
every step here.

## 1. Find out what the work is

Read the code before asking. Then confirm what you could not tell:

| Question | Why it matters |
|---|---|
| What is the entry command, and does it end by itself? | A Job's command must exit |
| What does it import or install? | Decides the image |
| Does it use a GPU? How much GPU memory? | GPU type; no GPU for CPU code |
| How big are the inputs and outputs, and where are they now? | PVC or S3 |
| How long does one run take on the user's machine? | Deadline, and whether tasks need packing |
| One run, or many (a sweep, a list of files)? How many, how long each? | Indexed Job and tasks per pod |
| Which namespace? | Never guess; `kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'` (no output: none is set, ask) |

## 2. Code: git or image, never a ConfigMap

| The code is | Do this |
|---|---|
| In a public git repository | Clone it in an init container: `assets/templates/job-run.yaml` |
| In a private repository | Same, with a read-only deploy token in a Secret created by the user (`kubectl create secret generic ... --from-literal`), referenced with `secretKeyRef` |
| Only on the user's machine | Ask the user to commit and push it (GitHub, or NRP GitLab at https://gitlab.nrp-nautilus.io). For a one-off, copy it to a PVC with the data (step 4) |
| Large, compiled, or reused for many runs | Bake it into an image: `references/images.md` |

Do not create a ConfigMap from source files, however small the project looks
today: the next version is bigger, and every version stays in etcd.

## 3. Environment: reuse an image, build one when it pays

- Start from an image that already has most of what the code needs: the
  project's own image, an upstream one (`pytorch/pytorch`, `python:3.12-slim`),
  or NRP's scientific images (`gitlab-registry.nrp-nautilus.io/nrp/scientific-images/python`).
- A few small packages: `pip install -r requirements.txt` at the start of the
  command is acceptable. It reruns in every pod.
- Build an image when installation takes minutes, needs a compiler, or the
  Job has many pods. Ask the user how: NRP GitLab CI is the recommended way
  (it builds on NRP and needs nothing installed locally), and docker on their
  own machine is fine if they already use it. Both, and what each needs:
  `references/images.md`.
- Pin a tag (`:v3`, a commit SHA), never `:latest`, so reruns get the same image.

## 4. Data in, results out

| Size | Use |
|---|---|
| Up to a few GB, from the user's machine | A PVC filled through a helper pod with `kubectl cp`: the storage reference of the `nrp` skill, section "Getting Local Files to a Job" |
| Tens of GB and more, or data other sites read | Ceph S3; the Job reads and writes it directly |
| Already on a PVC or in S3 | Mount it or pass the URL; put the Job in the same region |

Results always go to a PVC or S3. The pod's own filesystem disappears with
the pod, and NRP deletes a finished Job and its logs after one day.

## 5. Size the request from a small run

Never guess the requests, and never copy the largest numbers that fit.

1. Run **one** small case first: one task, a short input, a few training steps. Ask for modest resources (for example 2 CPU, 8 Gi, 1 GPU only if the code uses one).
2. Measure it while it runs and when it ends:
   `kubectl top pod <pod>`, `kubectl exec <pod> -- nvidia-smi`, and the exit
   state in `kubectl describe pod` (`OOMKilled` means too little memory).
   Wrapping the command in `/usr/bin/time -v` prints the peak memory in the log.
3. Set the real Job's requests to what you measured plus a small margin, with
   `limits == requests`. More than one GPU only if the first was near 100% used.
4. Set `activeDeadlineSeconds` from the measured time, with margin.

Tell the user the numbers you measured and the request you chose.

## 6. Many tasks: one Indexed Job, several tasks per pod

For a sweep or a list of inputs, do **not** create one Job per task and do
not loop `kubectl create`. Use one Indexed Job
(`assets/templates/job-sweep.yaml`):

- `parallelism` caps how many pods run at once. Start low (10 to 20) and
  raise it only if the namespace quota and the user agree. Many namespaces
  allow at most 200 pods in all (`kubectl get resourcequota -n <namespace>`).
- If one task takes less than a few minutes, set `TASKS_PER_POD` so each pod
  works for several minutes at least: 5,000 ten-second tasks are 50 pods of
  100 tasks, not 5,000 pods.
- Each task writes its own output file and skips itself if that file already
  exists, so a retried pod does not redo finished work.
- Something that must run every few minutes forever is not a CronJob: it is
  one small Deployment whose process sleeps between rounds.

## 7. Check, show, submit

1. `python3 ../nrp/scripts/nrp_preflight.py job.yaml` (the `nrp` skill's script): no `BLOCK`.
2. `kubectl apply --dry-run=server -f job.yaml`: no denial, no `Warning:`.
3. Show the user the manifest and a one-line summary: pods, CPU, memory, GPUs, deadline.
4. `kubectl apply -f job.yaml` once they agree.

## 8. Watch without hammering the API

```bash
kubectl wait --for=condition=complete job/<job> --timeout=2h    # blocks; no polling loop
kubectl logs -f job/<job>                                        # follow one pod
kubectl get pods -l job-name=<job>                               # state of all pods
kubectl describe pod <pod>                                       # Pending or failed: read Events
```

Do not poll in a tight loop; check every few minutes at most. In the first
ten minutes confirm the pods use what they asked for; if GPU utilization is
low or memory is far under the request, stop and resize rather than let a
long Job waste it.

## 9. Collect and clean up

Copy results from the PVC (helper pod and `kubectl cp`) or read them from S3.
Then delete the Job if it is still there, any helper pods, and PVCs the user
no longer needs. Report what ran, what it used, and where the results are.

## Gotchas

| Symptom | Cause and fix |
|---|---|
| `metadata.annotations: Too long: may not be more than 262144 bytes` | The object is too big for `kubectl apply`. It is a ConfigMap holding files: move them to git, an image, a PVC or S3. Do not switch to `kubectl create` |
| `denied the request: Need toleration key` | A toleration without `key`. Remove it; tolerate one named taint or none |
| `exceeded quota: low-priority-ban` / `high-priority-ban` | A `priorityClassName` users may not set. Remove the line or use `opportunistic` |
| `exceeded quota: a100-limit` (or `h100`, `h200`, `gh200`) | That GPU type needs quota. Use `nvidia.com/gpu` with product affinity, request A100 access, or run as `opportunistic` |
| Pod `Pending`, event `untolerated taint` | The chosen nodes are reserved or special. Change the affinity; do not add a toleration without the user's say |
| Pod killed at exactly 6 hours, `DeadlineExceeded` | It was a bare pod. Run it as a Job |
| Job and logs gone the next day | Default TTL of one day after finishing. Write results to a PVC or S3 |
| `ImagePullBackOff` on an NRP GitLab image | The CI build has not finished, or the project is private: the user creates a pull secret from a deploy token (`references/images.md`) |
| `exec format error` | The image was built for arm64 (an Apple Silicon Mac). Rebuild with `--platform linux/amd64` |
| Connection to a service in another namespace times out | Namespaces are isolated; the other namespace needs a NetworkPolicy allowing yours |

## Files

- `assets/templates/job-run.yaml` — one Job: clone the code, mount a PVC for data and results.
- `assets/templates/job-sweep.yaml` — Indexed Job that packs several tasks into each pod.
- `assets/templates/gitlab-ci-kaniko.yml` — the NRP docs' `.gitlab-ci.yml` that builds the project's Dockerfile.
- `references/images.md` — choosing GitLab CI or local docker, building, private registries, arm64.

## Docs

- Jobs: https://nrp.ai/documentation/userdocs/running/jobs/
- Policies: https://nrp.ai/documentation/userdocs/start/policies/
- GitLab and image builds: https://nrp.ai/documentation/userdocs/development/gitlab/
- Private images: https://nrp.ai/documentation/userdocs/development/private-repos/
- Storage: https://nrp.ai/documentation/userdocs/storage/intro/
