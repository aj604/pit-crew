# Playbook: artifact-capture (companion)

Not a speedup on its own. Apply it **with** another playbook when the job you change runs
browser or end-to-end tests (Playwright, Cypress, WebdriverIO, Puppeteer) and has no upload that
carries failure recordings. The guard warns when it can tell from the workflow. When a job runs a
package script (`npm run test:e2e`), read the script in `package.json` and decide yourself.

Why: the Proof lane shows Gemini 4 Carbon the baseline's and the candidate's failure recordings
side by side to confirm a replayed bug fails the same way. Without recordings a replay stays
*unverified*, and the proof card cannot show it.

## Recording is not free, so it is switched off in timing runs

`retain-on-failure` still records every test and throws the passing ones away, and Cypress
`video: true` records passing specs too. That costs time on green runs. If only the candidate
recorded, the rater would compare a recording candidate against a non-recording baseline and
under-report the speedup. So recording is **off unless `PIT_CREW_RECORD` is set**:

- Timing runs never set it. Baseline and candidate are timed the same way, and the change does not
  count as a second change.
- Replay runs set it (`PIT_CREW_RECORD=1`) on **both** sides, so both produce recordings under the
  same capture policy. For the baseline side, the dispatcher applies this companion's config the
  same way it overlays the candidate's files.

## Change

1. Have the runner record failing tests only when `PIT_CREW_RECORD` is set:

   | Runner | Config |
   |---|---|
   | Playwright | in `playwright.config.*`, under `use`: `video`, `trace` set to `process.env.PIT_CREW_RECORD ? 'retain-on-failure' : 'off'`, and `screenshot` to `process.env.PIT_CREW_RECORD ? 'only-on-failure' : 'off'` |
   | Cypress | in `cypress.config.*`: `video: Boolean(process.env.PIT_CREW_RECORD)`, `screenshotOnRunFailure: true` |
   | WebdriverIO, Puppeteer | in the after-test hook, take a screenshot when the test failed and `PIT_CREW_RECORD` is set |

   Never turn off a recording setting the repository already has; the guard rejects it. Only gate
   the settings you add.

2. Pass the switch through, and upload only when the job failed:

```yaml
env:
  PIT_CREW_RECORD: ${{ vars.PIT_CREW_RECORD }}   # unset in timing runs; the dispatcher sets it for replays
steps:
  # ... the job's existing steps ...
  - uses: actions/upload-artifact@v4
    if: failure() && env.PIT_CREW_RECORD != ''
    with:
      name: failure-recordings-${{ github.job }}-${{ strategy.job-index }}
      path: |
        test-results/
        playwright-report/
        cypress/videos/
        cypress/screenshots/
      retention-days: 7
      if-no-files-found: ignore
```

Use a unique `name` per job and matrix row (`strategy.job-index` does that), or parallel shards
overwrite each other's uploads.

## Keep

- `if: failure()` on the upload, and recording off unless `PIT_CREW_RECORD` is set.
- Existing recording settings. Never turn video, screenshots or traces off.
- Everything else from the main playbook's rules; this companion only adds recordings.
