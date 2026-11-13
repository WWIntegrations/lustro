"""HTML -> Markdown and metadata extraction (stdlib html.parser) - offline."""
from ww_scraper.extract import parse_html

HTML = """<!doctype html><html lang="en"><head>
<title> Hello  World </title>
<meta name="description" content="Desc here">
<meta name="robots" content="noindex, nofollow">
<meta property="og:title" content="OG T"><meta property="og:image" content="/i.png">
<link rel="canonical" href="/canon">
<link rel="alternate" hreflang="de" href="https://ex.com/de">
<script type="application/ld+json">{"@type":["Article","NewsArticle"]}</script>
<script type="application/ld+json">{broken</script>
</head><body>
<nav><a href="/menu">Menu</a></nav>
<main><h1>Title</h1><h2>Sub</h2><h3>Deep</h3>
<p>Para with <em>em</em>, <code>x=1</code> and <a href="rel/page">a link</a>.
<p>Second para
<ul><li>one<li>two<ol><li>inner</li></ol></ul>
<table><thead><tr><th>A<th>B|C</thead><tbody><tr><td>1<td>2</tbody></table>
<pre><code class="language-js">let a = 1;
  indented();</code></pre>
<blockquote>quote line</blockquote>
<img src="/p.jpg" alt="pic"><img src="/q.jpg">
<a href="javascript:evil()">bad</a>
</main>
<aside>side</aside><footer>foot</footer><script>alert(1)</script><style>.x{}</style>
</body></html>"""


def test_metadata():
    p = parse_html(HTML, "https://ex.com/dir/index.html")
    assert p.title == "Hello World"
    assert p.description == "Desc here"
    assert p.lang == "en"
    assert p.canonical == "https://ex.com/canon"
    assert p.og == {"og:title": "OG T", "og:image": "/i.png"}
    assert p.hreflang == [{"hreflang": "de", "href": "https://ex.com/de"}]
    assert p.jsonld_types == ["Article", "NewsArticle"] and p.jsonld_errors == 1
    assert p.headings == {"h1": ["Title"], "h2": ["Sub"], "h3": ["Deep"]}
    assert p.noindex and p.nofollow
    urls = [l["url"] for l in p.links]
    assert "https://ex.com/dir/rel/page" in urls and "https://ex.com/menu" in urls
    assert not any(u.startswith("javascript") for u in urls)
    assert p.images == [{"src": "https://ex.com/p.jpg", "alt": "pic"},
                        {"src": "https://ex.com/q.jpg", "alt": None}]


def test_markdown_main_content():
    md = parse_html(HTML, "https://ex.com/dir/index.html").markdown
    assert md.startswith("# Title")
    assert "## Sub" in md and "### Deep" in md
    assert "Para with *em*, `x=1` and [a link](https://ex.com/dir/rel/page)." in md
    assert "- one\n- two\n  1. inner" in md
    assert "| A | B\\|C |\n| --- | --- |\n| 1 | 2 |" in md
    assert "```js\nlet a = 1;\n  indented();\n```" in md
    assert "> quote line" in md
    assert "![pic](https://ex.com/p.jpg)" in md
    for gone in ("Menu", "side", "foot", "alert", ".x{}", "javascript"):
        assert gone not in md


def test_fallbacks_article_and_body_and_base():
    p = parse_html("<html><body><header><h1>Site</h1></header><article><h1>A</h1><p>t</p>"
                   "</article></body></html>", "https://ex.com/")
    assert p.markdown == "# A\n\nt"
    p = parse_html("<base href='https://cdn.ex.com/x/'><p>plain <a href='y'>y</a></p>",
                   "https://ex.com/")
    assert p.markdown == "plain [y](https://cdn.ex.com/x/y)"
    assert p.word_count == 2


def test_malformed_html_does_not_crash():
    p = parse_html("<div><p>open <b>bold <i>both</div></span><td>cell", "https://ex.com/")
    assert "open" in p.markdown and "bold" in p.markdown


def test_listing_of_articles_keeps_every_item_not_only_the_first():
    items = "".join(f'<li><article class="product_pod"><h3><a href="/b{i}">Book {i}</a></h3>'
                    f'<p class="price">£{i}.00</p></article></li>' for i in range(1, 6))
    html = f"<html><body><nav>Menu</nav><div class='page'><h1>All products</h1><ol>{items}</ol></div></body></html>"
    md = parse_html(html, "https://shop.example/").markdown
    for i in range(1, 6):
        assert f"Book {i}" in md and f"£{i}.00" in md


def test_one_dominant_article_wins_over_teasers():
    body = "<p>" + "Long body sentence. " * 60 + "</p>"
    html = (f"<html><body><article><h1>Post</h1>{body}</article>"
            "<aside><article><h3>Related one</h3></article><article><h3>Related two</h3></article></aside></body></html>")
    md = parse_html(html, "https://blog.example/post").markdown
    assert md.startswith("# Post") and "Related one" not in md
