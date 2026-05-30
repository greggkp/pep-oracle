"""Modal cloud function for speaker diarization.

Deploy with: modal deploy cloud/diarize_modal.py
"""
import modal

app = modal.App("pep-oracle-diarize")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    # pyannote.audio 3.3.2 passes use_auth_token through to hf_hub_download,
    # which dropped that kwarg in huggingface_hub 0.26. Pin hub < 0.26.
    # torch/torchaudio 2.5.1 avoids the AudioMetaData issue in 3.3.x.
    .pip_install(
        "torch==2.5.1",
        "torchaudio==2.5.1",
        "pyannote.audio==3.3.2",
        "huggingface_hub<0.26",
        "numpy<2",
        "soundfile",
    )
    .env({"HF_HOME": "/cache/hf"})
)

hf_secret = modal.Secret.from_name("huggingface-token")
model_cache = modal.Volume.from_name("pep-oracle-pyannote-cache", create_if_missing=True)


INTRO_WINDOW_SECONDS = 60.0  # Chas always speaks the intro; first-60s owner = Chas


@app.function(
    image=image,
    gpu="A100",
    secrets=[hf_secret],
    volumes={"/cache/hf": model_cache},
    timeout=1800,
)
def diarize(
    audio_url: str,
    num_speakers: int | None = None,
    max_speakers: int | None = None,
) -> dict:
    """Download audio from a URL and run pyannote 3.1 on GPU.

    `num_speakers` forces an exact count; `max_speakers` caps clustering while
    letting pyannote pick the natural number below it. Leave both None: the
    over-clustering is resolved downstream by matching cluster embeddings to
    reference voices, not by capping (which merges Chas and Dave).

    Returns ``{"segments": [{"speaker", "start", "end"}],
    "clusters": [{"speaker", "seconds", "intro_seconds", "embedding"}]}``.
    Per-cluster centroid embeddings come from the diarization model itself
    (``return_embeddings``); clusters with no valid embedding are dropped from
    ``clusters`` but their turns remain in ``segments``.
    """
    import math
    import os
    import subprocess
    import tempfile
    import urllib.request
    from pathlib import Path

    import torch
    from pyannote.audio import Pipeline

    hf_token = os.environ["HF_TOKEN"]
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    pipeline.to(torch.device("cuda"))

    with tempfile.TemporaryDirectory() as td:
        mp3_path = Path(td) / "audio.mp3"
        wav_path = Path(td) / "audio.wav"
        try:
            urllib.request.urlretrieve(audio_url, mp3_path)
        except Exception as e:
            raise RuntimeError(f"audio fetch failed: {e}") from e

        # pyannote 4.x rejects mp3s where decoded samples don't match the
        # requested chunk size (off-by-a-few due to mp3 frame boundaries).
        # Transcode to 16 kHz mono wav so chunking is sample-exact.
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), "-ac", "1", "-ar", "16000", str(wav_path)],
            check=True,
            capture_output=True,
        )

        kwargs = {"return_embeddings": True}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        elif max_speakers is not None:
            kwargs["max_speakers"] = max_speakers
        result = pipeline(str(wav_path), **kwargs)

    if isinstance(result, tuple):
        diarization, embeddings = result
    else:
        # pyannote ≥3.3 returns DiarizeOutput; unwrap to Annotation + embeddings.
        diarization = getattr(result, "speaker_diarization", result)
        embeddings = getattr(result, "embeddings", getattr(result, "centroids", None))

    segments = []
    times: dict[str, float] = {}
    intro: dict[str, float] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append({"speaker": speaker, "start": float(turn.start), "end": float(turn.end)})
        times[speaker] = times.get(speaker, 0.0) + turn.duration
        overlap = max(0.0, min(turn.end, INTRO_WINDOW_SECONDS) - turn.start)
        if overlap > 0:
            intro[speaker] = intro.get(speaker, 0.0) + overlap

    clusters = []
    labels = diarization.labels()
    for i, label in enumerate(labels):
        vec = embeddings[i] if embeddings is not None else None
        if vec is None or any(math.isnan(float(x)) for x in vec):
            continue
        clusters.append({
            "speaker": label,
            "seconds": times.get(label, 0.0),
            "intro_seconds": intro.get(label, 0.0),
            "embedding": [float(x) for x in vec],
        })

    return {"segments": segments, "clusters": clusters}
