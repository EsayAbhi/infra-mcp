# Publishing to GitHub (maintainer notes)

1. Create an empty repo on github.com (no README), named `infra-mcp`.
2. In this folder:
   ```bash
   git init -b main
   git add .
   git status            # confirm config.yaml / .env / audit.jsonl are NOT listed
   git commit -m "Initial release: infra-mcp 0.1.0"
   git remote add origin https://github.com/YOUR-USER/infra-mcp.git
   git push -u origin main
   ```
3. Replace `YOUR NAME` in LICENSE and `YOUR-USER` in README.
4. Release: `git tag v0.1.0 && git push origin v0.1.0`, then GitHub > Releases > Draft new release.
5. Repo settings: add topics `mcp`, `model-context-protocol`, `vmware`, `devops`, `sysadmin`, `ai-agents`.
