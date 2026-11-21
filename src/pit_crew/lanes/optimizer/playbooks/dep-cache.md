# Playbook: dep-cache

Use when a job spends its setup time downloading or building dependencies that did not change.

## Change

Prefer the cache built into the setup action the job already uses:

| Ecosystem | Change |
|---|---|
| Node | `actions/setup-node` with `cache: npm` (or `pnpm`, `yarn`); install with `npm ci` |
| Python, pip | `actions/setup-python` with `cache: pip` and `cache-dependency-path` set to the lockfile |
| Python, uv | `astral-sh/setup-uv` with `enable-cache: true` |
| Rust | add `Swatinem/rust-cache` after the toolchain step |
| Go | `actions/setup-go` caches by default; make sure `cache` is not `false` |
| Docker | `docker/build-push-action` with `cache-from: type=gha` and `cache-to: type=gha,mode=max` |

Only if no setup action fits, add `actions/cache` keyed on the OS and a hash of the lockfile:
`key: ${{ runner.os }}-deps-${{ hashFiles('**/package-lock.json') }}`.

## Keep

- Every check command after the install, unchanged.
- The key must change whenever the lockfile changes, so a stale cache can never mask a broken install.
- Do not cache test results, coverage, or build outputs that a check verifies.

## Example

```yaml
- uses: actions/setup-node@v4
  with:
    node-version: 22
    cache: npm
- run: npm ci
- run: npm test
```
