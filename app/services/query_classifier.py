import re

BROAD_PATTERNS = [
    r"\bsummar(y|ize|ise)\b",
    r"\bkey points?\b",
    r"\bmain points?\b",
    r"\boverview\b",
    r"\bwhat.*(document|pdf|this).*about\b",
    r"\bconclu(de|sion)\b",
]

COMPARISON_PATTERNS = [
    r"\bcompare\b",
    r"\bcomparison\b",
    r"\bdifference(s)? between\b",
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bsimilarit(y|ies)\b",
    r"\bcontrast\b",
]


def classify_question(question: str, document_count: int) -> str:
    q = question.lower()

    if document_count > 1 and any(re.search(p, q) for p in COMPARISON_PATTERNS):
        return "comparison"

    if any(re.search(p, q) for p in BROAD_PATTERNS):
        return "broad"

    return "specific"


def _normalize(name: str) -> str:
    name = re.sub(r"\.(pdf|PDF)$", "", name)
    name = re.sub(r"[_\-]+", " ", name)
    return name.strip().lower()


def extract_mentioned_documents(question: str, documents: list[dict]) -> list[str]:
    """
    documents: [{"filename": ..., "display_title": ...}, ...]

    Returns the filenames of documents that seem explicitly named in the
    question — matched against EITHER the filename OR its extracted
    display_title, so "compare paper1.pdf with the other one" and "compare
    the funding report with the other one" both work regardless of how
    meaningless the actual filename is. Empty list means nothing matched —
    callers should treat that as "use all documents".
    """
    q = question.lower()
    q_compact = re.sub(r"\s+", "", q)

    matched = []
    for doc in documents:
        candidates = [doc["filename"]]
        if doc.get("display_title"):
            candidates.append(doc["display_title"])

        for candidate in candidates:
            normalized = _normalize(candidate)
            normalized_compact = normalized.replace(" ", "")
            if normalized in q or normalized_compact in q_compact:
                matched.append(doc["filename"])
                break  # don't add the same filename twice if both candidates match

    return matched

def _normalize_section_words(text: str) -> set[str]:
    """
    Lowercases, strips punctuation, and crudely singularizes each word
    (drops a trailing "s", not a real stemmer) so "Projects"/"project",
    "Skills"/"skill" etc. match without needing an exact string match.
    """
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    normalized = set()
    for w in words:
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        normalized.add(w)
    return normalized


def match_section_title(question: str, section_titles: list[str]) -> str | None:
    """
    Checks whether `question` is plausibly asking for a whole document
    section by name (e.g. "skills", "education", "Positions of
    Responsibility") rather than asking a genuine question. Returns the
    matching section_title exactly as stored (for get_chunks_by_section),
    or None if nothing matches confidently enough.

    Deliberately conservative — checked in this order, first hit wins:
      1. Exact match (case-insensitive) to a section title.
      2. Normalized match — same set of (singularized) words, in any order
         ("technical skills" == "Skills Technical" == "Technical Skill").
      3. Every normalized word of the QUESTION appears in the section
         title's normalized words ("skills" -> {"skill"} subset-of
         {"technical","skill"} -> Technical Skills).
      4. Every normalized word of the SECTION TITLE appears in the
         question's normalized words ("what are the technical skills"
         contains both "technical" and "skill").

    Tiers 3/4 both require the question to have at least one real content
    word (not just stopword noise) so an empty/near-empty query can't
    accidentally match every section. A natural question like "where did
    the person study" won't match "Education" here — normal hybrid
    retrieval handles that case instead, which is intentional: this
    shortcut is only for queries that are themselves basically the section
    name, not for questions *about* a section's topic.
    """
    if not section_titles:
        return None

    q_stripped = question.strip().lower()
    q_words = _normalize_section_words(question)
    if not q_words:
        return None

    for title in section_titles:
        if q_stripped == title.strip().lower():
            return title

    for title in section_titles:
        if q_words == _normalize_section_words(title):
            return title

    for title in section_titles:
        title_words = _normalize_section_words(title)
        if title_words and q_words.issubset(title_words):
            return title

    for title in section_titles:
        title_words = _normalize_section_words(title)
        if title_words and title_words.issubset(q_words):
            return title

    return None