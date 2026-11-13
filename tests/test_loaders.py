"""Document loader and the chunks export (offline)."""
import json

import pytest
import requests

from ww_scraper.__main__ import main
from ww_scraper.api import ScraperAPI
from ww_scraper.loaders import DatasetLoader, Document, chunk_items, chunk_markdown, items_to_documents

PAGE = """Intro line before any heading.

# Guide

Short opening.

## Install

Run the installer.

```sh
# not a heading, a shell comment
pip install thing
```

## Usage

""" + ("Use it well. " * 300) + """

# Reference

### Deep

Details here.
"""


def test_chunks_follow_headings_and_size():
    chunks = chunk_markdown(PAGE, chunk_size=800, overlap=100)
    assert chunks[0]["headingPath"] == [] and chunks[0]["text"].startswith("Intro line")
    paths = [c["headingPath"] for c in chunks]
    assert ["Guide"] in paths
    # the short Install subsection joined its parent; the code comment did not start a section
    guide = next(c for c in chunks if c["headingPath"] == ["Guide"])
    assert "## Install" in guide["text"] and "# not a heading" in guide["text"]
    usage = [c for c in chunks if c["headingPath"] == ["Guide", "Usage"]]
    assert len(usage) >= 4 and all(len(c["text"]) <= 800 for c in usage)
    assert usage[1]["text"].startswith("Use it") or usage[1]["text"].startswith("well")
    deep = chunks[-1]
    # a heading with no body of its own takes its short subsection along
    assert deep["headingPath"] == ["Reference"] and "### Deep" in deep["text"] and "Details here." in deep["text"]
    alone = chunk_markdown("# A\n\n" + "a " * 300 + "\n\n## B\n\n" + "b " * 150, chunk_size=400, overlap=0)
    assert alone[-1]["headingPath"] == ["A", "B"]
    assert [c["chunkIndex"] for c in chunks] == list(range(len(chunks)))


def test_overlap_repeats_text_between_neighbours():
    text = " ".join(f"w{i}" for i in range(2000))
    a, b = chunk_markdown(text, chunk_size=1000, overlap=200)[:2]
    tail = a["text"][-150:].split(" ", 1)[1]
    assert tail in b["text"]


def test_bad_sizes():
    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=10)
    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=1000, overlap=600)


def test_chunk_items_metadata_and_stable_ids():
    items = [{"url": "https://example.com/a", "title": "A", "lang": "en", "markdown": PAGE},
             {"url": "https://example.com/empty", "markdown": ""}]
    one = list(chunk_items(items, 800, 100))
    two = list(chunk_items(items, 800, 100))
    assert [c["id"] for c in one] == [c["id"] for c in two]
    assert len({c["id"] for c in one}) == len(one)
    assert all(c["url"] == "https://example.com/a" and c["title"] == "A" and c["lang"] == "en" for c in one)
    assert one[0]["chunkCount"] == len(one) and one[0]["charCount"] == len(one[0]["text"])


def test_documents_have_the_langchain_shape():
    docs = items_to_documents([{"url": "u", "title": "T", "markdown": "# H\n\nbody"},
                               {"url": "v", "text": "plain text"}])
    assert [d.page_content for d in docs] == ["# H\n\nbody", "plain text"]
    assert docs[0].metadata == {"url": "u", "title": "T"}
    chunked = items_to_documents([{"url": "u", "markdown": "# H\n\nbody"}], chunk_size=500)
    assert chunked[0].metadata["headingPath"] == ["H"] and "text" not in chunked[0].metadata


def test_dataset_loader_by_id_and_by_name(storage):
    ds = storage.dataset("run-ds")
    ds.push([{"url": "https://example.com/", "title": "Home", "markdown": PAGE}])
    storage.named_dataset("docs").push([{"url": "https://example.com/n", "markdown": "# N\n\nnamed"}])
    assert len(DatasetLoader("run-ds", storage).load()) == 1
    chunks = DatasetLoader("run-ds", storage, chunk_size=800, overlap=100).load()
    assert len(chunks) > 3 and all(isinstance(d.page_content, str) for d in chunks)
    named = DatasetLoader("docs", str(storage.root)).load()
    assert named[0].page_content == "# N\n\nnamed"
    assert isinstance(named[0], Document) or hasattr(named[0], "page_content")


def test_chunks_export_cli_and_api(storage, capsys):
    ds = storage.dataset("pages")
    ds.push([{"url": "https://example.com/", "title": "Home", "markdown": PAGE}])
    assert main(["export", "pages", "--format", "chunks", "--chunk-size", "800", "--chunk-overlap", "100",
                 "--storage", str(storage.root)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert rows and rows[0]["url"] == "https://example.com/" and "headingPath" in rows[0]
    assert main(["export", "pages", "--format", "chunks", "--chunk-size", "5",
                 "--storage", str(storage.root)]) == 2
    api = ScraperAPI(storage, port=0, token="t")
    api.start()
    try:
        r = requests.get(f"{api.url}/v2/datasets/pages/items", params={"format": "chunks", "chunkSize": 800,
                                                                       "chunkOverlap": 100},
                         headers={"Authorization": "Bearer t"})
        assert r.status_code == 200 and r.headers["Content-Type"].startswith("application/jsonl")
        assert [json.loads(x) for x in r.text.splitlines()] == rows
        r = requests.get(f"{api.url}/v2/datasets/pages/items", params={"format": "chunks", "chunkSize": "x"},
                         headers={"Authorization": "Bearer t"})
        assert r.status_code == 400
    finally:
        api.shutdown()
