"""SRT interchange; does not normalize text or alter recognition timing."""
import re
from pathlib import Path
from ..errors import ValidationError
from ..models import TranscriptSegment, TranscriptTimeline

PARSER_VERSION = 1
_TIME = r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})"
_RANGE = re.compile(rf"^{_TIME}\s*-->\s*{_TIME}\s*$")


def _ms(parts: tuple[str, ...]) -> int:
    h, m, s, ms = map(int, parts)
    if m > 59 or s > 59:
        raise ValidationError("invalid SRT timestamp")
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def load_srt(path: Path) -> TranscriptTimeline:
    try:
        content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"cannot read SRT: {path}: {exc}") from exc
    segments = []
    for block in re.split(r"\n[ \t]*\n", content.strip("\n\ufeff")):
        if not block.strip():
            continue
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValidationError(f"invalid SRT cue {len(segments) + 1}")
        match = _RANGE.fullmatch(lines[1].strip())
        if not match:
            raise ValidationError(f"invalid SRT timing in cue {len(segments) + 1}")
        values = match.groups()
        segments.append(TranscriptSegment(str(len(segments) + 1), _ms(values[:4]),
                                          _ms(values[4:]), "\n".join(lines[2:])))
    if not segments:
        raise ValidationError("SRT contains no valid subtitles")
    return TranscriptTimeline(tuple(segments), max(s.end_ms for s in segments))
