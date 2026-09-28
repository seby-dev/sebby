# Benchmark data

The `revgate bench` command replays recorded review cases against the static
reviewer. Its inputs and the research prototypes behind them don't live in this
repository. This file describes where they are, what each one is, and how to check
them.

## Where the data lives

The data lives in `~/Developer/revgate-bench-data/`, or in the directory that the
`REVGATE_BENCH_DATA` environment variable names. The directory holds the following
entries:

- `inputs/`: the benchmark inputs.
- `prototypes/`: the research prototypes, kept as reference code.
- `MANIFEST.sha256`: a SHA-256 checksum for every file in `inputs/` and
  `prototypes/`.

The data stays outside this repository because this repository is public, and the
inputs hold reviewer transcripts and findings over a private project's code. The
directory isn't a git repository. Don't run `git` inside it: under a home directory
that has its own `.git`, the command resolves to that repository instead of failing.

The replay cases that reference the private project live in that project's own
repository, not here. This repository's tests use synthetic fixtures only.

## Input files

The inputs were copied on September 28, 2026, from a research session's scratchpad.
The following table names each input file and its source path under that
scratchpad's `algo/` directory, or under `research/` where the path says so:

| File under `inputs/` | Source under `scratchpad/algo/` |
|---|---|
| `d_findings.json` | `research/D-empirical-findings.md`'s 51 findings, normalized by hand |
| `d_empirical_findings.md` | `research/D-empirical-findings.md`, copied unchanged |
| `r1_recs3.json`, `r1_commits250.txt`, `r1_brief_triples.json` | `r1-emulation/recs3.json`, `r1-emulation/proto/commits250.txt`, `r1-emulation/brief_triples.json` |
| `r1_crit_imp_dump.md` | `r1-emulation/crit_imp_dump.md` |
| `r2_*.jsonl` | `r2-behavioral/failfirst_results.jsonl`, `hunkrevert_results.jsonl`, `amplify_results.jsonl`, `behavediff_results.jsonl` |
| `r3_corpus_results2.json`, `r3_judged_list.txt` | `r3-static/corpus_results2.json`, `judged_list.txt` |
| `r5_tasks.json`, `r5_dangling.json`, `r5_gaming_per_task.json`, `r5_review_fix_commits.json` | `r5/` files of the same names |
| `rb_plan_edges.json`, `rb_fix_validation.json`, `rb_pin_caps.json` | `review-b/plan_edges_lenient.json`, `fix_validation.json`, `pin_caps.json` |
| `ra_redteam.jsonl`, `ra_fix_sample40.txt`, `ra_deltas/` | `review-a/redteam_results.jsonl`, `fix_sample40.txt`, `delta_*.txt` |

No `trees/` directory was copied. Those directories held checked-out source trees,
and a replay reads historical trees with `git archive` and `git show` instead.

## The normalized findings

`inputs/d_findings.json` is a JSON list of 51 objects, one per finding, sorted by
`id` from `D#1` to `D#51`. It was normalized by hand on September 28, 2026. Every
object has the following keys:

- `id`: the finding's number, as `D#N`.
- `repo`: the repository the finding is about.
- `base` and `head`: the full commit SHAs of the reviewed range. For a review of one
  commit, `base` is that commit's parent.
- `file`: the repository-relative path of the finding's anchor.
- `symbol`: the anchor's name. A Python name is qualified with its module path; a
  TypeScript, Dart, or JavaScript name is the bare name.
- `lines`: the anchor's line range at `head`, as `[start, end]`.
- `category`: one of `contract`, `wiring`, `error-flow`, `docs`, `test`, `ui-state`,
  `domain`, `concurrency`, `config`, `plan`, or `other`, chosen from the finding's
  mechanism.
- `severity`: `critical`, `important`, or `minor`. A reviewer's "High" maps to
  `important`, and "Medium", "Low", or an unrated finding maps to `minor`.
- `text`: the finding in one sentence.
- `mechanism`: the coverage appendix's mechanism for the finding, copied verbatim.

A `null` field means that the finding's source didn't state that value, not that the
value is empty. A replay case treats a `null` `base` or `head` as a finding it can't
replay, and a `null` `file`, `symbol`, or `lines` as an anchor it can match only
more loosely. Line numbers come from the reviewer's own citation and are valid only
at `head`.

## Prototypes

`prototypes/` holds the research prototypes, the Python and JavaScript files that the
research stages used, with their paths under `algo/` preserved. They're reference code
only: `revgate` never imports them, and they aren't part of the package.

## Check the data

To check that the data is complete and unchanged, run the following command:

```bash
cd ~/Developer/revgate-bench-data && shasum -a 256 -c MANIFEST.sha256
```

Every line of the output ends with `OK`. A line that ends with `FAILED` names a file
that changed after the manifest was written.
