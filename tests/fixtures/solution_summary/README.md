# Solution Summary fixtures

`tests/test_solution_summary.py::test_same_summary_as_the_portal` checks that this repo's Solution Summary step and the
Engineering Direct portal's agent (`hnd.summarize` in mchung042/product_gt_microsoft_fall_2026) give the same summary
for the same run.

- `runs/`: 17 saved runs in the `result.json` format.
  - 15 are the troubleshooter demo routes, made by `recreator.solution_summary.demo` (demo data).
  - 2 are `mattermost-39212-*`, hand-written runs of a real public Mattermost bug (issue #39212). Their patch and
    screenshots come from the real fix (PR #39216). `suspects[].excerpt` is a proposed field (see `docs/solution-summary.md`).
- `expected/`: the summary the portal's TypeScript agent made from each run (`null` = nothing to summarize).

If either side changes how a summary is built, regenerate `expected/` from the portal and update both together.
