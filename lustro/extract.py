"""HTML extraction with the standard library only (html.parser).

parse_html(html, base_url) -> Page with:
  markdown   main content as Markdown (nav/footer/aside/script/style/forms dropped;
             headings, paragraphs, lists, links, code, tables, quotes, images kept)
  title, description, canonical, lang, robots, og (dict), headings (h1-h3),
  jsonld_types, hreflang, links, images, word_count, noindex, nofollow
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
DROP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas",
        "nav", "footer", "aside", "form", "button", "select", "dialog", "head", "title"}
BLOCK_CLOSES_P = {"p", "div", "ul", "ol", "dl", "table", "pre", "blockquote", "section",
                  "article", "header", "footer", "nav", "aside", "main", "form", "hr",
                  "h1", "h2", "h3", "h4", "h5", "h6", "figure"}
BLOCKS = {"p", "div", "section", "article", "main", "body", "header", "figure",
          "figcaption", "address", "details", "summary", "dl", "dt", "dd", "center"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def text(self) -> str:
        parts = []
        for c in self.children:
            parts.append(c if isinstance(c, str) else c.text())
        return "".join(parts)

    def find(self, pred):
        for n in self.iter():
            if pred(n):
                return n
        return None


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    @property
    def cur(self):
        return self.stack[-1]

    def _close(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return True
        return False

    def _close_within(self, tags, boundary):
        for i in range(len(self.stack) - 1, 0, -1):
            t = self.stack[i].tag
            if t in boundary:
                return
            if t in tags:
                del self.stack[i:]
                return

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_CLOSES_P and self.cur.tag == "p":
            self.stack.pop()
        if tag == "li":
            self._close_within({"li"}, {"ul", "ol"})
        elif tag in ("dt", "dd"):
            self._close_within({"dt", "dd"}, {"dl"})
        elif tag == "tr":
            self._close_within({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot"})
        elif tag in ("td", "th"):
            self._close_within({"td", "th"}, {"tr", "table"})
        elif tag in ("thead", "tbody", "tfoot"):
            self._close_within({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"})
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        self._close(tag)

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def build_tree(html: str) -> Node:
    b = _TreeBuilder()
    try:
        b.feed(html)
        b.close()
    except Exception:
        pass  # html.parser is lenient; keep whatever was built
    return b.root


# --- Markdown --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _abs(base, href):
    if not href:
        return ""
    href = href.strip()
    low = href.lower()
    if low.startswith(("javascript:", "data:", "vbscript:")):
        return ""
    try:
        return urljoin(base, href)
    except ValueError:
        return ""


class _MD:
    def __init__(self, base):
        self.base = base

    def inline(self, node, pre=False) -> str:
        out = []
        for c in node.children:
            if isinstance(c, str):
                out.append(c if pre else _WS.sub(" ", c))
            else:
                out.append(self.node(c, 0, inline=True))
        return "".join(out)

    def node(self, n, depth, inline=False) -> str:
        t = n.tag
        if t in DROP:
            return ""
        if t == "header" and not _inside(n, ("article", "main")):
            return ""
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            txt = self.inline(n).strip()
            return f"\n\n{'#' * int(t[1])} {txt}\n\n" if txt else ""
        if t == "br":
            return "\n"
        if t == "hr":
            return "\n\n---\n\n"
        if t == "a":
            txt = self.inline(n).strip()
            href = _abs(self.base, n.attrs.get("href"))
            if href.lower().startswith(("mailto:", "tel:")):
                return txt
            return f"[{txt}]({href})" if (txt and href) else txt
        if t in ("strong", "b"):
            txt = self.inline(n).strip()
            return f"**{txt}**" if txt else ""
        if t in ("em", "i"):
            txt = self.inline(n).strip()
            return f"*{txt}*" if txt else ""
        if t == "code":
            txt = n.text()
            return f"`{txt.strip()}`" if txt.strip() else ""
        if t == "img":
            src = _abs(self.base, n.attrs.get("src"))
            alt = _WS.sub(" ", n.attrs.get("alt", "")).strip()
            return f"![{alt}]({src})" if src else ""
        if t == "pre":
            lang = ""
            code = n.find(lambda x: x.tag == "code")
            if code is not None:
                m = re.search(r"language-([\w+-]+)", code.attrs.get("class", ""))
                lang = m.group(1) if m else ""
            body = n.text().strip("\n")
            return f"\n\n```{lang}\n{body}\n```\n\n"
        if t in ("ul", "ol"):
            return "\n\n" + self.list(n, depth) + "\n\n"
        if t == "blockquote":
            inner = self.blocks(n, depth).strip()
            return "\n\n" + "\n".join("> " + ln if ln else ">" for ln in inner.split("\n")) + "\n\n"
        if t == "table":
            return "\n\n" + self.table(n) + "\n\n"
        if t in BLOCKS or t == "li":
            return "\n\n" + self.blocks(n, depth) + "\n\n"
        return self.blocks(n, depth) if not inline else self.inline(n)

    def blocks(self, n, depth) -> str:
        out = []
        for c in n.children:
            out.append(_WS.sub(" ", c) if isinstance(c, str) else self.node(c, depth))
        return "".join(out)

    def list(self, n, depth) -> str:
        lines = []
        i = int(n.attrs.get("start", "1") or 1) if n.attrs.get("start", "").isdigit() else 1
        for li in n.children:
            if not isinstance(li, Node) or li.tag != "li":
                continue
            marker = f"{i}." if n.tag == "ol" else "-"
            i += 1
            body = _tidy(self.blocks(li, depth + 1))
            body_lines = [ln for ln in body.split("\n") if ln.strip()]
            if not body_lines:
                continue
            pad = "  " * depth
            first, rest = body_lines[0], body_lines[1:]
            lines.append(f"{pad}{marker} {first.strip()}")
            for ln in rest:
                nested = re.match(r"^\s*(?:[-*]|\d+\.)\s", ln)
                lines.append(ln if nested else f"{pad}  {ln.strip()}")
        return "\n".join(lines)

    def table(self, n) -> str:
        rows = []
        for tr in n.iter():
            if tr.tag != "tr":
                continue
            cells = [_WS.sub(" ", self.inline(c)).strip().replace("|", "\\|")
                     for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(out)


def _inside(n, tags) -> bool:
    p = n.parent
    while p is not None:
        if p.tag in tags:
            return True
        p = p.parent
    return False


def _tidy(md: str) -> str:
    out, in_code = [], False
    for ln in md.split("\n"):
        if ln.strip().startswith("```"):
            in_code = not in_code
            out.append(ln.strip())
            continue
        if in_code:
            out.append(ln)
            continue
        ln = ln.rstrip()
        if not re.match(r"^\s*(?:[-*]|\d+\.)\s", ln):
            ln = re.sub(r" {2,}", " ", ln.strip())
        out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main_content(root: Node) -> Node:
    for pred in (lambda n: n.tag == "main",
                 lambda n: n.attrs.get("role") == "main",
                 lambda n: n.tag == "article",
                 lambda n: n.tag == "body"):
        hit = root.find(pred)
        if hit is not None:
            return hit
    return root


def to_markdown(root: Node, base_url: str) -> str:
    md = _MD(base_url)
    node = main_content(root)
    return _tidy(md.node(node, 0) if node.tag != "#root" else md.blocks(node, 0))


# --- metadata --------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    title: str = ""
    description: str = ""
    canonical: str = ""
    lang: str = ""
    robots: str = ""
    og: dict = field(default_factory=dict)
    headings: dict = field(default_factory=dict)
    jsonld_types: list = field(default_factory=list)
    jsonld_errors: int = 0
    hreflang: list = field(default_factory=list)
    links: list = field(default_factory=list)
    images: list = field(default_factory=list)
    markdown: str = ""
    word_count: int = 0
    site_name: str = ""

    @property
    def noindex(self) -> bool:
        return "noindex" in self.robots or "none" in self.robots

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.robots or "none" in self.robots

    def metadata(self) -> dict:
        return {"title": self.title, "description": self.description,
                "canonical": self.canonical, "lang": self.lang, "robots": self.robots,
                "og": self.og, "headings": self.headings, "jsonldTypes": self.jsonld_types,
                "hreflang": self.hreflang}


def _jsonld_types(obj, out):
    if isinstance(obj, list):
        for o in obj:
            _jsonld_types(o, out)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        for v in (t if isinstance(t, list) else [t]):
            if isinstance(v, str) and v not in out:
                out.append(v)
        if "@graph" in obj:
            _jsonld_types(obj["@graph"], out)


def parse_html(html: str, base_url: str) -> Page:
    root = build_tree(html or "")
    page = Page(url=base_url)
    base_tag = root.find(lambda n: n.tag == "base" and n.attrs.get("href"))
    base = _abs(base_url, base_tag.attrs["href"]) if base_tag is not None else base_url
    if urlparse(base).scheme not in ("http", "https"):
        base = base_url
    html_node = root.find(lambda n: n.tag == "html")
    if html_node is not None:
        page.lang = html_node.attrs.get("lang", "").strip()
    page.headings = {"h1": [], "h2": [], "h3": []}
    for n in root.iter():
        t, a = n.tag, n.attrs
        if t == "title" and not page.title:
            page.title = _WS.sub(" ", n.text()).strip()
        elif t == "meta":
            name = (a.get("name") or "").lower()
            prop = (a.get("property") or "").lower()
            content = (a.get("content") or "").strip()
            if name == "description" and not page.description:
                page.description = _WS.sub(" ", content)
            elif name in ("robots", "lustro"):
                page.robots = ",".join(filter(None, [page.robots, content.lower()]))
            elif prop.startswith("og:") and prop not in page.og:
                page.og[prop] = content
        elif t == "link":
            rel = (a.get("rel") or "").lower().split()
            href = _abs(base, a.get("href"))
            if "canonical" in rel and href and not page.canonical:
                page.canonical = href
            elif "alternate" in rel and a.get("hreflang") and href:
                page.hreflang.append({"hreflang": a["hreflang"], "href": href})
        elif t in ("h1", "h2", "h3"):
            txt = _WS.sub(" ", n.text()).strip()
            if txt:
                page.headings[t].append(txt[:300])
        elif t == "script" and (a.get("type") or "").lower() == "application/ld+json":
            try:
                _jsonld_types(json.loads(n.text()), page.jsonld_types)
            except ValueError:
                page.jsonld_errors += 1
        elif t == "a" and a.get("href"):
            href = _abs(base, a["href"])
            if urlparse(href).scheme in ("http", "https"):
                page.links.append({"url": href, "text": _WS.sub(" ", n.text()).strip()[:200],
                                   "rel": (a.get("rel") or "").lower()})
        elif t == "img":
            src = _abs(base, a.get("src") or a.get("data-src"))
            if src:
                page.images.append({"src": src, "alt": a.get("alt") if "alt" in a else None})
    page.site_name = page.og.get("og:site_name", "")
    page.markdown = to_markdown(root, base)
    page.word_count = len(re.findall(r"\w+", re.sub(r"\]\([^)]*\)", "]", page.markdown)))
    return page
