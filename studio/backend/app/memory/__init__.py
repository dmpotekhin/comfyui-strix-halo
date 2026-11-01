"""Model Memory Estimator — advisory, read-only, no engine calls.

The package answers one question: *how much memory will this workflow need on this machine,
right now?* It never changes what the studio does; Step 6 is the only stage allowed to act on an
estimate (admission), and this one deliberately does not.

Public surface:

    catalog   Model Memory Catalog   — what each model file costs (derived from the files)
    analyze   Workflow Analyzer      — graph + parameters -> memory components
    history   Historical Profiler    — job_telemetry -> comparable observations
    estimate  Estimator              — pure function: components + measurements -> forecast
    probe     Live snapshot          — MemAvailable, cgroup, GPU counters, kept apart
"""
from __future__ import annotations

__all__ = ["catalog", "analyze", "history", "estimate", "probe"]
