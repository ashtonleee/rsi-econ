---
name: git-clean
description: End-of-session cleanup — audit state, squash-merge work to main with clean authorship, push to GitHub. Safe, interactive, min-regret.
user_invocable: true
---

# /git-clean — End-of-Session Git Cleanup

Safe, interactive session wrap-up. Audit first, confirm everything, never nuke anything without explicit approval.

All commands run from MAIN repo root: `/Users/ashton/code/rsi-econ`

## Phase 1: Read-Only Audit

Gather and present a summary. No mutations.

### Worktree inventory
```bash
cd /Users/ashton/code/rsi-econ
git fetch origin
git worktree list
```
For each worktree, classify:
- **PROTECTED** — on the allowlist (never touch): `sweet-chandrasekhar`
- **HAS WORK** — ahead>0 or dirty>0 (has stuff to potentially land)
- **IDLE** — ahead=0, dirty=0 (just sitting there, harmless)

Show a table like:
```
worktree             | branch                    | ahead | dirty | status
sweet-chandrasekhar  | claude/sweet-chan...       |     2 |     1 | PROTECTED
objective-faraday    | claude/objective-faraday   |     8 |     1 | HAS WORK
pedantic-colden      | claude/pedantic-colden     |     0 |     0 | IDLE
```

### Main branch state
- Is main clean? (`git status`)
- Is main pushed? (`git log origin/main..main --oneline`)
- Is origin ahead? (`git log main..origin/main --oneline`)

### Commit hygiene check on main (informational)
```bash
# Any Co-Authored-By mentioning Claude/AI on unpushed commits only
git log origin/main..main --format="%H %s%n%(trailers:key=Co-Authored-By)" | grep -i -B1 "claude\|anthropic"

# Any non-ashtonleee author on unpushed commits
git log origin/main..main --format="%h %an <%ae> %s" | grep -v ashtonleee
```

**Present the summary. Ask: "What do you want to land on main?"**

## Phase 2: Land Work on Main

For each worktree the user wants to land:

### 2a. Review the work
Show the commit log and a diffstat:
```bash
git -C .claude/worktrees/<name> log main..HEAD --oneline
git -C .claude/worktrees/<name> diff main --stat
```

Ask: "Here's what's on `<branch>`. Land this on main?"

### 2b. Squash-merge with clean authorship
```bash
cd /Users/ashton/code/rsi-econ
git checkout main
git merge --squash <branch>
```

Then commit with clean metadata:
- Author: `ashtonleee <nothsa2013@gmail.com>`
- Message: ask the user what the commit message should be, or propose one based on the diff. Keep it terse, human-sounding, lowercase-ish. No "feat:", "fix:", etc. unless the user's existing style uses them.
- NO Co-Authored-By trailers
- NO mentions of Claude, AI, agents, worktrees

```bash
GIT_AUTHOR_NAME="ashtonleee" GIT_AUTHOR_EMAIL="nothsa2013@gmail.com" \
GIT_COMMITTER_NAME="ashtonleee" GIT_COMMITTER_EMAIL="nothsa2013@gmail.com" \
git commit -m "<message>"
```

If the merge has conflicts, STOP. Show the conflicts. Ask the user how to proceed. Do NOT auto-resolve.

### 2c. Handle the landed worktree/branch
After landing, ask: "Work is on main now. Want to (a) leave the worktree as-is, (b) remove it?"

- If remove: `git worktree remove .claude/worktrees/<name>` (no --force). Then `git branch -d claude/<name>`.
- If --force is needed, explain why and ask permission.
- If leave: do nothing. It's harmless.

## Phase 3: Push

```bash
git log origin/main..main --oneline  # show what's about to be pushed
```

Ask: "Push these commits to origin/main?"

- Fast-forward: `git push origin main`
- Diverged: STOP. Explain the situation. Suggest `git pull --rebase` or ask user.
- NEVER force-push without the user explicitly saying "force-push".

## Phase 4: Optional Tidying

Only if the user asks for it. Don't suggest aggressively.

### Stale worktree cleanup
For IDLE worktrees (not on the allowlist), list them:
> "These worktrees have no unique work: [list]. Remove any?"

If user says yes to specific ones, remove them. Leave the rest.

### Orphan branch cleanup
List `claude/*` branches with no associated worktree:
> "These branches have no worktree: [list]. Delete any?"

Use `git branch -d` (safe delete). If it refuses (unmerged), tell the user and let them decide.

## Allowlist (NEVER touch these worktrees)

- `sweet-chandrasekhar`

## Safety Rules

1. **Read before write.** Phase 1 is mandatory. Never skip the audit.
2. **Ask before every mutation.** Merges, deletes, pushes — all need a yes.
3. **No --force by default.** Try safe ops first. Escalate only with permission.
4. **No history rewriting.** Don't rebase, filter-branch, or amend pushed commits. Only clean up at squash-merge time.
5. **Allowlisted worktrees are untouchable.** Don't list them as cleanup candidates.
6. **Leave idle worktrees alone unless asked.** They cost nothing and might have a sleeping agent.
7. **If in doubt, leave it.** Min-regret = don't delete. The user can always clean up later.
8. **Conflicts = stop.** Never auto-resolve merge conflicts.
