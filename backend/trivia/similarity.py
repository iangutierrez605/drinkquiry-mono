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
        .select_related("category")
    )
    pool = list(base.filter(category_id=question.category_id))
    if len(pool) < MIN_CATEGORY_POOL:
        pool = list(base)
    target = significant_words(question.question_text)
    scored = []
    for candidate in pool:
        score = jaccard(target, significant_words(candidate.question_text))
        if score > 0:
            scored.append((score, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].pk))
    return [
        {
            "id": c.id,
            "question_text": c.question_text,
            "answer": c.answer,
            "category_id": c.category_id,
            "category_name": c.category.name,
            "score": round(score, 3),
            "usage_count": c.usage_count,
        }
        for score, c in scored[:top_n]
    ]
