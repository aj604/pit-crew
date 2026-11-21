# Playbook: artifact-capture (companion)

Not a speedup on its own. Apply it **with** another playbook when the job you change runs
browser or end-to-end tests (Playwright, Cypress, WebdriverIO, Puppeteer) and uploads no failure
recordings. The guard warns when that is the case.

Why: the Proof lane shows Gemini 4 Carbon the baseline's and the candidate's failure recordings
side by side to confirm a replayed bug fails the same way. Without recordings a replay stays
*unverified*, and the proof card cannot show it. Recording only on failure costs nothing on a
green run, so it does not affect the timing the rater measures and does not count as a second
change.

## Change

1. Have the runner keep recordings of failing tests only:

   | Runner | Config |
   |---|---|
   | Playwright | in `playwright.config.*`, under `use`: `video: 'retain-on-failure'`, `screenshot: 'only-on-failure'`, `trace: 'retain-on-failure'` |
   | Cypress | in `cypress.config.*`: `video: true`, `screenshotOnRunFailure: true` (videos of passing specs can be deleted with `videoUploadOnPasses: false` on older versions) |
   | WebdriverIO, Puppeteer | take a screenshot in the framework's after-test hook when the test failed |

2. Upload them from the workflow, only when the job failed:

```yaml
- uses: actions/upload-artifact@v4
  if: failure()
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

- `if: failure()` on the upload. An unconditional upload slows every green run.
- Existing recordings settings. Never turn video, screenshots or traces off; the guard rejects it.
- Everything else from the main playbook's rules; this companion only adds recordings.
