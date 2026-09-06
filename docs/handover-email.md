Subject: iRadar — handover setup (GitHub + Railway on the team account)

Hi [name],

Quick note on how iRadar is set up so the team owns it properly and nothing depends on me being around.

Everything now lives on the team's Google account: the GitHub repo and the Railway deployment are both under it. I push to that GitHub, and Railway auto-publishes from it — so one push puts the change live, and the team always has exactly what's running. No single person holding the only key.

While I'm here, we work in that one place and I keep a copy on my side too. When I leave, my access is simply removed — the running site, the data, the code and the full history all stay in the team's hands, still working. Nothing has to be transferred or untangled on my last day, because the split is already how it's built.

Two things worth putting in writing at some point, not urgent:
- iradar.net is my personal domain — the team uses it until I leave, then it swaps to a company address (a 5-minute change in Railway). Good to treat the address as temporary in any docs/links.
- The code was written outside my role's duties, so who keeps what (team's copy vs. mine) is an agreement between me and Infineon worth recording somewhere that isn't a source file. The captured data is Infineon's either way.

Full technical detail is in DEPLOY.md and the handover note in the repo, and the how-it-works guide is on Confluence + live at iradar.net/guide.

Happy to walk anyone through it.

Best,
Anas
