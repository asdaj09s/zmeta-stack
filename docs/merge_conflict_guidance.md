# Choosing between incoming and current changes

When Git reports a merge conflict, the prompts "accept current change" and "accept incoming change" refer to two sides of the conflict:

- **Current change**: The code already in your branch (your local edits).
- **Incoming change**: The code coming from the branch you are merging or rebasing onto.

## How to decide
1. **Review intent on both sides**
   - Read the surrounding context and commit messages for each branch.
   - Identify what each change is trying to accomplish (bug fix, feature, refactor).
2. **Prefer correctness and recency**
   - If the incoming change fixes a bug or reflects the latest contract (e.g., API shape, schema), keep it and reapply any local improvements on top.
   - If your current change adds required functionality that the incoming side lacks, keep your version or combine them.
3. **Check compatibility**
   - Look for assumptions (function signatures, config names, data formats). Choose the version that matches the current codebase and tests.
   - If both versions are valuable, manually merge the logic instead of choosing one side wholesale.
4. **Verify with tests**
   - After resolving, run the relevant test suite or linters to confirm the merged code works.

## Practical commands
- Accept current side: `git checkout --ours -- <file>`
- Accept incoming side: `git checkout --theirs -- <file>`
- Open an interactive tool: `git mergetool` (configure your preferred tool first).

When unsure, avoid quick "accept" actions. Manually edit the file to combine the best parts, then retest.

## Which side are “Codex’s changes”?
If you are merging a branch that contains Codex’s updates into your own branch, those updates are on the **incoming** side. Use “accept incoming change” (or `git checkout --theirs -- <file>`) to take Codex’s version. If you are on the branch Codex authored and are merging someone else’s work in, then Codex’s changes are the **current** side instead. Identify which branch you have checked out to know which button corresponds to the version you want.
