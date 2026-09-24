"""
Inbox consolidation: clustering + note-building. Pure functions, no I/O,
no policy decisions — CLI wires this together with confirmation and
actual writes. Converged design from ChatGPT + Gemini review:

- Jaccard similarity (not a fixed shared-tag count) — proportional,
  still purely keyword-based, no embeddings.
- Transitive clustering via connected components (union-find): if A~B
  and B~C both pass the threshold, A/B/C form one cluster even if A~C
  alone wouldn't — documented explicitly per ChatGPT's request, since
  the alternative (requiring every pair in a cluster to pass, i.e. a
  clique) would silently produce different, less intuitive results for
  the same threshold.
- Clustering NEVER crosses a confirmed-project boundary: items are
  bucketed by their confirmed project_key (or None, for anything not
  explicitly linked) before clustering runs. Two items confirmed-linked
  to different projects can never end up in the same cluster — this
  makes Gemini/ChatGPT's "don't silently choose a project" requirement
  structurally true rather than a runtime check to remember.
- suggested_project_key plays NO role in bucketing or write destination
  — per Gemini's explicit correction, an AI's suggestion never grants
  write access to project memory.
"""

import json
from dataclasses import dataclass, field

JACCARD_THRESHOLD = 0.5


def normalize_tags(raw_tags: list[str]) -> set[str]:
    return {t.strip().lower() for t in raw_tags if t and t.strip()}


def jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass
class Cluster:
    project_key: str | None  # the CONFIRMED project_key shared by every item, or None
    items: list = field(default_factory=list)  # list of inbox item dicts
    shared_tags: set = field(default_factory=set)  # intersection of all items' tags


def _union_find_clusters(items: list[dict], tag_sets: dict, threshold: float) -> list[list[dict]]:
    """
    Connected-components clustering: items are grouped transitively
    through any chain of pairwise Jaccard >= threshold. Deterministic:
    items are processed in a fixed order (by item_id) so the same input
    always produces the same clusters and the same item order within
    each cluster.
    """
    ordered = sorted(items, key=lambda i: i["item_id"])
    parent = {i["item_id"]: i["item_id"] for i in ordered}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)  # deterministic: smaller id wins as root

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a_id, b_id = ordered[i]["item_id"], ordered[j]["item_id"]
            if jaccard_similarity(tag_sets[a_id], tag_sets[b_id]) >= threshold:
                union(a_id, b_id)

    groups: dict = {}
    for item in ordered:
        root = find(item["item_id"])
        groups.setdefault(root, []).append(item)

    return [group for group in groups.values() if len(group) >= 2]  # singletons aren't clusters


def find_clusters(items: list[dict], threshold: float = JACCARD_THRESHOLD) -> list[Cluster]:
    """
    Buckets items by confirmed project_key (None bucket = everything not
    confirmed-linked, regardless of any suggested_project_key), then
    clusters within each bucket independently via Jaccard similarity.
    Items with no tags are excluded entirely (Jaccard against an empty
    set is always 0, so they'd never cluster with anything).
    """
    candidates = [i for i in items if i.get("tags")]
    tag_sets = {i["item_id"]: normalize_tags(json.loads(i["tags"])) for i in candidates}
    candidates = [i for i in candidates if tag_sets[i["item_id"]]]  # drop items with only-blank tags

    buckets: dict = {}
    for item in candidates:
        buckets.setdefault(item.get("project_key"), []).append(item)

    clusters = []
    for project_key, bucket_items in buckets.items():
        for group in _union_find_clusters(bucket_items, tag_sets, threshold):
            shared = tag_sets[group[0]["item_id"]]
            for item in group[1:]:
                shared &= tag_sets[item["item_id"]]
            clusters.append(Cluster(project_key=project_key, items=group, shared_tags=shared))

    return clusters


def build_consolidated_note(cluster: Cluster) -> str:
    """Human-readable synthesis of a cluster, for both preview and the actual write."""
    lines = [f"## Consolidated: {', '.join(sorted(cluster.shared_tags)) or '(no fully shared tags)'}"]
    lines.append(f"Sources: {', '.join(i['item_id'] for i in cluster.items)}")
    lines.append("")
    lines.append("### Combined Summary")
    for item in cluster.items:
        title = item.get("title") or "(untitled)"
        summary = item.get("summary") or "(no summary)"
        lines.append(f"- **{item['item_id']} — {title}**: {summary}")
    return "\n".join(lines)
