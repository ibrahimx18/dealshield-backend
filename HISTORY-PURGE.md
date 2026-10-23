# Git history purge + credential rotation (N14) - DECISION FOR IBRAHIM

Nothing in this file has been run. Rewriting history needs a coordinated force-push
and every clone to be re-cloned, so it is Ibrahim's call.

## What is in the history

| Item | Commits | Notes |
|---|---|---|
| `data/test_auth.db` (SQLite test database) | added `b210bb5`, removed from tree `e76c11d` | still reachable from `main`, `origin/main` and every branch |
| `test_escrow_e2e.py` DB connection | introduced `460c345` | it never had literal credentials: it parsed `DATABASE_URL` out of `.env` at run time. Fixed on this branch (N14 commit): it now reads `E2E_DATABASE_URL`/`DATABASE_URL` from the environment and skips when unset. Purging is only needed if an older `.env` was ever committed - check below. |

Check before purging (read-only):

```bash
git log --all --format='%h %s' -- data/test_auth.db .env '.env.*' '*.db' '*.sqlite'
git log --all -p -S 'postgresql://' -- . ':(exclude)migrations' | grep -n 'postgresql://' | sed -E 's#(://[^:]+:)[^@]+@#\1<redacted>@#'
```

## Purge (on a fresh mirror, after every open branch is pushed)

```bash
pip install git-filter-repo
git clone --mirror git@github.com:<owner>/<backend-repo>.git dealshield-purge.git
cd dealshield-purge.git
# 1. remove the files from every commit
git filter-repo --invert-paths --path data/test_auth.db --path-glob '*.db' --path .env
# 2. optional: scrub any credential string found by the check above
#    (put one literal per line in ../replacements.txt as  OLDVALUE==>REDACTED )
# git filter-repo --replace-text ../replacements.txt
# 3. verify
git log --all --format=%h -- data/test_auth.db      # must print nothing
git rev-list --all | xargs -I{} git ls-tree -r --name-only {} | grep -c 'test_auth.db'   # must print 0
# 4. publish (this is the force-push - coordinate first)
git push --force --mirror
```

Afterwards: every collaborator deletes their clone and re-clones; GitHub caches
old objects in PR refs - ask GitHub Support to run a garbage collection if the
repo was ever public or forked; re-open any PRs from the new history.

## Rotation checklist (do this whether or not you purge)

- [ ] Database password for every PostgreSQL role that ever appeared in a `.env` used by this repo (live, staging, local defaults reused anywhere). Update `DATABASE_URL` in the server `.env`, restart the backend.
- [ ] `SAFEPAY_SECRET_KEY` / `SECRET_KEY` (JWT signing) - rotating logs everyone out (expected).
- [ ] `SAFEPAY_WEBHOOK_SECRET` (integration secret used by n8n) - update n8n credentials at the same time.
- [ ] `PAYSTACK_SECRET_KEY` and `FLUTTERWAVE_SECRET_KEY` (roll in the provider dashboards; update webhook signing on the provider side).
- [ ] `DEALSHIELD_KYC_KEY` - do NOT simply replace it: encrypted NIN/BVN rows need a re-encryption script first.
- [ ] SMTP / Termii / Twilio credentials if they were ever in a committed file.
- [ ] Inspect `data/test_auth.db` from history (`git show b210bb5:data/test_auth.db > /tmp/t.db; sqlite3 /tmp/t.db '.tables'`): if it holds any real user emails/phones/hashes, treat it as a data-protection incident per DATA_PROTECTION_POLICY.md.
