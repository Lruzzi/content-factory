"""Captions, hook and credit as an ASS subtitle file (FR-6). Pure: no FFmpeg, but a real font is measured."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import config_schema as cs
from services.render import captions as cap

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
needs_font = pytest.mark.skipif(not FONT.exists(), reason="DejaVu Sans Bold is not installed")

SIZE = (1080, 1920)
SAFE = {"top": 0.12, "bottom": 0.20, "right": 0.14, "left": 0.04}
CAPTIONS: dict[str, Any] = {
    "enabled": True,
    "style": "word_pop",
    "font": str(FONT),
    "size": 72,
    "color": "#FFFFFF",
    "highlight": "#FFE600",
    "outline": 6,
    "y": 0.62,
    "max_words_per_line": 3,
    "uppercase": True,
}
BRANDING = {"hook_overlay": {"enabled": True, "seconds": 2.5, "y": 0.14}, "credit": {"position": "bottom", "size": 34}}


def words(*pairs):
    return [cap.Word(text=t, start_s=s, end_s=e) for t, s, e in pairs]


SPEECH = words(
    ("savage", 0.0, 0.4), ("lord", 0.5, 0.9), ("comeback", 1.0, 1.6), ("gila", 1.8, 2.2), ("banget", 2.3, 2.9)
)


def found(pattern, text):
    """The regex match, which must exist."""
    match = re.search(pattern, text)
    assert match is not None, (pattern, text)
    return match


def ass(spoken=SPEECH, **kw):
    options: dict[str, Any] = {
        "captions": CAPTIONS,
        "branding": BRANDING,
        "safe_zone": SAFE,
        "size": SIZE,
        "font_path": FONT,
        "hook": None,
        "credit": None,
        "duration_s": 3.0,
    }
    return cap.build_ass(spoken, **(options | kw))


def dialogues(text):
    return [line for line in text.splitlines() if line.startswith("Dialogue:")]


def text_of(line):
    return line.split(",", 9)[9]


def plain(line):
    return re.sub(r"\{[^}]*\}", "", text_of(line))


def event_times(line):
    start, end = line.split(",")[1:3]
    return cap.parse_ass_time(start), cap.parse_ass_time(end)


# ---------- text safety ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("hello", "hello"),
        ("  two   spaces ", "two spaces"),
        ("{\\an8\\pos(0,0)}hack", "an8pos(0,0)hack"),
        ("back\\slash", "backslash"),
        ("line\nbreak\tand\rtab", "line break and tab"),
        ("\x00\x1bcontrol", "control"),
    ],
)
def test_transcript_text_cannot_inject_ass_override_codes(dirty, clean):
    assert cap.clean(dirty) == clean


def test_a_hostile_word_never_reaches_the_file_as_an_override_block():
    text = ass(words(("{\\fs500}BIG", 0.0, 1.0), ("x\\Ny", 1.0, 2.0)), duration_s=2.0)
    assert "\\fs500" not in text
    assert "{\\fs500}" not in text
    assert "\\N" not in " ".join(dialogues(text))


# ---------- ASS basics -----------------------------------------------------------------------------
def test_the_file_declares_the_output_size_so_positions_are_real_pixels():
    text = ass()
    assert "PlayResX: 1080" in text
    assert "PlayResY: 1920" in text
    assert "[Events]" in text


def test_colours_are_converted_to_ass_bgr():
    assert cap.ass_colour("#FFE600") == "&H0000E6FF"
    assert cap.ass_colour("#102030") == "&H00302010"


def test_times_are_written_in_ass_format():
    assert cap.ass_time(3725.456) == "1:02:05.46"
    assert cap.parse_ass_time("0:00:01.50") == pytest.approx(1.5)


# ---------- word pop ---------------------------------------------------------------------------------
@needs_font
def test_every_word_gets_an_event_and_the_spoken_word_is_highlighted():
    lines = dialogues(ass())
    assert len(lines) == len(SPEECH)
    highlight = cap.ass_colour("#FFE600")
    for line, word in zip(lines, SPEECH, strict=True):
        before, _, after = line.partition(highlight)
        assert word.text.upper() in after.split("}")[1], line


@needs_font
def test_words_are_shown_uppercase_when_asked_and_as_spoken_when_not():
    assert "SAVAGE" in ass()
    assert "SAVAGE" not in ass(captions=CAPTIONS | {"uppercase": False})
    assert "savage" in ass(captions=CAPTIONS | {"uppercase": False})


@needs_font
def test_at_most_the_configured_number_of_words_share_a_line():
    short = words(*[(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate("go on do it now ok hi".split())])
    texts = [plain(line) for line in dialogues(ass(short, duration_s=4.0))]
    assert max(len(t.split()) for t in texts) == 3


@needs_font
def test_a_line_is_broken_earlier_when_three_words_would_not_fit_the_screen():
    texts = [plain(line) for line in dialogues(ass())]  # SAVAGE LORD COMEBACK is wider than the safe width
    assert max(len(t.split()) for t in texts) <= 2


@needs_font
def test_a_line_stays_on_screen_from_its_first_word_to_its_last_and_events_do_not_overlap():
    spans = [event_times(line) for line in dialogues(ass())]
    assert spans[0][0] == pytest.approx(0.0, abs=0.01)
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert end <= start + 0.011


@needs_font
def test_event_times_are_relative_to_the_clip_not_the_stream():
    shifted = [cap.Word(text=w.text, start_s=w.start_s + 100, end_s=w.end_s + 100) for w in SPEECH]
    a, b = dialogues(ass()), dialogues(ass(shifted, offset_s=100.0))
    assert [event_times(x) for x in a] == [event_times(y) for y in b]


@needs_font
def test_words_outside_the_clip_are_dropped_and_words_crossing_its_end_are_cut():
    spoken = words(("before", -3.0, -2.0), ("inside", 0.5, 1.0), ("crossing", 2.5, 4.0), ("after", 5.0, 6.0))
    lines = dialogues(ass(spoken, duration_s=3.0))
    joined = " ".join(lines).upper()
    assert "BEFORE" not in joined
    assert "AFTER" not in joined
    assert "INSIDE" in joined
    assert max(event_times(line)[1] for line in lines) <= 3.0 + 0.011


@needs_font
def test_no_words_means_no_caption_events():
    assert dialogues(ass([])) == []


@needs_font
def test_captions_can_be_switched_off():
    assert dialogues(ass(captions=CAPTIONS | {"enabled": False})) == []


# ---------- fitting inside the safe zone ------------------------------------------------------------------
@needs_font
def test_every_line_fits_the_width_between_the_safe_margins():
    measure = cap.TextMeasure(FONT, 72)
    limit = SIZE[0] * (1 - SAFE["left"] - SAFE["right"]) - 2 * CAPTIONS["outline"]
    long_words = words(("internationalization", 0.0, 1.0), ("responsibilities", 1.0, 2.0), ("ok", 2.0, 2.5))
    for line in dialogues(ass(long_words, duration_s=3.0)):
        fs = re.search(r"\\fs(\d+)", line)
        size = int(fs.group(1)) if fs else 72
        text = plain(line).upper()
        assert measure.at(size).width(text) <= limit + 1, (size, text)


@needs_font
def test_a_word_too_wide_for_the_screen_is_shrunk_not_cut():
    line = dialogues(ass(words(("pneumonoultramicroscopicsilicovolcanoconiosis", 0.0, 1.0)), duration_s=1.0))[0]
    assert int(found(r"\\fs(\d+)", line).group(1)) < 72


@needs_font
def test_a_whole_line_is_centred_between_the_safe_margins_at_the_configured_height():
    line = dialogues(ass(captions=CAPTIONS | {"reveal": "line"}))[0]
    x, y = map(int, found(r"\\pos\((\d+),(\d+)\)", line).groups())
    assert x == round(SIZE[0] * (SAFE["left"] + (1 - SAFE["left"] - SAFE["right"]) / 2)) and r"\an5" in line
    assert y == round(SIZE[1] * 0.62)


SHORT = words(("gg", 0.0, 0.4), ("lord", 0.5, 0.9), ("win", 1.0, 1.6), ("nice", 1.8, 2.2), ("one", 2.3, 2.9))


@needs_font
def test_the_default_shows_a_word_only_once_it_is_spoken_and_the_line_fills_up_word_by_word():
    shown = [plain(line) for line in dialogues(ass(SHORT))]
    assert shown[:3] == ["GG", "GG LORD", "GG LORD WIN"]  # nothing is shown before its word
    assert shown[3:] == ["NICE", "NICE ONE"]


@needs_font
def test_the_whole_line_can_still_be_shown_at_once():
    shown = [plain(line) for line in dialogues(ass(SHORT, captions=CAPTIONS | {"reveal": "line"}))]
    assert shown[:3] == ["GG LORD WIN"] * 3 and shown[3:] == ["NICE ONE"] * 2


@needs_font
def test_a_line_that_fills_up_keeps_its_left_edge_where_the_finished_line_will_start():
    lines = dialogues(ass(SHORT))[:3]
    anchors = {found(r"\\an4\\pos\((\d+),(\d+)\)", line).groups() for line in lines}
    assert len(anchors) == 1  # the words already there do not move when the next one comes
    left = int(next(iter(anchors))[0])
    full = cap.TextMeasure(FONT, 72).width("GG LORD WIN")
    middle = SIZE[0] * (SAFE["left"] + (1 - SAFE["left"] - SAFE["right"]) / 2)
    assert left == pytest.approx(middle - full / 2, abs=1)  # the finished line is centred


@needs_font
def test_captions_can_be_shown_a_little_later_than_the_word_times():
    on_time = dialogues(ass(captions=CAPTIONS | {"offset_s": 0.0}))[0]
    later = dialogues(ass(captions=CAPTIONS | {"offset_s": 0.25}))[0]
    start = lambda line: cap.parse_ass_time(line.split(",")[1])  # noqa: E731
    assert start(later) - start(on_time) == pytest.approx(0.25, abs=0.02)


@needs_font
def test_the_delay_does_not_push_a_caption_past_the_end_of_the_clip():
    out = dialogues(ass(words(("late", 2.8, 2.95)), captions=CAPTIONS | {"offset_s": 0.5}, duration_s=3.0))
    assert all(cap.parse_ass_time(line.split(",")[2]) <= 3.0 + 0.02 for line in out)


@needs_font
def test_a_caption_height_outside_the_safe_zone_is_rejected():
    with pytest.raises(ValueError, match="safe zone"):
        ass(captions=CAPTIONS | {"y": 0.95})
    with pytest.raises(ValueError, match="safe zone"):
        ass(captions=CAPTIONS | {"y": 0.02})


# ---------- hook and credit --------------------------------------------------------------------------------
@needs_font
def test_the_hook_shows_for_its_configured_seconds_at_the_top_of_the_safe_zone():
    hook = [line for line in dialogues(ass(hook="Wait for the last second")) if ",Hook," in line]
    assert len(hook) == 1
    assert event_times(hook[0]) == pytest.approx((0.0, 2.5), abs=0.01)
    y = int(found(r"\\pos\(\d+,(\d+)\)", hook[0]).group(1))
    assert y >= round(SIZE[1] * SAFE["top"])
    assert "\\an8" in hook[0]


@needs_font
def test_the_hook_is_cleaned_and_limited_in_length():
    nasty = "{\\fs900}" + "x" * 500
    hook = [line for line in dialogues(ass(hook=nasty)) if ",Hook," in line][0]
    assert "\\fs900" not in hook
    assert len(plain(hook).replace("\\N", " ")) <= cap.MAX_HOOK_CHARS + 2


@needs_font
def test_no_hook_when_disabled_or_empty():
    off = BRANDING | {"hook_overlay": {"enabled": False, "seconds": 2.5, "y": 0.14}}
    assert not [line for line in dialogues(ass(hook="Hi", branding=off)) if ",Hook," in line]
    assert not [line for line in dialogues(ass(hook="  ")) if ",Hook," in line]


@needs_font
def test_the_credit_stays_for_the_whole_clip_inside_the_bottom_safe_edge():
    credit = [line for line in dialogues(ass(credit="Full stream: Leo Kabuki")) if ",Credit," in line]
    assert len(credit) == 1
    assert event_times(credit[0]) == pytest.approx((0.0, 3.0), abs=0.01)
    y = int(found(r"\\pos\(\d+,(\d+)\)", credit[0]).group(1))
    assert y <= round(SIZE[1] * (1 - SAFE["bottom"]))
    assert "\\an2" in credit[0]


def test_the_credit_text_fills_in_only_the_source_name():
    assert cap.credit_text("Full stream: {source_name}", "Leo Kabuki") == "Full stream: Leo Kabuki"
    assert cap.credit_text("{source_name.__class__} {other}", "x") == "{source_name.__class__} {other}"
    assert cap.credit_text(None, "x") is None


# ---------- measuring text --------------------------------------------------------------------------------
@needs_font
def test_text_width_grows_with_the_text_and_the_size():
    m = cap.TextMeasure(FONT, 72)
    assert 0 < m.width("A") < m.width("AB") < m.width("ABC")
    assert m.at(36).width("ABC") == pytest.approx(m.width("ABC") / 2, rel=0.02)


@needs_font
def test_the_font_family_name_comes_from_the_font_file():
    family, bold = cap.font_family(FONT)
    assert family == "DejaVu Sans"
    assert bold is True


def test_a_missing_font_is_a_clear_error_not_a_default():
    with pytest.raises(cap.FontError, match="Montserrat"):
        cap.font_family(Path("/nowhere/Montserrat-ExtraBold.ttf"))


def test_the_default_profile_font_path_is_a_font_the_repo_expects_to_be_supplied():
    default = cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml")[0].captions["font"]
    assert default.endswith(".ttf")
