"""Modal cloud function for audio transcription.

Deploy with: modal deploy cloud/transcribe_modal.py
"""
import modal

app = modal.App("pep-oracle-transcribe")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "torch==2.5.1",
        "torchaudio==2.5.1",
        "faster-whisper==1.1.0",
        "ctranslate2==4.5.0",
        "numpy<2",
        "requests",
    )
    # ctranslate2 needs the torch-bundled cuDNN 9 and cuBLAS on LD_LIBRARY_PATH.
    .env({
        "LD_LIBRARY_PATH": (
            "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:"
            "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib"
        )
    })
)

model_cache = modal.Volume.from_name("pep-oracle-whisper-cache", create_if_missing=True)


@app.function(
    image=image,
    gpu="A100",
    volumes={"/models": model_cache},
    timeout=1800,
)
def transcribe(audio_url: str) -> list[dict]:
    """Download audio from a URL and run faster-whisper large-v3 on GPU.

    Returns a list of {"text": str, "start_time": float, "end_time": float}
    dicts in chronological order.
    """
    import subprocess
    import tempfile
    import urllib.request
    from pathlib import Path

    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory() as td:
        mp3_path = Path(td) / "audio.mp3"
        wav_path = Path(td) / "audio.wav"
        try:
            urllib.request.urlretrieve(audio_url, mp3_path)
        except Exception as e:
            raise RuntimeError(f"audio fetch failed: {e}") from e

        # Transcode to 16 kHz mono wav for clean sample boundaries (same as diarize).
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), "-ac", "1", "-ar", "16000", str(wav_path)],
            check=True,
            capture_output=True,
        )

        model = WhisperModel(
            "large-v3-turbo",
            device="cuda",
            compute_type="float16",
            download_root="/models",
        )
        segments, _ = model.transcribe(
            str(wav_path),
            word_timestamps=False,
            vad_filter=False,
        )
        return [
            {
                "text": s.text.strip(),
                "start_time": float(s.start),
                "end_time": float(s.end),
            }
            for s in segments
        ]
