from pep_oracle.references import build_references
from pep_oracle.transcripts.diarize import cos_dist


def test_build_references_chas_from_intro_dave_from_title():
    e_chas, e_dave = [1.0, 0.0], [0.0, 1.0]
    episodes = [
        ("PEP with Chas & Dr Dave (Ep 1)", {
            "S0": {"embedding": e_chas, "seconds": 100.0, "intro_seconds": 50.0},  # intro -> Chas
            "S1": {"embedding": e_dave, "seconds": 60.0, "intro_seconds": 0.0},    # substantive -> Dave
            "S2": {"embedding": [0.7, 0.7], "seconds": 4.0, "intro_seconds": 0.0}, # tiny tail -> ignored
        }),
    ]
    refs = build_references(episodes)
    assert set(refs) == {"Chas", "Dave"}
    assert cos_dist(refs["Chas"], e_chas) < 0.01
    assert cos_dist(refs["Dave"], e_dave) < 0.01


def test_build_references_guest_episode_yields_no_dave():
    # Title has no Dave -> the 2nd cluster (a guest) must not become Dave.
    episodes = [
        ("PEP with Chas & Melina Wicks", {
            "S0": {"embedding": [1.0, 0.0], "seconds": 100.0, "intro_seconds": 50.0},
            "S1": {"embedding": [0.0, 1.0], "seconds": 60.0, "intro_seconds": 0.0},
        }),
    ]
    refs = build_references(episodes)
    assert "Chas" in refs
    assert "Dave" not in refs


def test_build_references_skips_tiny_second_cluster_as_dave():
    # On a Dr-Dave episode where the 2nd cluster is tiny (<15%), don't call it Dave.
    episodes = [
        ("PEP with Chas & Dr Dave", {
            "S0": {"embedding": [1.0, 0.0], "seconds": 100.0, "intro_seconds": 50.0},
            "S1": {"embedding": [0.0, 1.0], "seconds": 5.0, "intro_seconds": 0.0},  # 5% -> Lachie
        }),
    ]
    refs = build_references(episodes)
    assert "Chas" in refs
    assert "Dave" not in refs


def test_build_references_averages_chas_across_episodes():
    episodes = [
        ("PEP with Chas & Dr Dave", {
            "A": {"embedding": [1.0, 0.0], "seconds": 100.0, "intro_seconds": 50.0},
        }),
        ("PEP with Chas & Dr Dave", {
            "A": {"embedding": [0.0, 1.0], "seconds": 100.0, "intro_seconds": 50.0},
        }),
    ]
    refs = build_references(episodes)
    # Mean of the two unit vectors points at 45 degrees.
    assert abs(refs["Chas"][0] - refs["Chas"][1]) < 1e-9
