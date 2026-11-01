"""Role-based access policy: who may run which workflow, use which models, upload what.

Why a separate module
---------------------
``auth.py`` answers *who are you*; this module answers *what may that role do*. Keeping the
two apart means the policy can be data (``studio/registry/rbac.json``) instead of a chain of
``if role == ...`` scattered through the routes, and every denial goes through one function
that a test can pin down.

Design rules, in order of importance:

* **Deny by default.** A role the policy does not mention gets nothing. So does a malformed
  policy file: a typo in configuration must not silently widen access. Admin is the one
  exception — the policy cannot lock the owner out of their own machine.
* **Server-side only.** The frontend may hide a button; that is cosmetics. Nothing here is
  trusted from the client: the role comes from the session row, not from a header or body.
* **Fail closed, but stay usable.** A missing file falls back to the built-in matrix below
  (adults: everything, child: nothing), and the reason is recorded so ``/api/engine/refresh``
  or an admin can see it.
* **One place for the numbers.** ``max_active_jobs`` per role lives here; the global ceiling in
  settings still applies underneath it — a role policy can only tighten, never loosen it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.schemas import canonical_category

log = logging.getLogger(__name__)

ADMIN = "admin"
USER = "user"
CHILD = "child"

#: Full role list for account creation/validation. Order matters for the CLI's ``--role`` help.
ROLES: tuple[str, ...] = (ADMIN, USER, CHILD)

#: Decimal gigabytes on purpose: the policy file says "20", people mean 20 000 000 000 bytes of
#: output, and `du -h` showing 18.6 GiB for that is not a bug report we want to answer.
GB = 1_000_000_000


@dataclass(frozen=True)
class RolePolicy:
    """What one role may do. Frozen: a request must not be able to mutate policy in passing."""

    role: str
    all_workflows: bool = False
    workflows: frozenset[str] = field(default_factory=frozenset)
    categories: frozenset[str] = field(default_factory=frozenset)
    all_models: bool = False
    model_folders: frozenset[str] = field(default_factory=frozenset)
    allow_upload: bool = False
    #: ``None`` means "no role-specific cap": the global ``settings.max_user_active_jobs`` applies.
    max_active_jobs: int | None = None
    #: How much disk the role's outputs may occupy, in bytes. ``None`` = unlimited. Checked at
    #: submission time because there is no per-file delete hook to run the check in: the account
    #: is stopped from *adding* more once it is over, and deleting old outputs frees the space.
    max_output_bytes: int | None = None
    #: How many submissions the role may make per UTC day. ``None`` = unlimited. A household
    #: control, not a performance limit: it is the only way to stop a child profile from
    #: queueing hundreds of runs overnight on a machine the whole family shares.
    max_jobs_per_day: int | None = None

    def allows_workflow(self, workflow_id: str, category: str | None = None) -> bool:
        if self.all_workflows:
            return True
        if workflow_id and workflow_id in self.workflows:
            return True
        return bool(category) and category in self.categories

    def allows_model_folder(self, folder: str) -> bool:
        if self.all_models:
            return True
        return bool(folder) and folder in self.model_folders


def _deny(role: str) -> RolePolicy:
    """Least privilege: nothing listed, nothing allowed."""
    return RolePolicy(role=role)


def _everything(role: str) -> RolePolicy:
    return RolePolicy(role=role, all_workflows=True, all_models=True, allow_upload=True)


def _str_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):           # a single string is a one-item list, not a set of letters
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return frozenset()
    return frozenset(str(v).strip() for v in value if str(v).strip())


def _int_or_none(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _gb_to_bytes(value: Any) -> int | None:
    """A human wrote gigabytes; the database counts bytes. Non-numeric or non-positive → None."""
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return None
    return int(gb * GB) if gb > 0 else None


def policy_from_dict(role: str, raw: dict[str, Any] | None) -> RolePolicy:
    """Build one policy. Unknown keys are ignored; wrong types degrade to the safe default."""
    if role == ADMIN:
        # An admin policy in the file is accepted but cannot subtract rights: the owner must
        # never be able to lock themselves out with a JSON edit.
        return _everything(ADMIN)
    if not isinstance(raw, dict):
        return _deny(role)
    return RolePolicy(
        role=role,
        all_workflows=bool(raw.get("all_workflows", False)),
        workflows=_str_set(raw.get("workflows")),
        # Workflow categories are normalised at the schema boundary (``image-enhancement`` ->
        # ``enhancement``), so a policy written with the older spelling would otherwise match
        # nothing and quietly deny a whole family of workflows. Same alias table, both sides.
        categories=frozenset(canonical_category(c) for c in _str_set(raw.get("categories"))),
        all_models=bool(raw.get("all_models", False)),
        model_folders=_str_set(raw.get("model_folders")),
        allow_upload=bool(raw.get("allow_upload", False)),
        max_active_jobs=_int_or_none(raw.get("max_active_jobs")),
        max_output_bytes=_gb_to_bytes(raw.get("max_output_gb")),
        max_jobs_per_day=_int_or_none(raw.get("max_jobs_per_day")),
    )


class Rbac:
    """The loaded policy for every role, plus the questions the routes ask it."""

    def __init__(self, policies: dict[str, RolePolicy], *, source: str | None,
                 error: str | None = None) -> None:
        self._policies = dict(policies)
        self.source = source
        self.error = error

    # -- construction ------------------------------------------------------------------
    @classmethod
    def defaults(cls) -> "Rbac":
        """Built-in matrix used when no file exists: adults work, children wait for a policy."""
        return cls({ADMIN: _everything(ADMIN), USER: _everything(USER), CHILD: _deny(CHILD)},
                   source=None)

    @classmethod
    def load(cls, path: Path | str | None) -> "Rbac":
        """Read ``rbac.json``. A broken file denies rather than trusts."""
        base = cls.defaults()
        if not path:
            return base
        p = Path(path)
        if not p.is_file():
            return base
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("roles", {}), dict):
                raise ValueError("expected an object with a 'roles' object")
        except (OSError, ValueError) as exc:
            log.warning("rbac policy %s is unusable (%s); members keep built-in rights, "
                        "child stays denied", p, exc)
            return cls(dict(base._policies), source=str(p), error=str(exc)[:200])
        roles_raw = doc.get("roles") or {}
        out: dict[str, RolePolicy] = {ADMIN: _everything(ADMIN)}
        for role in ROLES:
            if role in roles_raw:
                out[role] = policy_from_dict(role, roles_raw[role])
            else:
                out[role] = base._policies[role]
        # Any extra role named in the file is still least-privilege: it is not in ROLES, so no
        # account can hold it, but parsing it silently would hide a typo.
        return cls(out, source=str(p))

    # -- queries -----------------------------------------------------------------------
    def policy(self, role: str | None) -> RolePolicy:
        """Unknown or missing role ⇒ deny. Never fall back to 'user'."""
        if not role:
            return _deny("")
        return self._policies.get(role) or _deny(role)

    def may_run(self, role: str | None, workflow_id: str, category: str | None = None) -> bool:
        return self.policy(role).allows_workflow(workflow_id, category)

    def may_see_folder(self, role: str | None, folder: str) -> bool:
        return self.policy(role).allows_model_folder(folder)

    def may_upload(self, role: str | None) -> bool:
        return self.policy(role).allow_upload

    def max_active_jobs(self, role: str | None, fallback: int) -> int:
        """The tighter of the role cap and the global one. Zero/None means 'no extra limit'."""
        cap = self.policy(role).max_active_jobs
        if cap is None:
            return int(fallback)
        return min(int(cap), int(fallback))

    def max_output_bytes(self, role: str | None) -> int | None:
        """The role's storage allowance in bytes, or ``None`` for no allowance."""
        return self.policy(role).max_output_bytes

    def max_jobs_per_day(self, role: str | None) -> int | None:
        """The role's daily submission allowance, or ``None`` for no allowance limit."""
        return self.policy(role).max_jobs_per_day

    def visible_workflow_ids(self, role: str | None,
                             defs: Iterable[Any]) -> set[str] | None:
        """``None`` means "no filtering"; otherwise the ids the role may see."""
        pol = self.policy(role)
        if pol.all_workflows:
            return None
        allowed = set(pol.workflows)
        for wf in defs:
            if getattr(wf, "category", None) in pol.categories:
                allowed.add(wf.id)
        return allowed

    def visible_folders(self, role: str | None) -> set[str] | None:
        pol = self.policy(role)
        return None if pol.all_models else set(pol.model_folders)

    def capabilities(self, role: str | None) -> dict[str, Any]:
        """A description the UI may render. It is *not* the enforcement point — the routes are."""
        pol = self.policy(role)
        return {
            "role": pol.role,
            "admin": pol.role == ADMIN,
            "workflows": {"all": pol.all_workflows, "ids": sorted(pol.workflows),
                          "categories": sorted(pol.categories)},
            "models": {"all": pol.all_models, "folders": sorted(pol.model_folders)},
            "upload": pol.allow_upload,
            "max_active_jobs": pol.max_active_jobs,
            "max_jobs_per_day": pol.max_jobs_per_day,
            "max_output_gb": (round(pol.max_output_bytes / GB, 1)
                              if pol.max_output_bytes else None),
        }

    def describe(self) -> dict[str, Any]:
        return {"source": self.source, "error": self.error,
                "roles": {r: self.capabilities(r) for r in ROLES}}
