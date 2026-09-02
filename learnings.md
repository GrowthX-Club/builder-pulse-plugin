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

## Verify every installer change against the real agent binary before promoting it
### Fifty-one iterations passed mocked unit tests while the real Codex install never worked once.
- `tests/integration/` runs the real `codex` (and optionally `claude`) in an isolated HOME against a loopback stand-in for the service; no installer change merges without those scenarios passing.
- Reproduce the member's exact broken state first (seed snapshots), then fix; every one of the five v0.5.x root causes was found this way within an hour.

## Hook trust is a content hash of the hook definition; treat hooks.json as frozen
### Changing the hook command in v0.4.6 silently forced a `/hooks` review on every member, and the installer's rollback uninstalled the plugin they had to approve.
- `hooks/hooks.json` stays byte-identical to v0.4.5 (test-enforced); a change costs every member a review.
- Review required is a state (exit 3, plugin left installed and enabled), never a failure that rolls back.

## Agent tools write runtime files into installed plugin folders; never byte-compare installed trees
### Codex writes `.codex-marketplace-install.json`; Claude Code writes `.in_use/<pid>`, `.orphaned_at`, `.links_materialized`.
- Verify an installed package by its manifest name/version and the immutable tag it was installed from, not by hashing every file.
- Provenance of the previous package is not the installer's job: Codex pins the tag; rollback is "reinstall the previous tag".

## Every failure must leave a redacted log and end with `Details: <path>`
### A member whose terminal closed had nothing to send; the real error was discarded in favour of "activation failed".
- Log every step and command (secrets, home directory and project paths redacted) under `~/.builder-pulse/logs/`.
- Print the machine-readable reason (`hookStatus`, `detail`) in the human error, never a generic sentence.

## Prompts are executable interfaces; agents do exactly what the text allows
### Agents `cd` into the temporary clone and let the installer enroll it as the project.
- The installer refuses temp folders and its own checkout as project roots, and repair never enrolls without an explicit confirmation.
- Say once what the installer guarantees instead of restating every rule to the agent; shorter prompts are followed more reliably.

## Release train: tag, immutable release, then the prompt, then the deploy
### The backend on Vercel deploys manually; a merged prompt pointing at a missing tag stops every member at step one.
- Order: plugin PR → CI + harness → merge → tag → immutable release → harness against the tag → backend prompt PR → manual `vercel deploy --prod` → verify the production prompt via the platform API.
