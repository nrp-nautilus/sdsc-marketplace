#!/usr/bin/env bash
# Check, with server-side dry runs, the NRP admission behaviour that the nrp skill and
# nrp_preflight.py rely on. Nothing is created: every call uses --dry-run=server.
#
#   ./verify.sh <namespace>  | tee "$(date +%F).txt"
#
# Runs on your machine against the current kubectl context. Needs rights to create Jobs,
# Pods and ConfigMaps in <namespace> (any member has them). Each step prints the time,
# the command, its raw output and the exit code.
set -u
NS="${1:?usage: verify.sh <namespace>}"
RES='{"requests":{"cpu":"100m","memory":"64Mi"},"limits":{"cpu":"100m","memory":"64Mi"}}'

step() {  # step <title> <manifest-json> [kubectl verb] [extra args...]
  local title="$1" manifest="$2" verb="${3:-apply}"; shift 3 2>/dev/null || shift $#
  echo; echo "### $title"; echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')]"
  echo "\$ kubectl -n $NS $verb --dry-run=server -f - $* <<< '<manifest>'"
  printf '%s' "$manifest" | kubectl -n "$NS" "$verb" --dry-run=server -f - "$@" 2>&1 | cut -c1-600
  echo "exit=${PIPESTATUS[1]}"
}
pod() { printf '{"apiVersion":"v1","kind":"Pod","metadata":{"name":"preflight-verify"},"spec":{%s"containers":[{"name":"c","image":"busybox","command":["true"],"resources":%s}]}}' "$1" "$RES"; }
job() { printf '{"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"preflight-verify"},"spec":{"template":{"spec":{"restartPolicy":"Never","containers":[{"name":"c","image":"busybox","command":["true"],"resources":%s}]}}}}' "${1:-$RES}"; }

echo "context: $(kubectl config current-context)   namespace: $NS"
step "1. toleration without a key is rejected" "$(pod '"tolerations":[{"operator":"Exists"}],')"
step "2. tolerating nautilus.io/system is rejected" "$(pod '"tolerations":[{"key":"nautilus.io/system","operator":"Exists"}],')"
step "3. a priority class outside the allowed set is rejected" "$(pod '"priorityClassName":"nice",')"
step "4. priorityClassName opportunistic is accepted" "$(pod '"priorityClassName":"opportunistic",')"
step "5. a bare pod gets activeDeadlineSeconds (6 hours)" "$(pod '')" apply -o 'jsonpath={.spec.activeDeadlineSeconds}{"\n"}'
step "6. selecting arm64 adds the hardware toleration" "$(pod '"nodeSelector":{"kubernetes.io/arch":"arm64"},')" apply -o 'jsonpath={.spec.tolerations[?(@.key=="nautilus.io/hardware")]}{"\n"}'
step "7. a Job without a TTL gets ttlSecondsAfterFinished" "$(job)" apply -o 'jsonpath={.spec.ttlSecondsAfterFinished}{"\n"}'
step "8. a Job without requests and limits is rejected" "$(job '{}')"
step "9. a Job with limits above 1.2x requests gets a warning" "$(job '{"requests":{"cpu":"100m","memory":"64Mi"},"limits":{"cpu":"1","memory":"64Mi"}}')"
step "10. CUDA_VISIBLE_DEVICES is rejected" "$(job | sed 's/"command":\["true"\]/"command":["true"],"env":[{"name":"CUDA_VISIBLE_DEVICES","value":"0"}]/')"

BIG="$(python3 -c 'import json;print(json.dumps({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"preflight-verify"},"data":{"f":"x"*300*1024}}))')"
step "11. kubectl apply refuses a 300 KiB ConfigMap (annotation limit)" "$BIG" apply
step "12. kubectl create accepts the same 300 KiB ConfigMap: no size policy on the cluster" "$BIG" create -o name
