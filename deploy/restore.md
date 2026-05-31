# Restore pep-oracle on a new machine

The backup tarball (`pep-oracle-backup-*.tar.gz`, produced by `pep-oracle backup`)
is a complete, recompute-free snapshot: restoring it costs **no Modal, no
re-embedding, and no LLM spend**. New episodes ingest via Modal going forward.

1. **Install** the app and create `.env`:
   ```bash
   git clone <repo> /opt/pep-oracle/app && cd /opt/pep-oracle/app
   uv pip install -e ".[server]"
   cp .env.example /opt/pep-oracle/.env   # then fill in ANTHROPIC_API_KEY, MODAL_*, PEP_ORACLE_* vars
   ```

2. **Pull the latest backup** (install + configure rclone first if needed:
   `rclone config`, recreating the remote named in `PEP_ORACLE_BACKUP_REMOTE`):
   ```bash
   rclone copy "$PEP_ORACLE_BACKUP_REMOTE" ./restore --include 'pep-oracle-backup-*.tar.gz'
   tar xzf "$(ls -t restore/pep-oracle-backup-*.tar.gz | head -1)" -C restore
   ```

3. **Restore derived state + Modal caches** into the data dir:
   ```bash
   mkdir -p ~/.pep-oracle/cache
   cp restore/speaker_profiles.json restore/topics.json ~/.pep-oracle/
   cp -r restore/cache/* ~/.pep-oracle/cache/
   ```

4. **Rebuild ChromaDB** from the export (do NOT copy chroma files directly):
   ```bash
   pep-oracle import restore/episodes.json
   ```

5. **Verify**: `pep-oracle status` (chunk/episode counts) and a sample
   `pep-oracle ask "..."`. Re-enable the systemd units (`pep-oracle-api`,
   `pep-oracle-ingest.timer`) to resume serving and ingestion.
