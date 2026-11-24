# NRP Usage Policies

Read this when you need the exact rule, its source, or what the cluster
enforces by itself. Policy text: https://nrp.ai/documentation/userdocs/start/policies/.
Admission behaviour was checked on the live cluster on 2026-11-15 with
server-side dry runs.

## Contents

- Requests and limits
- What admission enforces
- Utilization thresholds
- Workload types
- Many pods, many Jobs, CronJobs
- Priority classes
- Taints and tolerations
- GPUs
- Large CPU-only work
- Storage and data
- Checklist before `kubectl apply`

## Requests and limits

- Every container needs CPU and memory requests and limits.
- In Jobs and pods, each limit is within **20%** of its request. Above about
  **100** pods or Jobs, limits equal requests.
- Deployments for idle services are the exception: minimal requests with
  sensible limits (Burstable QoS).
- A container over its memory limit is OOMKilled; over its CPU limit it is
  throttled. Whole cores with `requests == limits` avoid throttling surprises.
- Scratch on `emptyDir` above 50Gi per container needs an `ephemeral-storage`
  request, or the pod can be evicted.

## What admission enforces

`kubectl apply --dry-run=server -f <file>` runs all of this without creating
anything. A denial prints `admission webhook ... denied the request`; a
warning prints `Warning: [...]` and the object is still created. Treat
warnings as errors to fix.

| Check | Applies to | Result |
|---|---|---|
| CPU and memory requests and a memory limit are set | Job, Deployment | denied (Gatekeeper `container-must-have-limits-and-requests`) |
| Limit/request ratio at most 1.2 | Job | warning (`container-must-meet-memory-and-cpu-ratio`) |
| Requests and limits on bare pods | Pod | warning |
| `CUDA_VISIBLE_DEVICES` set | any workload | denied (`block-cuda-visible-devices`) |
| A toleration without a key | Pod | denied by the nautilus webhook: `Need toleration key` |
| Tolerating `nautilus.io/system` or the control plane | Pod | denied (`forbidden-toleration-system`) |
| A priority class outside the allowed set | Pod | denied: `exceeded quota: low-priority-ban` or `high-priority-ban` |
| DaemonSet, LoadBalancer or NodePort Service, `nodeName` | those objects | denied |
| Linstor PVC above 512 Gi (64 Gi for `linstor-ha`) | PVC | denied |
| Bare pod | Pod | `activeDeadlineSeconds: 21600` is added |
| Job without `ttlSecondsAfterFinished` | Job | set to 86400 |
| `kubernetes.io/arch: arm64` selected | Pod | `nautilus.io/hardware=arm64` toleration is added |

Nothing on the cluster limits ConfigMap size, the number of ConfigMaps, or
how fast pods are created. Those rules are yours to follow; `scripts/nrp_preflight.py`
checks them before you apply.

## Utilization thresholds

| Resource | Target, as a share of the request |
|---|---|
| GPU | above 40% (ideally near 100%) |
| CPU | 20–200% |
| Memory | 20–150% (read the RSS column in Grafana, not usage with cache) |

- A user may have at most **4 pods** outside these bounds.
- Pods requesting at most 1 CPU core and 2 GB of memory are exempt.
- Namespaces whose requests are consistently unused get banned.
- Your pods' status: https://nrp.ai/userinfo. Dashboards: https://grafana.nrp-nautilus.io.

## Workload types

| Type | Rules |
|---|---|
| **Bare pod** (no controller) | Killed after **6 hours** unless the namespace has an exception. At most 2 GPUs, 16 CPU cores, 32 GB RAM. `sleep` is allowed. |
| **Job** | The way to run computation. The command must end by itself: `sleep infinity`, or a script that ends in a sleep, **leads to a ban**. |
| **Deployment** | For a long-running, mostly idle pod: minimal requests, no GPU. Deleted after **2 weeks** unless the namespace is on the exception list. Stop it without deleting it: `kubectl scale deployment <name> --replicas=0`. |

- An exception for a permanent service is requested in NRP support, with a
  description and how long it will run. Long idle pods do not get one.
- Never run an idle pod as a Job: the cleaner does not remove Jobs, so it
  stays forever.

## Many pods, many Jobs, CronJobs

- Nautilus has **no fair queue**. Do not submit more than **400 Jobs at once**;
  about 1,000 block other users.
- One Indexed Job with a `parallelism` cap replaces many Jobs. For work that
  arrives over time, a queue (Redis) with a fixed pool of workers.
- Short pods are the expensive kind. In the week before 2026-11-15 the
  cluster created about 2,100 pods an hour; 57% of the finished ones ran for
  under a minute. If one task takes seconds, give each pod a slice of many
  tasks (see the `nrp-run` skill).
- A CronJob that fires every minute creates 1,440 pods a day. About a third
  of all pods on Nautilus came from CronJobs on 1 to 10 minute schedules.
  For a check that runs every few minutes, run one Deployment whose process
  sleeps between checks. NRP has not set a numeric limit; the most common
  schedule on the cluster is every 10 minutes.

## Priority classes

Leave `priorityClassName` unset. The only values users may set:

| Class | Use |
|---|---|
| `opportunistic`, `opportunistic2` | Bypasses the GPU quotas (A100, H100, H200, GH200) and suits large CPU-only work; the pod **can be preempted at any time** |
| `armada-default`, `owner-no-preempt` | Accepted by admission, not needed for ordinary work |

Anything else (`default`, `owner`, `nice`, `batch-low`, `system-*`, ...) is
rejected. If a tool adds one, remove the line.

## Taints and tolerations

By default you may use only nodes without taints.

| Taint key | May a user tolerate it? |
|---|---|
| `nautilus.io/hardware` (`arm64`, `large-gpu`) | Yes when the work needs that hardware. `arm64` is added for you when you select the architecture; `large-gpu` is added for 4- and 8-GPU Jobs. |
| `nautilus.io/reservation=<group>` | Only members of the approved group. Ask the user whether NRP approved their group for this reservation; add it if they say yes, leave it out if they do not know. |
| `nautilus.io/science-dmz` | Yes, if the work needs no public internet. |
| `nautilus.io/system`, `nautilus.io/issue` | Never. |

To run **only** on your group's reserved nodes, add node affinity on
`nautilus.io/reservation` as well as the toleration. Unauthorized
tolerations get a user banned.

## GPUs

- Request a GPU only if the code uses it, and more than one only when one is near 100% used.
- More than **50 GPUs**: present the plan in NRP support first.
- A100 quota is **0** for every namespace by default; request access at https://nrp.ai/reservations.
- H100, H200 and GH200 are not requestable; they are reachable only as `opportunistic`.

## Large CPU-only work

Nautilus is mostly used for GPU work. For large CPU-only Jobs, set
`priorityClassName: opportunistic` and keep off the GPU nodes:

```yaml
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: feature.node.kubernetes.io/pci-10de.present
                operator: NotIn
                values: ["true"]
```

## Storage and data

- Not an archive. Delete what you do not need; a volume not accessed for
  **6 months** can be purged without notice.
- Not suitable for HIPAA, PID, FISMA, FERPA or other protected data.
- Non-commercial use only (Acceptable Use Policy).
- Acknowledge NRP in papers as the AUP specifies.

## Checklist before `kubectl apply`

1. Right context and namespace.
2. `python3 scripts/nrp_preflight.py <file>` reports no `BLOCK`.
3. `kubectl apply --dry-run=server -f <file>` prints no denial and no warning.
4. A Job, not a Deployment, for computation; the command ends by itself.
5. Requests match what the code uses; no GPU for CPU work.
6. Many tasks: one Indexed Job, a `parallelism` cap, several tasks per pod when a task is short.
7. No file contents in a ConfigMap or Secret.
8. The user has seen the manifest and agreed.
