"""Tests for plugins/nrp-nautilus/skills/nrp/scripts/nrp_preflight.py.

    python3 -m unittest discover -s tests -v

Standard library only; manifests are built as JSON so neither PyYAML nor kubectl is needed.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "plugins/nrp-nautilus/skills/nrp/scripts/nrp_preflight.py"
spec = importlib.util.spec_from_file_location("nrp_preflight", SCRIPT)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

RES = {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}}


def job(name="j", command=("python", "run.py"), resources=RES, **spec):
    pod = {"restartPolicy": "Never",
           "containers": [{"name": "main", "image": "python:3.12", "command": list(command), "resources": resources}]}
    pod.update(spec.pop("pod", {}))
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name},
            "spec": {"template": {"spec": pod}, **spec}}


def configmap(name, kib):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": {"f": "x" * (kib * 1024)}}


def check(*objs):
    found = pf.Findings()
    pf.check_objects(list(objs), found)
    return {(lvl, rule) for lvl, rule, _, _ in found.items}


def check_cmd(command):
    found = pf.Findings()
    pf.check_command(command, found)
    return {(lvl, rule) for lvl, rule, _, _ in found.items}


class Manifests(unittest.TestCase):
    def test_clean_job_passes(self):
        self.assertEqual(check(job(activeDeadlineSeconds=3600)), set())

    def test_configmap_sizes(self):
        self.assertEqual(check(configmap("settings", 10)), set())
        self.assertEqual(check(configmap("big", 150)), {("WARN", "configmap-size")})
        self.assertEqual(check(configmap("code", 300)), {("BLOCK", "configmap-size")})

    def test_split_configmaps(self):
        parts = [configmap(f"model-part-{i}", 100) for i in range(4)]
        self.assertIn(("BLOCK", "configmap-split"), check(*parts))

    def test_secret_payload_counts(self):
        secret = {"kind": "Secret", "metadata": {"name": "blob"}, "stringData": {"f": "x" * 400 * 1024}}
        self.assertEqual(check(secret), {("BLOCK", "configmap-size")})

    def test_job_that_never_ends(self):
        self.assertIn(("BLOCK", "job-never-ends"), check(job(command=("sleep", "infinity"))))
        self.assertIn(("BLOCK", "job-never-ends"), check(job(command=("sh", "-c", "python train.py; sleep 3600"))))
        self.assertIn(("BLOCK", "job-never-ends"), check(job(command=("tail", "-f", "/dev/null"))))
        self.assertEqual(check(job(command=("sh", "-c", "sleep 5 && python train.py"))), set())

    def test_tolerations(self):
        self.assertIn(("BLOCK", "toleration"), check(job(pod={"tolerations": [{"operator": "Exists"}]})))
        self.assertIn(("BLOCK", "toleration"),
                      check(job(pod={"tolerations": [{"key": "nautilus.io/system", "operator": "Exists"}]})))
        self.assertEqual(check(job(pod={"tolerations": [{"key": "nautilus.io/reservation", "value": "g"}]})),
                         {("WARN", "toleration")})
        self.assertEqual(check(job(pod={"tolerations": [
            {"key": "nautilus.io/hardware", "operator": "Equal", "value": "arm64", "effect": "NoSchedule"}]})), set())

    def test_priority_class(self):
        self.assertIn(("BLOCK", "priority-class"), check(job(pod={"priorityClassName": "system-node-critical"})))
        self.assertEqual(check(job(pod={"priorityClassName": "opportunistic"})), set())

    def test_resources(self):
        self.assertIn(("BLOCK", "resources"), check(job(resources={})))
        loose = {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "2Gi"}}
        self.assertEqual(check(job(resources=loose)), {("WARN", "resources")})

    def test_cuda_visible_devices(self):
        j = job()
        j["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "CUDA_VISIBLE_DEVICES", "value": "0"}]
        self.assertIn(("BLOCK", "cuda-visible-devices"), check(j))

    def test_deployment_gpu_and_allow(self):
        gpu = {"requests": {"cpu": "1", "memory": "2Gi", "nvidia.com/gpu": 1},
               "limits": {"cpu": "1", "memory": "2Gi", "nvidia.com/gpu": 1}}
        dep = {"kind": "Deployment", "metadata": {"name": "d"}, "spec": {"template": {"spec": {
            "containers": [{"name": "c", "resources": gpu}]}}}}
        self.assertIn(("BLOCK", "deployment-gpu"), check(dep))
        os.environ["NRP_PREFLIGHT_ALLOW"] = "deployment-gpu"
        try:
            self.assertEqual(check(dep), {("WARN", "deployment-gpu")})
        finally:
            del os.environ["NRP_PREFLIGHT_ALLOW"]

    def test_bare_pod_size(self):
        big = {"requests": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": 4},
               "limits": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": 4}}
        pod = {"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": [{"name": "c", "resources": big}]}}
        self.assertIn(("BLOCK", "bare-pod-size"), check(pod))

    def test_many_pods(self):
        loose = {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "1200m", "memory": "2Gi"}}
        self.assertIn(("BLOCK", "many-pods"), check(job(resources=loose, parallelism=150, completions=300)))
        self.assertEqual(check(job(parallelism=20, completions=2000)), {("WARN", "many-pods")})
        self.assertEqual(check(job(parallelism=20, completions=100)), set())

    def test_too_many_jobs(self):
        self.assertIn(("BLOCK", "jobs-at-once"), check(*[job(name=f"j{i}") for i in range(401)]))
        self.assertNotIn(("WARN", "pods-quota"), check(*[job(name=f"j{i}") for i in range(401)]))

    def test_namespace_pod_quota(self):
        # 250 one-pod Jobs: under the 400-Job block, over the 200-pod quota many namespaces have
        self.assertEqual(check(*[job(name=f"j{i}") for i in range(250)]), {("WARN", "pods-quota")})
        self.assertEqual(check(*[job(name=f"j{i}") for i in range(200)]), set())
        # one Indexed Job running 300 pods at once (limits == requests, so no many-pods block)
        self.assertIn(("WARN", "pods-quota"), check(job(parallelism=300, completions=300)))
        self.assertNotIn(("WARN", "pods-quota"), check(job(parallelism=200, completions=300)))
        # pods add up across objects: a 150-pod Job plus 60 one-pod Jobs
        self.assertIn(("WARN", "pods-quota"),
                      check(job(name="big", parallelism=150, completions=150), *[job(name=f"j{i}") for i in range(60)]))

    def test_cronjob_frequency(self):
        def cron(schedule):
            return {"kind": "CronJob", "metadata": {"name": "c"},
                    "spec": {"schedule": schedule, "jobTemplate": {"spec": job()["spec"]}}}
        self.assertEqual(check(cron("* * * * *")), {("WARN", "cronjob-frequency")})
        self.assertEqual(check(cron("*/5 * * * *")), {("WARN", "cronjob-frequency")})
        self.assertEqual(check(cron("*/10 * * * *")), set())
        self.assertEqual(check(cron("0 3 * * *")), set())

    def test_blocked_kinds(self):
        self.assertIn(("BLOCK", "blocked-kind"), check({"kind": "DaemonSet", "metadata": {"name": "d"}, "spec": {}}))
        self.assertIn(("BLOCK", "blocked-kind"),
                      check({"kind": "Service", "metadata": {"name": "s"}, "spec": {"type": "NodePort"}}))
        self.assertIn(("BLOCK", "blocked-kind"), check(job(pod={"nodeName": "node-1"})))


class Commands(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.big = Path(self.dir.name, "model.bin")
        self.big.write_bytes(b"x" * 600 * 1024)
        self.small = Path(self.dir.name, "settings.yaml")
        self.small.write_text("a: 1\n")
        self.bad = Path(self.dir.name, "bad.json")
        self.bad.write_text(json.dumps(job(command=("sleep", "infinity"))))

    def test_force_delete(self):
        self.assertEqual(check_cmd("kubectl delete pod stuck --grace-period=0 --force"), {("BLOCK", "force-delete")})
        self.assertEqual(check_cmd("kubectl -n ns delete job done"), set())

    def test_create_configmap_from_file(self):
        self.assertEqual(check_cmd(f"kubectl create configmap code --from-file={self.big}"),
                         {("BLOCK", "configmap-size")})
        self.assertEqual(check_cmd(f"kubectl -n ns create cm code --from-file=model={self.big}"),
                         {("BLOCK", "configmap-size")})
        self.assertEqual(check_cmd(f"kubectl create configmap code --from-file {self.dir.name}"),
                         {("BLOCK", "configmap-size")})
        self.assertEqual(check_cmd(f"kubectl create configmap s --from-file={self.small}"), set())

    def test_dry_run_is_always_allowed(self):
        self.assertEqual(check_cmd(f"kubectl create configmap code --from-file={self.big} --dry-run=client -o yaml"),
                         set())
        self.assertEqual(check_cmd(f"kubectl apply --dry-run=server -f {self.bad}"), set())

    def test_apply_file_and_heredoc(self):
        self.assertEqual(check_cmd(f"cd /tmp && kubectl apply -f {self.bad}"), {("BLOCK", "job-never-ends")})
        heredoc = "kubectl apply -f - <<'EOF'\n" + json.dumps(job(command=("sleep", "inf"))) + "\nEOF"
        self.assertEqual(check_cmd(heredoc), {("BLOCK", "job-never-ends")})
        self.assertEqual(check_cmd("cat job.yaml | kubectl apply -f -"), {("WARN", "not-inspected")})

    def test_loop(self):
        self.assertIn(("WARN", "loop-create"),
                      check_cmd("for i in $(seq 1 20); do kubectl create job t$i --image=busybox -- true; done"))
        self.assertIn(("BLOCK", "jobs-at-once"),
                      check_cmd("for i in $(seq 1 900); do kubectl create job t$i --image=busybox -- true; done"))
        self.assertIn(("BLOCK", "jobs-at-once"),
                      check_cmd("for i in {0..4999}; do kubectl apply -f job-$i.yaml; done"))

    def test_loop_over_pod_quota(self):
        found = check_cmd("for i in $(seq 1 250); do kubectl create job t$i --image=busybox -- true; done")
        self.assertEqual(found, {("WARN", "loop-create"), ("WARN", "pods-quota")})
        self.assertNotIn(("WARN", "pods-quota"),
                         check_cmd("for i in $(seq 1 20); do kubectl create job t$i --image=busybox -- true; done"))

    def test_loop_with_xargs(self):
        self.assertIn(("BLOCK", "jobs-at-once"),
                      check_cmd("seq 1 900 | xargs -I N kubectl create job t-N --image=busybox -- true"))
        self.assertIn(("WARN", "loop-create"),
                      check_cmd("cat names.txt | xargs -n1 kubectl create job --image=busybox"))

    def test_loops_that_create_nothing(self):
        # The first three are commands from real agent runs (2026-11-17) that an earlier version flagged.
        checking = ("for f in k8s/pvc.yaml k8s/job.yaml; do python3 nrp_preflight.py $f; done; "
                    "kubectl apply --dry-run=server -f k8s/pvc.yaml")
        self.assertEqual(check_cmd(checking), set())
        writing_a_manifest = (
            "mkdir -p k8s && cat > k8s/job.yaml <<'EOF'\napiVersion: batch/v1\nkind: Job\nmetadata:\n  name: sweep\n"
            "spec:\n  template:\n    spec:\n      containers:\n      - name: main\n        command:\n        - sh\n"
            "        - -c\n        - for i in $(seq 0 599); do python run_task.py --task $i; done\nEOF\n"
            "kubectl apply --dry-run=server -f k8s/job.yaml")
        self.assertEqual(check_cmd(writing_a_manifest), set())
        writing_a_script = "kubectl delete job analysis --wait=true\ncat > fixed.py <<'EOF'\nfor b in blocks:\n    touch(b)\nEOF"
        self.assertEqual(check_cmd(writing_a_script), set())
        self.assertEqual(check_cmd("for i in $(seq 1 900); do kubectl apply --dry-run=server -f job-$i.yaml; done"), set())
        self.assertEqual(check_cmd("kubectl exec pod -- sh -c 'seq 0 599 | xargs -P 4 -I N python run_task.py --task N'"), set())

    def test_template_placeholders_do_not_crash(self):
        # A Job template an agent wrote in a real run (2026-11-18): quantities and counts still to be filled in.
        doc = job()
        container = doc["spec"]["template"]["spec"]["containers"][0]
        container["resources"] = {"requests": {"cpu": "__CPU__", "memory": "__MEM__"}, "limits": {"cpu": "__CPU__", "memory": "__MEM__"}}
        doc["spec"]["parallelism"] = "__N__"
        doc["spec"]["completions"] = "__N__"
        heredoc = "kubectl apply -f - <<'EOF'\n" + json.dumps(doc) + "\nEOF"
        self.assertEqual(check_cmd(heredoc), {("WARN", "not-inspected")})

    def test_other_commands_untouched(self):
        self.assertEqual(check_cmd("kubectl get pods && ls -la && helm list"), set())


class Hook(unittest.TestCase):
    def run_hook(self, command, env=None):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        return subprocess.run([sys.executable, str(SCRIPT), "--hook"], input=json.dumps(event), text=True,
                              capture_output=True, env={**os.environ, **(env or {})})

    def test_blocks_on_nrp(self):
        run = self.run_hook("kubectl delete pod x --force --grace-period=0", {"NRP_PREFLIGHT_ASSUME_NRP": "1"})
        self.assertEqual(run.returncode, 2)
        self.assertIn("force-delete", run.stderr)

    def test_warning_becomes_context(self):
        run = self.run_hook("cat j.yaml | kubectl apply -f -", {"NRP_PREFLIGHT_ASSUME_NRP": "1"})
        self.assertEqual(run.returncode, 0)
        self.assertIn("additionalContext", run.stdout)

    def test_other_cluster_is_left_alone(self):
        run = self.run_hook("kubectl --context some-other-cluster delete pod x --force",
                            {"NRP_PREFLIGHT_ASSUME_NRP": "0", "NRP_PREFLIGHT_CONTEXTS": "nautilus"})
        self.assertEqual(run.returncode, 0)

    def test_non_kubectl_is_fast_path(self):
        run = self.run_hook("ls -la")
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
