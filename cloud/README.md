# Cloud functions

## diarize_modal.py

Speaker diarization on a Modal L4 GPU. Replaces local pyannote for the ingestion pipeline.

### One-time setup

1. Install Modal: `uv pip install modal`
2. Authenticate: `modal token new` (opens browser)
3. Create HuggingFace secret:
   ```
   modal secret create huggingface-token HF_TOKEN=<your-hf-token>
   ```
   Token must have access to `pyannote/speaker-diarization-3.1` (accept the license at https://huggingface.co/pyannote/speaker-diarization-3.1).
4. Deploy: `modal deploy cloud/diarize_modal.py`
5. Copy `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from `~/.modal/token` into `/opt/pep-oracle/app/.env`.
6. Restart the pep-oracle server.

### Redeploy

When `diarize_modal.py` changes: `modal deploy cloud/diarize_modal.py`. The client code looks up the deployed function by name at call time; no client change needed.

### Cost

~$0.05 per 2-hour episode on L4 ($0.80/hr, ~5 min per episode).
