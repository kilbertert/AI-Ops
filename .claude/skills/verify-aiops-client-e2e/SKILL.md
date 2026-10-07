---
name: verify-aiops-client-e2e
description: "Drive the PRODUCTION AI-Ops gateway as a real end user on the gateway host — resolve a real thirdSession, call the answer surfaces (/v1/faq/*, /v1/shortcuts), and ask the access log what the deployed client actually calls. Use when you must prove user-facing behaviour end to end, or when a client-side symptom needs a cause (never called / called and failed / wrong version)."
---

# Verify the AI-Ops client surfaces end to end

Two questions this answers that nothing else in the repo does:

1. **Does the deployed gateway actually serve a real user, right now?** — the
   answer-surface routes refuse a device token (401), so the throwaway-root
   skill cannot reach them at all. This one resolves a **real session** and
   drives the production app over its real ASGI surface.
2. **What does the deployed client actually ask for?** — the production access
   log separates "the client never called it" from "the client called it and it
   failed". Those look identical to a user and are opposite problems.

Read `verify-aiops-gateway` first if you are verifying the CONTROL plane
(health, enrollment, run routes) — a throwaway root is the right tool there and
it touches nothing real.

## Boundary: what runs where

| Part | Runs on | Touches |
|---|---|---|
| `probe_gateway_as_real_user.py` | **the gateway host (41)** | production config + one real session; read-only |
| `what_does_the_client_call.py` | **a host that owns the access log** | log file only; read-only |

Neither is a `TestClient` smoke test: the first builds the **production app from
the production config** and drives it, the second reads the **real traffic**.
Both print evidence per language and exit non-zero on failure.

## Running the production probe

```bash
# 1. Copy the script to the host and make it readable by the service account.
scp .claude/skills/verify-aiops-client-e2e/scripts/probe_gateway_as_real_user.py aiops-41:/tmp/probe.py
ssh aiops-41 'chmod a+r /tmp/probe.py'

# 2. Run it WITH the service's own environment and AS the service account.
#    Both matter:
#      - `GatewayServerSettings.from_env()` reads the gateway's env, not a shell's;
#      *  and the config file is 0600 aiops41, so root cannot read it either.
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env \
  $(tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
    | grep -E "^AIOPS_" | xargs -d"\n") \
  /opt/aiops-41/.venv/bin/python /tmp/probe.py --languages zh,en,zh-Hant,vi,th,km'
```

The verdict is per (language, path): **HTTP 200 _and_ `language` == the requested
tag**. A 200 that echoes the requested tag while serving fallback copy is the
failure mode this whole area has, so "it returned 200" is not the check.

### Two things that cost the most time to find

- **The header is `X-Third-Session`.** FastAPI strips underscores from header
  names, so a lowercase `third-session` arrives as `None` — and the resolver
  then reports *"service authentication failed"*, which points at the **token**
  rather than at the header. If you get that message with a token you know is
  right, check the header spelling first.
- **The session value is the Redis KEY, not the value.** The key is
  `app:3rd_session:<x>`; the header carries `<x>`. `GET` on that key returns a
  Java-serialised blob the resolver parses.

### Credential discipline

The script reads the service token, the Redis password and a **real user
session** — the session authenticates as that user until it expires. None of the
three is printed, and none belongs in an evidence file. If you need to cite a
run, cite the counts and the served languages, not the material.

## Running the client-traffic probe

```bash
ssh aiops-41 'python3 /dev/stdin /www/wwwlogs/api.mall.qushiyun.com.log \
  --since 07/Oct/2026 --ua Html5Plus --missing /v1/shortcuts' \
  < .claude/skills/verify-aiops-client-e2e/scripts/what_does_the_client_call.py
```

`--missing` exits 1 and prints the distinction explicitly when the path has zero
requests. **Zero requests is a different finding from any error code**: an error
leaves a 4xx/5xx line, so no line at all means the call was never made.

Which log file: the AI-Ops routes are proxied per-vhost, and the client's own
domain may not be the one you expect. `grep -l "v1/<route>" /www/wwwlogs/*.log`
finds the vhost rather than assuming it.

## The client-side decision tree

When a feature is visibly missing, establish which of these it is **before**
theorising. Each step is cheap and rules out a whole branch:

1. **Does the backend serve it?** → run the production probe. If that passes,
   the backend is not the cause and no amount of backend reading will find one.
2. **Does the client ask for it?** → run the traffic probe. Zero requests ends
   the backend investigation: the call is not being made.
3. **Is the deployed client able to ask?** → decompile the client bundle. For a
   uni-app APK the business JS is at
   `assets/apps/__UNI__*/www/app-service.js`; a Python regex over the extracted
   file beats `ssh | grep` (that times out on a 15 MB bundle). This is how you
   tell "this build never had the call" from "the call is there and the build is
   older than the feature".
4. **Which build is the user on?** → the User-Agent carries the uni-app runtime
   version (`uni-app (Immersed/<n>)`). Compare it against the builds you have,
   because "the feature is missing" and "the user is on a build that predates
   the feature" have the same screenshot.

### Reading a client bundle without guessing

`assets/apps/__UNI__*/www/manifest.json` carries `version.name` / `version.code`.
That is the only reliable way to say which build a bundle IS; PGYer's page title
can show a different (usually the newest) build than the file it served.

## What this cannot prove

- **Model-backed answers.** `/v1/assistant/questions` and a real diagnosis need a
  provider key and live data sources. The probe can tell you the route is
  reachable; it cannot tell you the model obeyed the language.
- **The frontend RENDERED it.** A 200 with correct copy proves the backend; it
  does not prove the user saw it. A client that ignores the response is outside
  every check here — see step 3/4 above and the frontend handoff docs.
- **Translation quality.** No automated check decides whether copy reads well.

## Related

- `verify-aiops-gateway` — control plane on a throwaway root (no production contact).
- `docs/agents/env-41-runbook.md` — host facts, the two env files, deployment.
- `docs/agents/frontend-operator-handoff.md` — the operator-side frontend contract.

## A worked example, including the wrong turns

A user reported a feature missing from a screenshot: the shortcut row was gone,
while a Thai-language catalogue had just shipped. Five explanations fit the
screenshot. The probes above settled it; the order below is the order that
actually terminated:

| Step | Finding |
|---|---|
| Backend serves it? | `/v1/shortcuts` returned 3 published rows for that tenant |
| Client asks for it? | **0 requests in the whole day**, while the same device made 11 calls to `/v1/faq/recommendations` |
| Can the deployed build ask? | Build **19** (`1.2.9`) HAS the call; build **20** (`1.2.11`, published five days later) **removed it** — `getShortcuts` is gone and `showShortcuts` is initialised `!1` with nothing ever setting it true |
| Which build is the user on? | Same UA (`Immersed/40.285713`) — inconclusive from the log alone, and PGYer's list route serves whichever build it likes |

What made this slow, and what to skip next time: three hypotheses (backend
deploy, auth/config, kill-switch flag) all fit the screenshot and none was
checkable, so they were researched instead of ruled out. **Step 2 costs one
`grep` and eliminates the entire backend branch.** Do it first.

Also note what the evidence did NOT establish: the exact build on the user's
device, and whether the removal was intentional. Both need someone who knows the
frontend — this skill tells you what to ask them, not the answer.
