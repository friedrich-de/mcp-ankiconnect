"""Unit tests for the field-cleaning helpers used by the Anki MCP tools.

The fixtures use real HTML snippets pulled from a `search_notes is:due` response
against a clinical-knowledge deck, so the assertions verify behavior against the
shapes the LLM actually encounters in the wild.
"""

from __future__ import annotations

import pytest

from mcp_ankiconnect.server import _clean_field_html, _format_note_clean

# --- _clean_field_html ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Empty / passthrough text
        ("", ""),
        ("plain text", "plain text"),
        # <div> and <br> are pure presentation
        ("<div>Uveitis</div>", "Uveitis"),
        ("<div><br></div>", ""),
        ("line1<br>line2", "line1 line2"),
        ("line1<br/>line2", "line1 line2"),
        ("line1<br />line2", "line1 line2"),
        # Meaning-bearing tags stay
        ("<b>red eye</b>", "<b>red eye</b>"),
        ("<i>italic</i>", "<i>italic</i>"),
        ("<u>underline</u>", "<u>underline</u>"),
        # Cloze markers are plain text and pass through (may wrap HTML inside)
        (
            "A {{c1::<b>cherry red spot</b>::fundoscopic finding}} is characteristic",
            "A {{c1::<b>cherry red spot</b>::fundoscopic finding}} is characteristic",
        ),
        # <font color=...> drops the wrapper, keeps inner text
        (
            '<font color="#8b8787">Antipsychotics</font>',
            "Antipsychotics",
        ),
        # <span style=...> drops the wrapper, keeps inner text
        (
            '<span style="background-color: rgb(251, 250, 248); color: rgb(14, 14, 14);">Ketoconazole 2% shampoo</span>',
            "Ketoconazole 2% shampoo",
        ),
        # Entity decoding
        ("foo&nbsp;bar", "foo bar"),
        ("a&amp;b", "a&b"),
        ("&lt;tag&gt;", "<tag>"),
        ("she said &quot;hi&quot;", 'she said "hi"'),
        # <img> with src only -> [image: filename]
        (
            '<img src="paste-13769665151563.jpg">',
            "[image: paste-13769665151563.jpg]",
        ),
        # <img> with alt before src -> still extracts src filename
        (
            '<img alt="Red Eye Roundup" src="064_ro0319_f4-4.jpg">',
            "[image: 064_ro0319_f4-4.jpg]",
        ),
        # <img> with URL -> filename only (path stripped)
        (
            '<img src="https://example.com/assets/img.png">',
            "[image: img.png]",
        ),
        # Self-closing img
        (
            '<img src="diagram.png" />',
            "[image: diagram.png]",
        ),
        # Multiple images in one field
        (
            '<img src="a.jpg"><img src="b.jpg">',
            "[image: a.jpg] [image: b.jpg]",
        ),
        # <pre><code> -> <code> simplification (already done in current code)
        (
            "<pre><code>def f(): pass</code></pre>",
            "<code>def f(): pass</code>",
        ),
        # Whitespace collapse
        ("foo   bar\n\nbaz", "foo bar baz"),
        # Composite real-world example from the user's is:due output
        (
            "Uveitis<div><br></div><div>Acute closed-angle glaucoma</div>"
            "<div><br></div><div>Corneal ulcer</div><div><br></div>"
            '<div><br></div><div><img src="paste-13769665151563.jpg"></div>',
            "Uveitis Acute closed-angle glaucoma Corneal ulcer [image: paste-13769665151563.jpg]",
        ),
        # Non-string input passes through unchanged (defensive)
        (None, None),
        (123, 123),
    ],
)
def test__clean_field_html(raw, expected):
    assert _clean_field_html(raw) == expected


# --- _format_note_clean ---


def test_format_note_clean_basic_note_strips_html_and_elides_empty_fields():
    note = {
        "noteId": 101,
        "modelName": "Basic (with source and explanation)",
        "tags": ["Year5::GEMDeck"],
        "fields": {
            "Front": {
                "value": "What are three causes of a <u>red eye</u>?",
                "order": 0,
            },
            "Back": {
                "value": "Uveitis<div><br></div><div>Glaucoma</div>",
                "order": 1,
            },
            "Explanation": {"value": "", "order": 2},
            "Source": {"value": "", "order": 3},
        },
    }

    out = _format_note_clean(note)

    assert out["noteId"] == 101
    assert out["modelName"] == "Basic (with source and explanation)"
    assert out["tags"] == ["Year5::GEMDeck"]
    assert out["fields"] == {
        "Front": "What are three causes of a <u>red eye</u>?",
        "Back": "Uveitis Glaucoma",
    }
    # empty Explanation / Source dropped
    assert "Explanation" not in out["fields"]
    assert "Source" not in out["fields"]


def test_format_note_clean_image_occlusion_collapses_to_placeholder():
    note = {
        "noteId": 1772043786412,
        "modelName": "Image Occlusion Enhanced",
        "tags": ["Neurology::Neuroanatomy"],
        "fields": {
            "ID (hidden)": {
                "value": "d92b3d58826643d4ae1d881e50e6bdc8-ao-12",
                "order": 0,
            },
            "Header": {"value": "", "order": 1},
            "Image": {"value": '<img src="tmpt01td740.jpg" />', "order": 2},
            "Question Mask": {
                "value": '<img src="d92b3d58826643d4ae1d881e50e6bdc8-ao-12-Q.svg" />',
                "order": 3,
            },
            "Footer": {"value": "", "order": 4},
            "Remarks": {"value": "", "order": 5},
            "Sources": {"value": "", "order": 6},
            "Extra 1": {"value": "", "order": 7},
            "Extra 2": {"value": "", "order": 8},
            "Answer Mask": {
                "value": '<img src="d92b3d58826643d4ae1d881e50e6bdc8-ao-12-A.svg" />',
                "order": 9,
            },
            "Original Mask": {
                "value": '<img src="d92b3d58826643d4ae1d881e50e6bdc8-ao-O.svg" />',
                "order": 10,
            },
        },
    }

    out = _format_note_clean(note)

    assert out["noteId"] == 1772043786412
    assert out["modelName"] == "Image Occlusion Enhanced"
    assert out["fields"] == {"Image": "[image-occlusion]"}


def test_format_note_clean_missing_fields_and_model_defaults():
    note = {"noteId": 103, "tags": ["minimal"]}
    out = _format_note_clean(note)
    assert out == {
        "noteId": 103,
        "modelName": "UnknownModel",
        "fields": {},
        "tags": ["minimal"],
    }
