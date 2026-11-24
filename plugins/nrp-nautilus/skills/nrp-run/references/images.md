# Building and Using Container Images on NRP

Read this when the code needs an image of its own: heavy or compiled
dependencies, many pods, or reuse across runs. Everything here follows the
NRP docs pages "Building in GitLab", "Private Repos" and "K8s GitLab
Integration" as of 2026-11-24. The CI build and the local docker build have
not yet been run end to end for this skill.

## Contents

- When to build an image
- Choose how to build
- A Dockerfile that builds small and fast
- Build in NRP GitLab CI
- Build with docker on the user's machine
- Use the image in a Job
- Private projects
- arm64 nodes
- Deploying from CI

## When to build an image

| Situation | Choice |
|---|---|
| A few pure-Python packages, one pod | No image: `pip install` at the start of the command |
| Installation takes minutes, needs a compiler or system packages | Build an image |
| An Indexed Job with many pods | Build an image: every pod would repeat the install |
| The same environment for weeks of runs | Build an image and pin its tag |

## Choose how to build

Ask the user before setting anything up. Most users have neither path ready,
so say what each one takes:

| Path | What the user needs | Good for |
|---|---|---|
| NRP GitLab CI (recommended) | An account at https://gitlab.nrp-nautilus.io and a project there holding the code. Nothing to install locally beyond git | No docker locally; compiled dependencies; an Apple Silicon Mac; images rebuilt on every push |
| Docker on their machine | docker installed and running, and a registry they can push to that they have logged in to | Users who already build with docker |

If the user already builds with docker, use docker; do not move them to
GitLab. Either way the image should end up in a registry the nodes can pull
from. NRP's own (`gitlab-registry.nrp-nautilus.io`) is close to the nodes;
GHCR or Docker Hub also work.

## A Dockerfile that builds small and fast

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

Copy `requirements.txt` and install before copying the code, so a code change
does not reinstall everything. Keep datasets and model weights out of the
image: they belong on a PVC or in S3. Compilers and headers only needed for
the build can go in a first stage of a multi-stage build.

## Build in NRP GitLab CI

1. The user signs in at https://gitlab.nrp-nautilus.io and creates a project
   (or uses one they have). The code and the `Dockerfile` go in it.
2. Add `assets/templates/gitlab-ci-kaniko.yml` as `.gitlab-ci.yml` at the top
   of the project, unchanged: it is the NRP docs template. The shared runners
   need no setup. Its parts:
   - image `ghcr.io/osscontainertools/kaniko:debug`;
   - the job variable `GODEBUG: 'http2client=0'`, without which pushes to
     GitLab are very slow;
   - a first script line that writes the registry login GitLab gives every
     job into `/kaniko/.docker/config.json`;
   - the executor, with `--push-retry=10` and `--image-fs-extract-retry=10`
     (retries a base-image download that drops).
3. Commit and push. Each push builds and publishes
   `gitlab-registry.nrp-nautilus.io/<group>/<project>:<short-sha>` (and
   `:latest`). The image path is also shown in the project under
   Deploy → Container Registry.
4. Wait for the build to finish before submitting the Job. The user sees it
   under Build → Jobs. For a public project the API answers without a token:

   ```bash
   curl -s "https://gitlab.nrp-nautilus.io/api/v4/projects/<group>%2F<project>/pipelines?sha=<full commit sha>"
   curl -s "https://gitlab.nrp-nautilus.io/api/v4/projects/<group>%2F<project>/pipelines/<pipeline id>/jobs"
   curl -s "https://gitlab.nrp-nautilus.io/<group>/<project>/-/jobs/<job id>/raw"
   ```

   `status` goes `created`, `pending`, `running`, then `success` or `failed`;
   the last command prints a job's log. Check every minute or two, not in a
   tight loop. For a private project, ask the user to say when the job passed.

Each CI job runs in its own virtual machine with 4 CPUs, 8Gi of memory and
50Gi of disk by default. A build that needs more asks in the job's
`variables`, within the runner's caps:

| Variable | Default | Cap |
|---|---|---|
| `KUBERNETES_CPU_REQUEST` / `KUBERNETES_CPU_LIMIT` | 1 / 4 | 2 / 12 |
| `KUBERNETES_MEMORY_REQUEST` / `KUBERNETES_MEMORY_LIMIT` | 8Gi / 8Gi | 50Gi |
| `KUBERNETES_EPHEMERAL_STORAGE_REQUEST` / `KUBERNETES_EPHEMERAL_STORAGE_LIMIT` | 0 / 50Gi | 1Ti |

| In the job log | Cause and fix |
|---|---|
| Exit code 137 | Out of memory: raise the memory variables, and add `--compressed-caching=false` to the executor |
| `Container build exceeded its local ephemeral storage limit` | Out of disk: set the ephemeral-storage request and limit to the same, larger value. Kaniko keeps the compressed base image on disk too |
| `failed to get filesystem from image: unexpected EOF` | A dropped base-image download; the template's `--image-fs-extract-retry=10` retries it. Push again if it still fails |

Ask for what the build uses: the nodes' disks are shared.

## Build with docker on the user's machine

The user logs in to the registry themselves (`docker login <registry>`); never
handle the password or token. For NRP's registry they need a project on NRP
GitLab, and its Deploy → Container Registry page shows the login and push
commands. Then build for Nautilus' x86 nodes and push, as the docs' docker
variant does:

```bash
docker buildx build --platform linux/amd64 --provenance=false -t <registry>/<path>:<tag> --push .
```

Use the commit's short SHA (`git rev-parse --short HEAD`) or a version as the
tag. On an Apple Silicon Mac, `--platform linux/amd64` is required, or pods
fail with `exec format error`; the build then runs under emulation and can be
slow for compiled code, which is when GitLab CI pays off. The image must be
public, or the Job needs a pull secret (next sections).

## Use the image in a Job

```yaml
      containers:
      - name: main
        image: gitlab-registry.nrp-nautilus.io/<group>/<project>:<short-sha>
```

Use the commit tag, not `latest`: a rerun then gets the same code.

## Private projects

A private project's image needs a pull secret. In the project, the user
creates a deploy token with `read_registry` (Settings → Repository → Deploy
tokens) and makes a `docker-registry` Secret named `regcred` from it in their
own terminal, typing the token where the shell does not record it:

```bash
read -rs REGISTRY_TOKEN
kubectl -n <namespace> create secret docker-registry regcred --docker-server=gitlab-registry.nrp-nautilus.io/<group>/<project> --docker-username=<deploy token username> --docker-password="$REGISTRY_TOKEN"
unset REGISTRY_TOKEN
```

The deploy token username looks like `gitlab+deploy-token-123`. The Job
names the Secret:

```yaml
    spec:
      imagePullSecrets: [{name: regcred}]
```

Never write the token into a manifest, a chat message or a file in the
repository. Docs: https://nrp.ai/documentation/userdocs/development/private-repos/

## arm64 nodes

GH200 nodes are arm64 and need an arm64 or multi-arch image. In CI, the
shared docker runner (job tag `docker`, image `docker:dind`) builds both; the
template is under "Multiarch builds" on
https://nrp.ai/documentation/userdocs/development/gitlab/. Its build line is
`docker buildx build -f Dockerfile --push --provenance=false --platform
linux/amd64,linux/arm64 ...`, after `docker buildx create --driver
docker-container --bootstrap --use`. There is one such runner, so use it only
when the image must run on arm64. Locally, the same `buildx` line works after
the same `buildx create`.

## Deploying from CI

nrp-run builds the image in CI and submits the Job from the user's machine.
Nothing more is needed for that. If the user wants every push to also roll
out a Deployment or submit a Job, that is the GitLab agent for Kubernetes:
https://nrp.ai/documentation/userdocs/development/k8s-integration/. It is set
up once per namespace with Helm, by someone who can create RoleBindings
there, and the user stores the agent token themselves. Do not use the older
certificate-based connection, which GitLab deprecated. Anyone who can run a
pipeline in an authorized project can then change the namespace, so set it
up only when the user asks for it.
