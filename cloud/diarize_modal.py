"""Modal cloud function for speaker diarization.

Deploy with: modal deploy cloud/diarize_modal.py
"""
import modal

app = modal.App("pep-oracle-diarize")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "pyannote.audio==3.3.*",
        "numpy",
        "soundfile",
        "torch",
        "torchaudio",
    )
)

hf_secret = modal.Secret.from_name("huggingface-token")


@app.function(
    image=image,
    gpu="L4",
    secrets=[hf_secret],
    timeout=1800,
)
def diarize(audio_url: str, num_speakers: int | None = None) -> list[dict]:
    """Download audio from a URL and run pyannote 3.1 on GPU.

    Returns a list of {"speaker": str, "start": float, "end": float} dicts
    sorted by start time.
    """
    import os
    import tempfile
    import urllib.request
    from pathlib import Path

    import torch
    from pyannote.audio import Pipeline

    hf_token = os.environ["HF_TOKEN"]
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    pipeline.to(torch.device("cuda"))

    with tempfile.TemporaryDirectory() as td:
        audio_path = Path(td) / "audio.mp3"
        try:
            urllib.request.urlretrieve(audio_url, audio_path)
        except Exception as e:
            raise RuntimeError(f"audio fetch failed: {e}") from e

        kwargs = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        result = pipeline(str(audio_path), **kwargs)

    # pyannote ≥3.3 returns DiarizeOutput; unwrap to Annotation
    diarization = getattr(result, "speaker_diarization", result)
    return [
        {"speaker": speaker, "start": float(turn.start), "end": float(turn.end)}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
