"""An HTML attachment renders as its content, not its markup.

Raw page source sent as text cost 12x the native document block's input tokens
on dev (scripts, styles, attributes). ``html_to_markdown`` keeps the words and
the structure: headings, lists, and tables in the same pipe shape a Word table
gets.
"""

from apis.shared.files.html_markdown import html_to_markdown


def test_head_scripts_and_styles_contribute_nothing():
    page = (
        "<html><head><title>T</title><style>.a{color:red}</style><script>var x = '<p>no</p>';</script></head>"
        "<body><noscript>enable js</noscript><p>Visible.</p><script>track()</script></body></html>"
    )
    assert html_to_markdown(page) == "Visible."


def test_headings_and_paragraphs():
    assert html_to_markdown("<h1>Fees</h1><p>One.</p><h3>Detail</h3><div>Two.</div>") == "# Fees\n\nOne.\n\n### Detail\n\nTwo."


def test_whitespace_collapses_like_a_browser():
    assert html_to_markdown("<p>  a\n   b\t c  </p>") == "a b c"


def test_nested_lists_keep_markers_and_depth():
    page = "<ol><li>one<ul><li>sub a</li><li>sub b</li></ul></li><li>two</li></ol><ol start='4'><li>four</li></ol>"
    assert html_to_markdown(page) == "1. one\n  - sub a\n  - sub b\n2. two\n4. four"


def test_tables_with_spans_and_blank_cells():
    page = (
        "<table><tr><th rowspan=2>Item</th><th colspan=2>Timeline</th></tr>"
        "<tr><th>Due</th><th>Status</th></tr>"
        "<tr><td>Memo</td><td></td><td>Open</td></tr></table>"
    )
    assert html_to_markdown(page) == (
        "| Item | Timeline | Timeline |\n|---|---|---|\n| Item | Due | Status |\n| Memo |  | Open |"
    )


def test_cell_text_cannot_break_the_table():
    assert html_to_markdown("<table><tr><td>a|b<br>c</td></tr></table>") == "| a\\|b<br>c |\n|---|"


def test_images_are_named_by_alt_text():
    assert html_to_markdown('<p>See <img src="x.png" alt="Figure 1"> and <img src="y.png"></p>') == "See [image: Figure 1] and [image]"


def test_entities_are_decoded():
    assert html_to_markdown("<p>Fees &amp; fines: &lt;$50&gt; &nbsp;caf&eacute;</p>") == "Fees & fines: <$50> \xa0café"


def test_pre_keeps_its_lines():
    assert html_to_markdown("<pre>  a\n  b</pre><p>after</p>") == "  a\n  b\n\nafter"


def test_malformed_markup_still_renders():
    assert html_to_markdown("<p>open <b>bold <li>stray</p></div></table>") == "open bold\n\n- stray"


def test_a_nested_table_reads_inside_its_cell():
    page = "<table><tr><td>outer</td><td><table><tr><td>x</td><td>y</td></tr><tr><td>z</td></tr></table></td></tr></table>"
    assert html_to_markdown(page) == "| outer | x ; y<br>z |\n|---|---|"


def test_deterministic():
    page = "<h2>A</h2><ul><li>x</li></ul><table><tr><td>1</td></tr></table>"
    assert html_to_markdown(page) == html_to_markdown(page)
