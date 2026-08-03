"""
Script to make the archived Teams 100% private to the owner account
by removing all other organization members.
"""
import logging
from c2t import graph, cli

logging.basicConfig(level=logging.INFO)

cfg, st = cli.setup('config.yaml')
gc = graph.GraphClient(cfg)

# Owner account UPN configured in config.yaml
owner_upn = cfg.get("dms", {}).get("owner_upn") or cfg.get("graph", {}).get("default_owner_upn")

# Find created Teams
teams = st.rows("SELECT * FROM teams_ WHERE teams_team_id IS NOT NULL")
if not teams:
    print("No teams found in state.db.")
    exit(0)

for team in teams:
    team_id = team["teams_team_id"]
    team_name = team.get("team_key") or "Archive Team"
    print(f"\n--- Checking Team: {team_name} ({team_id}) ---")
    
    members = gc.http.get(f'/groups/{team_id}/members').get('value', [])
    print(f"Total team members found: {len(members)}")
    
    removed_count = 0
    for m in members:
        upn = m.get('userPrincipalName', '')
        mid = m.get('id')
        name = m.get('displayName')
        if upn and owner_upn and upn.lower() != owner_upn.lower():
            try:
                gc.http.request('DELETE', f'/groups/{team_id}/members/{mid}/$ref')
                print(f"Removed: {name} ({upn})")
                removed_count += 1
            except Exception as e:
                print(f"Error removing {name}: {e}")

    print(f"Successfully removed {removed_count} members from {team_name}.")
