## Test upgrades from every supported previously-working state before promoting a release
### Clean-install coverage does not protect existing users from destructive upgrade regressions.
- Cover Codex-only, Claude-only, both-agent, legacy-layout, current-layout, missing-command, and partially removed installations.
- For every failure, assert that identity, configuration, registrations, queues, enrolled projects, and capture state remain unchanged or are restored exactly.

## Make failed upgrades leave users no worse off as a release invariant
### Installers and migrations must preflight first, mutate transactionally, and retain a verified rollback source.
- Prove compatibility, executable availability, and rollback feasibility before removing or replacing anything.
- Preserve identity and user data during recovery without requiring a new claim, invite, or manual file editing.

## Promote coordinated components as one release train
### A fix spanning an installer, prompt generator, backend proxy, and deployment is incomplete until every boundary is verified.
- Require the immutable installer release before promoting a prompt that references it.
- Verify the production alias, exact deployed commit, generated prompt version, and recovery mode before declaring rollout complete.

---

### Known gaps
- Add upgrade fixtures that exercise actual Codex and Claude command adapters; the current CI matrix is cross-platform but primarily mocked and unit-level.
- Automate the gate from installer tag and immutable release through prompt promotion and production verification.
- Document one canonical incident runbook with the exact member message, internal checks, rollback procedure, and do-not-delete-data rule.
