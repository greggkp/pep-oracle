# Set up an AWS S3 bucket for pep-oracle backups

`pep-oracle backup` bundles the corpus export + derived state + Modal caches into
one timestamped tarball and `rclone copy`s it to the remote named in
`PEP_ORACLE_BACKUP_REMOTE`. This doc wires that remote to an **AWS S3** bucket
from scratch. rclone talks to S3 directly — no AWS CLI needed on the box.

Prereq: `rclone` installed (`rclone version`). On Debian/Ubuntu:
`sudo apt install rclone` or `curl https://rclone.org/install.sh | sudo bash`.

---

## Part 1 — In the AWS console (one-time)

### 1a. Create the bucket
S3 → **Create bucket**
- **Name**: globally unique, e.g. `pep-oracle-backup-<suffix>`. Write it down.
- **Region**: pick one near you, e.g. `us-east-1`. Write it down — rclone needs it.
- Leave **Block all public access** ON (default). Backups must never be public.
- *(Recommended)* enable **Bucket Versioning** — protects against a corrupted
  overwrite or accidental delete.

### 1b. Create a scoped IAM user
IAM → Users → **Create user**
- Name e.g. `pep-oracle-backup`. Do **not** grant console access.
- Permissions → **Add inline policy** → JSON. Paste this, replacing **both**
  `BUCKET` placeholders with your bucket name. This is least-privilege: only
  what backup (PutObject) and restore (GetObject + ListBucket) need, on this one
  bucket. `DeleteObject` lets you prune old remote tarballs later.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListThisBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET"
    },
    {
      "Sid": "RWThisBucketObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::BUCKET/*"
    }
  ]
}
```

### 1c. Create an access key
The user → **Security credentials** → **Create access key** → "Application
running outside AWS". Copy the **Access key ID** and **Secret access key**.
The secret is shown only once.

---

## Part 2 — Configure the rclone remote

Run on the box, substituting your real values. The secret is stored only in
`~/.config/rclone/rclone.conf` (mode 0600), never on the command line of any
other process.

```bash
mkdir -p ~/.config/rclone && rclone config create pep-backup s3 \
    provider AWS \
    env_auth false \
    access_key_id YOUR_ACCESS_KEY_ID \
    secret_access_key YOUR_SECRET_ACCESS_KEY \
    region YOUR_REGION \
    acl private \
    no_check_bucket true
```

`no_check_bucket true` is **required** with the least-privilege policy above:
without it, rclone issues a `CreateBucket` call before each upload, which the
policy denies (no `s3:CreateBucket`) — the upload then fails with
`IllegalLocationConstraintException`. The flag tells rclone to upload straight
to the already-existing bucket. (If you ever recreate the remote, set it with
`rclone config update pep-backup no_check_bucket true`.)

Verify connectivity against **the bucket** (not `rclone lsd pep-backup:`, which
lists *all* buckets and is correctly denied — the policy grants no
`s3:ListAllMyBuckets`). Replace `BUCKET`; exit code 0 with empty output is
success on a new bucket:

```bash
rclone ls pep-backup:BUCKET
```

---

## Part 3 — Point pep-oracle at the remote and test

Add `PEP_ORACLE_BACKUP_REMOTE=pep-backup:BUCKET` (replace `BUCKET`) to `.env`.

> **Gotcha — two `.env` files on this deployment.** The systemd units read
> `EnvironmentFile=/opt/pep-oracle/.env`, but the CLI (`uv run pep-oracle ...`
> from `/opt/pep-oracle/app`) loads `/opt/pep-oracle/app/.env` via
> python-dotenv's `find_dotenv` (nearest `.env` walking up from cwd). Set the
> var in **both** so the manual test *and* the automatic service agree:
>
> ```
> PEP_ORACLE_BACKUP_REMOTE=pep-backup:BUCKET
> ```

Run a full backup and confirm the tarball lands:

```bash
uv run pep-oracle backup
rclone ls pep-backup:BUCKET    # should show pep-oracle-backup-<timestamp>.tar.gz
```

That's it. The `pep-oracle-backup.service` systemd unit fires automatically via
`OnSuccess=` on the ingest unit, so once `PEP_ORACLE_BACKUP_REMOTE` is set,
off-site backups happen on every successful ingest with no further wiring.

To restore onto a fresh machine, see `restore.md` — recreate this `pep-backup`
remote (Part 2) so `PEP_ORACLE_BACKUP_REMOTE` resolves, then follow those steps.
