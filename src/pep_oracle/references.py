"""Auto-derive Chas/Dave reference voice embeddings from diarized episodes.

No manual labeling: Chas is the intro speaker (he always opens the show), and
Dave is the substantive second voice on "Dr Dave"-titled episodes. The spike
confirmed the intro speaker is the same voice across episodes (cosine ≤0.026)
and Dave's cluster is consistent and ~0.9 from guests, so averaging across
several episodes yields clean references.
"""

import math

from pep_oracle.transcripts.diarize import (
    SUBSTANTIVE_SPEAKER_SHARE,
    host_roster_from_title,
    load_cluster_info,
)


def _normalized_mean(vecs: list[list[float]]) -> list[float]:
    """Mean of L2-normalized vectors (the centroid direction)."""
    normed = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        normed.append([x / n for x in v])
    dim = len(normed[0])
    return [sum(v[i] for v in normed) / len(normed) for i in range(dim)]


def _intro_label(clusters: dict[str, dict]) -> str:
    return max(clusters, key=lambda lbl: clusters[lbl].get("intro_seconds", 0.0))


def build_references(episodes) -> dict[str, list[float]]:
    """Derive {name: embedding} from an iterable of (title, clusters_dict).

    Chas = the intro cluster (max intro_seconds), averaged over all episodes.
    Dave = the substantive top non-Chas cluster on Dr-Dave episodes, averaged.
    """
    chas_vecs: list[list[float]] = []
    dave_vecs: list[list[float]] = []
    for title, clusters in episodes:
        if not clusters:
            continue
        total = sum(c.get("seconds", 0.0) for c in clusters.values()) or 1.0
        chas_label = _intro_label(clusters)
        chas_emb = clusters[chas_label].get("embedding")
        if chas_emb:
            chas_vecs.append(chas_emb)

        if host_roster_from_title(title) == ["Chas", "Dave"]:
            others = sorted(
                ((lbl, c) for lbl, c in clusters.items() if lbl != chas_label),
                key=lambda kv: kv[1].get("seconds", 0.0),
                reverse=True,
            )
            if others:
                lbl, info = others[0]
                share = info.get("seconds", 0.0) / total
                if info.get("embedding") and share >= SUBSTANTIVE_SPEAKER_SHARE:
                    dave_vecs.append(info["embedding"])

    refs: dict[str, list[float]] = {}
    if chas_vecs:
        refs["Chas"] = _normalized_mean(chas_vecs)
    if dave_vecs:
        refs["Dave"] = _normalized_mean(dave_vecs)
    return refs


def diarized_episodes_from_collection(collection) -> list[tuple[str, dict]]:
    """Collect (title, clusters) for every diarized episode whose cache carries
    cluster embeddings."""
    got = collection.get(include=["metadatas"])
    titles: dict[str, str] = {}
    for m in got["metadatas"]:
        if "speakers" not in m:
            continue
        titles.setdefault(m["episode_guid"], m.get("episode_title", ""))
    episodes = []
    for guid, title in titles.items():
        clusters = load_cluster_info(guid)
        if clusters:
            episodes.append((title, clusters))
    return episodes
