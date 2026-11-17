"""Which source frames get painted (Viggle Remake "pins"). Pure, so it is tested without WanGP."""
WIN, OVERLAP = 294, 17  # WanGP sliding window for long takes; frames in a window's first 17 are the previous take's, never pinned
HOP = WIN - OVERLAP
EDGE = 3  # frames kept away from a cut or a window edge (WanGP: "at least 3 frames from a cut")
MAX = 4  # Viggle Remake takes 1 to 4 painted frames per render


def window_of(rel, windows):
    """Window index of a frame counted from the render start, or None when it sits in an overlap or past the edge margin."""
    k = min(rel // HOP, windows - 1)  # window k spans [k*HOP, k*HOP+WIN); its first OVERLAP frames end window k-1
    if k and rel < k * HOP + OVERLAP + EDGE:
        return None
    if k < windows - 1 and rel >= (k + 1) * HOP - EDGE:
        return None
    return k


def usable(f, group, cuts):
    (r0, _), (a, b) = group["render"], group["own"]
    if not a + EDGE <= f < b - EDGE or any(abs(f - c) < EDGE for c in cuts):
        return False
    return window_of(f - r0, group["windows"]) is not None


def best(lo, hi, group, cuts, sharp):
    """Sharpest usable frame in the middle half of [lo, hi)."""
    q = (hi - lo) // 4
    cands = [f for f in range(lo + q, hi - q) if f < len(sharp) and usable(f, group, cuts)]
    cands = cands or [f for f in range(lo, hi) if f < len(sharp) and usable(f, group, cuts)]
    return max(cands, key=lambda f: sharp[f]) if cands else None


def choose(group, cuts, sharp, asked=()):
    (r0, _), (a, b) = group["render"], group["own"]
    mine = sorted(f for f in asked if a <= f < b and usable(f, group, cuts))
    if mine:
        return mine[:MAX]
    if group["windows"] > 1:  # one long take: one pin per window
        spans = [(max(a, r0 + k * HOP), min(b, r0 + k * HOP + WIN)) for k in range(group["windows"])]
    else:  # one pin per shot, the longest shots first
        edges = [a, *[c for c in cuts if a < c < b], b]
        spans = sorted(zip(edges, edges[1:]), key=lambda s: s[0] - s[1])[:MAX]
    picked = {best(lo, hi, group, cuts, sharp) for lo, hi in spans}
    picked.discard(None)
    return sorted(picked)[:MAX] or [best(a, b, group, cuts, sharp) or a]
