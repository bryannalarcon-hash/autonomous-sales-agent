# Tests for the authored KB + grounded RAG (plan U5). Two layers:
#   - DB-FREE unit test (always runs): all NINE objections (plan R30) have rebuttal content present.
#   - DB-DEPENDENT integration tests (skip unless DATABASE_URL): ingest the KB with the DETERMINISTIC
#     FakeEmbedder (no model download, no network), then assert a price question and a competitor
#     question retrieve the RIGHT chunks; retrieval is SCOPED to kb_version (a different version
#     returns nothing — plan R12 pinning); and grounded() flags an invented claim + passes a
#     supported one (the U9 groundedness guardrail). Uses pytest-asyncio (asyncio_mode=auto).
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from src.kb import retriever
from src.kb.embeddings import FakeEmbedder

# DB-dependent tests need asyncpg; skip those if the driver is absent (probe WITHOUT importing it,
# so the DB-free objection-coverage test below still runs). importlib.util.find_spec avoids the
# E402 "import not at top" the existing store test sidesteps with pytest.importorskip.
_HAVE_ASYNCPG = importlib.util.find_spec("asyncpg") is not None

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_INIT_SQL = _MIGRATIONS / "001_init.sql"  # provides the vector extension / base schema
_KB_SQL = _MIGRATIONS / "002_kb.sql"

KB_VERSION = "kb_v0"
OTHER_VERSION = "kb_vX"  # a version with NO ingested content — scope-isolation target


# ---------------------------------------------------------------------------
# DB-FREE: nine-objection coverage (plan R30) — runs without a database.
# ---------------------------------------------------------------------------
def test_all_nine_objections_have_rebuttal_content():
    """All nine objection slugs (R30 taxonomy) have a non-empty rebuttal section in the KB."""
    expected = {
        "price",
        "efficacy_doubt",
        "diy_free",
        "timing",
        "decision_maker",
        "scheduling",
        "online_vs_in_person",
        "student_resistance",
        "trust_legitimacy",
    }
    declared = set(retriever.list_objection_slugs())
    with_content = set(retriever.objection_slugs_with_content())

    assert declared == expected, f"declared taxonomy != R30 nine: {declared ^ expected}"
    # Every declared objection actually has rebuttal CONTENT present (not merely declared).
    assert with_content == expected, f"missing rebuttal content for: {expected - with_content}"


# ---------------------------------------------------------------------------
# DB-DEPENDENT integration: ingest + retrieve + scope + groundedness.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not (_HAVE_ASYNCPG and os.environ.get("DATABASE_URL")),
    reason="DATABASE_URL not set / asyncpg missing — DB-dependent KB integration test",
)


@pytest.fixture
def embedder() -> FakeEmbedder:
    """Deterministic 384-dim fake — no model download, no network (plan U5)."""
    return FakeEmbedder(dim=384)


@pytest.fixture(autouse=True)
async def _schema():
    """Per-test: bind the store pool to THIS event loop, apply migrations, start from a clean KB.

    Mirrors tests/integration/test_store.py: pytest-asyncio (auto) runs each test on its own loop,
    so the module-level pool must be dropped + recreated here. Migrations are idempotent. We TRUNCATE
    kb_chunk so each test ingests into a known-empty table and scope assertions are deterministic.
    """
    from src.memory import store

    await store.close_pool()
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))
        await conn.execute(_KB_SQL.read_text(encoding="utf-8"))
        await conn.execute("TRUNCATE kb_chunk")
    yield
    await store.close_pool()


async def test_ingest_loads_all_authored_chunks(embedder):
    """ingest() chunks every content/*.json section and tags them with the kb_version."""
    n = await retriever.ingest(embedder, KB_VERSION)
    # Expected count is derived from the authored corpus (single source of truth, not a hardcoded
    # number) so the KB can grow without this test going stale — the contract is "every authored
    # section becomes exactly one chunk." The real Nerdy KB has 175 sections across the 7 content
    # files (programs/pricing/policies/competitors/objections/results/company); the demo had 27.
    expected = len(retriever._load_content_chunks())
    assert n == expected
    assert expected > 100, f"expected the full Nerdy corpus (>100 chunks), got {expected}"

    from src.memory import store

    pool = await store.get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM kb_chunk WHERE kb_version = $1", KB_VERSION
        )
    assert count == expected


async def test_retrieve_price_question_returns_pricing_chunks(embedder):
    """A price question retrieves the membership-pricing chunks (cited by source)."""
    await retriever.ingest(embedder, KB_VERSION)
    # FakeEmbedder is hash-based (not semantic), so use a query string that is lexically close to
    # the pricing chunk text — the deterministic embedding makes this reproducible.
    chunks = await retriever.retrieve(
        "How pricing works monthly tiered membership recurring effective per-hour",
        kb_version=KB_VERSION,
        embedder=embedder,
        k=4,
    )
    assert chunks, "expected at least one retrieved chunk"
    sources = [c.source for c in chunks]
    assert any(s.startswith("pricing#") for s in sources), f"no pricing chunk in {sources}"
    # Every chunk carries the pinned version + a citation source.
    for c in chunks:
        assert c.kb_version == KB_VERSION
        assert "#" in c.source


async def test_retrieve_competitor_question_returns_competitor_chunks(embedder):
    """A competitor-differentiator question retrieves the competitor chunks (plan AE7)."""
    await retriever.ingest(embedder, KB_VERSION)
    chunks = await retriever.retrieve(
        "Versus tutor marketplaces managed vetted matching progress tracking",
        kb_version=KB_VERSION,
        embedder=embedder,
        k=4,
    )
    assert chunks
    sources = [c.source for c in chunks]
    assert any(s.startswith("competitors#") for s in sources), f"no competitor chunk in {sources}"


async def test_retrieval_is_scoped_to_kb_version(embedder):
    """Retrieval scoped to a kb_version with NO content returns nothing (plan R12 pinning)."""
    await retriever.ingest(embedder, KB_VERSION)
    # Same query, but pinned to a version that was never ingested -> empty.
    chunks = await retriever.retrieve(
        "How pricing works monthly tiered membership recurring",
        kb_version=OTHER_VERSION,
        embedder=embedder,
        k=4,
    )
    assert chunks == [], "a different kb_version must not leak the ingested content"


async def test_grounded_flags_invented_claim_and_passes_supported_one(embedder):
    """grounded() rejects an invented/unsupported claim and accepts a KB-supported one."""
    await retriever.ingest(embedder, KB_VERSION)
    chunks = await retriever.retrieve(
        "How pricing works monthly tiered membership recurring effective per-hour Core tier",
        kb_version=KB_VERSION,
        embedder=embedder,
        k=4,
    )
    assert chunks

    # A SUPPORTED answer: phrased entirely from the retrieved pricing content.
    support_text = " ".join(c.text for c in chunks)
    supported_answer = (
        "Pricing is a monthly tiered membership billed recurring; tiers bundle hours "
        "at a lower effective per-hour rate."
    )
    report_ok = retriever.grounded(supported_answer, chunks)
    assert bool(report_ok), (
        f"supported answer wrongly flagged; unsupported terms={report_ok.unsupported_terms}, "
        f"numbers={report_ok.unsupported_numbers}; support={support_text[:200]!r}"
    )

    # An INVENTED answer: a fabricated price + a guarantee absent from the KB.
    invented_answer = (
        "We offer a special $4999 lifetime membership with a guaranteed 200 point score increase "
        "or your money back forever, plus complimentary airfare to our headquarters."
    )
    report_bad = retriever.grounded(invented_answer, chunks)
    assert not bool(report_bad), "invented claim should be flagged ungrounded"
    assert report_bad.unsupported_numbers, "the fabricated $4999 / 200 figures should be flagged"


async def test_grounded_with_no_chunks_rejects_factual_answer(embedder):
    """With no retrieved chunks, a factual answer is ungrounded (agent must defer, not fabricate)."""
    report = retriever.grounded("The membership costs $349 per month.", [])
    assert not bool(report)


async def test_grounded_flags_invented_claim_token_despite_high_ratio(embedder):
    """FINDING 3: a single invented qualitative PROMISE word ('guarantee') in an otherwise on-KB
    sentence must fail groundedness and be surfaced in unsupported_terms — claim tokens are STRICT
    like numbers, not merely diluted by the 60%-supported content-word ratio."""
    await retriever.ingest(embedder, KB_VERSION)
    chunks = await retriever.retrieve(
        "How pricing works monthly tiered membership recurring effective per-hour Core tier",
        kb_version=KB_VERSION,
        embedder=embedder,
        k=4,
    )
    assert chunks
    support_text = " ".join(c.text for c in chunks)
    assert "guarantee" not in support_text.lower(), (
        "test precondition: the pricing chunks must NOT themselves contain 'guarantee'"
    )

    # Otherwise fully on-KB pricing sentence with ONE injected invented promise word.
    answer_with_claim = (
        "Pricing is a monthly tiered membership billed recurring; tiers bundle hours at a lower "
        "effective per-hour rate, and we guarantee it."
    )
    report = retriever.grounded(answer_with_claim, chunks)
    assert not bool(report), "an invented 'guarantee' promise must flip grounded to False"
    assert "guarantee" in report.unsupported_terms, (
        f"the invented claim token must be surfaced; unsupported_terms={report.unsupported_terms}"
    )


# ---------------------------------------------------------------------------
# ingest_corpus entrypoint + the shared live retrieve hook (plan U5 live wiring).
# ---------------------------------------------------------------------------
async def test_ingest_corpus_populates_chunks_and_live_hook_retrieves(embedder):
    """src.kb.ingest.ingest_corpus populates kb_chunk and the shared build_live_retrieve_hook (the
    SAME hook the demo API + voice worker import) returns the pricing chunks over the real store."""
    from src.kb.ingest import ingest_corpus
    from src.kb.live import build_live_retrieve_hook

    n = await ingest_corpus(kb_version=KB_VERSION, embedder=embedder)
    # Derived from the authored corpus (single source of truth), not a hardcoded number, so the KB
    # can grow without this test going stale. The real Nerdy KB is 175 sections; the demo was 27.
    expected = len(retriever._load_content_chunks())
    assert n == expected

    from src.memory import store

    pool = await store.get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM kb_chunk WHERE kb_version = $1", KB_VERSION
        )
    assert count == expected

    # The live hook (closure over the embedder + kb_version) retrieves real grounding over pgvector.
    hook = build_live_retrieve_hook(embedder, kb_version=KB_VERSION, k=4)
    chunks = await hook(
        "How pricing works monthly tiered membership recurring effective per-hour",
        kb_version=KB_VERSION,
        k=4,
    )
    assert chunks, "live retrieve hook should return chunks"
    assert any(c.source.startswith("pricing#") for c in chunks), [c.source for c in chunks]
