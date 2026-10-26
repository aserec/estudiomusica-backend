import hashlib
import runpy
import subprocess
import sys
from pathlib import Path

from app.schemas.categories import _next_grapheme, category_name_key
from app.schemas.unicode_props import (
    ASSIGNED,
    EMOJI_MODIFIER_BASE,
    EMOJI_PRESENTATION,
    EXTENDED_PICTOGRAPHIC,
    GRAPHEME_CONTROL,
    GRAPHEME_CR,
    GRAPHEME_EXTEND,
    GRAPHEME_LF,
    GRAPHEME_PREPEND,
    GRAPHEME_SPACINGMARK,
    INCB_CONSONANT,
    INCB_LINKER,
    JOINING_D,
    JOINING_L,
    JOINING_R,
    JOINING_T,
    UCD_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]


def test_generated_unicode_tables_match_vendored_ucd() -> None:
    generated = runpy.run_path(str(ROOT / "scripts" / "generate_unicode_tables.py"))
    assert generated["UCD_VERSION"] == UCD_VERSION == "16.0.0"
    tables = generated["parse_tables"](generated["UCD_DIR"])
    assert tables["JOINING_L"] == JOINING_L
    assert tables["JOINING_D"] == JOINING_D
    assert tables["JOINING_R"] == JOINING_R
    assert tables["JOINING_T"] == JOINING_T
    assert tables["EXTENDED_PICTOGRAPHIC"] == EXTENDED_PICTOGRAPHIC
    assert tables["EMOJI_PRESENTATION"] == EMOJI_PRESENTATION
    assert tables["EMOJI_MODIFIER_BASE"] == EMOJI_MODIFIER_BASE
    assert not any(start <= 0x00A9 <= end for start, end in EMOJI_MODIFIER_BASE)
    assert any(start <= 0x1F469 <= end for start, end in EMOJI_MODIFIER_BASE)
    assert any(start <= 0x261D <= end for start, end in EMOJI_MODIFIER_BASE)
    assert tables["ASSIGNED"] == ASSIGNED
    assert tables["GRAPHEME_CR"] == GRAPHEME_CR
    assert tables["GRAPHEME_LF"] == GRAPHEME_LF
    assert tables["GRAPHEME_CONTROL"] == GRAPHEME_CONTROL
    assert tables["GRAPHEME_PREPEND"] == GRAPHEME_PREPEND
    assert tables["GRAPHEME_EXTEND"] == GRAPHEME_EXTEND
    assert tables["GRAPHEME_SPACINGMARK"] == GRAPHEME_SPACINGMARK
    assert any(start <= 0x000D <= end for start, end in GRAPHEME_CR)
    assert any(start <= 0x000A <= end for start, end in GRAPHEME_LF)
    assert any(start <= 0x0000 <= end for start, end in GRAPHEME_CONTROL)
    assert tables["INCB_LINKER"] == INCB_LINKER
    assert tables["INCB_CONSONANT"] == INCB_CONSONANT
    assert any(start <= 0x0600 <= end for start, end in GRAPHEME_PREPEND)
    assert any(start <= 0x0E33 <= end for start, end in GRAPHEME_SPACINGMARK)
    assert any(start <= 0x094D <= end for start, end in INCB_LINKER)
    assert any(start <= 0x200C <= end for start, end in GRAPHEME_EXTEND)
    assert not any(start <= 0x088F <= end for start, end in ASSIGNED)
    assert not any(start <= 0x1ACF <= end for start, end in ASSIGNED)
    assert not any(start <= 0x1FAEF <= end for start, end in ASSIGNED)
    assert any(start <= 0x0710 <= end for start, end in JOINING_R)
    assert any(start <= 0x07CA <= end for start, end in JOINING_D)
    assert any(start <= 0x1820 <= end for start, end in JOINING_D)
    assert any(start <= 0x1E922 <= end for start, end in JOINING_D)
    assert any(start <= 0x0841 <= end for start, end in JOINING_D)
    assert any(start <= 0x08C8 <= end for start, end in JOINING_D)
    assert any(start <= 0x2600 <= end for start, end in EXTENDED_PICTOGRAPHIC)
    assert any(start <= 0x2764 <= end for start, end in EXTENDED_PICTOGRAPHIC)


def test_rgi_zwj_sequences_keep_every_zwj() -> None:
    generated = runpy.run_path(str(ROOT / "scripts" / "generate_unicode_tables.py"))
    path = generated["UCD_DIR"] / "emoji-zwj-sequences.txt"
    sequences = generated["parse_zwj_sequences"](path)
    assert len(sequences) == 1468
    lost = [
        seq
        for seq in sequences
        if seq.count("\u200d") != category_name_key(seq).count("\u200d")
    ]
    assert lost == []


def _parse_grapheme_break_test(path: Path) -> list[tuple[str, list[str]]]:
    cases: list[tuple[str, list[str]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        clusters: list[str] = []
        current: list[str] = []
        for token in line.split():
            if token == "÷":
                if current:
                    clusters.append("".join(current))
                    current = []
            elif token == "×":
                continue
            else:
                current.append(chr(int(token, 16)))
        if current:
            clusters.append("".join(current))
        cases.append(("".join(clusters), clusters))
    return cases


def _grapheme_clusters(value: str) -> list[str]:
    clusters: list[str] = []
    index = 0
    while index < len(value):
        nxt = _next_grapheme(value, index)
        clusters.append(value[index:nxt])
        index = nxt
    return clusters


def test_grapheme_break_test_16_all_1093() -> None:
    path = ROOT / "third_party" / "ucd" / "16.0.0" / "GraphemeBreakTest.txt"
    cases = _parse_grapheme_break_test(path)
    assert len(cases) == 1093
    failed = [
        (raw, expected, _grapheme_clusters(raw))
        for raw, expected in cases
        if _grapheme_clusters(raw) != expected
    ]
    assert failed == []
    cr_lf = "\r\n"
    assert _grapheme_clusters(cr_lf) == [cr_lf]
    assert _next_grapheme("\rA", 0) == 1


def test_generate_unicode_tables_help_does_not_rewrite() -> None:
    target = ROOT / "app" / "schemas" / "unicode_props.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    script = ROOT / "scripts" / "generate_unicode_tables.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
