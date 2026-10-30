import pytest

from reviewer.context.retriever import build_context, changed_symbols, symbols_in_line
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource

DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 def helper_func(x):
-    return x
+    return x + 1
"""


def test_body_only_change_still_finds_enclosing_symbol():
    assert changed_symbols(parse_unified_diff(DIFF)[0]) == ["helper_func"]


async def test_related_callers_pulled_in(settings):
    src = InMemorySource({
        "a.py": "def helper_func(x):\n    return x + 1\n",
        "b.py": "from a import helper_func\nprint(helper_func(1))\n",
        "c.py": "unrelated = True\n",
    })
    [fc] = await build_context(src, parse_unified_diff(DIFF), settings)
    assert [r.path for r in fc.related] == ["b.py"]
    assert "helper_func(1)" in fc.related[0].text


# ---------------------------------------------------------------- language-aware symbol detection


def _diff(path: str, body: list[str], header_ctx: str = "") -> str:
    """A one-hunk diff adding `body` lines to `path`."""
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +1,{len(body)} @@{(' ' + header_ctx) if header_ctx else ''}\n"
        + "".join(f"+{ln}\n" for ln in body)
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("function loadUser(id) {", ["loadUser"]),
        ("async function* streamRows() {", ["streamRows"]),
        ("export default function renderPage(props) {", ["renderPage"]),
        ("export const fetchData = async (url) => {", ["fetchData"]),
        ("const toSlug = s => s.toLowerCase();", ["toSlug"]),
        ("const handler: Handler = (req, res) => {", ["handler"]),
        ("let legacy = function (x) {", ["legacy"]),
        ("export class UserStore extends Base {", ["UserStore"]),
        ("export interface Props {", ["Props"]),
        ("type Result<T> = { ok: true; value: T };", ["Result"]),
        ("  async refreshToken(force = false) {", ["refreshToken"]),
        ("  private static parseHeader(raw: string): Header {", ["parseHeader"]),
        ("  get displayName() {", ["displayName"]),
        ("  onClick = (event) => {", ["onClick"]),
        ("  if (user) {", []),
        ("  } else if (retry) {", []),
        ("  setTimeout(function () {", []),
        ("  return compute(value);", []),
    ],
)
def test_js_ts_symbols(line, expected):
    assert symbols_in_line(line, "src/app.ts") == expected
    assert symbols_in_line(line, "src/app.jsx") == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("func ParseConfig(path string) (*Config, error) {", ["ParseConfig"]),
        ("func (r *Repo) FindByID(ctx context.Context, id int) (*User, error) {", ["FindByID"]),
        ("func (s Server) Handle(w http.ResponseWriter, r *http.Request) {", ["Handle"]),
        ("func Map[T, U any](xs []T, f func(T) U) []U {", ["Map"]),
        ("type Repo struct {", ["Repo"]),
        ("type Store interface {", ["Store"]),
        ("\tdefer cleanup()", []),
        ("\tif err := run(); err != nil {", []),
    ],
)
def test_go_symbols(line, expected):
    assert symbols_in_line(line, "pkg/repo.go") == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("public class OrderService {", ["OrderService"]),
        ("public interface Repository<T> {", ["Repository"]),
        ("    public List<Order> findOrders(String customerId) {", ["findOrders"]),
        ("    private static int computeTotal(int[] prices) throws IOException {", ["computeTotal"]),
        ("    @Override public String toString() {", ["toString"]),
        ("    <T> T convert(Object value) {", ["convert"]),
        ("    public OrderService(Repository repo) {", ["OrderService"]),
        ("        return computeTotal(prices);", []),
        ("        throw new IllegalStateException(msg);", []),
        ("        Order order = repo.find(id);", []),
        ("        } else if (order == null) {", []),
    ],
)
def test_java_symbols(line, expected):
    assert symbols_in_line(line, "src/main/java/OrderService.java") == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("class PaymentGateway(private val client: Client) {", ["PaymentGateway"]),
        ("data class Invoice(val id: String)", ["Invoice"]),
        ("object Registry {", ["Registry"]),
        ("fun chargeCard(amount: Long): Receipt {", ["chargeCard"]),
        ("    override suspend fun fetchAll(): List<Item> {", ["fetchAll"]),
        ("fun <T> List<T>.secondOrNull(): T? = getOrNull(1)", ["secondOrNull"]),
        ("    val total = items.sumOf { it.price }", []),
    ],
)
def test_kotlin_symbols(line, expected):
    assert symbols_in_line(line, "app/Payment.kt") == expected


def test_python_and_unknown_languages_use_generic_patterns():
    assert symbols_in_line("async def fetch_rows(conn):", "db.py") == ["fetch_rows"]
    assert symbols_in_line("pub fn parse_args() -> Args {", "main.rs") == ["parse_args"]


@pytest.mark.parametrize(
    ("path", "body", "header_ctx", "expected"),
    [
        ("web/api.ts", ["export const fetchUser = async (id: string) => {"], "", ["fetchUser"]),
        ("svc/repo.go", ["\treturn nil"], "func (r *Repo) Delete(id int) error {", ["Delete"]),
        ("src/Svc.java", ["        log.info(x);"], "public void process(Event x) {", ["process"]),
        ("src/Svc.kt", ["    retry()"], "suspend fun syncAccounts() {", ["syncAccounts"]),
    ],
)
def test_changed_symbols_uses_file_language(path, body, header_ctx, expected):
    [d] = parse_unified_diff(_diff(path, body, header_ctx))
    assert changed_symbols(d) == expected
