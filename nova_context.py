"""
Short-term conversational context, pronoun/ordinal reference tracking, and safe
multi-command segmentation for Nova.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Optional

ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "top": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
}

_CMD_START_RE = re.compile(
    r"^(?:open|launch|start|close|quit|exit|check|tell|what|what's|whats|how|find|locate|"
    r"search|read|summarize|summarise|set|turn|mute|unmute|volume|brightness|battery|wifi|"
    r"wi fi|disk|storage|maximize|minimise|minimize|snap|move|tile|switch|take|list|show|"
    r"play|pause|resume|next|previous|skip|stop|create|make|git|github|system|running|"
    r"lock|sleep|refresh|reload|new|copy|paste|select|scroll|clear)\b"
)

_NO_SPLIT_RE = re.compile(
    r"^(?:create|save|add|make)\s+(?:a\s+)?routine\b|"
    r"^(?:remember|note|jot|type|dictate|google|look up|search\b)|"
    r"\b(?:add|create|put)\b.*\b(?:task|to ?do|reminder|alarm|timer)\b|"
    r"\bremind me\b|\bmove\b.*\bfrom\b.*\bto\b|\bcreate\b.*\bfolder\b|"
    r"\b(?:difference between|compare|vs|versus)\b"
)


def extract_ordinal(t: str) -> Optional[int]:
    """Return 0-based index for 'first', '2nd', 'number 2', etc., else None."""
    if m := re.search(r"\b(?:number|result|item|file|option|#)\s*(\d+)\b", t):
        idx = int(m.group(1)) - 1
        return idx if 0 <= idx < 20 else None
    for tok in re.findall(r"[a-z0-9]+", t.lower()):
        if tok in ORDINALS:
            return ORDINALS[tok]
    return None


_CLAUSE_FILLER_RE = re.compile(r"^(?:please|also|then|and|hey|ok|okay|can you|could you|would you|will you)\s+")


def _clean_clause(s: str) -> str:
    c = " ".join(re.sub(r"[^\w\s']", " ", s.lower()).split())
    for _ in range(3):
        nxt = _CLAUSE_FILLER_RE.sub("", c).strip()
        if nxt == c:
            break
        c = nxt
    return c


def split_compound(t: str, raw: str = "") -> list[tuple[str, str]]:
    """Split safe multi-action utterances into `[(clean_part, raw_part)]` steps.

    Splits on `raw` when punctuation (`,` / `;`) is present so commas stripped during
    ASR normalization still cleanly separate parallel commands.
    """
    low = (t or "").strip()
    orig = (raw or low).strip()
    if not low or _NO_SPLIT_RE.search(low):
        return [(low, orig)]
    sep = r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*"
    raw_parts = [p.strip() for p in re.split(sep, orig, flags=re.I) if p.strip()]
    use_raw = ("," in orig or ";" in orig) and len(raw_parts) >= 2
    candidates = raw_parts if use_raw else [p.strip() for p in re.split(sep, low) if p.strip()]
    if not (2 <= len(candidates) <= 4):
        return [(low, orig)]
    final_parts = []
    for p in candidates:
        cp = _clean_clause(p)
        if not cp or not _CMD_START_RE.match(cp):
            return [(low, orig)]
        final_parts.append(cp)
    if len(raw_parts) != len(final_parts):
        raw_parts = list(final_parts)
    return list(zip(final_parts, raw_parts))


@dataclass
class SessionContext:
    """Short-term working state across turns within a session."""

    last_folder: Optional[Path] = None
    last_files: list[Path] = field(default_factory=list)
    last_file: Optional[Path] = None
    last_app: str = ""
    last_results: list[dict[str, str]] = field(default_factory=list)
    last_url: str = ""
    last_search_query: str = ""
    last_topic: str = ""
    last_reply: str = ""
    privacy_mode: bool = False

    def reset(self, keep_privacy: bool = True) -> None:
        priv = self.privacy_mode
        self.last_folder = None
        self.last_files.clear()
        self.last_file = None
        self.last_app = ""
        self.last_results.clear()
        self.last_url = ""
        self.last_search_query = ""
        self.last_topic = ""
        self.last_reply = ""
        self.privacy_mode = priv if keep_privacy else False

    def record_folder(self, folder: Path) -> None:
        self.last_folder = Path(folder)

    def record_files(self, files: list[Path]) -> None:
        self.last_files = [Path(f) for f in files]
        self.last_file = self.last_files[0] if self.last_files else None
        if self.last_file:
            self.last_folder = self.last_file.parent

    def record_search(self, query: str, results: list[dict[str, str]]) -> None:
        self.last_search_query = query
        self.last_results = list(results or [])
        if self.last_results:
            self.last_url = self.last_results[0].get("url", "")

    def record_url(self, url: str) -> None:
        self.last_url = url

    def record_app(self, app_name: str) -> None:
        self.last_app = app_name

    def summary_prompt(self) -> str:
        bits = []
        if self.last_app:
            bits.append(f"last_app={self.last_app}")
        if self.last_folder:
            bits.append(f"last_folder={self.last_folder}")
        if self.last_file:
            bits.append(f"last_file={self.last_file}")
        if self.last_results:
            bits.append(f"search_results={len(self.last_results)} for '{self.last_search_query}'")
        elif self.last_url:
            bits.append(f"last_url={self.last_url}")
        return ("Active context: " + "; ".join(bits) + ".") if bits else ""
