---
name: nrp
description: >-
  National Research Platform (NRP) Nautilus Kubernetes cluster: access, rules
  and policies. Use before creating or changing anything on NRP with kubectl
  (Job, pod, Deployment, CronJob, ConfigMap, PVC), when setting up access
  (CILogon, kubelogin, nrp.ai/config, namespaces), when choosing GPUs
  (gpu.product affinity, nvidia.com/a100, opportunistic) or Ceph storage and
  S3, and when an apply is rejected by Gatekeeper, a quota such as
  low-priority-ban, or the nautilus admission webhook ("Need toleration key").
compatibility: Requires kubectl and the kubelogin (oidc-login) plugin on the local machine, an NRP account, and membership in an NRP namespace. No login node or SSH is involved. scripts/nrp_preflight.py needs Python 3.9+.
metadata:
  version: "2.0"
  system: nautilus
  facility: nrp
  scheduler: kubernetes
  checked: "2026-11-15"
allowed-tools: Bash(kubectl get *) Bash(kubectl describe *) Bash(kubectl logs *) Bash(kubectl config *) Bash(kubectl auth can-i *) Bash(kubectl top *) Read(/${CLAUDE_SKILL_DIR}/**)
---

# NRP Nautilus — National Research Platform

Nautilus is a nationally distributed Kubernetes cluster shared by thousands of
researchers. There is **no login node, no batch scheduler, no allocation and
no billing**: you submit Kubernetes objects with `kubectl` from your own
machine into a **namespace**, and usage policies take the place of a bill.
Everything you request is unavailable to others until it is released, and
every object you create lives in one control-plane database (etcd) that the
whole cluster shares.

Related skills: `nrp-run` (take a user's code to a running Job), `nrp-llm`
(the managed LLM endpoint), `kubernetes` (general Kubernetes for people coming
from Slurm or PBS).

## Hard rules

These are the ones that take the cluster down for everyone or get a user
banned. Each has a safe alternative; use it instead of looking for a way
around the rule.

1. **No code, data or files in a ConfigMap or Secret.** They are stored in
   etcd. On 2026-11-15, ConfigMaps in user namespaces held 2.8 GiB of a 6 GiB
   etcd, almost all created with `kubectl create configmap --from-file`. A
   ConfigMap is for a few KB of settings. Code comes from a git repository
   (init container) or a container image; input data goes on a PVC or in S3.
   Never split a file across several ConfigMaps, and never switch from
   `kubectl apply` to `kubectl create` or `--server-side` because apply said
   `metadata.annotations: Too long`.
2. **A Job's command must end by itself.** `sleep infinity`, `tail -f
   /dev/null`, or a script that ends in a sleep, in a Job, gets the user
   banned. For an interactive shell use a bare pod (deleted after 6 hours);
   for a long-lived idle service use a Deployment with minimal requests.
3. **No floods of short pods.** 57% of the pods that finished on Nautilus in
   the week before 2026-11-15 ran for under a minute. Each one costs
   scheduling, image setup and etcd writes. Give a pod minutes of work, not
   seconds: pack many small tasks into each pod of one Indexed Job with a
   `parallelism` cap. Never loop `kubectl create job`, never submit more than
   400 Jobs at once, and do not use a CronJob that fires every few minutes
   where one long-lived Deployment that loops would do. Many namespaces also
   allow at most 200 pods (`kubectl get resourcequota -n <namespace>`); keep
   the pods running at once under the namespace's cap.
4. **Request what the work uses, then release it.** Every container sets CPU
   and memory requests and limits; in Jobs and pods each limit is within 20%
   of its request (use `requests == limits`, required above ~100 pods).
   Requested GPUs must be used: above 40% utilization. Delete Jobs, pods and
   PVCs when the work is done.
5. **Tolerate only a taint you are entitled to, by key.** A toleration with
   `operator: Exists` and no `key` is rejected (`Need toleration key`).
   `nautilus.io/system` and `nautilus.io/issue` are never allowed;
   `nautilus.io/reservation` only for a group NRP admins approved: ask the
   user, and add it only if they confirm. If a pod
   is `Pending` on an untolerated taint, tell the user which taint.
6. **Never force-delete** (`kubectl delete --force --grace-period=0`). A pod
   stuck `Terminating` needs an admin: ask in NRP support.
7. **Never set `CUDA_VISIBLE_DEVICES`** (rejected), and set
   `priorityClassName` only to `opportunistic` or `opportunistic2`, or not at
   all. Other classes fail with `exceeded quota: low-priority-ban` or
   `high-priority-ban`.
8. **No protected data.** NRP is not suitable for HIPAA, PID, FISMA or FERPA
   data, is not an archive, and is for non-commercial use.

## Before every change to the cluster

1. Confirm the context and namespace:
   `kubectl config current-context` and
   `kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'`.
   No output means the context sets no namespace: ask the user which one and
   pass `-n <namespace>` on every command.
2. Run the preflight on the manifest. It creates nothing:
   `python3 scripts/nrp_preflight.py job.yaml`. Fix every `BLOCK`; read every
   `WARN`. It also checks a command line:
   `python3 scripts/nrp_preflight.py --command 'kubectl create configmap ...'`.
3. Run the cluster's own admission policies without creating anything:
   `kubectl apply --dry-run=server -f job.yaml`. Fix denials and `Warning:` lines.
4. Show the user the manifest and what it requests (CPU, memory, GPUs, how
   many pods, for how long). Apply only after they agree.
5. After it starts, check it uses what it requested (`kubectl top pod`,
   `kubectl exec <pod> -- nvidia-smi`), and clean up when it is done.

If an apply is rejected, fix the cause the message names. Do not rephrase the
command, change the verb, or split the object to get past a check.

## What NRP does to your workload

Checked with server-side dry runs on 2026-11-15 (`validation/nrp-admission`
in the marketplace repository has the transcript).

| You create | NRP does |
|---|---|
| A pod with no controller | Adds `activeDeadlineSeconds: 21600`: killed after **6 hours**. Limited to 2 GPUs, 16 CPU cores, 32 GB RAM. `sleep` is allowed here. |
| A Job without `ttlSecondsAfterFinished` | Sets it to 86400: the Job, its pods and their logs are deleted **1 day** after it finishes. Write results to a PVC or S3. |
| A Deployment | Deletes it after **2 weeks** (3 notices first) unless the namespace has an exception. It cannot request GPUs. Volumes stay. |
| A Job or Deployment without CPU and memory requests and a memory limit | Rejects it. |
| A Job with a limit above 1.2x its request | Warns; it is still a policy violation. |
| A pod selecting `kubernetes.io/arch: arm64` | Adds the `nautilus.io/hardware=arm64` toleration for you. |
| A DaemonSet, a LoadBalancer or NodePort Service, `nodeName` | Rejects it. Expose HTTP with an Ingress. |

Pods in other namespaces cannot connect to yours unless a NetworkPolicy in
your namespace allows them; the connection times out.

## Access

1. Log in at https://nrp.ai with your institution through CILogon and accept the Acceptable Use Policy.
2. Get a namespace. Students: ask a supervisor to add you. Faculty, staff, postdocs: request one at https://get-started.nrp-nautilus.io. Memberships: https://nrp.ai/namespaces.
3. Install `kubectl` and the `kubelogin` plugin (`kubectl-oidc_login` on the `PATH`).
4. Get the kubeconfig and set the namespace:

```bash
mkdir -p ~/.kube && curl -fsSL -o ~/.kube/nautilus.config https://nrp.ai/config   # merge it: see references/access.md
kubectl config use-context nautilus
kubectl get nodes                       # opens the CILogon login in a browser
kubectl config set contexts.nautilus.namespace <namespace>
kubectl auth can-i create jobs          # yes
```

The token lasts about 30 minutes and renews itself. After joining a namespace
run `kubectl oidc-login clean`. No browser on this machine, merging with an
existing kubeconfig, and a ServiceAccount for an agent: `references/access.md`.

## Usage policies

| Rule | Value |
|---|---|
| Utilization of what you request | GPU above 40%; CPU 20–200%; memory 20–150% |
| Pods allowed outside those bounds | 4 per user; pods requesting at most 1 CPU and 2 GB are exempt |
| GPUs | Up to 8 per Job pod (one node), 2 per bare pod; more than 1 only when one is near 100% used |
| Large plans | More than 50 GPUs: present the plan in NRP support first |
| Large CPU-only work | `priorityClassName: opportunistic` and keep off GPU nodes (`references/policies.md`) |
| Storage | Not an archive; a volume not accessed for 6 months can be purged without notice |
| Fair share | None. Nothing queues your work behind others', so size and pace it yourself |

Violations show at https://nrp.ai/userinfo. Namespaces with consistently
unused requests get banned. Details: `references/policies.md`.

## GPUs

```yaml
        resources:
          requests: {cpu: "4", memory: 16Gi, nvidia.com/gpu: 1}
          limits:   {cpu: "4", memory: 16Gi, nvidia.com/gpu: 1}
```

Choose a model with node affinity on `nvidia.com/gpu.product`; list what
exists now with `kubectl get nodes -L nvidia.com/gpu.product`. A100, A40, RTX
A6000, RTX 8000, H100, H200 and GH200 have their own resource names
(`nvidia.com/a100`, ...). A100, H100, H200 and GH200 have a namespace quota of
0: A100 access is requested at https://nrp.ai/reservations, and any of the
four runs without quota under `priorityClassName: opportunistic`, where the pod
**can be preempted at any time** (checkpoint, and use a Job). Details:
`references/gpus.md`.

## Storage

| Need | Use |
|---|---|
| Environments, many small files, databases (one pod) | Block PVC, `ReadWriteOnce`: `rook-ceph-block` and its regional variants |
| Large files, checkpoints, data shared by many pods | CephFS PVC, `ReadWriteMany`: `rook-cephfs` and its regional variants. No conda or pip installs on it |
| Data exchange with your machine, very large datasets | Ceph S3: `https://s3-west.nrp-nautilus.io` (also `s3-central`, `s3-east`) |
| Scratch inside a pod | `emptyDir`; above 50Gi set an `ephemeral-storage` request or the pod is evicted |

Put compute in the region of its storage (`topology.kubernetes.io/region`).
Delete many files with `find <dir> -type f -delete`, not `rm -rf`. Storage
classes, S3 clients and moving files in and out: `references/storage.md`.

## Templates

`assets/templates/` holds NRP-ready manifests (requests equal to limits, a
deadline, a TTL): `job-cpu.yaml`, `job-gpu.yaml`, `job-indexed.yaml`,
`job-git.yaml`, `pvc-cephfs.yaml`, `pod-interactive.yaml`.

## Help

- Usage and violations: https://nrp.ai/userinfo. Grafana: https://grafana.nrp-nautilus.io
- Live resources: https://nrp.ai/viz/resources/. Docs: https://nrp.ai/documentation/
- Support (Matrix): https://nrp.ai/contact/

## Reference files

- `references/access.md` — accounts, namespaces, kubeconfig, kubelogin, a ServiceAccount for an agent.
- `references/policies.md` — every policy with its source, what admission enforces, taints, priority classes.
- `references/gpus.md` — GPU selection, special resources, opportunistic priority, arm64, CUDA labels.
- `references/storage.md` — storage classes by region, S3, getting files in and out.
- `scripts/nrp_preflight.py` — run it on every manifest before applying (`--rules` lists what it checks).
