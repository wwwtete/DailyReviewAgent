# Daily Review Agent Implementation Plan

> For agentic workers: use superpowers:executing-plans; this is a standalone deliverable, not an existing Git checkout.

**Goal:** Independent daily review from Codex, Lark, Git, browsers with deny-by-rule collection and durable state.

**Architecture:** Python collectors write normalized SQLite events. A tool-free Responses request generates evidence-based reports and proposed actions. A launchd tick calls the same CLI with disk state and explicit timezone.

**Tech Stack:** Python 3.9+, standard library, tomli on Python <3.11, existing lark-cli/git.

**Spec:** design.md

## Global Constraints
- Default allow within discovered accessible sources; deny rules applied before body reads where source APIs permit.
- No dependence on this chat, no generated source commands executed, no secrets in logs.
- User confirmed no blacklist, daily 10:00 Asia/Shanghai reviewing the previous full calendar day.

## Review Focus
- Forbidden resources must not reach database or model; late blacklist changes invalidate derived history.
- Failed pagination is a partial source, not an empty successful day.
- Repeated runs and restart must not duplicate events or actions.
- Timezone boundaries, late-night work and sleep catch-up must preserve the intended date.
- Model failure must be visible, and untrusted source text cannot trigger tools.

### Task 1: Policy and durable store
Files: daily_review/core.py, tests/test_core.py.
- [x] Test policy default allow, path boundary, domains, old-event filtering, idempotence, lock and dates.
- [x] Implement Policy, Store and event helpers; run unittest.

### Task 2: Collectors
Files: daily_review/collectors.py, tests/test_collectors.py.
- [x] Use synthetic Codex/browser databases, temporary Git repository and fake CLI response fixtures.
- [x] Test pre-read deny, browser boundaries, no stale task completion claim, pagination failures.
- [x] Implement collectors and source status; run unittest.

### Task 3: Reports and CLI scheduling
Files: daily_review/report.py, daily_review/cli.py, config.json, prompt.md, scripts and README.
- [x] Test model input filtering, response parsing, model failure drafts, persistent actions and scheduling catch-up.
- [x] Implement chunked tool-free model calls, CLI and launchd installer.
- [x] Run synthetic end-to-end, live readiness checks, and authorized real runs.

### Task 4: Review and delivery
- [x] Fresh review of source filtering, persistence, scheduling and failure behavior; fix material findings.
- [x] Run full tests, compile, CLI smoke, launchd plist validation, document observed deployment status.
