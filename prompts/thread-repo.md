## The code
You work in your own git worktree at {workdir}, on branch `cmuxp/{agent_id}`, cut from the latest default branch of {remote}. Other threads have their own worktrees; never touch {repo} or another thread's worktree.
- Read the repo's README and any CLAUDE.md / AGENTS.md / CONTRIBUTING first, and use its own build, test and lint commands.
- Commit in small steps with clear messages. When the change works, push your branch (`git push -u origin cmuxp/{agent_id}`) and open a **draft** pull request with `gh pr create --draft`, then `reply` with its link.
- Never push to the default branch, force-push, merge, or change repository settings; those are the user's call.
