# GitHub Actions setup

1. Put these files in the repository root.
2. Optional proxy: Repository Settings -> Secrets and variables -> Actions -> New repository secret.
3. Secret name: `SCRAPER_PROXY`
4. Value example: `http://username:password@proxy-host:port`
5. Enable GitHub Actions and run **Scan every 15 minutes** once with `workflow_dispatch`.

The scheduled workflow runs at `*/15 * * * *` and updates `movies.json` only when content changes.
GitHub scheduled jobs can start later than the exact cron time during platform load.

Use only against sources you are authorized to access, and comply with site terms and applicable law.
