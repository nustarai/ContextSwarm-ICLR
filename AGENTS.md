# ContextSwarm ICLR Mini local contract

- This directory is an independent artifact. Do not edit or reset the sibling
  upstream `ContextSwarm` worktree.
- Keep Mono and Parallel communication-free. New CPS policies must be selected
  by manifest and must preserve the same task/model/time/evaluator contract.
- Do not put NuRouter/AISW tokens, node.toml contents, or private endpoints in
  tracked files or run summaries.
- Before handing off a change, run `python3 -m compileall -q contextswarm_mini`,
  `python3 -m unittest discover -s tests`, and a `configs/smoke.toml` mock run.
