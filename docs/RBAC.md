# Roles and permissions (RBAC)

Who may do what, where it is enforced, and how to change it without touching code.

Status: implemented and covered by tests (`studio/backend/tests/test_rbac.py`, 26 cases).
Scope: the Studio API on `:8000`. The ComfyUI engine (`:8188`) and the Caddy door (`:8189`) are
separate and unchanged by this document.

## The one rule that matters

**Every restriction is enforced in the backend.** The SPA reads `capabilities` from
`/api/auth/me` and hides things that would only be refused, but hiding a button is decoration.
If you can reach a route, that route decides — not the page you found it on.

## Three roles

| Role | Intended for | Can |
|------|--------------|-----|
| `admin` | the owner of the machine | everything: users, settings, models & downloads, engine refresh, all workflows, all jobs |
| `user` | a household member | generation, own projects/history, all workflows, uploads, up to 3 concurrent jobs |
| `child` | a child profile | only the workflows/categories listed in `rbac.json`, only the model folders listed there, own history, 1 concurrent job |

An account that exists with a role the policy does not mention gets **nothing**. "Deny by
default" is deliberate: a typo in a config file must not widen access.

### What is already scoped by owner (unchanged by RBAC)

Jobs, outputs, gallery, telemetry and events are filtered per user in `db.py`/`api.py`
(`_owner_scope`, `_may_see_job`): a member cannot see or rerun another member's job, and a child
cannot see an admin's. Admin sees everything. This predates RBAC and still applies.

## The policy file: `studio/registry/rbac.json`

Read at startup and on `POST /api/engine/refresh`, so changing it does not need a restart.

```json
{
  "version": 1,
  "roles": {
    "user":  { "all_workflows": true, "all_models": true, "allow_upload": true, "max_active_jobs": 3 },
    "child": {
      "categories": ["image-editing", "enhancement", "vision-language"],
      "workflows": ["format-square", "style-anime"],
      "model_folders": ["checkpoints", "vae", "upscale_models"],
      "allow_upload": true,
      "max_active_jobs": 1
    }
  }
}
```

| Key | Meaning |
|-----|---------|
| `all_workflows` | `true` = every workflow in the registry, present and future |
| `categories` | workflow categories allowed wholesale (see `studio/registry/categories.json`) |
| `workflows` | individual workflow ids allowed in addition to the categories |
| `all_models` | `true` = every model folder is visible |
| `model_folders` | folders shown in `/api/models` and `/api/models/catalog` |
| `allow_upload` | may `POST /api/uploads` (image inputs for img2img-style workflows) |
| `max_active_jobs` | ceiling for this account's queued+running jobs; can only tighten the global `STUDIO_MAX_USER_ACTIVE_JOBS` |
| `max_output_gb` | total size of the account's outputs, in decimal GB. At or over the allowance a new submission is refused (429) until the account deletes something |
| `max_jobs_per_day` | how many submissions the account may make per UTC day (`null`/absent = no allowance). Reruns count. Checked before the queue-depth cap, so a finished run does not buy a new one |

A workflow is allowed when **its id is listed or its category is listed**. Nothing else.
`admin` cannot be restricted by this file — the owner must not be able to lock themselves out.

Missing file → built-in defaults: admin and user unrestricted, child denied everything until a
policy is written. Categories are the *canonical* ones (a workflow file may still say
`image-enhancement`; the schema rewrites it to `enhancement`, and the policy loader applies the
same alias table, so either spelling matches). The shipped `rbac.json` gives a child 55 of the 127
registered workflows (33 image-editing, 9 enhancement, 9 vision-language, plus 4 named generation workflows),
one active job at a time, 40 runs per day, and 10 GB of outputs; its model folders are exactly the ones those 55
workflows reference (`checkpoints`, `upscale_models`, `vae`, `vision-models`, `vlm-models`),
derived from the registry rather than guessed. Malformed file → same defaults plus a warning in the log; the child stays
denied rather than accidentally trusted.

## Endpoint map

| Route | admin | user | child |
|-------|-------|------|-------|
| `/api/workflows`, `/categories`, `/{id}`, `/validate`, `/graph-check`, `/{id}/stats` | all | all | filtered; 403 on a workflow not allowed, 404 on one that does not exist |
| `POST /api/jobs`, `POST /api/jobs/{id}/rerun` | ✓ | ✓ | only allowed workflows, within `max_active_jobs` and `max_jobs_per_day` (429 with the number that was hit) |
| `/api/jobs*`, `/api/gallery`, `/api/outputs*`, `/api/events`, `/api/telemetry` | all users' data | own data | own data |
| `/api/models`, `/api/models/catalog` | everything | everything | only the folders in the policy |
| `/api/uploads` | ✓ | ✓ | per `allow_upload` |
| `/api/me/usage` | own totals | own totals | own totals (counts, bytes, active jobs, the cap that applies) |
| `/api/auth/sessions`, `POST /api/auth/sessions/revoke-others` | own sessions | own sessions | own sessions |
| `/api/users/usage` | every account | 403 | 403 |
| `/api/models/plan`, `/api/models/downloads`, `/api/models/{f}/delete`, `/api/settings` (write), `/api/users*`, `/api/engine/refresh` | ✓ | 403 | 403 |

`GET /api/settings` is admin-only (it exposes engine and paths). `/api/auth/me` returns
`capabilities` for the current account; the SPA uses it for navigation and upload fields.

### Sessions

`GET /api/auth/sessions` lists where the account is signed in — `created_at`, `last_seen`,
`expires_at`, `client_ip`, `user_agent`, and which one is the caller. The row id *is*
`sha256(cookie)`, i.e. the bearer key, so it never leaves `db.list_sessions()`; the route has no
field to leak it into.

`POST /api/auth/sessions/revoke-others` deletes every session of the caller except the one making
the request, so "sign out other devices" cannot sign you out too. Two devices pressing it at the
same moment would otherwise delete each other and empty the account — the statement carries an
`EXISTS` guard, so the first request wins and the loser gets `401 session was signed out by
another device` instead of a false `200`.

### A job may only read the caller's own uploads

`/api/outputs` refuses to *serve* another account's file, but a workflow with an image parameter
is a second door: the job loads the file server-side and the rendered result lands in the
caller's own outputs. So submission checks every `image` parameter with the same
`_media_allowed("input", …)` rule (own `<user_id>_*` names for everyone but admin; refusal is
`403 image '…' is not one of your uploads` plus a `warn` event), and the workflow picker is
filtered with it too. Admins are not restricted.

## Adding a role or moving the line

* New role: add it to `ROLES` in `app/rbac.py` (the single definition), add a section to
  `rbac.json`, and restart the Studio. Accounts can then be created with
  `python -m app.cli create-user --role <role>` or `PATCH /api/users/{id}`.
* Change what a child may run: edit `rbac.json`, then `POST /api/engine/refresh` (admin) or
  restart. No code change, no migration.
* Tighten uploads or job caps: same file.

## What is *not* covered yet

Honest list, so nobody mistakes this for a finished permission system:

1. **Projects** do not exist as an entity. "Own projects" today means "own jobs and outputs".
2. **Shared history** (letting a member publish a run to the household) is not implemented.
3. **Per-model restrictions are visibility + workflow gating**, not per-file ACLs: a child can
   still see the *files* in an allowed folder, and a workflow that needs a hidden model is
   simply not offered.
4. **Allowances are checked at submission, not reserved**: two runs submitted just under the
   storage allowance can both finish over it, and the storage check uses the recorded output
   sizes (files written outside the outputs table are not counted).
5. **Audit trail of denials** covers the three submission refusals (active cap, daily
   allowance, storage allowance) and a job referencing somebody else's upload, as `warn` events
   with the `user_id`; a 403 on a forbidden workflow or a blocked upload route is *not* logged yet.
6. **`benchmark`/`compare` accept any workflow id** (`/api/workflows/{id}/benchmark`,
   `/compare`). They aggregate only the caller's own telemetry, so a child gets empty groups for a
   workflow they may not run — no data leak, but the gate is inconsistent with `/{id}` (which
   answers 403). Left as is on purpose: adding `_workflow_for` there would change 404/403 semantics
   for a route that leaks nothing.
7. **The SPA is not the boundary** and never will be — treat `dist/assets/*.js` as UI only.
