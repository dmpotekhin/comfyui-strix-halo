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
      "categories": ["image-editing", "image-enhancement", "vision-language"],
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
| `max_jobs_per_day` | how many submissions the account may make per UTC day (`null`/absent = no allowance). Reruns count. Checked before the queue-depth cap, so a finished run does not buy a new one |

A workflow is allowed when **its id is listed or its category is listed**. Nothing else.
`admin` cannot be restricted by this file — the owner must not be able to lock themselves out.

Missing file → built-in defaults: admin and user unrestricted, child denied everything until a
policy is written. The shipped `rbac.json` gives a child 55 of the 127 registered workflows
(33 image-editing, 9 image-enhancement, 9 vision-language, plus 4 named generation workflows),
one active job at a time, and 40 runs per day; its model folders are exactly the ones those 55
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
| `/api/users/usage` | every account | 403 | 403 |
| `/api/models/plan`, `/api/models/downloads`, `/api/models/{f}/delete`, `/api/settings` (write), `/api/users*`, `/api/engine/refresh` | ✓ | 403 | 403 |

`GET /api/settings` is admin-only (it exposes engine and paths). `/api/auth/me` returns
`capabilities` for the current account; the SPA uses it for navigation and upload fields.

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
4. **Quotas** (disk, generations per day) are not implemented; only concurrent-job caps.
5. **Audit trail of denials** covers submissions over the job cap; other 403s are not logged.
6. **The SPA is not the boundary** and never will be — treat `dist/assets/*.js` as UI only.
