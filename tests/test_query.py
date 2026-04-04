from pep_oracle.query import build_context, format_timestamp


def test_format_timestamp():
    assert format_timestamp(0.0) == "0:00:00"
    assert format_timestamp(3661.0) == "1:01:01"
    assert format_timestamp(None) == "?"


def test_format_timestamp_large():
    assert format_timestamp(12163.0) == "3:22:43"


def test_build_context_single_result():
    results = [{
        "episode_title": "MINING NEMO? PEP with Chas & Dr Dave (Ep 251, 20 March)",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 235.0,
        "text": "Let's get peppy. Welcome to PEP 251.",
    }]
    ctx = build_context(results)
    assert "MINING NEMO?" in ctx
    assert "Ep 251" in ctx
    assert "2026-03-21" in ctx
    assert "0:00:10" in ctx
    assert "0:03:55" in ctx
    assert "Let's get peppy" in ctx


def test_build_context_no_episode_number():
    results = [{
        "episode_title": "Bonus Episode",
        "episode_number": None,
        "episode_date": "2026-01-01",
        "start_time": None,
        "end_time": None,
        "text": "Some content.",
    }]
    ctx = build_context(results)
    assert "Ep " not in ctx
    assert "?–?" in ctx


def test_build_context_multiple_results():
    results = [
        {
            "episode_title": "Ep 250",
            "episode_number": 250,
            "episode_date": "2026-03-14",
            "start_time": 100.0,
            "end_time": 300.0,
            "text": "First chunk.",
        },
        {
            "episode_title": "Ep 249",
            "episode_number": 249,
            "episode_date": "2026-03-11",
            "start_time": 500.0,
            "end_time": 700.0,
            "text": "Second chunk.",
        },
    ]
    ctx = build_context(results)
    assert "First chunk." in ctx
    assert "Second chunk." in ctx
    assert ctx.index("First chunk.") < ctx.index("Second chunk.")
