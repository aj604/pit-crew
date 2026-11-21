# Pit Crew — Brand Guidelines (Las Vegas Night Race Spec v2.0)

**Tagline:** *Faster CI. Proven on every bug.*  
**Core Rule:** *The agent proposes. Deterministic code judges.*  
**One-Liner:** An autonomous managed deep agent that speeds up CI pipelines and proves—through deterministic bug replays and permutation tests—that every historical bug is still caught.

---

## 1. Visual DNA: Formula 1 × Las Vegas Grand Prix

Pit Crew fuses the **Formula 1 master visual system** (engineered speed, carbon depth, square technical grids, broadcast timing towers, starting gantries, and red/white curbs) with the **Las Vegas Strip night race** (floodlit obsidian asphalt, Sphere LED halos, neon magenta/purple/cyan telemetry glow, and high-visibility contrast).

### Signature Visual Motifs
1. **5-Light Starting Gantry:** 4 red lights + 1 DRS green light representing the 5-stage pipeline (`Analysis → Optimizer → Proof → Fork CI → Rater Verdict`).
2. **Las Vegas Night Circuit:** A stylized street-circuit loop mapping Proposal Sectors (`S1`, `S2`, `S3`) inside the Managed Agent Sandbox, separated by a dashed **Red Trust Wall** from deterministic Measurement (`Pit Straight`) and Verdict (`Finish Gantry`).
3. **Broadcast Timing Tower:** Left-anchored telemetry leaderboard using 3-letter lane codes (`ANA`, `OPT`, `PRF`, `DSP`, `RAT`), tabular monospace timings, and sector color bars.
4. **Red/White Apex Kerbs & Square Grid:** `8px` square grid rhythm with bold F1 kerb trims on hero banners and cards.
5. **Pirelli-Style Compound Rings:** Distinct color-coded tire rings for each Optimizer playbook (`dep-cache`, `shard-by-timing`, `affected-tests`, `fail-fast-order`, `flake-quarantine`, `dedupe-jobs`).

---

## 2. Color System

| Swatch | Hex | Origin | Semantic Role |
|---|---|---|---|
| **Void Asphalt** | `#0B0B11` | Vegas Night Track | Deepest backdrop / canvas |
| **F1 Carbon Black** | `#15151E` | F1 Master Brand | Primary dark surface & cards |
| **Paddock Slate** | `#1C1C28` | Pit Wall Panels | Elevated interactive containers |
| **F1 Warm Red** | `#FF1E00` | F1 Master Brand | Primary brand signal, Trust Wall, Red Flag / Reject |
| **Strip Neon Magenta** | `#FF008C` | Las Vegas Strip | Dispatcher / Fork CI measurement runs |
| **Sector Purple** | `#B84DFF` | F1 Fastest Lap | Record CI speedup / Optimizer lane |
| **Sphere Cyan** | `#00F0FF` | Vegas Sphere LED | Live telemetry stream / Analysis lane |
| **Vegas Gold** | `#FFD000` | Safety Car / Caution | Proof lane / Bug replay verification |
| **DRS Green** | `#00E676` | Track Clear / DRS | Rater Verdict `KEEP`, all bugs caught |
| **High-Vis White** | `#F4F4F7` | F1 Off-White | Primary text, racing stripe, Checkered Flag |

**Contrast Rule:** Never place neon accents (`#FF008C`, `#B84DFF`, `#00F0FF`, `#FFD000`, `#00E676`) on light backgrounds without darkening them by at least 20% or placing them inside a `#15151E` carbon badge.

---

## 3. Typography

- **Engineered Display:** `Chakra Petch` (700 Italic, uppercase, `0.06em–0.10em` tracking) — echoes F1 Wide/Bold for wordmarks, lap headers, and hero titles.
- **Mechanic Sans:** `Titillium Web` (with `Inter` fallback, 400/600/700) — F1's own web editorial workhorse for body copy, documentation, and PR explanations.
- **Telemetry Mono:** `JetBrains Mono` (`tabular-nums`, 500/700) — mandatory for every timing delta (`-142.4s`), p-value (`p=0.004`), SHA, lane code, and compound label.

---

## 4. Logo Rules & Clear Space

- **Clear space:** Keep at least `0.5×` the mark height free on all sides.
- **Minimum size:** `24px` height for `logo-mark.svg`; `140px` width for horizontal lockups.
- **Do not:** Recolor the F1 Warm Red chassis to green/yellow, drop the italic forward slant on the wordmark, stretch proportions, or place the dark lockup on busy photography without a `#15151E` carbon scrim.

---

## 5. Race Control Flag Language & Voice

Pit Crew writes like a **calm race engineer on the pit wall**: terse, data-first, zero hype, transparent about statistical uncertainty.

| Flag | System State | Standard Copy Pattern |
|---|---|---|
| 🟢 **GREEN FLAG** | `Verdict(keep=True)` | `VERDICT: KEEP // -42.8% median CI (p=0.004) • 12/12 bugs caught` |
| 🟣 **PURPLE SECTOR** | New repo-best CI time | `PURPLE SECTOR // New fastest workflow median: 3m 09s (-2m 22s)` |
| 🟡 **YELLOW FLAG** | `< 5` samples or `p > 0.05` | `YELLOW FLAG // Speedup not statistically significant (p=0.14, N=5)` |
| 🔴 **RED FLAG** | `bugs_caught < bugs_total` or `0` replays | `RED FLAG // Rejected: missed bug replay 8a4f91c (11/12 caught)` |
| 🏁 **CHECKERED FLAG** | Upstream PR opened | `CHECKERED FLAG // PR opened with verified telemetry ledger #cand_042` |

---

## 6. Optimizer Compound Taxonomy

Each Optimizer playbook maps to an F1 compound ring so PRs, dashboards, and CLI logs communicate strategy at a glance:

| Ring | Compound | Playbook (`Hypothesis.skill`) | Strategy |
|---|---|---|---|
| 🔴 `S` | **SOFT** (`#FF1E00`) | `dep-cache` | Fast grip: dependency & build caching |
| 🟡 `M` | **MEDIUM** (`#FFD000`) | `shard-by-timing` | Balanced pace: test suite timing sharding |
| ⚪ `H` | **HARD** (`#F4F4F7`) | `affected-tests` | High-mileage precision: change-scoped execution |
| 🟢 `I` | **INTER** (`#00E676`) | `fail-fast-order` | Early signal: reorder fast/high-signal checks first |
| 🔵 `W` | **WET** (`#00B0FF`) | `flake-quarantine` | Wet-track stability: isolate non-deterministic tests |
| 🟣 `HS` | **HYPER** (`#FF008C`) | `dedupe-jobs` | Strip excess weight: eliminate redundant CI jobs |

---

## 7. GitHub PR Verdict Comment Template

Use this canonical structure when `pit-crew[bot]` opens an upstream PR after `Verdict(keep=True)`:

```markdown
### 🏁 PIT CREW // VERDICT: KEEP

> **Compound:** `[M] shard-by-timing` · **Candidate:** `cand_042` · **Trust Boundary:** Measured on fork Actions, judged by `rater.py`

| Telemetry Metric | Baseline (`N=10`) | Candidate (`N=10`) | Delta |
|---|---:|---:|---:|
| **Median Wall-Clock** | `5m 32s` | `3m 09s` | **🟣 `-42.8%` (`-142.4s`)** |
| **Exact Permutation Test** | — | `p = 0.004` | **🟢 `p <= 0.05` PASS** |
| **Bug Replays Caught** | `12 / 12` | `12 / 12` | **🟢 `100%` SAFE** |

<details>
<summary>Replay Proof Ledger (12/12 historical red commits failed as expected)</summary>

- `8a4f91c` — `test_auth_token_expiry` → ❌ Caught (expected)
- `3d91b0e` — `test_ledger_idempotency` → ❌ Caught (expected)
</details>
```

---

## 8. Complete Asset Inventory

| Asset | File | Use Case |
|---|---|---|
| Primary Mark | [`logo-mark.svg`](./logo-mark.svg) | App icons, headers, slide corners |
| Monochrome Marks | [`logo-mark-mono-light.svg`](./logo-mark-mono-light.svg), [`logo-mark-mono-dark.svg`](./logo-mark-mono-dark.svg) | Watermarks, print, dark/light single-ink |
| Horizontal Lockups | [`logo-lockup-dark.svg`](./logo-lockup-dark.svg), [`logo-lockup-light.svg`](./logo-lockup-light.svg) | Docs navbars, decks, partner walls |
| Stacked Badge Lockup | [`logo-stacked-dark.svg`](./logo-stacked-dark.svg) | Stickers, square cards, merch |
| Favicon | [`favicon.svg`](./favicon.svg) | Browser tab icon |
| GitHub Bot Avatar | [`github-bot-avatar.svg`](./github-bot-avatar.svg) | `pit-crew[bot]` GitHub App avatar |
| README Hero Banner | [`readme-hero.svg`](./readme-hero.svg) | Top of `README.md` (`1200×360`) |
| Social / OG Card | [`social-card.svg`](./social-card.svg) | GitHub social preview, X, LinkedIn (`1280×640`) |
| Circuit Architecture | [`circuit-architecture.svg`](./circuit-architecture.svg) | Docs & pitch deck pipeline diagram |
| Status Badges | [`badges/verdict-keep.svg`](./badges/verdict-keep.svg), [`badges/verdict-reject.svg`](./badges/verdict-reject.svg), [`badges/proof-caught.svg`](./badges/proof-caught.svg), [`badges/powered-by-pit-crew.svg`](./badges/powered-by-pit-crew.svg) | READMEs, PR comments, status dashboards |
| Design Tokens | [`tokens.css`](./tokens.css), [`tokens.json`](./tokens.json) | Web UI, CLI themes, Tailwind/CSS vars |
| Visual Brand Sheet | [`brand-sheet.html`](./brand-sheet.html) | Interactive standalone brand board |
