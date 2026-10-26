import runpy
from pathlib import Path

from app.schemas.categories import category_name_key
from app.schemas.unicode_props import (
    ASSIGNED,
    EMOJI_MODIFIER_BASE,
    EMOJI_PRESENTATION,
    EXTENDED_PICTOGRAPHIC,
    JOINING_D,
    JOINING_L,
    JOINING_R,
    JOINING_T,
    REGIONAL_INDICATOR,
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
    assert tables["REGIONAL_INDICATOR"] == REGIONAL_INDICATOR
    assert not any(start <= 0x00A9 <= end for start, end in EMOJI_MODIFIER_BASE)
    assert any(start <= 0x1F469 <= end for start, end in EMOJI_MODIFIER_BASE)
    assert any(start <= 0x261D <= end for start, end in EMOJI_MODIFIER_BASE)
    assert tables["ASSIGNED"] == ASSIGNED
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
