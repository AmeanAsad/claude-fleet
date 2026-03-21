## Review Focus Areas (required)

<!-- Tag specific files or areas by review depth. Use `/review-focus` to auto-generate this. -->
<!-- Not every file in a PR needs the same scrutiny. -->

| File / Area | Focus | Why |
|-------------|-------|-----|
| `example/crypto.rs` | Critical | Attestation verification |
| `example/handler.rs` | Standard | New API endpoint |
| `example/README.md` | Light | Docs update |

<!-- Focus levels:
  Critical — crypto, attestation, auth, TEE trust boundaries. Review every line.
  Standard — business logic, APIs, data flows. Thorough review.
  Light — docs, config, formatting. Quick skim.
-->

## What Changed

<!-- 1-3 sentences. If you need more, the PR is probably too big. -->



## Why

<!-- What problem does this solve? Link to issue if applicable. -->



## Design Doc

<!-- Required for large PRs (>500 lines or architectural changes). Link or N/A. -->



## Checklist

- [ ] New dependencies justified (if any)
- [ ] Tests verify invariants, not just output format
- [ ] No secrets in code, logs, or error messages
- [ ] For TEE code: reproducible build maintained
- [ ] For crypto code: constant-time, zeroization, audited libraries only
