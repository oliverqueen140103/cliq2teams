# Runbook — migrating your own Zoho Cliq history into Teams

Every person runs this for themselves. Zoho Cliq's API is strictly per-user: your
token can read **your** DMs and the channels **you have joined**, and nothing else.
There is no org-wide export endpoint (`/api/v2/users/{id}/chats` is a 404), so one
person cannot migrate the organisation on everyone's behalf.

Budget about an hour of mostly-waiting for a typical account.

---

## 1. Get a Zoho self-client token

1. Go to <https://api-console.zoho.in> (use the console for your data centre — see
   `zoho.dc` in `config.yaml`) and create a **Self Client**.
2. Generate a code with this scope list, all read-only:

   ```
   ZohoCliq.Channels.READ ZohoCliq.Chats.READ ZohoCliq.Messages.READ
   ZohoCliq.Attachments.READ ZohoCliq.Users.READ ZohoCliq.Buddies.READ
   ZohoCliq.Profile.READ
   ```

3. Exchange the code for a **refresh token** (`refreshtoken.py` in this repo does
   the exchange).
4. Copy `.env.example` to `.env` and fill in `ZOHO_CLIENT_ID`,
   `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`.

**Important:** channels you have never opened report `chat_id=null` and cannot be
read at all. If you care about such a channel, **join it in Cliq first**, then run
`extract-chats` again. `verify-extract` lists them as `UNREACHABLE`.

## 2. Entra ID app permissions (one-time, done by an admin)

The tenant app needs these **application** permissions, with admin consent:

| Permission | Needed for |
|---|---|
| `Teamwork.Migrate.All` | importing backdated messages; `startMigration` / `completeMigration` |
| `Chat.Create` | creating 1:1 and group chats |
| `Chat.Read.All` | reading a chat's `createdDateTime` — needed to tell a correctly backdated conversation from one Teams will not render in full |
| `ChatMessage.Read.All` | `verify-teams` / `verify-dms` on chats |
| `ChatMember.ReadWrite.All` | `share-history` — without this hidden history cannot be fixed |
| `ChannelMessage.Read.All` | `verify` |
| `Group.ReadWrite.All`, `Group.Read.All` | creating the archive team |
| `Files.ReadWrite.All`, `Sites.ReadWrite.All` | attachments |
| `User.Read.All` | mapping Cliq users to AAD users |
| `TeamMember.ReadWriteNonOwnerRole.All` | adding members to the archive team (optional: without it `complete` still finishes, it just logs a `403` per member and skips them) |

Import itself only needs the first two plus the file and user permissions. Everything
else exists so you can **check** the result — and an unverified migration is how you
end up believing 18 months imported when your client shows three.

To see what the app is actually granted, decode the `roles` claim of a token rather
than trusting the portal list; a 403 from Graph also names the roles on the request.

Put `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` in `.env`.
`MS_CLIENT_SECRET` is the secret **Value**, not the secret ID. Secrets expire —
`AADSTS7000215` at startup means it needs regenerating in the Azure portal.

## 3. Configure

Copy `config.example.yaml` to `config.yaml` and set:

- `graph.default_owner_upn`, `mapping.orphan_author_upn`, `dms.owner_upn` → **your**
  UPN in the tenant.
- `teams.single_team_name` → a name unique to you, e.g. `"Cliq Archive — Priya"`.
  Two people sharing a team name will collide.
- `mapping.domain_rewrite` → your Cliq domain to your tenant domain.

`state.db` is yours alone. Never copy someone else's, and never delete it midway:
it is the only record of what has already been imported, and re-importing
duplicates everything.

## 4. Extract

```bash
python -m c2t probe            # sanity-check the token and endpoints
python -m c2t extract-chats
python -m c2t extract-messages # walks every chat back to its FIRST message
python -m c2t extract-users
python -m c2t extract-files    # downloads attachments; the slow one
python -m c2t verify-extract   # <- the gate: must say "0 incomplete"
```

`verify-extract` re-walks every chat against the live API and compares counts and
oldest timestamps with what is stored. Do not proceed until it prints
*"every reachable chat is stored in full, first message to last."*

If it reports `DB SHORT`, run `python -m c2t extract-messages --rescan` and check
again. `--rescan` is idempotent and also picks up new activity, so it is the right
command for any later top-up run too.

Zoho's own `total_message_count` reads a few higher than what the API returns on
some channels — it counts deleted and system entries that are never served. That
difference is expected and is not a shortfall.

### Attachments: every file, any size

Keep `dms.max_attachment_mb: 0`. Zero means no limit, and it is the default.
Graph switches to a resumable upload session above 4 MB, so large files are fine —
this archive includes a 362 MB video and 14 GB in total. Any non-zero value
silently drops files from their messages; `verify-teams` prints an
`attachments: N/M in Teams` line so a shortfall cannot pass unnoticed.

If a blob is missing from disk at load time the file is put back into the queue
rather than dropped — re-run `extract-files`, then the load command again.

## 5. Load

```bash
python -m c2t map-users        # resolve Cliq users to AAD; fix any unmapped
python -m c2t plan             # review before anything is written
python -m c2t load-teams
python -m c2t load-messages
python -m c2t plan-dms         # review the 1:1 / group chat plan
python -m c2t load-dms
```

Pilot first: `load-dms --only "SOME NAME"` imports a single chat so you can eyeball
it in Teams before committing to the rest.

`map-users` cannot invent identities: anyone whose Cliq email or name has no AAD
match is left `unmapped` and their DMs are skipped. When you know who they are, put
`zoho_email,aad_upn,notes` rows in `mapping/users.csv` and set
`mapping.strategy: csv`. The CSV is consulted **before** the automatic email /
domain-rewrite / display-name heuristics, so it fixes and disambiguates; re-run
`map-users` and watch the `unmapped` count drop. Anyone still unresolved afterwards
is genuinely not in the tenant and can only be kept via `import-as-channel`.

## 6. Complete, then make the history visible

```bash
python -m c2t complete         # IRREVERSIBLE — channels/team leave migration mode
python -m c2t complete-dms
python -m c2t share-history    # <- do not skip this
```

**`share-history` is the step that makes a group chat readable.** A chat member
carries a `visibleHistoryStartDateTime` — the earliest message they are allowed to
see. It cannot be set when the chat is created and cannot be patched afterwards, so
backdated imported messages sit *behind* that cutoff and stay invisible even though
they imported successfully. The only documented remedy is to remove each member and
add them back with the value backdated, which is what this command does.

Symptom if you skip it: "I imported 18 months but Teams only shows the last few."

The command only touches members whose cutoff is actually set and too late. It
leaves 1:1 chats alone unless they genuinely report one, because Teams treats
`oneOnOne` membership as fixed and will refuse the removal. Run `verify-teams`
first to see which destinations are really affected.

### Re-opening a chat that is already `completed`

A chat imported before a later top-up (e.g. a message sent after the first pass)
can be re-opened — Teams allows `startMigration` again after `completeMigration`.
Do **not** reuse the original backdated date: `startMigration` requires
`conversationCreationDateTime` to be *strictly older* than the chat's current
`createdDateTime`, and equality is rejected with
`'ConversationCreationDateTime' must be older than the existing 'CreatedDateTime'`.

```bash
python -m c2t reopen-chats --dry-run              # see what would change
python -m c2t reopen-chats --only "THE NAME"      # pilot one
python -m c2t reopen-chats                        # all completed chats
```

`reopen-chats` calls `startMigration` again with a far-older floor (default
`2024-01-01T00:00:00Z`, override with `--floor`) and then re-completes. It refuses
a floor that is not older than the oldest message in the archive. Messages are
already imported, so nothing is re-posted and nothing is duplicated — only the
conversation's start marker moves.

To import *new* messages into a re-opened chat, run `load-dms --only "THE NAME"`
before `complete-dms`.

Messages that failed earlier are still queued in `state.db` as `pending`. The
loader marks a failing message `pending` with the error in the `error` column and
**continues** — a run never aborts on a single bad message, so check `status-dms`
afterwards rather than assuming silence means everything went through.

## 7. Verify

```bash
python -m c2t verify           # channels: source vs Teams counts
python -m c2t verify-teams     # counts + real date ranges + visibility cutoffs
```

`verify-teams` is the one that proves the outcome. Every row should show an
`imported range` starting at your oldest Cliq message and a `history visible from`
that is earlier still. Any row marked `HIDES HISTORY` needs `share-history`.

Then check by hand, because no API call can confirm what a human sees: open an old
1:1 chat in the Teams client and scroll to the top. The oldest message should be
your first-ever Cliq message with that person.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Teams shows *"Migration for this conversation is in progress"* | `completeMigration` has not succeeded for that chat. Run `complete-dms --force`. Until it does, Teams hides or reorders the backdated history — this is the most common cause of "only recent months are visible" |
| Only recent months visible in the Teams **client** | First `complete-dms --force`, then `verify-teams`. If that reports `0 with hidden or missing history` and the banner is gone, the messages are in Teams with the right dates — see "Old messages are in Teams but I cannot see them" below |
| "Find in chat" locates an old message but scrolling never reaches it | Stale client cache — the conversation's local start watermark predates the import. Full resync required: private window, or clear the client cache. Not a data problem; nothing to re-import |
| `verify-teams` reports `HIDES HISTORY` | `share-history` not run, or `ChatMember.ReadWrite.All` not granted |
| `AADSTS7000215` | Graph client secret expired or is the secret ID rather than its Value |
| `403` on chat rows in `verify-teams` | missing `ChatMessage.Read.All`; import still works, only the read-back is blocked |
| `UNREACHABLE (chat_id=null)` | join that channel in Cliq, then re-run `extract-chats` |
| `dm skipped: 1 participants resolved` | the other person is not in the tenant; use `import-as-channel` to keep the history |
| `startMigration ... must be older than the existing 'CreatedDateTime'` | the chat was already migrated; re-open per "Re-opening a chat" above — equality is rejected, pick a strictly older date |
| `403 Forbidden` on `GET /chats/{id}` | app has no `Chat.Read*` scope, so the chat cannot be read back; import and `verify-*` still work |
| `RosterAddMemberBlocked-Roster is blocked from remove member` | expected on a **1:1** chat: Teams makes its roster immutable, so a member's `visibleHistoryStartDateTime` can never be changed after the chat exists — not even with `ChatMember.ReadWrite.All`. It is fixed by whatever `startMigration` set. `share-history` therefore only works on group chats; for a 1:1 conversation use `reopen-chats` (moves the conversation start marker) or `import-as-channel` (channels have no per-member cutoff) |
| `403 Forbidden` on `POST /teams/{id}/members` | missing `TeamMember.ReadWriteNonOwnerRole.All`; `complete` still finishes, members are just skipped |
| a message stays `pending` in `status-dms` | read its `error` column; the loader continues past individual failures, fix the cause and re-run |
| A chat's counter is higher than the API total | deleted/system entries Zoho counts but never serves; not a shortfall |

## Old messages are in Teams but I cannot see them

**Check for the migration banner first.** If the chat shows *"Migration for this
conversation is in progress. Messages may be out of order during this time"*, that
is the whole problem: `completeMigration` has not succeeded for that chat, and
Teams will not render the full backdated history until it does. Run:

```bash
python -m c2t complete-dms --force
```

`--force` re-issues `completeMigration` even for chats `state.db` already calls
completed, because the database can be wrong — a 204 response means the chat was
in fact still open. On this migration 16 of 19 chats were stuck this way while the
database reported all 19 complete. Do not trust `chat_migration='completed'`;
trust the banner and the 204.

Only once the banner is gone, separate the two remaining possibilities:

```bash
python -m c2t verify-teams
```

If that prints `0 with hidden or missing history`, the import is **correct**: the
messages exist in Teams, carry their original Cliq `createdDateTime`, and no
member's visibility cutoff is blocking them. Anything you are not seeing at that
point is the Teams client, not the archive. In that order:

1. **Try "Find in chat" on an old message.** This is the single most useful test,
   because search and scroll-back read different things: search queries the
   service, scroll-back reads the client's locally replicated copy of the
   conversation.

   **If search finds the message but scrolling never reaches it, the migration is
   correct and complete — the client's cached copy is stale.** Teams records a
   "beginning of conversation" watermark when it first syncs a chat, and messages
   backdated *below* that watermark do not retroactively extend it. This is
   guaranteed to happen to anyone whose client had the chat open while it was
   still in migration mode. An incremental sync will never fix it; only a full
   resync will:

   - browser: open [teams.microsoft.com](https://teams.microsoft.com) in a
     **private window**, or Clear site data for `teams.microsoft.com`
   - desktop: sign out and back in, or quit and delete the client cache
     (`%APPDATA%\Microsoft\Teams`, or
     `%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache` for new Teams)

   Tell people to do this *after* `complete-dms`, not before, or they will just
   re-cache the in-migration state.
2. **Check Teams on the web** at <https://teams.microsoft.com>, ideally in a
   private window. This is the decisive test because it has no local cache. If the
   full history appears on the web but not in the desktop app, the desktop cache is
   stale.
3. **Force the desktop client to re-sync.** Backfilled history is written straight
   into the service and an already-cached conversation does not always pick it up.
   Sign out and back in, or quit Teams entirely and clear its cache
   (`%APPDATA%\Microsoft\Teams` on Windows,
   `~/Library/Application Support/Microsoft/Teams` on macOS), then reopen.
4. **Confirm you are in the right conversation.** A 1:1 import lands in your
   *existing* chat with that person, so old imported messages and recent live ones
   share one thread — `verify-teams` showing a Teams count higher than `src` is
   that, and is normal.
5. **Re-open the conversation with an older start marker.** If the client still
   stops part-way back, the conversation's own `createdDateTime` is the limit, not
   the messages. `python -m c2t reopen-chats` re-runs `startMigration` with a
   far-older floor and re-completes. This was observed to be the difference
   between chats that scrolled back to the first Cliq message and chats that
   stopped at a few months, even though both reported full history via the API.

To settle it for one specific chat, read the months straight out of Graph:
`GET /chats/{id}/messages` and group by `createdDateTime` — if the old months are
in that response, the data is there and the client is the only thing left.

## What cannot be migrated

Reactions, message edit history, read receipts, presence and pins have no import
path in Graph. Bots, Cliq commands, widgets and forms are flattened to text.
