import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
import yaml

load_dotenv("/home/varagunarajan/Documents/migration/cliq2teams/.env")

ROOT = Path("/home/varagunarajan/Documents/migration/cliq2teams")
sys.path.insert(0, str(ROOT))

from c2t import chats, graph, transform
from c2t.state import State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

TEAM_KEY = "Zoho Cliq DMs"
MISSING = ["NISO WATCH ESP32", "Meeting", "NANDIGAMA VARSHA", "SHYLJIN SHAMILA"]

cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
st = State(cfg["paths"]["state_db"])
gc = graph.GraphClient(cfg)
headroom = cfg["teams"]["backdate_headroom_hours"]

team = st.one("SELECT * FROM teams_ WHERE team_key=?", (TEAM_KEY,))
if team and team["teams_team_id"]:
    print(f"team {TEAM_KEY} already exists: {team['teams_team_id']}")
else:
    mn = min(
        st.one("SELECT first_msg_ts FROM chats WHERE title=?", (t,))["first_msg_ts"]
        for t in MISSING
    )
    created = transform.backdate(mn, headroom * 2)
    tid = gc.create_team_migration(
        TEAM_KEY, created, "DM and group chats imported from Zoho Cliq")
    st.db.execute(
        """INSERT INTO teams_(team_key, display_name, created_dt, teams_team_id,
                              migration_done, status)
           VALUES (?,?,?,?,0,'pending')
           ON CONFLICT(team_key) DO UPDATE SET
             display_name=excluded.display_name, created_dt=excluded.created_dt,
             teams_team_id=excluded.teams_team_id, migration_done=0""",
        (TEAM_KEY, TEAM_KEY, created, tid))
    print(f"team {TEAM_KEY} created: {tid} (backdated to {created})")

for title in MISSING:
    c = st.one("SELECT * FROM chats WHERE title=?", (title,))
    if not c:
        print(f"!! {title}: not found in state")
        continue
    print(f"\n=== {title} ===")
    n = chats.import_chat_as_channel(gc, st, cfg, c["zoho_chat_id"],
                                     channel_name=title, team_key=TEAM_KEY)
    print(f"{title}: {n} messages imported")

st.close()
print("\ndone. Run:  python3 -m c2t.cli complete   (finalizes team + adds members)")
