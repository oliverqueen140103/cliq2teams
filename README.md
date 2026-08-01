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

| Thing | Status |
|---|---|
| Channel messages with original timestamps | Supported (import mode) |
| Threaded replies | Supported (`/messages/{id}/replies`) |
| File attachments | Supported (upload to SharePoint, attach by reference) |
| **1:1 chats / group chats** | **Supported** via chat migration mode — see §1.1 |
| Reactions / emoji reactions | Not importable |
| Message edit history | Not importable |
| Read receipts, presence, pins | Not importable |
| Bots, Cliq commands, widgets, forms | No equivalent — flatten to text |
| Cliq "threads" (separate thread objects) | Map to Teams replies or to their own channel |

### 1.1 DMs: chat migration mode

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

1. Zoho Cliq **super admin** (needed for org-wide chat/channel visibility).
2. Self-client OAuth app at <https://api-console.zoho.com> → *Self Client*.
   - Generate a grant code, exchange once for a **refresh token** (never expires
     unless revoked). Store it in `.env`.
3. Scopes:
   ```
   ZohoCliq.Channels.READ
   ZohoCliq.Chats.READ
   ZohoCliq.Messages.READ
   ZohoCliq.Attachments.READ
   ZohoCliq.Users.READ
   ZohoCliq.Organisation.READ
   ```
4. Confirm your **data centre** — the API host differs:
   `zoho.com` (US) · `zoho.eu` · `zoho.in` · `zoho.com.au` · `zoho.jp` ·
   `zohocloud.ca`. Set `zoho.dc` in config.

> Verify every Cliq endpoint against your DC's live API docs before a real run.
> Zoho versions and renames these; the paths in `c2t/zoho.py` are centralised in
> one constants block precisely so you can correct them in one place.

### Microsoft side

1. Entra ID app registration (single tenant).
2. **Application** permissions (not delegated), all admin-consented:
   ```
   Teamwork.Migrate.All      <- the critical one; enables backdating
   Chat.Create               <- required for DM import only
   Group.ReadWrite.All
   ChannelMessage.Read.All
   Files.ReadWrite.All
   User.Read.All
   Sites.ReadWrite.All
   ChatMessage.Read.All
   ```
3. Client secret or certificate.
4. Teams licences assigned to every target user *before* load — messages
   authored by an unlicensed/nonexistent AAD user will fail import.

### Local

- Python 3.11+
- ~1.5× the total attachment volume in free disk (staged blobs)
- Stable egress; a large tenant is a multi-day run

---

## 3. Order of operations

```bash
cp config.example.yaml config.yaml   # edit
cp .env.example .env                 # secrets

python -m c2t.cli init                    # create state.db
python -m c2t.cli extract-users
python -m c2t.cli map-users               # writes users_unmapped.csv for manual fixes
python -m c2t.cli extract-chats
python -m c2t.cli extract-messages        # long; resumable
python -m c2t.cli extract-files           # long; resumable

python -m c2t.cli plan                    # dry-run report: what will be created
python -m c2t.cli load-teams              # migration-mode teams + channels
python -m c2t.cli load-messages           # the slow part
python -m c2t.cli complete                # completeMigration + add members
python -m c2t.cli verify                  # count reconciliation
```

DMs are a separate, independent track — nothing below touches the channel team:

```bash
python -m c2t.cli plan-dms                        # dry run: who resolves, what gets created
python -m c2t.cli load-dms --only "PAUL DANIEL A" # pilot on the smallest chat first
python -m c2t.cli load-dms                        # the rest; resumable
python -m c2t.cli verify-dms                      # source vs Teams counts
python -m c2t.cli complete-dms                    # leave migration mode
```

`plan-dms` is not optional: it is the only place that tells you which DMs cannot
be represented in Teams before anything irreversible happens.

`plan` and `verify` are not optional. `plan` catches unmapped users before you
create anything; `verify` is what you hand to whoever signed off on the project.

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

`verify` reconciles per channel:
- source message count vs `GET /teams/{id}/channels/{id}/messages` count
- attachment count and total bytes
- a sampled content hash comparison

Anything non-zero in the delta column is a real failure, not rounding.
# cliq2teams
