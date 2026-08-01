"""§K1 (Handoff #8): dependency-free near-duplicate ranking for reviewers.

A review AID, never auto-rejection: for a question under review, rank the
existing APPROVED questions by word overlap so the moderator can eyeball
"is this a duplicate?" before approving. Candidates come from the same
category first; if the category is thin (< MIN_CATEGORY_POOL candidates) the
pool falls back to all approved questions globally.

Ranking: normalize (casefold, strip punctuation), drop stopwords, then
Jaccard similarity on the remaining significant words. Smarter similarity
(trigram / embeddings) is a punted §M candidate — this is deliberately dumb,
fast, and explainable at the queue's scale.

§F4 (Handoff #10): categories are an M2M now, so "same category first"
became "shares ANY category" — the primary pool is every approved question
sharing at least one category with the one under review; the global fallback
for thin pools is unchanged. Each match carries `category_names` (sorted
list) instead of the old single name.
"""
import re
import string

from django.db.models import Count, Q

from .models import ModerationStatus, Question

# Words that carry no duplicate signal in trivia phrasing.
STOPWORDS = frozenset(
    """a an and are as at be but by did do does for from had has have how in is
    it its name of on or that the their there these this to was what when
    where which who whom whose why with you your""".split()
)

_PUNCT = re.compile(f"[{re.escape(string.punctuation)}]")

MIN_CATEGORY_POOL = 5  # fewer same-category candidates than this → go global
TOP_N = 5


def significant_words(text: str) -> frozenset[str]:
    """Casefold, strip punctuation, drop stopwords and single letters."""
    cleaned = _PUNCT.sub(" ", (text or "").casefold())
    return frozenset(w for w in cleaned.split() if len(w) > 1 and w not in STOPWORDS)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _match_row(candidate: Question, score: float) -> dict:
    """One match row — the ONE shape both the single and batch endpoints
    return (§K1 pinned by the #15 parity test). category_names deliberately
    lists ALL of the candidate's categories, deleted included — same reading
    as the single endpoint has always had."""
    return {
        "id": candidate.id,
        "question_text": candidate.question_text,
        "answer": candidate.answer,
        # §F4: the list replaces the old category_id/category_name pair.
        "category_names": sorted(cat.name for cat in candidate.categories.all()),
        "score": round(score, 3),
        "usage_count": candidate.usage_count,
    }


def similar_questions_batch(questions, *, top_n: int = TOP_N) -> dict[int, list[dict]]:
    """§F2 (Handoff #15): similar_questions() for MANY targets in ONE pass.

    The #14c herd was 50 per-card calls, each independently loading (and,
    for thin categories, re-scanning) the whole approved corpus. Here the
    corpus is fetched ONCE (with category ids + the §J1 usage annotation),
    `significant_words` is computed ONCE per candidate, and each target then
    runs the same pool logic in Python over the prefetched category-id sets:
    primary pool = approved questions sharing ANY category (soft-deleted
    categories still count as shared, exactly like the single endpoint's
    `categories__in` SQL), global fallback when the pool is thinner than
    MIN_CATEGORY_POOL, the target always excluded from both.

    PARITY CONTRACT (tested): for every target, the returned rows equal
    `similar_questions(target)` exactly — same candidates, same scores,
    same (-score, pk) ordering, same row shape. Change one, change both.
    Returns {target_pk: [match_row, ...]} for every target passed in.
    """
    approved = Q(moderation_status=ModerationStatus.APPROVED)
    corpus = list(
        Question.objects.filter(approved, deleted_at__isnull=True)
        .annotate(usage_count=Count("boardcell", distinct=True))
        .prefetch_related("categories")
    )
    words = {c.pk: significant_words(c.question_text) for c in corpus}
    category_ids = {c.pk: {cat.pk for cat in c.categories.all()} for c in corpus}

    results: dict[int, list[dict]] = {}
    for question in questions:
        target_categories = {cat.pk for cat in question.categories.all()}
        pool = [c for c in corpus if c.pk != question.pk and (category_ids[c.pk] & target_categories)]
        if len(pool) < MIN_CATEGORY_POOL:
            pool = [c for c in corpus if c.pk != question.pk]
        target = significant_words(question.question_text)
        scored = []
        for candidate in pool:
            score = jaccard(target, words[candidate.pk])
            if score > 0:
                scored.append((score, candidate))
        scored.sort(key=lambda pair: (-pair[0], pair[1].pk))
        results[question.pk] = [_match_row(c, score) for score, c in scored[:top_n]]
    return results


def similar_questions(question: Question, *, top_n: int = TOP_N) -> list[dict]:
    """Top matches for `question` among approved questions, each with the
    §J1 usage count (cells referencing it, across all games) and the answer —
    everything a reviewer needs to call "duplicate" at a glance."""
    approved = Q(moderation_status=ModerationStatus.APPROVED)
    base = (
        # §I: deleted questions can't be duplicated-against — they're gone
        # from every listing a reviewer could compare with.
        Question.objects.filter(approved, deleted_at__isnull=True)
        .exclude(pk=question.pk)
        .annotate(usage_count=Count("boardcell", distinct=True))
        .prefetch_related("categories")
    )
    # §F4: shares ANY category. .distinct(): the M2M join repeats a candidate
    # once per shared category otherwise.
    pool = list(base.filter(categories__in=question.categories.all()).distinct())
    if len(pool) < MIN_CATEGORY_POOL:
        pool = list(base)
    target = significant_words(question.question_text)
    scored = []
    for candidate in pool:
        score = jaccard(target, significant_words(candidate.question_text))
        if score > 0:
            scored.append((score, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].pk))
    # §F2 (#15): rows come from the shared _match_row — the batch endpoint
    # returns the SAME shape and the parity test compares them verbatim.
    return [_match_row(c, score) for score, c in scored[:top_n]]
