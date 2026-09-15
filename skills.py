"""Deterministic skill-list cleanup shared by extraction and matching."""
import re
import unicodedata

SKILL_ALIASES = {
    "js": "javascript", "ts": "typescript", "py": "python",
    "postgres": "postgresql", "nodejs": "node.js", "node js": "node.js",
    "reactjs": "react", "react.js": "react", "c sharp": "c#",
    "amazon web services": "aws", "k8s": "kubernetes",
    "llms": "llm", "large language models": "llm", "large language model": "llm",
    "rest apis": "rest api", "restful apis": "rest api", "restful api": "rest api",
    "apis": "api", "vector embeddings": "vector embedding",
    "scikit learn": "scikit-learn", "sklearn": "scikit-learn",
}
_QUALIFIERS = {"advanced", "intermediate", "beginner", "fluent", "native", "basic", "proficient"}


def normalize_skill(skill):
    normalized = " ".join(unicodedata.normalize("NFKC", skill).casefold().split()).strip(" ,;•")
    return SKILL_ALIASES.get(normalized, normalized)


def unique_skills(skills):
    """Expand category lines, delimiters and parenthesized tool lists into atoms.

    Preserve meaningful punctuation: C++ differs from C and CI/CD stays intact.
    Stable canonical ordering prevents extraction order from changing gap priority.
    """
    found = set()
    for entry in skills:
        for line in re.split(r"[\n;|]", entry):
            if ":" in line:
                line = line.split(":", 1)[1]
            line = re.sub(r"\bCI\s*/\s*CD\b", "CI_CD_TOKEN", line, flags=re.I)
            line = re.sub(r"[()]", ",", line)
            for item in re.split(r"[,/]", line):
                item = item.replace("CI_CD_TOKEN", "CI/CD")
                normalized = normalize_skill(item)
                if normalized and normalized not in _QUALIFIERS:
                    found.add(normalized)
    return sorted(found)


def source_backed_skills(skills, source):
    """Display only extracted skill names (or explicit aliases) actually in the CV."""
    source = unicodedata.normalize("NFKC", source).casefold()
    supported = []
    for skill in unique_skills(skills):
        aliases = [skill] + [alias for alias, canonical in SKILL_ALIASES.items() if canonical == skill]
        if any(re.search(r"(?<![\w+#])" + r"\s+".join(re.escape(word) for word in alias.split()) + r"(?![\w+#])", source) for alias in aliases):
            supported.append(skill)
    return supported
