# Agent playbook

`AGENTS.md` is the authoritative operating guide for coding agents in this repository.

This document is a quick index for humans dispatching agents.

## Minimal prompts

Because the repository carries the detailed rules, dispatch prompts can stay short.

### Implement

    Take issue #N in banddude/officeadmin-building-model. Read and follow AGENTS.md, the full issue/comments, relevant docs, and the merged #2 canonical model contract. Work autonomously through PR completion. Do not merge your own implementation PR.

### Fix

    Fix PR #N in banddude/officeadmin-building-model. Read and follow AGENTS.md, the underlying issue, the merged #2 contract, the full PR, and every review comment. Address all blocking findings, add regressions, run full tests, and push to the existing PR. Do not merge it yourself.

### Independent review

    Independently review PR #N in banddude/officeadmin-building-model. Follow AGENTS.md. Verify it against the underlying issue, merged #2 contract, full diff/comments/checks, and run additional tests as needed. Merge only if it fully satisfies the issue and repo rules. Otherwise leave precise blocking findings.

### Compare duplicate PRs

    For issue #N in banddude/officeadmin-building-model, independently compare all open competing PRs. Follow AGENTS.md. Review each against the issue and merged #2 contract. Merge only the qualifying best implementation, close redundant PRs, or merge none if no candidate fully qualifies.

## Why prompts are intentionally short

The agent should not depend on private chat history. Requirements belong in versioned repository documents, GitHub issues, PR reviews, tests, and fixtures.

If a future agent cannot understand an assignment from those sources, improve the repository documentation or issue rather than making dispatch prompts permanently larger.
