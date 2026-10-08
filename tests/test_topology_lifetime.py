from app.indexer import FirmwareIndexer
from app.memory_lifetime_service import MemoryLifetimeService
from app.platform_repository import PlatformRepository


def _indexed(source: str):
    files = [{"path": "src/tasks.cpp", "content": source, "content_hash": FirmwareIndexer.digest(source)}]
    result = FirmwareIndexer().index(files, project_id="PRJ-A")
    analysis = MemoryLifetimeService().analyze("PRJ-A", files, result.symbols, result.relations, result.allocations)
    return result, analysis


def test_topology_resolves_task_chain_and_ignores_comments_and_strings():
    result, _ = _indexed('''
void function_a(void) { helper(); }
void helper(void) { }
void task_b(void *arg) { function_a(); }
void init(void) { xTaskCreate(task_b, "b", 1024, 0, 1, 0); }
// xTaskCreate(fake_task, "fake")
const char *text = "function_a();";
''')
    observed = [item for item in result.relations if item["relation_kind"] in {"CALLS", "TASK_ENTRY"} and item["relation_state"] == "OBSERVED"]
    names = {item["target_symbol_id"] for item in observed}
    function_ids = {item["id"] for item in result.symbols if item["kind"] == "function"}
    assert names <= function_ids
    assert any(item["relation_kind"] == "TASK_ENTRY" for item in observed)
    assert not any(item.get("target_name") == "fake_task" for item in result.relations)


def test_duplicate_function_names_are_inferred_not_false_observed_edges():
    files = [
        {"path": "src/a.c", "content": "void helper(void) {}\nvoid caller(void) { helper(); }"},
        {"path": "src/b.c", "content": "void helper(void) {}"},
    ]
    result = FirmwareIndexer().index(files, project_id="PRJ-A")
    helper_edges = [item for item in result.relations if item["relation_kind"] == "CALLS" and item.get("target_name") == "helper"]
    assert helper_edges
    assert all(item["relation_state"] == "INFERRED" and item["target_symbol_id"] is None for item in helper_edges)


def test_unbalanced_error_exit_creates_candidate_seed():
    result, analysis = _indexed('''
void task_b(void *arg) {
    char *buffer = malloc(32);
    if (read_sensor() != 0) {
        return;
    }
    free(buffer);
}
''')
    assert analysis.seeds
    seed = analysis.seeds[0]
    assert seed.variable == "buffer"
    assert seed.path == ("task_b", "allocation@3", "error_exit@5")
    assert result.allocations[0]["ownership_state"] == "UNBALANCED_EXIT"


def test_comments_do_not_create_release_or_exit_facts():
    _, analysis = _indexed('''
void worker(void) {
    char *p = malloc(8);
    // free(p); return;
}
''')
    fact = analysis.facts[0]
    assert fact.release_lines == ()
    assert fact.exit_lines == ()
    assert analysis.seeds == []


def test_strings_do_not_create_release_or_exit_facts():
    _, analysis = _indexed('''
void worker(void) {
    char *p = malloc(8);
    const char *text = "free(p); return;";
}
''')
    fact = analysis.facts[0]
    assert fact.release_lines == ()
    assert fact.exit_lines == ()
    assert analysis.seeds == []


def test_conditional_release_does_not_cover_unconditional_return():
    _, analysis = _indexed('''
void worker(void) {
    char *p = malloc(8);
    if (ok) free(p);
    return;
}
''')
    fact = analysis.facts[0]
    assert fact.release_lines == (4,)
    assert fact.exit_lines == (5,)
    assert fact.ownership_state == "UNKNOWN"
    assert any("branch coverage" in assumption for assumption in fact.assumptions)
    assert analysis.seeds == []


def test_nested_conditional_release_does_not_cover_outer_exit():
    _, analysis = _indexed('''
void worker(void) {
    char *p = malloc(8);
    if (outer_ok) {
        if (inner_ok) {
            free(p);
        }
        return;
    }
}
''')
    fact = analysis.facts[0]
    assert fact.ownership_state == "UNKNOWN"
    assert any("branch coverage" in assumption for assumption in fact.assumptions)
    assert analysis.seeds == []


def test_returned_stored_and_unknown_ownership_is_not_a_leak_seed():
    for body, expected in (
        ("char *p = malloc(8);\nreturn p;", "RETURNS_OWNERSHIP"),
        ("char *p = malloc(8);\nowner->buffer = p;\nreturn;", "STORES_OWNERSHIP"),
        ("char *p = malloc(8);\nhand_off(p);\nreturn;", "PASSES_TO_UNKNOWN"),
    ):
        _, analysis = _indexed(f"void worker(void) {{\n    {body}\n}}\n")
        assert analysis.facts[0].ownership_state == expected
        assert analysis.seeds == []


def test_release_on_direct_exit_has_no_seed():
    _, analysis = _indexed('''
void worker(void) {
    char *p = malloc(8);
    free(p);
    return;
}
''')
    assert analysis.facts[0].ownership_state == "RELEASED"
    assert analysis.seeds == []


def test_topology_persistence_is_project_scoped_and_repeatable(tmp_path):
    repository = PlatformRepository(str(tmp_path / "topology.db"))
    repository.create_project({"id": "PRJ-A", "name": "A", "description": "", "source_type": "MANUAL", "language": None, "framework": None, "target": None, "build_system": None, "source_directory": None, "created_at": "now", "updated_at": "now"})
    result, _ = _indexed("void task_b(void *arg) { return; }")
    repository.replace_topology("PRJ-A", result.symbols, result.relations, result.allocations, source_snapshot_hash="source-a", relation_fingerprint=result.relation_fingerprint, indexed_at="now")
    repository.replace_topology("PRJ-A", result.symbols, result.relations, result.allocations, source_snapshot_hash="source-a", relation_fingerprint=result.relation_fingerprint, indexed_at="now")
    assert repository.topology_snapshot("PRJ-A")["source_snapshot_hash"] == "source-a"
    assert len(repository.list_indexed_symbols("PRJ-A")) == len(result.symbols)
    assert repository.list_indexed_symbols("PRJ-B") == []
