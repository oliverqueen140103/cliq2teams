# cliq2teams

Local, resumable migration pipeline: **Zoho Cliq → Microsoft Teams**.

Three phases, each independently runnable and idempotent:

```
EXTRACT            TRANSFORM             LOAD
Zoho Cliq API  ->  normalized JSONL  ->  Graph import mode
+ file blobs       + identity map        + SharePoint upload
      |                   |                     |
      +-------------- state.db (SQLite) --------+
```

State lives in `state.db`. Every unit of work (user, chat, message, file) has a
row with a status. Re-running a phase skips anything already `done`. Kill it
mid-run and restart; nothing is duplicated.

---

## 1. Hard limitations — read before building

These are platform constraints, not gaps in this tool.

### 1.0 Scope: this migrates ONE person's account

Cliq's REST API is strictly per-user. `/api/v2/chats` returns only the token
owner's DMs and group chats, `/api/v2/channels` only the channels that account has
joined, and there is no admin override — `/api/v2/users/{id}/chats` is a 404 and no
`/organization/*` endpoint exists. Being a Cliq super admin does not change this.
`/api/v2/users` is the one org-wide read, and it returns the directory only.

So a full organisational archive means **every person runs this tool for
themselves**, each with their own self-client token, their own `state.db` and their
own team name. See [RUNBOOK.md](RUNBOOK.md).

A channel the account has never opened reports `chat_id=null` and is unreachable by
any endpoint — there is no `/channels/{unique_name}/messages` to fall back to
(it 404s). Join the channel in Cliq, then re-run `extract-chats`.

### 1.1 Feature support

| Thing | Status |
|---|---|
| Channel messages with original timestamps | Supported (import mode) |
| Threaded replies | Supported (`/messages/{id}/replies`) |
| File attachments | Supported (upload to SharePoint, attach by reference) |
| **1:1 chats / group chats** | **Supported** via chat migration mode — see §1.3 |
| Reactions / emoji reactions | Not importable |
| Message edit history | Not importable |
| Read receipts, presence, pins | Not importable |
| Bots, Cliq commands, widgets, forms | No equivalent — flatten to text |
| Cliq "threads" (separate thread objects) | Map to Teams replies or to their own channel |

### 1.2 Imported history is hidden until you share it

Every Teams chat member carries a `visibleHistoryStartDateTime`: the earliest
message they may see. It cannot be set when the chat is created and cannot be
patched afterwards, so backdated imported messages fall *behind* that cutoff and
stay invisible even though every POST succeeded. The only documented remedy is to
remove the member and add them back with the value backdated — `share-history`.

**Group chats** can be fixed this way. **1:1 chats cannot.** Verified against Graph
on 2026-08-01: removing a member from a `oneOnOne` chat returns `403
InsufficientPrivileges` / `RosterAddMemberBlocked-Roster is blocked from remove
member`, even with `ChatMember.ReadWrite.All`. Its roster is immutable, so a 1:1
member's cutoff is permanently whatever `startMigration` set when the conversation
was backdated. `share-history` acts only on members whose cutoff is present and too
late, and skips the rest rather than attempting a removal Graph will refuse.

For a 1:1 conversation the two levers that do work are `reopen-chats`, which
re-runs `startMigration` with a far-older `conversationCreationDateTime` and moves
the conversation's start marker, and `import-as-channel`, which re-homes the
history into a channel where no per-member cutoff exists.

Confirm which case you are in with `verify-teams`, which prints the real cutoff per
destination and flags offenders as `HIDES HISTORY`. It needs `ChatMessage.Read.All`
and `ChatMember.ReadWrite.All`; without them chat rows report `cannot read` and are
counted as unknown, not as good. If a 1:1 chat really is hiding history and cannot
be fixed, `import-as-channel` re-homes that conversation into a channel, where
history is unrestricted.

### 1.3 DMs: chat migration mode

Graph now backdates 1:1 and group chats. Unlike teams and channels, a chat
cannot be *created* in migration mode — the sequence is create, then start:

```
POST /chats                                       Chat.Create
POST /chats/{id}/startMigration                   Teamwork.Migrate.All
     { "conversationCreationDateTime": "..." }    (older than the chat's own createdDateTime)
POST /chats/{id}/messages                         Teamwork.Migrate.All
     { createdDateTime, from.user, body }         (later than the chat's, unique to the ms)
POST /chats/{id}/completeMigration
```

Three consequences that shape `c2t/chats.py`:

- **No channel files folder exists for a chat.** Attachments must already live in
  SharePoint or OneDrive and be attached by reference, so blobs are uploaded to
  the owner's OneDrive under `dms.onedrive_folder` with an organization-scoped
  sharing link. Inline images are the only media the schema hosts directly;
  everything else is a link.
- **`createdDateTime` must be unique to the millisecond** within a chat, or the
  POST returns `409`. Retried with the timestamp nudged forward 1 ms.
- **A `oneOnOne` POST returns the pair's existing chat** if they already have
  one, which is what you want — history lands in their live conversation. It also
  means the import is effectively irreversible: a 1:1 chat can't be deleted and
  `createdDateTime` only ever moves backwards. Pilot with `--only`.

Participation is reconstructed from who actually spoke plus a display-name match
on the chat title, because Cliq's chat list carries no participant roster. A DM
therefore can't be migrated when the other party is a bot or has left the tenant
— those keep the HTML export as their record.

`dms.strategy` selects the behaviour: `teams_chats`, `html_export`, or `none`.
Running both is reasonable: the HTML transcript stays the fallback record.

---

## 2. What you need to provision

### Zoho side

1. A normal Cliq account — **no admin role is required or helps**. The token sees
   that account's own chats and joined channels and nothing else (§1.0), so each
   person provisions their own.
2. Self-client OAuth app at your DC's console, e.g.
   <https://api-console.zoho.in> → *Self Client*.
   - Generate a grant code, exchange once for a **refresh token** (never expires
     unless revoked). `refreshtoken.py` does the exchange. Store it in `.env`.
3. Scopes — all read-only, and these are exactly what the tool uses:
   ```
   ZohoCliq.Channels.READ
   ZohoCliq.Chats.READ
   ZohoCliq.Messages.READ
   ZohoCliq.Attachments.READ
   ZohoCliq.Users.READ
   ZohoCliq.Buddies.READ
   ZohoCliq.Profile.READ
   ```
   There is no organisation-wide Cliq scope that widens this; `/api/v2/teams`
   exists but is unrelated to message access.
4. Confirm your **data centre** — the API host differs:
   `zoho.com` (US) · `zoho.eu` · `zoho.in` · `zoho.com.au` · `zoho.jp` ·
   `zohocloud.ca`. Set `zoho.dc` in config.

> Verify every Cliq endpoint against your DC's live API docs before a real run.
> Zoho versions and renames these; the paths in `c2t/zoho.py` are centralised in
> one constants block precisely so you can correct them in one place.

### Microsoft side

1. Entra ID app registration (single tenant).
2. **Application** permissions, all admin-consented. The tool uses client
   credentials — there is no signed-in user in it — so Delegated permissions of
   the same name have no effect and only make the grant list harder to audit:
   ```
   Teamwork.Migrate.All                  backdated import; start/completeMigration
   Chat.Create                           creating 1:1 and group chats
   Group.ReadWrite.All                   creating the archive team
   Group.Read.All
   Files.ReadWrite.All                   attachments
   Sites.ReadWrite.All                   attachments
   User.Read.All                         mapping Cliq users to AAD users
   TeamMember.ReadWriteNonOwnerRole.All  adding members during `complete`
   ChannelMessage.Read.All               verify
   ChatMessage.Read.All                  verify-teams / verify-dms on chats
   Chat.Read.All                         reading a chat's createdDateTime
   ChatMember.ReadWrite.All              share-history
   Chat.ManageDeletion.All
   
   ``
   The last four are easy to miss: import succeeds without them, but you cannot
   read a chat back to check it, cannot see whether the conversation was actually
   backdated, and cannot fix hidden history (§1.2). An unverified migration is how
   you end up believing 18 months imported when the client shows three.
   `Chat.ManageDeletion.All` is deliberately **not** on this list. It appears to
   offer a way to clean up a duplicated import, but message deletion is
   delegated-only: an app-only token gets `405` from
   `POST /chats/{id}/messages/{id}/softDelete` and `412 "not supported in
   application-only context"` from the `/users/{id}/chats/...` form, and
   chat-level `softDelete` does not exist. A duplicate posted into a 1:1 chat is
   permanent, so the loader reconciles against the destination before posting.
3. Client secret or certificate. A secret is the **Value**, not the secret ID,
   and it expires — `AADSTS7000215` at startup means regenerate it.
4. Teams licences assigned to every target user *before* load — messages
   authored by an unlicensed/nonexistent AAD user will fail import.

### Local

- Python 3.11+
- ~1.5× the total attachment volume in free disk (staged blobs)
- Stable egress; a large tenant is a multi-day run

---

## 3. Order of operations

`python -m c2t <cmd>` and `python -m c2t.cli <cmd>` are equivalent.

```bash
cp config.example.yaml config.yaml   # edit
cp .env.example .env                 # secrets

python -m c2t init                    # create state.db
python -m c2t extract-users
python -m c2t map-users               # writes users_unmapped.csv for manual fixes
python -m c2t extract-chats
python -m c2t extract-messages        # long; resumable; walks back to message #1
python -m c2t extract-files           # long; resumable
python -m c2t verify-extract          # GATE: must print "0 incomplete"

python -m c2t plan                    # dry-run report: what will be created
python -m c2t load-teams              # migration-mode teams + channels
python -m c2t load-messages           # the slow part
python -m c2t complete                # completeMigration + add members
python -m c2t verify                  # count reconciliation
```

DMs are a separate, independent track — nothing below touches the channel team:

```bash
python -m c2t plan-dms                        # dry run: who resolves, what gets created
python -m c2t load-dms --only "PAUL DANIEL A" # pilot on the smallest chat first
python -m c2t load-dms                        # the rest; resumable
python -m c2t verify-dms                      # source vs Teams counts
python -m c2t complete-dms                    # leave migration mode
python -m c2t share-history                   # unhide imported history (§1.2)
python -m c2t verify-teams                    # counts + date ranges + visibility
```

Three of these are not optional:

- **`verify-extract`** re-walks every chat against the live API and refuses to
  pass unless the database holds every message the API still returns, back to the
  first one. If it reports `DB SHORT`, run `extract-messages --rescan` — which is
  idempotent and also the right command for any later top-up run.
- **`plan` / `plan-dms`** catch unmapped users and un-representable DMs before
  anything irreversible happens.
- **`verify-teams`** is what proves the outcome and what you hand to whoever
  signed off: real Teams counts, real `createdDateTime` ranges, per-member
  visibility cutoffs, and an attachment reconciliation.

`run --phase extract` chains the extract half unattended and gates on
`verify-extract` before anything is loaded.

---

## 4. Timestamp ordering rule

Import mode enforces a strict chain:

```
team.createdDateTime <= channel.createdDateTime <= every message.createdDateTime
```

Violations return 400 with an unhelpful message. `c2t/transform.py` computes the
minimum timestamp per channel and per team and backdates the containers by 24h
to give headroom.

## 5. Throttling

Graph throttles import aggressively and the limits are not publicly fixed.
`c2t/http.py` uses a token bucket (default 4 req/s, tunable) plus exponential
backoff that honours `Retry-After`. Do not parallelise beyond a few workers per
channel — messages within a channel must land in order for replies to resolve
their parent.

## 6. Verification

Three separate checks, because "it ran without errors" proves nothing:

| Command | Side | What it proves |
|---|---|---|
| `verify-extract` | Zoho | every chat is stored back to its first message; re-walks the live API and compares counts and oldest timestamps |
| `verify` | Teams | per-channel source count vs `GET /teams/{id}/channels/{id}/messages` |
| `verify-teams` | Teams | per-destination count, real `createdDateTime` range, worst member visibility cutoff, and attachments uploaded vs total |

A negative delta is a real failure. A *positive* delta on a 1:1 chat is normal —
the pair may have had a live Teams conversation before the import.

None of these can confirm what a human actually sees. Finish by opening an old
chat in the Teams client and scrolling to the top.

## 7. Attachments

`dms.max_attachment_mb: 0` means no limit and is the default. Graph switches to a
resumable upload session above 4 MB, so size is not the constraint — this archive
includes a 362 MB file. Any non-zero value **drops** files from their messages;
`verify-teams` prints an `attachments: N/M in Teams` line so that cannot pass
unnoticed. A blob missing from disk at load time is requeued rather than skipped:
re-run `extract-files`, then the load command again.
