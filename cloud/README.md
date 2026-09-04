# Modal GPU functions

The former AWS Fargate ingestion task called two scale-to-zero Modal functions
on A100 GPUs. The apps, volumes, secret, and API tokens were deleted during the
2026-09-04 full decommission. The setup below is a restoration procedure, not a
description of live resources.

- `transcribe_modal.py` runs faster-whisper and caches models in the
  `pep-oracle-whisper-cache` volume.
- `diarize_modal.py` runs pyannote and caches models in the
  `pep-oracle-pyannote-cache` volume.

## One-time setup

1. Install and authenticate Modal:

   ```bash
   uv pip install modal
   modal token new
   ```

2. Accept the Hugging Face terms for
   [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1),
   then create the secret used by the diarization function:

   ```bash
   modal secret create huggingface-token HF_TOKEN=<your-hf-token>
   ```

3. Deploy both functions:

   ```bash
   modal deploy cloud/transcribe_modal.py
   modal deploy cloud/diarize_modal.py
   ```

4. Store the Modal service credentials in the SSM SecureString parameters consumed
   by the Fargate task:

   ```bash
   aws ssm put-parameter --name /pep-oracle/modal-token-id --type SecureString \
     --value "$MODAL_TOKEN_ID" --overwrite --region ap-southeast-2
   aws ssm put-parameter --name /pep-oracle/modal-token-secret --type SecureString \
     --value "$MODAL_TOKEN_SECRET" --overwrite --region ap-southeast-2
   ```

## Redeploying

Run `modal deploy` for each changed function. The ingestion client resolves the
deployed function by app and function name at runtime; application code does not
contain a deployment URL. No serving-Lambda restart is required.

Modal pricing and GPU availability can change. Check Modal's current pricing before
estimating an ingestion run rather than relying on a fixed per-episode estimate.
