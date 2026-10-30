from __future__ import annotations

import math

import pytest

from reviewer.context.embedding import (
    EmbeddingRetriever,
    chunk_file,
    cosine,
    hashing_embed,
)
from reviewer.context.retriever import LexicalRetriever, build_context, make_retriever
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource

TOPICS = ["refund", "invoice", "weather", "forecast"]


class FakeEmbedder:
    """Deterministic: one dimension per topic word, counting occurrences. Records every batch."""

    def __init__(self):
        self.batches: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(texts)
        return [[float(t.lower().count(w)) for w in TOPICS] for t in texts]


DIFF = """\
diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -1,2 +1,2 @@
 def issue_refund(invoice):
-    return invoice.total
+    return invoice.total - invoice.refund_fee
"""

FILES = {
    "billing.py": "def issue_refund(invoice):\n    return invoice.total - invoice.refund_fee\n",
    # no shared symbol with the diff, but about the same thing
    "ledger.py": "def post_entry(invoice, amount):\n    # refund lines are negative\n    record(invoice, -amount)\n",
    "weather.py": "def forecast(city):\n    return weather_api(city)\n",
}


async def test_finds_related_code_without_a_shared_symbol(settings):
    emb = FakeEmbedder()
    [fc] = await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings, EmbeddingRetriever(emb))
    assert [r.path for r in fc.related] == ["ledger.py"]  # own file excluded, weather.py scores 0
    assert fc.related[0].start_line == 1
    assert "    1 | def post_entry" in fc.related[0].text
    assert "semantically similar" in fc.related[0].reason
    # the lexical retriever can't see it: `issue_refund` appears nowhere else
    [lex] = await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings, LexicalRetriever())
    assert lex.related == []


async def test_one_snippet_per_file_and_capped(settings):
    files = {
        "billing.py": FILES["billing.py"],
        **{f"m{i}.py": "refund invoice\n" * 3 + "\n".join(f"x{j} = {j}" for j in range(10)) for i in range(5)},
    }
    r = EmbeddingRetriever(FakeEmbedder(), chunk_lines=2)
    [fc] = await build_context(InMemorySource(files), parse_unified_diff(DIFF), settings, r)
    paths = [s.path for s in fc.related]
    assert len(paths) == settings.max_related_per_file
    assert len(set(paths)) == len(paths)
    assert all(s.start_line == 1 for s in fc.related)  # best chunk of each file is its refund-heavy head


async def test_index_built_once_per_review(settings):
    two = DIFF + DIFF.replace("billing.py", "billing2.py")
    files = {**FILES, "billing2.py": FILES["billing.py"]}
    emb = FakeEmbedder()
    ctxs = await build_context(InMemorySource(files), parse_unified_diff(two), settings, EmbeddingRetriever(emb))
    assert len(ctxs) == 2 and all(fc.related for fc in ctxs)
    # 1 batch embedding all 4 files' chunks, then one single-text query per changed file
    assert [len(b) for b in emb.batches] == [4, 1, 1]


async def test_async_embedder_and_batching(settings):
    calls: list[int] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [[float(t.count("refund")), 1.0] for t in texts]

    files = {"billing.py": FILES["billing.py"], **{f"f{i}.py": f"refund_{i} = 1\n" for i in range(100)}}
    [fc] = await build_context(InMemorySource(files), parse_unified_diff(DIFF), settings, EmbeddingRetriever(embed))
    assert calls[:2] == [64, 37]  # 101 chunks in batches of 64, then the query
    assert len(fc.related) == settings.max_related_per_file


async def test_bad_embedder_output_is_an_error(settings):
    r = EmbeddingRetriever(lambda texts: [[1.0]])
    with pytest.raises(ValueError, match="vectors"):
        await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings, r)


async def test_noise_and_ignored_paths_not_indexed(settings):
    files = {
        **FILES,
        "vendor/refunds.py": "refund invoice refund invoice\n",
        "docs/refund.py": "refund invoice refund invoice\n",
        "poetry.lock": "refund invoice\n",
    }
    emb = FakeEmbedder()
    s = settings.model_copy(update={"ignore": ["docs/"]})
    await build_context(InMemorySource(files), parse_unified_diff(DIFF), s, EmbeddingRetriever(emb))
    indexed = {t.split("\n", 1)[0] for t in emb.batches[0]}
    assert indexed == {"billing.py", "ledger.py", "weather.py"}


async def test_max_files_limits_index(settings):
    emb = FakeEmbedder()
    await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings, EmbeddingRetriever(emb, max_files=1))
    assert {t.split("\n", 1)[0] for t in emb.batches[0]} == {"billing.py"}


class SearchOnlySource(InMemorySource):
    """Like GitHubSource: no list_files(), so candidates come from code search."""

    list_files = None  # type: ignore[assignment]


async def test_falls_back_to_search_candidates(settings):
    files = {**FILES, "caller.py": "from billing import issue_refund\nissue_refund(inv)  # refund\n"}
    emb = FakeEmbedder()
    [fc] = await build_context(SearchOnlySource(files), parse_unified_diff(DIFF), settings, EmbeddingRetriever(emb))
    assert {t.split("\n", 1)[0] for t in emb.batches[0]} == {"billing.py", "caller.py"}
    assert [r.path for r in fc.related] == ["caller.py"]


def test_setting_selects_retriever(settings):
    assert isinstance(make_retriever(settings), LexicalRetriever)
    r = make_retriever(settings.model_copy(update={"retriever": "embedding", "embedding_chunk_lines": 7}))
    assert isinstance(r, EmbeddingRetriever) and r.chunk_lines == 7


async def test_embedding_setting_works_without_injected_embedder(settings):
    s = settings.model_copy(update={"retriever": "embedding"})
    files = {**FILES, "refunds.py": "def compute_refund_fee(invoice):\n    return invoice.total * 0.03\n"}
    [fc] = await build_context(InMemorySource(files), parse_unified_diff(DIFF), s)
    assert fc.related and fc.related[0].path == "refunds.py"


def test_chunk_file_skips_blank_chunks():
    chunks = chunk_file("a.py", "a\nb\n\n\nc\n", size=2)
    assert [(c.start_line, c.lines) for c in chunks] == [(1, ["a", "b"]), (5, ["c"])]


def test_hashing_embed_is_deterministic_and_normalised():
    [a, b, c] = hashing_embed(["getUserName(user)", "get_user_name(user)", "parse_weather_forecast()"])
    assert hashing_embed(["getUserName(user)"])[0] == a
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
    assert cosine(a, b) > cosine(a, c)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
