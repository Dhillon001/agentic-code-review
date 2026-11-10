"""Secret pre-pass. Fake credentials are assembled at runtime so this file never contains a string that
looks like a real key to GitHub push protection or other scanners."""

from __future__ import annotations

import pytest
from conftest import FakeLLM

from reviewer.ingest.diff import parse_unified_diff
from reviewer.models import FileContext, Finding, PRContext, Severity
from reviewer.pipeline import review
from reviewer.secret_scan import detect, redact, scan, scrub, shannon_entropy

AWS = "AK" + "IA" + "Q3EGUWZ7XK4PJN2R"
GH = "gh" + "p_" + "R8vT2kLm9QxZ4wYb7NcP1sDf6GhJ3aKe5Ui0"
GH_PAT = "github" + "_pat_" + "11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789"
SLACK = "xo" + "xb-" + "1234567890-0987654321-AbCdEfGhIjKlMnOp"
SLACK_HOOK = "https://hooks.slack.com/services/" + "T0AB12CD3/B0EF45GH6/" + "aBcDeFgHiJkLmNoPqRsTuVwX"
STRIPE = "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
GENERIC = "q9X2v" + "LmT7pRz4KwB8"  # 13 chars, high entropy


# ---------------------------------------------------------------- detectors


@pytest.mark.parametrize(
    "line, kind, value",
    [
        (f'aws_key = "{AWS}"', "AWS access key ID", AWS),
        (f"export TOKEN={GH}", "GitHub token", GH),
        (f'pat: "{GH_PAT}"', "GitHub token", GH_PAT),
        (f"const slack = '{SLACK}';", "Slack token", SLACK),
        (f"WEBHOOK = {SLACK_HOOK!r}", "Slack webhook URL", SLACK_HOOK),
        (f"stripe.api_key = '{STRIPE}'", "Stripe API key", STRIPE),
        (PEM, "private key", PEM),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
        (f'password = "{GENERIC}"', "hardcoded credential", GENERIC),
        (f"DB_PASSWORD={GENERIC}", "hardcoded credential", GENERIC),
        (f"  api_key: {GENERIC}  # prod", "hardcoded credential", GENERIC),
        (f'{{"client_secret": "{GENERIC}"}}', "hardcoded credential", GENERIC),
    ],
)
def test_detects(line, kind, value):
    assert detect(line) == (kind, value)


@pytest.mark.parametrize(
    "line",
    [
        'password = os.environ["DB_PASSWORD"]',
        "password = get_password()",
        'password = "hunter2"',  # too short to call high-entropy
        'api_key = "your-api-key-here"',
        'secret = "xxxxxxxxxxxxxxxx"',
        'token = "${GITHUB_TOKEN}"',
        "password: {{ vault_db_password }}",
        'api_key = "<API_KEY>"',
        'password = "aaaaaaaaaaaaaaaaaaaa"',  # long but low entropy
        "max_tokens = 4096",
        "token = request.headers.get('Authorization')",
        "AKIA" + "SHORT",  # not 16 chars after the prefix
        "-----BEGIN PUBLIC KEY-----",
        "-----BEGIN CERTIFICATE-----",
    ],
)
def test_no_false_positive(line):
    assert detect(line) is None


def test_entropy():
    assert shannon_entropy("") == 0
    assert shannon_entropy("aaaa") == 0
    assert shannon_entropy("abcd") == 2


def test_redact_keeps_short_prefix_only():
    r = redact(AWS)
    assert r.startswith("AKIA") and AWS not in r and set(r[4:]) == {"*"}
    assert redact(GENERIC).startswith("q9X") and GENERIC not in redact(GENERIC)
    assert redact(PEM) == PEM  # the header isn't secret


# ---------------------------------------------------------------- scanning a diff

DIFF = f"""diff --git a/app/settings.py b/app/settings.py
--- a/app/settings.py
+++ b/app/settings.py
@@ -1,4 +1,5 @@
 import os
-OLD_KEY = "{AWS}"
+DEBUG = False
+AWS_KEY = "{AWS}"
 REGION = "us-east-1"
 BUCKET = "{GH}"
+STRIPE = os.environ["STRIPE"]
"""
CONTENT = f'import os\nDEBUG = False\nAWS_KEY = "{AWS}"\nREGION = "us-east-1"\nBUCKET = "{GH}"\nSTRIPE = os.environ["STRIPE"]\n'


def _ctx(diff: str = DIFF, content: str = CONTENT) -> list[FileContext]:
    return [FileContext(diff=d, content=content) for d in parse_unified_diff(diff)]


def test_scan_only_added_lines_with_new_line_numbers():
    findings, values = scan(_ctx())
    # removed line (OLD_KEY) and context line (BUCKET) are not reported
    assert [(f.file, f.line) for f in findings] == [("app/settings.py", 3)]
    f = findings[0]
    assert (f.agent, f.category, f.severity, f.confidence) == ("secrets", "secrets", Severity.CRITICAL, 1.0)
    assert f.title == "Hardcoded AWS access key ID"
    assert AWS not in f.model_dump_json() and redact(AWS) in f.detail
    assert values == {AWS}


def test_scan_skips_deleted_files():
    diff = f"diff --git a/k.pem b/k.pem\ndeleted file mode 100644\n--- a/k.pem\n+++ /dev/null\n@@ -1 +0,0 @@\n-{PEM}\n"
    assert scan(_ctx(diff, "")) == ([], set())


def test_scrub_redacts_agent_text_and_drops_leaky_replacement():
    f = Finding(agent="security", file="a.py", line=1, severity=Severity.HIGH, category="secrets-in-code",
                title=f"Key {AWS}", detail=f"`{AWS}` is hardcoded", replacement=f'KEY = "{AWS}"')
    (out,) = scrub([f], {AWS})
    assert AWS not in out.model_dump_json()
    assert redact(AWS) in out.detail and out.replacement is None
    untouched = f.model_copy(update={"title": "t", "detail": "d", "replacement": "x = 1"})
    assert scrub([untouched], {AWS}) == [untouched]


# ---------------------------------------------------------------- pipeline


def _pr() -> PRContext:
    return PRContext(repo="t/t", title="t", files=_ctx())


async def test_secret_posted_without_verifier(settings):
    llm = FakeLLM({})
    result = await review(_pr(), llm, settings)
    assert [(f.file, f.line, f.agent) for f in result.inline] == [("app/settings.py", 3, "secrets")]
    assert result.stats["secrets"] == 1 and result.stats["posted"] == 1
    assert "report_verdicts" not in llm.calls  # nothing else to verify; the secret skipped it


async def test_secret_never_rejected_and_agent_duplicate_merged(settings):
    dup = {"file": "app/settings.py", "line": 3, "severity": "high", "category": "hardcoded-secret",
           "title": "AWS key", "detail": f"`{AWS}` committed", "confidence": 0.9}
    other = {"file": "app/settings.py", "line": 6, "severity": "medium", "category": "config",
             "title": "KeyError", "detail": f"crashes if unset (unlike {AWS})", "confidence": 0.9}
    # the fake verifier would reject line 3 if it ever saw it
    llm = FakeLLM({"security": [dup], "correctness": [other]}, reject={("app/settings.py", 3)})
    result = await review(_pr(), llm, settings)
    assert [(f.line, f.agent) for f in result.inline] == [(3, "secrets"), (6, "correctness")]
    assert result.rejected == []
    assert AWS not in result.model_dump_json()  # redacted even where an agent quoted it


async def test_acr_ignore_secrets_suppresses(settings):
    content = CONTENT.replace(f'"{AWS}"', f'"{AWS}"  # acr: ignore[secrets]')
    diff = DIFF.replace(f'+AWS_KEY = "{AWS}"', f'+AWS_KEY = "{AWS}"  # acr: ignore[secrets]')
    pr = PRContext(repo="t/t", title="t", files=_ctx(diff, content))
    result = await review(pr, FakeLLM({}), settings)
    assert result.inline == [] and result.stats["suppressed"] == 1


async def test_secret_scan_can_be_disabled(settings):
    result = await review(_pr(), FakeLLM({}), settings.model_copy(update={"secret_scan": False}))
    assert result.inline == [] and result.stats["secrets"] == 0
