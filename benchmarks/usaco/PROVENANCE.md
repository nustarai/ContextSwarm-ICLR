# USACO Season 26 Contest 3 provenance

This suite is the twelve-problem USACO 2026 Third Contest, CPIDs 1587--1598.
Unlike a Git-hosted formal benchmark, the authoritative source is the live
USACO contest page, individual statement pages, and official test-data ZIPs.
The ContextSwarm contest definitions were introduced in commit
`9318cb9e88b27fb3cd16c675f8f348f4abd5e702`; the current public-only metadata
projection was checked in by commit
`92a98684ae3aefddb14bb3c1130c9e8de7fd486b`.

The source was fetched again on 2026-08-22 with
`ContextSwarmJudge/scripts/import_legacy_usaco_season26.py` from the pinned
Judge revision
[`cc16c9768c659f3bfc1b0536f9de0b06317a180f`](https://github.com/shiyegao/ContextSwarmJudge/commit/cc16c9768c659f3bfc1b0536f9de0b06317a180f),
resident service version `2026-08-22.07`. Each public metadata entry records the
official URLs plus SHA-256 digests for cleaned statement text, the downloaded
ZIP, the ordered extracted test corpus, and the combined content contract.
These content hashes are the immutable source identity because the publisher
does not provide a Git commit for the live pages or ZIPs.

The audit found a ContextSwarm projection bug, not an error in the official
problem statements: the old checked-in public text included material outside
`span#probtext-text`, including contest status, page JavaScript, and Cloudflare
code. The refreshed projection keeps only the official problem span and sample
input/output. It deliberately retains `num_tests = 0`: hidden tests are resident
Judge data and must not be committed to ContextSwarm.

The resident corpus has 185 paired cases (370 `.in`/`.out` files). Official
hidden test counts in CPID order are
`13, 12, 12, 11, 23, 11, 11, 23, 21, 21, 13, 14`. Formal experiment preflight
requires the exact twelve-problem id set, `problem_count = ready_problem_count =
12`, every per-problem count/contract id/corpus hash/readiness record, and the
whole resident inventory digest
`72aad502630fc27913012d2e689d7604a4a61ec2b4847265ba25ca9c437a9221`.
The corresponding public twelve-problem contract digest is
`70a802bf47b23ff384fd5755940fae69429b4327bcaf68a1b2345f0c754de1f7`.
Missing or extra problems and any count or digest drift fail closed before
workers start. A public-only checkout therefore cannot silently act as a
zero-test judge.

Problems 1589, 1597, and 1598 are constructive/output-flexible tasks and use
the bundled semantic checkers. During this audit, their large outputs exposed a
second repository-local bug: the Rust OJ applied its roughly 5 MB outward
transport bound before the checker, so a correct candidate could be checked
against truncated text. The corrected pipeline captures up to 16 MiB, lets the
semantic checker consume that complete capture, and only then bounds the HTTP
response. A regression candidate whose output is 6,000,021 bytes, ending in a
sentinel, is accepted by the checker while the outward stdout is 5,000,012
bytes, marked truncated, and does not expose the sentinel. ContextSwarmJudge
also omits successful custom-checker output from response/cache/ledger records
and bounds failure diagnostics to 4 KiB.

USACO 1597 makes that separation necessary rather than hypothetical: official
oracle files 8--13 range from 9,077,541 to 13,596,619 bytes, all above the
outward transport limit but below the execution capture limit. The first
canonical recheck also found that ordinary C++ submissions were being compiled
with implicit AddressSanitizer/UndefinedBehaviorSanitizer instrumentation. The
official 1597 solution then used 276656 KiB on case 8 and exceeded the published
256 MiB limit. Judge revision `9a0183e1` removes implicit sanitizer flags from
the default production profile, pins it to `-O2 -std=c++17 -DONLINE_JUDGE`, and
retains sanitizer instrumentation only when explicitly requested. This was a
local execution-semantics defect, not evidence that USACO 1597 was unsolvable.

The post-fix release canaries were launched from the canonical Judge checkout at
revision `9a0183e1`, using the canonical release OJ and resident digest above.
They exercise the real resident Judge and all cases:

| Problem | Official solution page | Extracted C++ SHA-256 | Result | Response | Max memory |
| --- | --- | --- | --- | --- | --- |
| `1589_bronze_swap_to_win` | `sol_prob3_bronze_season26contest3.html` | `96eb4c4e05835d79edb07ac8e88dda4017fabd0711371ec543ddde0ab02d99e2` | `AC 12/12` | 3210 B | 8424 KiB |
| `1597_platinum_blast_damage` | `sol_prob2_platinum_season26contest3.html` | `8a10676d6947429bc299672c5476da2e0d7c6d3acd7768e33a7bf2b533f3625d` | `AC 13/13` | 3439 B | 17536 KiB |
| `1598_platinum_min_max_subarrays_ii` | `sol_prob3_platinum_season26contest3.html` | `b188340248c88bade550dbb9d32d6ec1ee4ed93fec23d0d08643ff3708d0906a` | `AC 14/14` | 3647 B | 39620 KiB |

For 1597 specifically, repaired canonical case 8 used 12556 KiB and 200 ms,
compared with the instrumentation-induced 276656 KiB MLE before the compiler
profile correction. The three bounded ledger records total 38439 bytes and all retained per-test candidate-output fields are empty.

The hashes identify the extracted C++ source submitted to the Judge, not the
surrounding HTML page. `benchmark_integrity.json` is the machine-readable
authority for these pins and results; per-problem source/test identities remain
in `public_dataset/usaco_2025_dict.json`.
