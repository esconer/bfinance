# Committing to GitHub with Privacy — what we did, why, and how to keep it

Status: living doc · Applies to: this repository (`bfinance`)

---

## TL;DR

Every git commit permanently embeds an **author name + email** inside itself. On GitHub that email
becomes public on every commit page of a public repo, and scrapers harvest them for spam and
doxxing. We therefore commit with GitHub's **private noreply address** instead of a real one:

```bash
git config user.name  "esconer"
git config user.email "83386859+esconer@users.noreply.github.com"
```

This is **repo-local** (`--local`, the default when run inside the repo), so other projects on this
machine are unaffected.

## Why this exact format

- `83386859` is the immutable numeric ID of the GitHub account `esconer`
  (visible via `https://api.github.com/users/esconer`).
- `{id}+{username}@users.noreply.github.com` is the address GitHub itself assigns when
  **Settings → Emails → "Keep my email addresses private"** is enabled.
- Commits authored with it: attribute to your profile ✅, count toward the contribution graph ✅,
  expose zero real-world identity ✅.
- A plain `esconer@users.noreply.github.com` (no ID) only works for accounts created before
  mid-2017; the ID-prefixed form is the current standard.

## One-time GitHub web settings (do these once)

1. **Settings → Emails** (https://github.com/settings/emails)
   - Enable **"Keep my email addresses private"**.
   - Enable **"Block command line pushes that expose my email"** — GitHub will *reject* any push
     containing a commit authored with your real address, turning a silent leak into a loud error.
2. Optional: **Settings → Security → enable 2FA** (push access is a credential too).

## Verify at any time

```bash
# what identity will NEW commits use?
git config user.name && git config user.email

# what identities did EXISTING commits use?  (%ae = author email)
git log --format="%h %an <%ae>" | sort -u

# after pushing: open any commit on github.com — author should show as
# "esconer" linked to the profile, with NO raw email rendered.
```

## If a real email ever does get committed

1. **Stop the bleeding**: fix `git config user.email` (as above) before the next commit.
2. If not yet pushed: rewrite locally, e.g.
   `git rebase -i <bad-commit>^` with `--reset-author`, or
   `git filter-repo --email-callback` (preferred tool: `pip install git-filter-repo`).
3. If already pushed: history rewrite + force-push removes it going forward, **but** GitHub caches
   orphaned commits by SHA — also contact GitHub Support to purge dangling objects, and treat the
   exposed address as burned (spam-filter it; rotate if it's tied to sensitive accounts).
