#!/usr/bin/env python3
"""NRP preflight: check Kubernetes manifests and kubectl commands against NRP Nautilus rules
before anything reaches the cluster.

    nrp_preflight.py job.yaml [more.yaml ...]       check manifest files (YAML or JSON)
    nrp_preflight.py --command 'kubectl create configmap code --from-file=src/'
    nrp_preflight.py --hook                         Claude Code PreToolUse hook (JSON on stdin)
    nrp_preflight.py --rules                        list the rules and their limits

Output: one finding per line, `LEVEL [rule] Kind/name: message`, then `RESULT=ok|warn|block`.
Exit codes: 0 no BLOCK finding, 1 usage or parse error, 2 at least one BLOCK finding.

It creates nothing and changes nothing. It is not a substitute for
`kubectl apply --dry-run=server -f <file>`, which runs the cluster's own admission policies.

Settings (environment):
    NRP_PREFLIGHT_ALLOW       comma-separated rule names to downgrade from BLOCK to WARN, for a
                              namespace with an exception from NRP support (e.g. deployment-gpu)
    NRP_PREFLIGHT_CONTEXTS    kubectl context names treated as NRP in --hook mode (default: nautilus)
    NRP_PREFLIGHT_ASSUME_NRP  set to 1 to check every kubectl command (in-cluster agents, which
                              have no kubeconfig context)

Standard library only. YAML is read with PyYAML when it is installed, otherwise through
`kubectl create --dry-run=client -o json`.
"""
import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

KIB = 1024

# Every limit below has a source. Ones marked PROVISIONAL are not NRP policy yet.
LIMITS = {
    # `kubectl apply` stores a copy of the object in an annotation, and Kubernetes caps
    # annotations at 262144 bytes, so plain `kubectl apply` already refuses a larger ConfigMap.
    # Anything bigger was pushed past that error. PROVISIONAL as an NRP limit.
    "configmap_block_bytes": 256 * KIB,
    # 99% of the ConfigMaps written by Helm, operators and controllers in Nautilus user
    # namespaces hold less than this (census of 2026-11-15, 2,639 objects). PROVISIONAL.
    "configmap_warn_bytes": 113 * KIB,
    # NRP docs, Using Nautilus: "Do not submit more than 400 Jobs at once."
    "jobs_at_once": 400,
    # 718 of 2,425 Nautilus namespaces, nearly all made before 2026, have a ResourceQuota
    # `reached-quota` with `pods: 200` (census of 2026-11-24). Pods above it are refused and the
    # Job controller retries them. This script cannot see a namespace's quota, so it only warns.
    "namespace_pod_quota": 200,
    # NRP policies, Resource allocation: limits within 20% of requests.
    "limit_ratio": 1.2,
    # NRP policies: with more than ~100 pods or Jobs, limits must equal requests.
    "guaranteed_above_pods": 100,
    # NRP policies, Interactive use: a pod without a controller gets at most this.
    "bare_pod_gpus": 2,
    "bare_pod_cpu": 16,
    "bare_pod_memory_bytes": 32 * 1024**3,
    # The most common CronJob schedule on Nautilus is every 10 minutes (52 of 261, 2026-11-15);
    # six run every minute and create 1,440 pods a day each. PROVISIONAL.
    "cronjob_min_interval_minutes": 10,
}
ALLOWED_PRIORITY_CLASSES = {"", "armada-default", "owner-no-preempt", "opportunistic", "opportunistic2"}
FORBIDDEN_TOLERATIONS = {
    "nautilus.io/system", "nautilus.io/issue",
    "node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master",
}
RULES = {
    "configmap-size": "No code, data or files in a ConfigMap or Secret (shared etcd). Code: git or an image. Data: PVC or S3.",
    "configmap-split": "Never split a file across several ConfigMaps to get under a size limit.",
    "job-never-ends": "A Job's command must end by itself: no `sleep infinity`, no script ending in sleep (NRP bans for this).",
    "toleration": "Tolerate only a specific taint you are entitled to, by key. Never `operator: Exists` without a key.",
    "priority-class": "Only unset, armada-default, owner-no-preempt, opportunistic or opportunistic2.",
    "resources": "Every container sets CPU and memory requests and limits; Job and pod limits within 20% of requests.",
    "cuda-visible-devices": "Never set CUDA_VISIBLE_DEVICES; the device plugin assigns GPUs.",
    "deployment-gpu": "Deployments cannot request GPUs. Use a Job.",
    "bare-pod-size": "A pod without a controller: at most 2 GPUs, 16 CPU cores, 32 GB RAM, and it is deleted after 6 hours.",
    "many-pods": "Hundreds of pods: limits equal requests, a `parallelism` cap, and several short tasks packed into each pod.",
    "jobs-at-once": "No more than 400 Jobs at once. One Indexed Job replaces many Jobs.",
    "pods-quota": "Many namespaces allow at most 200 pods (ResourceQuota `reached-quota`; check with "
                  "`kubectl get resourcequota -n <namespace>`). Pods above the cap are refused and retried. Keep "
                  "the pods running at once under it: lower `parallelism`, or give each pod more tasks.",
    "cronjob-frequency": "A CronJob that fires every few minutes creates thousands of short pods. Run one long-lived Deployment that loops.",
    "blocked-kind": "DaemonSets, LoadBalancer and NodePort Services, and `nodeName` are rejected by NRP admission policy.",
    "force-delete": "Never force-delete (`--force`, `--grace-period=0`): the pod's resources stay stuck on the node.",
    "loop-create": "A shell loop that creates one object per iteration floods the cluster. Use one Indexed Job.",
    "not-inspected": "The manifest could not be read from the command; check the file with this script first.",
}


class Findings:
    def __init__(self):
        self.items = []
        self.allow = {r.strip() for r in os.environ.get("NRP_PREFLIGHT_ALLOW", "").split(",") if r.strip()}

    def add(self, level, rule, where, message):
        if level == "BLOCK" and rule in self.allow:
            level, message = "WARN", message + " (allowed by NRP_PREFLIGHT_ALLOW)"
        self.items.append((level, rule, where, message))

    def block(self, rule, where, message):
        self.add("BLOCK", rule, where, message)

    def warn(self, rule, where, message):
        self.add("WARN", rule, where, message)

    @property
    def blocked(self):
        return any(i[0] == "BLOCK" for i in self.items)

    def lines(self):
        return [f"{lvl} [{rule}] {where}: {msg}" for lvl, rule, where, msg in self.items]

    def result(self):
        return "block" if self.blocked else "warn" if self.items else "ok"


# ---------------------------------------------------------------- reading manifests

def human(n):
    return f"{n / KIB / KIB:.1f} MiB" if n >= KIB * KIB else f"{n / KIB:.0f} KiB"


def flatten(docs):
    out = []
    for d in docs:
        if isinstance(d, dict) and d.get("kind", "").endswith("List") and isinstance(d.get("items"), list):
            out += flatten(d["items"])
        elif isinstance(d, dict) and d.get("kind"):
            out.append(d)
    return out


def parse_json_stream(text):
    dec, i, docs = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return docs
        obj, i = dec.raw_decode(text, i)
        docs.append(obj)


def load_manifest_text(text, label="manifest"):
    """Return the list of objects in a YAML or JSON manifest."""
    if text.lstrip().startswith(("{", "[")):
        docs = parse_json_stream(text)
        return flatten(docs[0] if len(docs) == 1 and isinstance(docs[0], list) else docs)
    try:
        import yaml  # optional
        return flatten([d for d in yaml.safe_load_all(text) if d])
    except ImportError:
        pass
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
    try:
        run = subprocess.run(
            ["kubectl", "create", "--dry-run=client", "--validate=false", "-o", "json", "-f", fh.name],
            capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(fh.name)
    if run.returncode != 0:
        raise ValueError(f"cannot read {label}: {run.stderr.strip()[:300]} (install PyYAML, or check kubectl access)")
    return flatten(parse_json_stream(run.stdout))


def load_path(path):
    p = Path(path)
    files = sorted(f for f in p.rglob("*") if f.suffix in (".yaml", ".yml", ".json")) if p.is_dir() else [p]
    objs = []
    for f in files:
        objs += load_manifest_text(f.read_text(), str(f))
    return objs


# ---------------------------------------------------------------- manifest checks

def cpu(q):
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


MEM_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def mem(q):
    q = str(q)
    for unit in sorted(MEM_UNITS, key=len, reverse=True):
        if q.endswith(unit):
            return float(q[: -len(unit)]) * MEM_UNITS[unit]
    return float(q)


def quantity(parse, value):
    """A CPU or memory quantity as a number, or None when it cannot be read (a template placeholder, a typo)."""
    try:
        return parse(value)
    except (ValueError, TypeError):
        return None


def whole(value, default=1):
    """A count such as parallelism or completions; `default` when it is absent or not a number."""
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


def pod_template(obj):
    """(pod spec, pods this object can run at once) for workload kinds, else (None, 0)."""
    kind, spec = obj.get("kind"), obj.get("spec") or {}
    if kind == "Pod":
        return spec, 1
    if kind == "Job":
        return (spec.get("template") or {}).get("spec") or {}, whole(spec.get("parallelism"))
    if kind == "CronJob":
        job = (spec.get("jobTemplate") or {}).get("spec") or {}
        return (job.get("template") or {}).get("spec") or {}, whole(job.get("parallelism"))
    if kind in ("Deployment", "StatefulSet", "ReplicaSet", "DaemonSet"):
        return (spec.get("template") or {}).get("spec") or {}, whole(spec.get("replicas"))
    return None, 0


def payload_bytes(obj):
    n = sum(len(str(v).encode()) for v in (obj.get("data") or {}).values()) if obj["kind"] == "ConfigMap" else 0
    n += sum(len(v) * 3 // 4 for v in (obj.get("binaryData") or {}).values())
    if obj["kind"] == "Secret":
        n += sum(len(v) * 3 // 4 for v in (obj.get("data") or {}).values())
        n += sum(len(str(v).encode()) for v in (obj.get("stringData") or {}).values())
    return n


NEVER_ENDS = [
    (r"\bsleep\s+inf(inity)?\b", "runs `sleep infinity`"),
    (r"\btail\s+(-f|--follow)\s+/dev/null\b", "runs `tail -f /dev/null`"),
    (r"(^|[;&|\n])\s*sleep\s+\S+\s*;?\s*$", "ends in a sleep"),
]


def cron_interval_minutes(schedule):
    """Shortest gap in minutes for the common `*` and `*/N` minute fields; None when not that simple."""
    f = schedule.split()
    if len(f) != 5 or f[1:] != ["*", "*", "*", "*"]:
        return None
    if f[0] == "*":
        return 1
    m = re.fullmatch(r"(?:\*|\d+-\d+)/(\d+)", f[0])
    return int(m.group(1)) if m else None


def check_objects(objs, found):
    name = lambda o: f"{o.get('kind')}/{(o.get('metadata') or {}).get('name', '?')}"

    # --- ConfigMaps and Secrets
    stores = [o for o in objs if o.get("kind") in ("ConfigMap", "Secret")
              and not str(o.get("type", "")).startswith("helm.sh/")]
    for o in stores:
        size = payload_bytes(o)
        if size > LIMITS["configmap_block_bytes"]:
            found.block("configmap-size", name(o),
                        f"holds {human(size)}. {RULES['configmap-size']} Never work around this by splitting it, "
                        f"switching to `kubectl create` or `--server-side`.")
        elif size > LIMITS["configmap_warn_bytes"]:
            found.warn("configmap-size", name(o),
                       f"holds {human(size)}, more than 99% of real configuration on Nautilus. If this is code or "
                       f"data, use git, an image, a PVC or S3 instead.")
    groups = {}
    for o in stores:
        stem = re.sub(r"([-_.]?(part|chunk|piece|split)?[-_.]?\d+)$", "", (o.get("metadata") or {}).get("name", ""))
        groups.setdefault((o["kind"], stem), []).append(o)
    for (kind, stem), members in groups.items():
        total = sum(payload_bytes(o) for o in members)
        if len(members) > 1 and total > LIMITS["configmap_block_bytes"]:
            found.block("configmap-split", f"{kind}/{stem}*",
                        f"{len(members)} objects with numbered names hold {human(total)} together. {RULES['configmap-split']}")

    # --- workloads
    jobs = [o for o in objs if o.get("kind") == "Job"]
    if len(jobs) > LIMITS["jobs_at_once"]:
        found.block("jobs-at-once", f"{len(jobs)} Jobs", RULES["jobs-at-once"])
    else:
        at_once_total = sum(n for pod, n in map(pod_template, objs) if pod is not None)
        if at_once_total > LIMITS["namespace_pod_quota"]:
            found.warn("pods-quota", f"{at_once_total:,} pods at once", RULES["pods-quota"])

    for o in objs:
        kind, spec = o.get("kind"), o.get("spec") or {}
        if kind == "DaemonSet":
            found.block("blocked-kind", name(o), RULES["blocked-kind"])
        if kind == "Service" and spec.get("type") in ("LoadBalancer", "NodePort"):
            found.block("blocked-kind", name(o), f"Service type {spec['type']}. {RULES['blocked-kind']} Use an Ingress.")
        if kind == "CronJob":
            gap = cron_interval_minutes(str(spec.get("schedule", "")))
            if gap is not None and gap < LIMITS["cronjob_min_interval_minutes"]:
                found.warn("cronjob-frequency", name(o),
                           f"runs every {gap} min, {round(1440 / gap):,} pods a day. {RULES['cronjob-frequency']}")

        pod, at_once = pod_template(o)
        if pod is None:
            continue
        if pod.get("nodeName"):
            found.block("blocked-kind", name(o), f"sets nodeName. {RULES['blocked-kind']} Use node affinity.")
        if pod.get("priorityClassName", "") not in ALLOWED_PRIORITY_CLASSES:
            found.block("priority-class", name(o),
                        f"priorityClassName {pod['priorityClassName']!r} is rejected on NRP. {RULES['priority-class']}")
        for t in pod.get("tolerations") or []:
            key = t.get("key", "")
            if not key:
                found.block("toleration", name(o), f"a toleration without a key tolerates every taint. {RULES['toleration']}")
            elif key in FORBIDDEN_TOLERATIONS:
                found.block("toleration", name(o), f"tolerates {key}, which users may never tolerate.")
            elif key == "nautilus.io/reservation":
                found.warn("toleration", name(o),
                           f"tolerates reservation {t.get('value', '')!r}: allowed only if NRP admins approved this group for it.")

        containers = (pod.get("initContainers") or []) + (pod.get("containers") or [])
        gpus = cpus = memory = 0.0
        guaranteed = True
        for c in containers:
            res = c.get("resources") or {}
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            where = f"{name(o)} container {c.get('name', '?')}"
            if any(e.get("name") == "CUDA_VISIBLE_DEVICES" for e in c.get("env") or []):
                found.block("cuda-visible-devices", where, RULES["cuda-visible-devices"])
            missing = [f"{side}.{r}" for side, d, r in (("requests", req, "cpu"), ("requests", req, "memory"),
                                                        ("limits", lim, "memory")) if r not in d]
            if missing:
                level = found.warn if kind == "Pod" else found.block
                level("resources", where, f"missing {', '.join(missing)}. {RULES['resources']}")
            if "cpu" not in lim:
                found.warn("resources", where, "no CPU limit; NRP policy asks for limits on every container.")
            unread = [f"{side}.{r}: {d[r]!r}" for side, d in (("requests", req), ("limits", lim))
                      for r, parse in (("cpu", cpu), ("memory", mem)) if r in d and quantity(parse, d[r]) is None]
            if unread:
                found.warn("not-inspected", where,
                           f"cannot read {', '.join(unread)}. If this is a template, check the manifest that will really "
                           f"be applied, after the values are filled in.")
            for r, parse in (("cpu", cpu), ("memory", mem)):
                if r in req and r in lim:
                    low, high = quantity(parse, req[r]), quantity(parse, lim[r])
                    if low is None or high is None:
                        guaranteed = False
                        continue
                    ratio = high / low if low else float("inf")
                    guaranteed = guaranteed and abs(ratio - 1) < 1e-9
                    if ratio > LIMITS["limit_ratio"] + 1e-9 and kind in ("Pod", "Job", "CronJob"):
                        found.warn("resources", where,
                                   f"{r} limit {lim[r]} is {ratio:.1f}x the request {req[r]}; NRP allows 1.2x.")
                else:
                    guaranteed = False
            if c in (pod.get("containers") or []):
                gpus += sum(quantity(float, v) or 0 for k, v in {**req, **lim}.items() if k.startswith("nvidia.com/"))
                cpus += quantity(cpu, lim.get("cpu", req.get("cpu", 0)) or 0) or 0
                memory += quantity(mem, lim.get("memory", req.get("memory", 0)) or 0) or 0
                if kind in ("Job", "CronJob"):
                    text = " ".join(str(x) for x in (c.get("command") or []) + (c.get("args") or [])).strip()
                    for pattern, what in NEVER_ENDS:
                        if re.search(pattern, text):
                            found.block("job-never-ends", where, f"the command {what}. {RULES['job-never-ends']}")
                            break

        if kind in ("Deployment", "StatefulSet") and gpus:
            found.block("deployment-gpu", name(o), RULES["deployment-gpu"] if kind == "Deployment" else
                        "long-running controllers cannot hold GPUs on NRP without an exception. Use a Job.")
        if kind == "Pod" and (gpus > LIMITS["bare_pod_gpus"] or cpus > LIMITS["bare_pod_cpu"]
                              or memory > LIMITS["bare_pod_memory_bytes"]):
            found.block("bare-pod-size", name(o), RULES["bare-pod-size"] + " Use a Job for more.")
        if kind == "Job":
            completions = whole(spec.get("completions"))
            if at_once > LIMITS["guaranteed_above_pods"] and not guaranteed:
                found.block("many-pods", name(o),
                            f"parallelism {at_once}: above ~100 pods NRP requires limits equal to requests.")
            if completions >= LIMITS["jobs_at_once"]:
                found.warn("many-pods", name(o),
                           f"creates {completions:,} pods ({at_once} at a time). If one task runs for less than a few "
                           f"minutes, give each pod a slice of many tasks instead of one.")


# ---------------------------------------------------------------- command checks

HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", re.S)
GLOBAL_FLAGS_WITH_VALUE = {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "-s", "--server",
                           "--as", "--as-group", "--request-timeout"}


def simple_commands(command):
    body = HEREDOC.sub(" ", command)
    for part in re.split(r"&&|\|\||[;|\n]", body):
        try:
            tokens = shlex.split(part, comments=True)
        except ValueError:
            tokens = part.split()
        while tokens and re.fullmatch(r"\w+=.*|sudo|time|exec|do|then|else", tokens[0]):
            tokens = tokens[1:]
        if tokens:
            yield tokens


def kubectl_call(tokens):
    """(verb, args, context) for a kubectl command, else None."""
    if Path(tokens[0]).name != "kubectl":
        return None
    args, context, i, rest = tokens[1:], None, 0, []
    while i < len(args):
        a = args[i]
        if a == "--context" and i + 1 < len(args):
            context, i = args[i + 1], i + 2
        elif a.startswith("--context="):
            context, i = a.split("=", 1)[1], i + 1
        elif a in GLOBAL_FLAGS_WITH_VALUE and not rest:
            i += 2
        else:
            rest.append(a)
            i += 1
    return (rest[0], rest[1:], context) if rest else None


def flag_values(args, *names):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        for n in names:
            if a == n and i + 1 < len(args):
                out.append(args[i + 1])
                i += 1
            elif a.startswith(n + "=") or (len(n) == 2 and a.startswith(n) and len(a) > 2 and not a.startswith("--")):
                out.append(a[len(n):].lstrip("="))
        i += 1
    return out


def path_bytes(spec):
    path = Path(os.path.expanduser(spec.split("=", 1)[1] if "=" in spec and not Path(spec).exists() else spec))
    if path.is_dir():
        return sum(f.stat().st_size for f in path.iterdir() if f.is_file())
    return path.stat().st_size if path.is_file() else 0


CREATE_VERBS = {"apply", "create", "run", "replace"}


def is_dry_run(args):
    return any(a.startswith("--dry-run") and a != "--dry-run=none" for a in args)


def creates_objects(shell):
    """True when a kubectl call in this piece of shell creates something. A dry run does not."""
    for tokens in simple_commands(shell):
        call = kubectl_call(tokens)
        if call and call[0] in CREATE_VERBS and not is_dry_run(call[1]):
            return True
    return False


def loop_counts(command):
    """One entry per shell loop or xargs call that creates objects: its iteration count, or None if unreadable.

    Heredoc bodies are left out first. A `for` in a manifest, or in a script that is only being written to a
    file, is not a shell loop, and neither is a loop whose body creates nothing.
    """
    shell = HEREDOC.sub(" ", command)

    def span(text):
        return [int(b) - int(a or 1) + 1 for a, b in re.findall(r"\bseq\s+(?:(\d+)\s+)?(\d+)\b", text)] + \
               [int(b) - int(a) + 1 for a, b in re.findall(r"\{(\d+)\.\.(\d+)\}", text)]

    counts = []
    for header, body in re.findall(r"\b(?:for|while|until)\b(.*?)\bdo\b(.*?)\bdone\b", shell, re.S):
        if creates_objects(body):
            counts.append(max(span(header), default=None))
    for pipeline in re.split(r"&&|\|\||[;\n]", shell):
        fed = re.search(r"\bxargs\b(.*)", pipeline, re.S)
        if fed and re.search(r"\bkubectl\b[^|]*?\b(apply|create|run|replace)\b", fed.group(1)) \
                and not re.search(r"--dry-run(?!=none)", fed.group(1)):
            counts.append(max(span(pipeline), default=None))
    return counts


def check_command(command, found):
    heredocs = [m.group(3) for m in HEREDOC.finditer(command)]
    counts = loop_counts(command)
    known = [c for c in counts if c is not None]
    if known and max(known) > LIMITS["jobs_at_once"]:
        found.block("jobs-at-once", "command", f"this loop creates {max(known):,} objects. {RULES['jobs-at-once']}")
    elif counts:
        found.warn("loop-create", "command", RULES["loop-create"] + " " + RULES["jobs-at-once"])
        if known and max(known) > LIMITS["namespace_pod_quota"]:
            found.warn("pods-quota", "command", f"this loop creates {max(known):,} objects. {RULES['pods-quota']}")
    for tokens in simple_commands(command):
        call = kubectl_call(tokens)
        if not call:
            continue
        verb, args, _ = call
        if any(a.startswith("--dry-run") and a != "--dry-run=none" for a in args):
            continue  # nothing is created; dry runs are how sizes and policies get checked
        if verb == "delete":
            if "--force" in args or "--force=true" in args:
                found.block("force-delete", "kubectl delete", RULES["force-delete"] +
                            " A pod stuck Terminating needs an admin: ask in NRP support.")
        elif verb == "create" and args and args[0] in ("configmap", "cm", "secret"):
            size = 0
            for spec in flag_values(args, "--from-file", "--from-env-file"):
                size += path_bytes(spec)
            size += sum(len(v.split("=", 1)[-1].encode()) for v in flag_values(args, "--from-literal"))
            where = f"kubectl create {args[0]}"
            if size > LIMITS["configmap_block_bytes"]:
                found.block("configmap-size", where, f"would hold {human(size)}. {RULES['configmap-size']}")
            elif size > LIMITS["configmap_warn_bytes"]:
                found.warn("configmap-size", where,
                           f"would hold {human(size)}, more than 99% of real configuration on Nautilus. "
                           f"Code belongs in git or an image, data on a PVC or in S3.")
        elif verb in ("apply", "create", "replace"):
            for target in [t for v in flag_values(args, "-f", "--filename") for t in v.split(",")]:
                try:
                    if target == "-":
                        if heredocs:
                            check_objects(load_manifest_text(heredocs.pop(0), "heredoc"), found)
                        else:
                            found.warn("not-inspected", f"kubectl {verb} -f -", RULES["not-inspected"])
                    elif re.match(r"https?://", target):
                        found.warn("not-inspected", f"kubectl {verb} -f {target}", RULES["not-inspected"])
                    elif Path(os.path.expanduser(target)).exists():
                        check_objects(load_path(os.path.expanduser(target)), found)
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    found.warn("not-inspected", f"kubectl {verb} -f {target}", str(exc)[:200])


def targets_nrp(command):
    """True when the kubectl calls in this command go to NRP. Unknown contexts are left alone."""
    if os.environ.get("NRP_PREFLIGHT_ASSUME_NRP") == "1":
        return True
    wanted = {c.strip() for c in os.environ.get("NRP_PREFLIGHT_CONTEXTS", "nautilus").split(",")}
    contexts = {call[2] for call in map(kubectl_call, simple_commands(command)) if call}
    if None in contexts:
        try:
            run = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=10)
            contexts.add(run.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return bool(contexts & wanted)


# ---------------------------------------------------------------- entry points

def run_hook():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    if event.get("tool_name") != "Bash" or "kubectl" not in command or not targets_nrp(command):
        return 0
    found = Findings()
    try:
        check_command(command, found)
    except Exception as exc:                              # a bug here must never stop someone's work, nor pass in silence
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"NRP preflight could not check this command ({type(exc).__name__}: {str(exc)[:200]}). "
                                 "It was not blocked. Check the manifest by hand against the nrp skill's rules.",
        }}))
        return 0
    if found.blocked:
        print("NRP preflight stopped this command:\n" + "\n".join(found.lines()) +
              "\nFix the cause; do not rephrase the command to get around the check.", file=sys.stderr)
        return 2
    if found.items:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "NRP preflight warnings for this command:\n" + "\n".join(found.lines()),
        }}))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("files", nargs="*", help="manifest files or directories")
    ap.add_argument("--command", help="a shell command line to check")
    ap.add_argument("--hook", action="store_true", help="read a Claude Code PreToolUse event from stdin")
    ap.add_argument("--rules", action="store_true", help="print the rules and limits")
    args = ap.parse_args()

    if args.hook:
        return run_hook()
    if args.rules:
        for rule, text in RULES.items():
            print(f"{rule}: {text}")
        print("limits: " + json.dumps(LIMITS))
        return 0
    if not args.files and not args.command:
        ap.print_usage(sys.stderr)
        return 1
    found = Findings()
    try:
        for f in args.files:
            check_objects(load_path(f), found)
        if args.command:
            check_command(args.command, found)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    for line in found.lines():
        print(line)
    print(f"RESULT={found.result()}")
    return 2 if found.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
