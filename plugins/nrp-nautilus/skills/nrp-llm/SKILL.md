---
name: nrp-llm
description: >-
  NRP's managed open-weights LLMs behind an OpenAI-compatible API
  (ellm.nrp-nautilus.io). Use when writing code, a pipeline, an agent or a Job
  that calls NRP-hosted models, when choosing an NRP model, when a request
  returns HTTP 429, an empty answer or a 500 on a large body, when pointing a
  coding tool (Claude Code, OpenCode, VS Code) at NRP models, or when planning
  how many requests a workflow may send under the fair-use limits. Needs an
  NRP LLM token, not a Kubernetes namespace.
compatibility: Requires network access to https://ellm.nrp-nautilus.io and an NRP LLM token created by the user at https://nrp.ai/llmtoken. Examples use the openai Python package or curl.
metadata:
  version: "1.0"
  system: nautilus
  facility: nrp
  checked: "2026-11-15"
---

# NRP managed LLMs

NRP hosts a changing catalog of open-weights models on its own GPUs. There is
nothing to deploy: any member of an NRP group with the LLM flag creates a
token and calls an OpenAI-compatible API. The service is shared and
non-commercial, so the rules below are about not starving other users.

## Access

- Base URL: `https://ellm.nrp-nautilus.io/v1` (OpenAI-compatible). An
  Anthropic-compatible endpoint is at `https://ellm.nrp-nautilus.io/anthropic`.
- Token: the user creates it at https://nrp.ai/llmtoken. Their group needs the
  LLM flag (https://nrp.ai/namespaces shows memberships); a Kubernetes
  namespace is not required.
- Never put the token in code, a manifest, a command line or a chat message.
  Read it from an environment variable; in a Job, from a Secret with
  `secretKeyRef`.

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["NRP_LLM_TOKEN"], base_url="https://ellm.nrp-nautilus.io/v1")
reply = client.chat.completions.create(
    model="gpt-oss",
    messages=[{"role": "user", "content": "One sentence on what a Kubernetes Job is."}],
)
print(reply.choices[0].message.content)
```

## Choosing a model

The catalog changes; do not hard-code a list. Ask the API, and read the model
cards before committing a pipeline to one:

```bash
curl -s -H "Authorization: Bearer $NRP_LLM_TOKEN" https://ellm.nrp-nautilus.io/v1/models
```

- Model cards and feature matrix: https://nrp.ai/documentation/userdocs/ai/llm-managed/models/
- What was added or retired: https://nrp.ai/documentation/userdocs/ai/llm-managed/lifecycle/
- Live health: https://nrp.ai/llm-status

Starting points from the NRP docs (2026-11): `qwen3` for long context and
multimodal input; `qwen3`, `glm-5`, `deepseek-v4-flash` or `minimax` for
agentic coding; `gpt-oss` for reproducible general use; `qwen3-embedding` for
embeddings; `service` when staying up matters more than which model answers
(it falls back through the catalog).

## Fair-use limits

From https://nrp.ai/documentation/userdocs/ai/llm-managed/fair-use/ (read
2026-11-14). Check the page before a large run.

| Limit | Value |
|---|---|
| Output tokens per minute, per token and model | 48,000; above it the gateway returns HTTP 429 |
| Concurrent requests per user: `kimi`, `glm-5` | 2 |
| Concurrent requests per user: `minimax`, `qwen3-small`, `gemma`, `gemma-small` | 8 |
| Concurrent requests per user: `qwen3`, `deepseek-v4-flash`, `service`, `gpt-oss`, `qwen3-embedding` | 16 |
| A request using 35% or more of the model's context | 1 at a time; all concurrent requests together stay under 35% of the context |

SDSC and Internet2 affiliates get twice these limits.

Design the client for them:

- Cap concurrency in code (a semaphore or a worker pool) at or below the model's limit.
- Retry 429 and 5xx with increasing waits, indefinitely for batch work. Models go down and come back for maintenance.
- Lower concurrency when latency rises; do not add workers to push through.
- Before a batch, estimate it: `requests x output tokens / 48,000` is the least number of minutes it can take on one token and model.

## Request settings that matter

- **`max_tokens`**: leave it unset unless the client requires it. It is the
  maximum output length, not the context size. If required, use a quarter to a
  third of the context window; never the full context length (the request errors).
- **Thinking is on by default** for most models, often at a high effort, and
  it spends output tokens. A small `max_tokens` can be used up by thinking and
  return an empty answer. For short, latency-sensitive or bulk calls turn it
  off per request and check that the answer is not empty:

  ```python
  extra_body={"chat_template_kwargs": {"enable_thinking": False}}   # gemma, qwen3 family
  extra_body={"chat_template_kwargs": {"thinking": False}}          # deepseek-v4-flash
  ```

  These worked on 2026-11-13; switches are model-specific and change with the
  serving setup, so test one request first.
- **`cache_salt`**: prompts are cached across users of the same model. For
  prompts that must stay private, pass a secret random string of 43 or more
  characters as `extra_body={"cache_salt": ...}`; the price is a cold cache.
- **Request size**: keep one request body under 4 MiB (images and long
  documents included). Larger ones failed with HTTP 500 and an empty body on
  most models when checked in 2026-10.

## Calling the LLM from a Job

The user creates the Secret themselves (so the token never passes through the
agent), and the Job reads it:

```yaml
        env:
        - name: NRP_LLM_TOKEN
          valueFrom:
            secretKeyRef: {name: nrp-llm-token, key: token}
```

Many pods calling the LLM share **one** token's limits: 20 pods with 16
threads each are 320 concurrent requests on a model that allows 16. Set the
per-pod concurrency to the model's limit divided by `parallelism`.

## Coding tools

Ready-to-paste settings for Claude Code, OpenCode, VS Code, Crush, pi, Kimi
Code and Copilot CLI are at
https://nrp.ai/documentation/userdocs/ai/llm-managed/client-configs/. Claude
Code uses the `/anthropic` endpoint with `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL`. Not every NRP model works
through that endpoint; try the one you chose before relying on it.

## Gotchas

| Symptom | Cause and fix |
|---|---|
| HTTP 429 | Over 48,000 output tokens a minute on this token and model, or over its concurrency. Back off and lower concurrency |
| Empty `content`, `finish_reason: length` | Thinking used the whole `max_tokens`. Turn thinking off or raise `max_tokens` |
| Error naming the context length | `max_tokens` was set to the context size. Remove it |
| HTTP 500 with an empty body on a large request | Body over 4 MiB. Send fewer or smaller images, or split the document |
| "failed to find candidate pods", sudden errors on one model | That model is down or restarting. Retry with backoff, or use `service` |
| Answers that look like someone else's prompt | Shared cache. Set `cache_salt` |

## Docs

- Overview: https://nrp.ai/documentation/userdocs/ai/llm-managed/
- API access: https://nrp.ai/documentation/userdocs/ai/llm-managed/api-access/
- Fair use: https://nrp.ai/documentation/userdocs/ai/llm-managed/fair-use/
- Support: the AI/ML channel at https://nrp.ai/contact/
