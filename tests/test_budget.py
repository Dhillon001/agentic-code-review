from conftest import FakeLLM

from reviewer.agents.specialists import (
    SPECIALISTS,
    Specialist,
    batch_system,
    build_review_prompt,
)
from reviewer.budget import estimate_cost, estimate_tokens, fit_budget
from reviewer.cli import format_text
from reviewer.config import Settings
from reviewer.models import FileContext, FileDiff, Hunk, PRContext, ReviewResult
from reviewer.output.github_review import budget_note, format_summary
from reviewer.output.markdown import format_markdown
from reviewer.pipeline import review
from reviewer.usage import ModelPrice

ONE = [Specialist("correctness", "logic")]
PRICES = {"m": ModelPrice(input=1.0, output=5.0)}  # $1/MTok in, so cache write 1.25, read 0.1


def _file(path: str, lines: int = 20, body: str = "x = 1") -> FileContext:
    added = [f"+{body}  # {i}" for i in range(lines)]
    hunk = Hunk(header=f"@@ -0,0 +1,{lines} @@", new_start=1, lines=added, added_lines=list(range(1, lines + 1)))
    content = "\n".join(line[1:] for line in added) + "\n"
    return FileContext(diff=FileDiff(path=path, status="added", hunks=[hunk]), content=content)


def _pr(*files: FileContext) -> PRContext:
    return PRContext(repo="t/t", title="budget", files=list(files))


def _settings(settings: Settings, **kw) -> Settings:
    return settings.model_copy(update={"reviewer_model": "m", "model_prices": PRICES, **kw})


def test_estimate_tokens_is_chars_over_four_rounded_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 8) == 2
    assert estimate_tokens("a" * 9) == 3


def test_estimate_prices_cached_prefix_and_instructions(settings):
    s = _settings(settings)
    pr = _pr(_file("a.py"))
    prefix = estimate_tokens(batch_system(pr.files, s)) + estimate_tokens(build_review_prompt(pr, pr.files, s))
    instr = sum(estimate_tokens(sp.instructions) for sp in SPECIALISTS)
    # four specialists on one model, warm-up on: one cache write, three cache reads, plus their instructions
    expected = (prefix * 1.25 + prefix * 3 * 0.1 + instr) / 1_000_000
    assert abs(estimate_cost(pr, pr.files, SPECIALISTS, s) - expected) < 1e-12


def test_estimate_without_warmup_writes_cache_on_every_call(settings):
    pr = _pr(_file("a.py"))
    warm = estimate_cost(pr, pr.files, SPECIALISTS, _settings(settings))
    cold = estimate_cost(pr, pr.files, SPECIALISTS, _settings(settings, cache_warmup=False))
    assert cold > warm


def test_estimate_per_model_and_unpriced_models_are_free(settings):
    pr = _pr(_file("a.py"))
    base = _settings(settings)
    pricier = base.model_copy(
        update={"agent_models": {"security": "big"}, "model_prices": {**PRICES, "big": ModelPrice(input=10, output=50)}}
    )
    assert estimate_cost(pr, pr.files, SPECIALISTS, pricier) > estimate_cost(pr, pr.files, SPECIALISTS, base)
    unpriced = base.model_copy(update={"model_prices": {}})
    assert estimate_cost(pr, pr.files, SPECIALISTS, unpriced) == 0.0
    assert estimate_cost(pr, [], SPECIALISTS, base) == 0.0


def test_estimate_grows_with_files(settings):
    s = _settings(settings)
    a, b = _file("a.py"), _file("b.py", lines=200)
    assert estimate_cost(_pr(a, b), [a, b], ONE, s) > estimate_cost(_pr(a, b), [a], ONE, s)


def test_no_budget_keeps_everything(settings):
    files = [_file("a.py"), _file("b.py")]
    for budget in (None, 0, -1):
        kept, dropped, est = fit_budget(_pr(*files), files, ONE, _settings(settings, max_cost_usd=budget))
        assert kept == files and dropped == [] and est > 0


def test_budget_drops_from_the_end_until_it_fits(settings):
    files = [_file("a.py"), _file("b.py"), _file("c.py"), _file("d.py")]
    s = _settings(settings)
    pr = _pr(*files)
    two = estimate_cost(pr, files[:2], ONE, s)
    kept, dropped, est = fit_budget(pr, files, ONE, _settings(settings, max_cost_usd=two))
    assert [f.path for f in kept] == ["a.py", "b.py"]
    assert [f.path for f in dropped] == ["d.py", "c.py"]
    assert est == two


def test_budget_too_small_for_anything_drops_all(settings):
    files = [_file("a.py"), _file("b.py")]
    kept, dropped, est = fit_budget(_pr(*files), files, ONE, _settings(settings, max_cost_usd=1e-9))
    assert kept == [] and len(dropped) == 2 and est == 0.0


async def test_pipeline_skips_over_budget_files_and_reports_them(settings):
    files = [_file("a.py"), _file("b.py"), _file("c.py")]
    s = _settings(settings)
    pr = _pr(*files)
    budget = estimate_cost(pr, files[:1], SPECIALISTS, s) * 1.01
    finding = {"file": "a.py", "line": 1, "severity": "high", "category": "bug", "title": "t",
               "detail": "d", "confidence": 0.9}
    llm = FakeLLM({"correctness": [finding]})
    result = await review(pr, llm, _settings(settings, max_cost_usd=budget))

    assert result.skipped_budget == ["c.py", "b.py"]
    assert result.stats["skipped_budget"] == 2
    assert result.stats["files"] == 3
    assert result.stats["agent_calls"] == 4
    reviewed = [u for u, tool in zip(llm.users, llm.calls) if tool == "report_findings"]
    assert all("b.py" not in str(u) and "c.py" not in str(u) for u in reviewed)
    assert [(f.file, f.line) for f in result.inline] == [("a.py", 1)]


async def test_secret_scan_still_covers_files_dropped_by_budget(settings):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    files = [_file("a.py"), _file("conf.py", lines=1, body=f'TOKEN = "{token}"')]
    llm = FakeLLM({})
    result = await review(_pr(*files), llm, _settings(settings, max_cost_usd=1e-9))
    assert result.skipped_budget == ["conf.py", "a.py"]
    assert "report_findings" not in llm.calls and result.stats["agent_calls"] == 0
    assert [(f.file, f.category) for f in result.inline] == [("conf.py", "secrets")]


async def test_no_budget_reviews_everything(settings):
    files = [_file("a.py"), _file("b.py")]
    result = await review(_pr(*files), FakeLLM({}), _settings(settings))
    assert result.skipped_budget == [] and result.stats["skipped_budget"] == 0


def test_reports_list_skipped_files():
    result = ReviewResult(skipped_budget=["big.py", "huge.py"])
    for text in (format_summary(result), format_markdown(result)):
        assert "Cost budget" in text and "`big.py`" in text and "`huge.py`" in text
    assert "not reviewed: big.py, huge.py" in format_text(result)
    assert budget_note(ReviewResult()) == ""
    assert "Cost budget" not in format_summary(ReviewResult())


def test_budget_note_truncates_long_lists():
    note = budget_note(ReviewResult(skipped_budget=[f"f{i}.py" for i in range(25)]))
    assert note.startswith("💰 **Cost budget:** 25 files") and "`f19.py`" in note
    assert "`f20.py`" not in note and "and 5 more" in note


def test_max_cost_setting_from_env(monkeypatch):
    monkeypatch.setenv("MAX_COST_USD", "0.25")
    assert Settings(_env_file=None).max_cost_usd == 0.25
    monkeypatch.setenv("MAX_COST_USD", "")
    assert Settings(_env_file=None).max_cost_usd is None
