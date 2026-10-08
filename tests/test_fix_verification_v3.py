from __future__ import annotations

from datetime import UTC, datetime

from app.fix_verification import build_differential_context, capture_finding_baseline, validate_fix_verification
from app.platform_schemas import FindingRead, FindingVerificationBaseline, FixVerificationEvidence, FixVerificationPathEdge, FixVerificationResult


def _finding(*, file: str = "src/main.c", function: str = "unsafe_read", line: int = 2, category: str = "CONCURRENCY") -> FindingRead:
    return FindingRead.model_validate({
        "id": "FS-001",
        "project_id": "PRJ-1",
        "review_id": "REV-1",
        "title": "Unsynchronized shared state access",
        "classification": "CONFIRMED_BUG",
        "severity": "high",
        "category": category,
        "confidence": 0.9,
        "location": {"file": file, "function": function, "line_start": line, "line_end": line + 1},
        "summary": "Two tasks can mutate the shared buffer at the same time.",
        "evidence": [{"description": "The shared write has no synchronization.", "file": file, "line": line}],
        "execution_path": ["task_a", function, "shared_buffer write"],
        "runtime_scenario": "Two concurrent tasks can overwrite the same state.",
        "impact": "The result can be corrupted.",
        "assumptions": [],
        "recommendation": "Add a mutex around the shared update.",
        "verification": {"status": "PASSED", "notes": "The original path was confirmed."},
        "decision": "ACCEPTED",
        "topology_path": [],
        "lifetime_evidence": [],
        "decision_reason": None,
        "resolution": "OPEN",
        "resolved_at": None,
        "remediation": {"status": "UNVERIFIED"},
        "created_at": datetime.now(UTC).isoformat(),
    })


def _symbol(name: str, file: str, start: int, end: int, *, kind: str = "function", source_hash: str = "a" * 64, symbol_hash: str | None = None) -> dict:
    return {"id": f"{file}:{name}", "name": name, "kind": kind, "file": file, "line_start": start, "line_end": end, "source_hash": source_hash, "symbol_hash": symbol_hash or source_hash, "signature": f"void {name}(void)"}


def _relation(source: str, target: str, file: str, line: int, kind: str = "CALL") -> dict:
    return {"id": f"{source}>{target}", "source_symbol_id": source, "target_symbol_id": target, "target_name": target.split(":")[-1], "relation_kind": kind, "relation_state": "OBSERVED", "file": file, "line": line}


def _path_edge(source: str, target: str, file: str, line: int, kind: str = "CALLS") -> FixVerificationPathEdge:
    return FixVerificationPathEdge(source=source, relation=kind, target=target, file=file, line=line, relation_state="OBSERVED", source_id=f"{file}:{source}", target_id=f"{file}:{target}")


def _evidence(file: str, line: int, source_line: str, description: str, symbol: str | None = None) -> FixVerificationEvidence:
    snippet = source_line.strip()[:160]
    return FixVerificationEvidence(file=file, line=line, symbol=symbol, evidence_snippet=snippet, description=description)


def _result(
    verdict: str,
    *,
    failure: str = "Concurrent mutation of shared state remains possible.",
    path: list[str] | None = None,
    current_path: list[str] | None = None,
    mitigations: list[FixVerificationEvidence] | None = None,
    remaining: list[FixVerificationEvidence] | None = None,
    missing: list[str] | None = None,
    alternative: bool = False,
    summary: str = "Current source evidence supports this conclusion.",
) -> FixVerificationResult:
    return FixVerificationResult(
        verdict=verdict,
        original_failure_condition=failure,
        original_execution_path=path or ["task_a", "unsafe_read", "shared state write"],
        current_execution_path=current_path or ["task_a", "current source path inspected"],
        mitigations_found=mitigations or [],
        remaining_failure_evidence=remaining or [],
        inspected_files=[],
        inspected_symbols=[],
        missing_context=missing or [],
        alternative_mitigation=alternative,
        confidence=0.9,
        reasoning_summary=summary,
    )


def test_a_finding_context_selects_fix_deep_inside_large_file():
    prefix = "".join(f"void unrelated_{i}(void) {{ int value_{i} = {i}; }}\n" for i in range(180))
    before_tail = "void unsafe_read(void) {\n  char out[4];\n  memcpy(out, input, 4);\n}\n"
    after_tail = "void unsafe_read(void) {\n  char out[5] = {0};\n  memcpy(out, input, 4);\n  out[4] = '\\0';\n}\n"
    before = [{"path": "src/main.c", "content": prefix + before_tail}]
    current = [{"path": "src/main.c", "content": prefix + after_tail}]
    start = len(prefix.splitlines()) + 1
    finding = _finding(function="unsafe_read", line=start + 2)
    context = build_differential_context(
        finding, before, current,
        before_symbols=[_symbol("unsafe_read", "src/main.c", start, start + 3)],
        current_symbols=[_symbol("unsafe_read", "src/main.c", start, start + 4, source_hash="b" * 64)],
    )
    assert "out[4] = '\\0';" in context
    assert "unrelated_0" not in context
    assert "SOURCE DIFF (BEFORE -> AFTER)" in context
    assert len(context) <= 16_000


def test_b_single_writer_and_queue_copy_is_valid_alternative_mitigation():
    before = [{"path": "src/main.c", "content": "void task_a(void) { shared_state++; }\nvoid task_b(void) { shared_state++; }\n"}]
    current_text = "void task_a(void) { shared_state++; QueueMsg copy = shared_state; xQueueSend(state_queue, &copy, 0); }\nvoid task_b(void) { QueueMsg copy; xQueueReceive(state_queue, &copy, portMAX_DELAY); consume(copy); }\n"
    current = [{"path": "src/main.c", "content": current_text}]
    symbols = [_symbol("task_a", "src/main.c", 1, 1, source_hash="b" * 64), _symbol("task_b", "src/main.c", 2, 2, source_hash="b" * 64)]
    relation = _relation("src/main.c:task_a", "src/main.c:task_b", "src/main.c", 1, "QUEUE_COPY")
    finding = _finding(function="task_a", line=1)
    context = build_differential_context(finding, before, current, current_symbols=symbols, current_relations=[relation])
    assert "xQueueSend" in context and "xQueueReceive" in context
    assert "QUEUE_COPY" in context
    line = current_text.splitlines()[0]
    result = _result("FIXED", mitigations=[_evidence("src/main.c", 1, line, "The owner sends copied queue data.", "task_a")], alternative=True)
    checked = validate_fix_verification(result, current, symbols, finding)
    assert checked.verdict == "FIXED", (checked.validation_reasons, checked.mitigations_found)
    assert checked.alternative_mitigation is True


def test_c_caller_side_lock_is_in_context_while_callee_stays_unchanged():
    before = [{"path": "src/main.c", "content": "void caller(void) { unsafe_function(); }\nvoid unsafe_function(void) { shared_state++; }\n"}]
    current_text = "void caller(void) { lock(shared_lock); unsafe_function(); unlock(shared_lock); }\nvoid unsafe_function(void) { shared_state++; }\n"
    current = [{"path": "src/main.c", "content": current_text}]
    before_symbols = [_symbol("caller", "src/main.c", 1, 1), _symbol("unsafe_function", "src/main.c", 2, 2)]
    current_symbols = [_symbol("caller", "src/main.c", 1, 1, source_hash="b" * 64), _symbol("unsafe_function", "src/main.c", 2, 2)]
    edges = [_relation("src/main.c:caller", "src/main.c:unsafe_function", "src/main.c", 1)]
    context = build_differential_context(_finding(function="unsafe_function", line=2), before, current, before_symbols=before_symbols, current_symbols=current_symbols, before_relations=edges, current_relations=edges)
    assert "lock(shared_lock)" in context and "unlock(shared_lock)" in context
    assert "caller" in context


def test_d_still_present_requires_current_valid_source_evidence():
    source = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { unsafe_read(); }\nvoid unsafe_read(void) {\n  shared_state++;\n}\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2), _symbol("unsafe_read", "src/main.c", 3, 5)]
    relations = [_relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY"), _relation("src/main.c:task_a", "src/main.c:unsafe_read", "src/main.c", 2, "CALLS")]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 4, "  shared_state++;", "The current function mutates shared_state.", "unsafe_read")], current_path=["app_main creates task_a", "task_a calls unsafe_read"])
    result = result.model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/main.c", 1, "TASK_ENTRY"), _path_edge("task_a", "unsafe_read", "src/main.c", 2)]})
    assert validate_fix_verification(result, files, symbols, _finding(line=4), current_relations=relations).verdict == "STILL_PRESENT"


def test_e_historical_finding_alone_is_downgraded_to_inconclusive():
    files = [{"path": "src/new.c", "content": "void safe(void) { return; }\n"}]
    old_only = _result("STILL_PRESENT", remaining=[FixVerificationEvidence(file="src/old.c", line=12, evidence_snippet="old", description="Historical source says the old bug remains.")])
    checked = validate_fix_verification(old_only, files, [_symbol("safe", "src/new.c", 1, 1)])
    assert checked.verdict == "INCONCLUSIVE"
    assert checked.remaining_failure_evidence == []


def test_f_unresolved_caller_context_downgrades_still_present():
    files = [{"path": "src/main.c", "content": "void unsafe_read(void) { shared_state++; }\n"}]
    result = _result(
        "STILL_PRESENT",
        remaining=[_evidence("src/main.c", 1, "shared_state++;", "The operation remains unsafe.", "unsafe_read")],
        missing=["No current caller or entry path could be resolved."],
    )
    assert validate_fix_verification(result, files, [_symbol("unsafe_read", "src/main.c", 1, 1)], _finding(line=1)).verdict == "INCONCLUSIVE"


def test_g_moved_equivalent_function_can_be_still_present_at_new_location():
    source = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { renamed_unsafe_read(); }\nvoid renamed_unsafe_read(void) { shared_state++; }\n"
    files = [{"path": "src/moved.c", "content": source}]
    symbols = [_symbol("app_main", "src/moved.c", 1, 1), _symbol("task_a", "src/moved.c", 2, 2), _symbol("renamed_unsafe_read", "src/moved.c", 3, 3)]
    finding = _finding(file="src/old.c", function="unsafe_read")
    baseline = FindingVerificationBaseline(source_snapshot_hash="a" * 64, topology_fingerprint="b" * 64,
        files=[], symbols=[
            {"id":"src/old.c:unsafe_read", "name":"unsafe_read", "file":"src/old.c", "signature":"void unsafe_read(void)", "line_start":1,"line_end":1},
            {"id":"src/moved.c:task_a", "name":"task_a", "file":"src/moved.c", "signature":"void task_a(void)", "line_start":2,"line_end":2},
        ], topology_edges=[_path_edge("task_a", "unsafe_read", "src/old.c", 1)])
    finding = finding.model_copy(update={"verification_baseline": baseline})
    relations = [_relation("src/moved.c:app_main", "src/moved.c:task_a", "src/moved.c", 1, "TASK_ENTRY"), _relation("src/moved.c:task_a", "src/moved.c:renamed_unsafe_read", "src/moved.c", 2, "CALLS")]
    context = build_differential_context(finding, [{"path": "src/old.c", "content": "void unsafe_read(void) { shared_state++; }\n"}], files, current_symbols=symbols, current_relations=relations)
    assert "renamed_unsafe_read" in context
    result = _result("STILL_PRESENT", remaining=[_evidence("src/moved.c", 3, "void renamed_unsafe_read(void) { shared_state++; }", "Moved code retains the unsafe shared_state write.", "renamed_unsafe_read")])
    result = result.model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/moved.c", 1, "TASK_ENTRY"), _path_edge("task_a", "renamed_unsafe_read", "src/moved.c", 2)]})
    assert validate_fix_verification(result, files, symbols, finding, current_relations=relations).verdict == "STILL_PRESENT"


def test_h_removed_feature_has_current_caller_evidence_for_fix():
    source = "void app_main(void) { xTaskCreate(task_worker, \"w\", 1024, 0, 1, 0); }\nvoid task_worker(void) { return; }\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_worker", "src/main.c", 2, 2)]
    finding = _finding(function="unsafe_read", line=2)
    finding = finding.model_copy(update={"verification_baseline": FindingVerificationBaseline(
        source_snapshot_hash="a" * 64, topology_fingerprint="b" * 64,
        symbols=[
            {"id": "src/main.c:unsafe_read", "name": "unsafe_read", "file": "src/main.c", "signature": "void unsafe_read(void)", "line_start": 1, "line_end": 1},
                {"id": "src/main.c:task_worker", "name": "task_worker", "file": "src/main.c", "signature": "void task_worker(void)", "line_start": 2, "line_end": 2},
            ],
            topology_edges=[_path_edge("task_worker", "unsafe_read", "src/main.c", 2)],
        )})
    context = build_differential_context(
        finding,
        [{"path": "src/main.c", "content": "esp_err_t app_main(void) { unsafe_read(); }\nvoid unsafe_read(void) { shared_state++; }\n"}],
        files,
        current_symbols=symbols,
    )
    assert "unsafe_read" in context
    result = _result("FIXED", failure="The removed feature previously reached an unsafe shared write.", current_path=["task_worker now returns without entering the removed path."], mitigations=[_evidence("src/main.c", 2, "void task_worker(void) { return; }", "task_worker returns without calling unsafe_read.", "task_worker")])
    current_relations = [_relation("src/main.c:app_main", "src/main.c:task_worker", "src/main.c", 1, "TASK_ENTRY")]
    checked = validate_fix_verification(result, files, symbols, finding, current_relations=current_relations)
    assert checked.verdict == "FIXED", checked.model_dump()


def test_i_api_representation_change_can_break_original_invalid_length_condition():
    source = "void bounded_view(Span input) { copy_span(input); }\n"
    files = [{"path": "src/buffer.c", "content": source}]
    symbols = [_symbol("bounded_view", "src/buffer.c", 1, 1)]
    result = _result("FIXED", failure="An invalid caller length previously reached the buffer copy.", current_path=["caller -> bounded_view(Span) -> copy_span uses the validated span length"], mitigations=[_evidence("src/buffer.c", 1, source, "Span carries the bounded extent into the copy operation.", "bounded_view")], alternative=True)
    checked = validate_fix_verification(result, files, symbols, _finding(function="bounded_view", line=1, category="MEMORY_SAFETY"))
    assert checked.verdict == "FIXED", checked.model_dump()
    assert checked.alternative_mitigation


def test_j_invalid_ai_file_or_line_cannot_support_a_verdict():
    files = [{"path": "src/main.c", "content": "void safe(void) { return; }\n"}]
    result = _result("FIXED", mitigations=[FixVerificationEvidence(file="src/imaginary.c", line=999, evidence_snippet="lock", description="The imagined file contains a lock.")])
    assert validate_fix_verification(result, files, [_symbol("safe", "src/main.c", 1, 1)]).verdict == "INCONCLUSIVE"


def test_k_ownership_transfer_context_does_not_require_local_free():
    before = [{"path": "src/owner.c", "content": "void create(void) { Item *item = malloc(sizeof(Item)); consume(item); }\n"}]
    current_text = "void create(void) { Item *item = make_item(); ownership_transfer(item, item_queue); }\n"
    current = [{"path": "src/owner.c", "content": current_text}]
    symbol = _symbol("create", "src/owner.c", 1, 1, source_hash="b" * 64)
    context = build_differential_context(_finding(function="create", category="MEMORY_LIFETIME"), before, current, current_symbols=[symbol], current_relations=[_relation("src/owner.c:create", "item_queue", "src/owner.c", 1, "OWNERSHIP_TRANSFER")], current_allocations=[{"file": "src/owner.c", "line": 1, "ownership_state": "TRANSFERRED"}], lifetime_facts=[{"symbol": "create", "ownership_state": "TRANSFERRED", "file": "src/owner.c", "allocation_line": 1}])
    assert "ownership_transfer" in context
    assert "TRANSFERRED" in context
    assert "free()" not in context
    result = _result("FIXED", failure="Allocated resource was dropped on exit.", current_path=["create -> ownership_transfer -> item_queue owns the Item"], mitigations=[_evidence("src/owner.c", 1, current_text.strip(), "The resource lifetime escapes through explicit ownership transfer.", "create")], alternative=True)
    assert validate_fix_verification(result, current, [symbol], _finding(function="create", line=1, category="MEMORY_LIFETIME")).verdict == "FIXED"


def test_l_unrelated_changed_file_does_not_supply_fix_evidence():
    unsafe = "void unsafe_read(void) { shared_state++; }\n"
    before = [{"path": "src/main.c", "content": unsafe}, {"path": "src/other.c", "content": "int value = 1;\n"}]
    current = [{"path": "src/main.c", "content": unsafe}, {"path": "src/other.c", "content": "int value = 2;\n"}]
    context = build_differential_context(_finding(line=1), before, current, before_symbols=[_symbol("unsafe_read", "src/main.c", 1, 1)], current_symbols=[_symbol("unsafe_read", "src/main.c", 1, 1)])
    assert "shared_state++" in context
    assert "src/other.c" in context.split("CHANGED FILES:", 1)[1].splitlines()[0]
    unsupported = _result("FIXED", current_path=["task_a -> unsafe_read remains unchanged"], mitigations=[])
    assert validate_fix_verification(unsupported, current, [_symbol("unsafe_read", "src/main.c", 1, 1)]).verdict == "INCONCLUSIVE"


def test_m_current_mutex_relations_prevent_sink_only_still_present():
    source = """void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); xTaskCreate(task_b, \"b\", 1024, 0, 1, 0); }
void task_a(void) { xSemaphoreTake(shared_mutex, portMAX_DELAY); unsafe_write(); xSemaphoreGive(shared_mutex); }
void task_b(void) { xSemaphoreTake(shared_mutex, portMAX_DELAY); unsafe_write(); xSemaphoreGive(shared_mutex); }
void unsafe_write(void) { shared_state++; }
"""
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2), _symbol("task_b", "src/main.c", 3, 3), _symbol("unsafe_write", "src/main.c", 4, 4)]
    relations = [
        _relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY"),
        _relation("src/main.c:app_main", "src/main.c:task_b", "src/main.c", 1, "TASK_ENTRY"),
        _relation("src/main.c:task_a", "src/main.c:unsafe_write", "src/main.c", 2, "CALLS"),
        _relation("src/main.c:task_b", "src/main.c:unsafe_write", "src/main.c", 3, "CALLS"),
        {**_relation("src/main.c:task_a", "xSemaphoreTake", "src/main.c", 2, "USES_RESOURCE"), "target_name": "xSemaphoreTake", "target_symbol_id": None, "metadata": {"api": "xSemaphoreTake", "operation": "LOCK", "resource": "shared_mutex"}},
        {**_relation("src/main.c:task_a", "xSemaphoreGive", "src/main.c", 2, "USES_RESOURCE"), "target_name": "xSemaphoreGive", "target_symbol_id": None, "metadata": {"api": "xSemaphoreGive", "operation": "UNLOCK", "resource": "shared_mutex"}},
        {**_relation("src/main.c:task_b", "xSemaphoreTake", "src/main.c", 3, "USES_RESOURCE"), "target_name": "xSemaphoreTake", "target_symbol_id": None, "metadata": {"api": "xSemaphoreTake", "operation": "LOCK", "resource": "shared_mutex"}},
        {**_relation("src/main.c:task_b", "xSemaphoreGive", "src/main.c", 3, "USES_RESOURCE"), "target_name": "xSemaphoreGive", "target_symbol_id": None, "metadata": {"api": "xSemaphoreGive", "operation": "UNLOCK", "resource": "shared_mutex"}},
    ]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 4, "shared_state++;", "The shared_state sink remains.", "unsafe_write")])
    result = result.model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/main.c", 1, "TASK_ENTRY"), _path_edge("task_a", "unsafe_write", "src/main.c", 2)]})
    checked = validate_fix_verification(result, files, symbols, _finding(file="src/main.c", function="unsafe_write", line=4), current_relations=relations)
    assert checked.verdict == "INCONCLUSIVE", checked.model_dump()
    assert checked.validation_status == "DOWNGRADED"


def test_n_fabricated_current_path_edge_downgrades_result():
    source = "void app_main(void) { return; }\nvoid unsafe_read(void) { shared_state++; }\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("unsafe_read", "src/main.c", 2, 2)]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 2, "shared_state++;", "The shared_state operation remains.", "unsafe_read")])
    result = result.model_copy(update={"current_path_edges": [_path_edge("app_main", "unsafe_read", "src/main.c", 1, "CALLS")]})
    checked = validate_fix_verification(result, files, symbols, _finding(function="unsafe_read", line=2), current_relations=[])
    assert checked.verdict == "INCONCLUSIVE"
    assert checked.current_path_edges == []


def test_o_current_observed_path_and_sink_evidence_preserve_still_present():
    test_d_still_present_requires_current_valid_source_evidence()


def test_p_caller_side_validation_does_not_allow_unsupported_still_present():
    source = """void app_main(void) { xTaskCreate(parser_task, \"p\", 1024, 0, 1, 0); }
void parser_task(void) { if (input_len <= sizeof(input)) unsafe_parse(input, input_len); }
void unsafe_parse(const char *input, size_t input_len) { memcpy(buffer, input, input_len); }
"""
    files = [{"path": "src/parser.c", "content": source}]
    symbols = [_symbol("app_main", "src/parser.c", 1, 1), _symbol("parser_task", "src/parser.c", 2, 2), _symbol("unsafe_parse", "src/parser.c", 3, 3)]
    relations = [_relation("src/parser.c:app_main", "src/parser.c:parser_task", "src/parser.c", 1, "TASK_ENTRY"), _relation("src/parser.c:parser_task", "src/parser.c:unsafe_parse", "src/parser.c", 2, "CALLS")]
    finding = _finding(file="src/parser.c", function="unsafe_parse", line=3, category="MEMORY_SAFETY")
    result = _result("STILL_PRESENT", remaining=[_evidence("src/parser.c", 3, "memcpy(buffer, input, input_len);", "The original unchecked copy remains.", "unsafe_parse")])
    result = result.model_copy(update={"current_path_edges": [_path_edge("app_main", "parser_task", "src/parser.c", 1, "TASK_ENTRY"), _path_edge("parser_task", "unsafe_parse", "src/parser.c", 2)]})
    checked = validate_fix_verification(result, files, symbols, finding, current_relations=relations)
    assert checked.verdict != "STILL_PRESENT"


def test_r_function_symbol_hash_ignores_unrelated_function_changes_and_persists(tmp_path):
    from app.indexer import FirmwareIndexer
    from app.platform_repository import PlatformRepository

    indexer = FirmwareIndexer()
    before = indexer.index([{"path": "src/main.c", "content": "void function_a(void) { int a = 1; }\nvoid function_b(void) { int b = 1; }\n"}], "P")
    after = indexer.index([{"path": "src/main.c", "content": "void function_a(void) { int a = 1; }\nvoid function_b(void) { int b = 2; }\n"}], "P")
    before_hashes = {item["name"]: item["symbol_hash"] for item in before.symbols if item["kind"] == "function"}
    after_hashes = {item["name"]: item["symbol_hash"] for item in after.symbols if item["kind"] == "function"}
    assert before_hashes["function_a"] == after_hashes["function_a"]
    assert before_hashes["function_b"] != after_hashes["function_b"]
    repository = PlatformRepository(str(tmp_path / "symbols.db"))
    repository.create_project({"id": "P", "name": "P", "description": "", "source_type": "MANUAL", "language": "C", "framework": None, "target": None, "build_system": None, "source_directory": None, "created_at": "now", "updated_at": "now"})
    repository.replace_topology("P", after.symbols, after.relations, after.allocations, source_snapshot_hash="c" * 64, relation_fingerprint=after.relation_fingerprint, indexed_at="now")
    stored_hashes = {item["name"]: item["symbol_hash"] for item in repository.list_indexed_symbols("P") if item["kind"] == "function"}
    assert stored_hashes == after_hashes


def test_q_saved_finding_baseline_survives_later_sync_before_verify():
    finding_time = "void task_a(void) { unsafe_read(); }\nvoid unsafe_read(void) { shared_state++; }\n"
    pre_refresh = "void task_a(void) { unsafe_read(); }\nvoid unsafe_read(void) { shared_state += 1; }\n"
    current = "void task_a(void) { safe_read(); }\nvoid safe_read(void) { shared_state += 1; }\n"
    symbols_a = [_symbol("task_a", "src/main.c", 1, 1), _symbol("unsafe_read", "src/main.c", 2, 2)]
    relations_a = [_relation("src/main.c:task_a", "src/main.c:unsafe_read", "src/main.c", 1)]
    finding = _finding(file="src/main.c", function="unsafe_read", line=2)
    saved = capture_finding_baseline(
        finding, [{"path": "src/main.c", "content": finding_time}], symbols_a, relations_a,
        source_snapshot_hash="a" * 64, topology_fingerprint="b" * 64,
    )
    finding = finding.model_copy(update={"verification_baseline": saved})
    symbols_b = [_symbol("task_a", "src/main.c", 1, 1, symbol_hash="c" * 64), _symbol("unsafe_read", "src/main.c", 2, 2, symbol_hash="d" * 64)]
    symbols_c = [_symbol("task_a", "src/main.c", 1, 1, symbol_hash="e" * 64), _symbol("safe_read", "src/main.c", 2, 2, symbol_hash="f" * 64)]
    context = build_differential_context(
        finding, [{"path": "src/main.c", "content": pre_refresh}], [{"path": "src/main.c", "content": current}],
        before_symbols=symbols_b, current_symbols=symbols_c,
        before_relations=relations_a, current_relations=[], finding_baseline=saved,
    )
    assert "ORIGINAL FINDING BASELINE SOURCE" in context
    assert "shared_state++;" in context
    assert "shared_state += 1;" in context
    assert '"finding_baseline_is_known": true' in context


def test_s_model_snippet_is_replaced_with_canonical_current_source_line():
    source = "void unsafe_read(void) {\n  shared_state ++;\n}\n"
    evidence = FixVerificationEvidence(file="src/main.c", line=2, symbol="unsafe_read", evidence_snippet="shared_state++;", description="The shared_state value is still mutated.")
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("unsafe_read", "src/main.c", 1, 3)]
    relations = [_relation("src/main.c:app_main", "src/main.c:unsafe_read", "src/main.c", 1, "TASK_ENTRY")]
    # Entry edges target task/ISR functions; use a task wrapper for the real topology.
    files[0]["content"] = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { unsafe_read(); }\n" + source
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2), _symbol("unsafe_read", "src/main.c", 3, 5)]
    relations = [_relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY"), _relation("src/main.c:task_a", "src/main.c:unsafe_read", "src/main.c", 2, "CALLS")]
    evidence = evidence.model_copy(update={"line": 4})
    result = _result("STILL_PRESENT", remaining=[evidence]).model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/main.c", 1, "TASK_ENTRY"), _path_edge("task_a", "unsafe_read", "src/main.c", 2)]})
    checked = validate_fix_verification(result, files, symbols, _finding(file="src/main.c", function="unsafe_read", line=4), current_relations=relations)
    assert checked.verdict == "STILL_PRESENT", checked.validation_reasons
    assert checked.remaining_failure_evidence[0].evidence_snippet == "shared_state ++;"


def test_t_unrelated_line_in_sink_function_cannot_support_still_present():
    source = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { unsafe_read(); }\nvoid unsafe_read(void) {\n  shared_state++;\n  return;\n}\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2), _symbol("unsafe_read", "src/main.c", 3, 6)]
    relations = [_relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY"), _relation("src/main.c:task_a", "src/main.c:unsafe_read", "src/main.c", 2, "CALLS")]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 5, "return;", "The shared_state access remains unsafe.", "unsafe_read")]).model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/main.c", 1, "TASK_ENTRY"), _path_edge("task_a", "unsafe_read", "src/main.c", 2)]})
    checked = validate_fix_verification(result, files, symbols, _finding(file="src/main.c", function="unsafe_read", line=4), current_relations=relations)
    assert checked.verdict == "INCONCLUSIVE"
    assert checked.remaining_failure_evidence == []


def test_u_ambiguous_moved_functions_are_inconclusive():
    source = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { unsafe_read_new_a(); unsafe_read_new_b(); }\nvoid unsafe_read_new_a(void) { shared_state++; }\nvoid unsafe_read_new_b(void) { shared_state++; }\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2), _symbol("unsafe_read_new_a", "src/main.c", 3, 3), _symbol("unsafe_read_new_b", "src/main.c", 4, 4)]
    baseline = FindingVerificationBaseline(source_snapshot_hash="a" * 64, topology_fingerprint="b" * 64,
        symbols=[{"id":"src/old.c:unsafe_read", "name":"unsafe_read", "file":"src/old.c", "signature":"void unsafe_read(void)", "line_start":1,"line_end":1}, {"id":"src/main.c:task_a", "name":"task_a", "file":"src/main.c", "signature":"void task_a(void)", "line_start":2,"line_end":2}],
        topology_edges=[_path_edge("task_a", "unsafe_read", "src/old.c", 1)])
    finding = _finding(file="src/old.c", function="unsafe_read").model_copy(update={"verification_baseline": baseline})
    relations = [_relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY"), _relation("src/main.c:task_a", "src/main.c:unsafe_read_new_a", "src/main.c", 2), _relation("src/main.c:task_a", "src/main.c:unsafe_read_new_b", "src/main.c", 2)]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 3, "shared_state++;", "The shared_state sink remains.", "unsafe_read_new_a")]).model_copy(update={"current_path_edges": [_path_edge("app_main", "task_a", "src/main.c", 1, "TASK_ENTRY"), _path_edge("task_a", "unsafe_read_new_a", "src/main.c", 2)]})
    assert validate_fix_verification(result, files, symbols, finding, current_relations=relations).verdict == "INCONCLUSIVE"


def test_v_moved_function_is_supported_by_original_caller_redirect():
    test_g_moved_equivalent_function_can_be_still_present_at_new_location()


def test_w_removed_path_requires_resolved_original_callers_and_removal_evidence():
    before = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { unsafe_read(); }\nvoid unsafe_read(void) { shared_state++; }\n"
    current = "void app_main(void) { xTaskCreate(task_a, \"a\", 1024, 0, 1, 0); }\nvoid task_a(void) { return; }\n"
    baseline = FindingVerificationBaseline(source_snapshot_hash="a" * 64, topology_fingerprint="b" * 64,
        symbols=[{"id":"src/main.c:unsafe_read", "name":"unsafe_read", "file":"src/main.c", "signature":"void unsafe_read(void)", "line_start":3,"line_end":3}, {"id":"src/main.c:task_a", "name":"task_a", "file":"src/main.c", "signature":"void task_a(void)", "line_start":2,"line_end":2}],
        topology_edges=[_path_edge("task_a", "unsafe_read", "src/main.c", 2)])
    finding = _finding(function="unsafe_read", line=3).model_copy(update={"verification_baseline": baseline})
    files = [{"path": "src/main.c", "content": current}]
    symbols = [_symbol("app_main", "src/main.c", 1, 1), _symbol("task_a", "src/main.c", 2, 2)]
    relations = [_relation("src/main.c:app_main", "src/main.c:task_a", "src/main.c", 1, "TASK_ENTRY")]
    result = _result("FIXED", failure="task_a previously reached a shared_state write.", mitigations=[_evidence("src/main.c", 2, "void task_a(void) { return; }", "The task_a path now returns and no longer calls unsafe_read.", "task_a")])
    checked = validate_fix_verification(result, files, symbols, finding, current_relations=relations)
    assert checked.verdict == "FIXED", checked.model_dump()


def _indexed_edge(relation, symbols):
    by_id = {item["id"]: item for item in symbols}
    source = by_id[str(relation.get("source_symbol_id") or "")]
    target = by_id.get(str(relation.get("target_symbol_id") or ""))
    return FixVerificationPathEdge(
        source=source["name"], relation=relation["relation_kind"], target=(target or {}).get("name") or relation.get("target_name"),
        file=relation["file"], line=relation["line"], relation_state=relation["relation_state"],
        source_id=source["id"], target_id=(target or {}).get("id"),
    )


def _indexed_path_to(result, target_name):
    from collections import deque

    by_id = {item["id"]: item for item in result.symbols}
    targets = [item["id"] for item in result.symbols if item["kind"] == "function" and item["name"] == target_name]
    assert len(targets) == 1
    graph = {}
    entries = []
    for relation in result.relations:
        if relation["relation_state"] != "OBSERVED":
            continue
        kind = relation["relation_kind"]
        if kind in {"APP_ENTRY", "TASK_ENTRY", "ISR_ENTRY", "CALLBACK_ENTRY", "EVENT_HANDLER_ENTRY", "TIMER_ENTRY", "REGISTERED_HANDLER"}:
            entries.append(relation)
        elif kind in {"CALL", "CALLS"}:
            graph.setdefault(relation["source_symbol_id"], []).append(relation)
    queue = deque(([entry], str(entry.get("target_symbol_id"))) for entry in entries)
    while queue:
        path, node = queue.popleft()
        if node == targets[0]:
            return [_indexed_edge(item, result.symbols) for item in path]
        for relation in graph.get(node, []):
            queue.append(([*path, relation], str(relation.get("target_symbol_id"))))
    raise AssertionError(f"no indexed observed firmware path reaches {target_name}")


def _indexed_state(source, finding_function="unsafe_copy", category="MEMORY_SAFETY", finding_line=3):
    from app.indexer import FirmwareIndexer

    files = [{"path": "src/main.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-VERIFY-V5")
    finding = _finding(file="src/main.c", function=finding_function, line=finding_line, category=category)
    return files, indexed, finding


def _still_result(files, indexed, finding, target_function, sink_line, description="The current unsafe operation remains reachable."):
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", sink_line, files[0]["content"].splitlines()[sink_line - 1], description, target_function)])
    return result.model_copy(update={"current_path_edges": _indexed_path_to(indexed, target_function)})


def test_x_app_main_is_an_observed_firmware_entry_for_still_present():
    source = '#include "esp_err.h"\nvoid unsafe_copy(void) {\n  shared_buffer[index] = value;\n}\nesp_err_t app_main(void) { unsafe_copy(); return 0; }\n'
    files, indexed, finding = _indexed_state(source, finding_line=3)
    assert any(item["relation_kind"] == "APP_ENTRY" and item["relation_state"] == "OBSERVED" for item in indexed.relations)
    checked = validate_fix_verification(_still_result(files, indexed, finding, "unsafe_copy", 3), files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT", checked.validation_reasons


def test_app_main_name_alone_does_not_establish_esp_idf_entry():
    from app.indexer import FirmwareIndexer

    source = "void unsafe_copy(void) { shared_buffer[index] = value; }\nvoid app_main(void) { unsafe_copy(); }\n"
    indexed = FirmwareIndexer().index([{"path": "src/main.c", "content": source}], project_id="FS-APP-NAME-ONLY")
    assert not any(item["relation_kind"] == "APP_ENTRY" for item in indexed.relations)


def test_y_mqtt_callback_registration_is_an_observed_entry_path():
    source = """void unsafe_copy(void) { shared_buffer[index] = value; }
void parse_payload(void) { unsafe_copy(); }
void mqtt_event_handler(void) { parse_payload(); }
void register_mqtt(void) { esp_mqtt_client_register_event(client, event, mqtt_event_handler, NULL); }
"""
    files, indexed, finding = _indexed_state(source, finding_line=1)
    entries = [item for item in indexed.relations if item["relation_kind"] == "CALLBACK_ENTRY"]
    assert len(entries) == 1 and entries[0]["relation_state"] == "OBSERVED"
    assert entries[0]["target_symbol_id"] == next(item["id"] for item in indexed.symbols if item["name"] == "mqtt_event_handler")
    result = _still_result(files, indexed, finding, "unsafe_copy", 1)
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT"
    assert checked.current_path_edges[0].relation == "CALLBACK_ENTRY"


def test_z_http_handler_is_observed_only_from_static_registered_initializer():
    source = """httpd_uri_t uri = { .uri = "/", .handler = request_handler };
void unsafe_copy(void) { shared_buffer[index] = value; }
void parse_request(void) { unsafe_copy(); }
void request_handler(void) { parse_request(); }
void start_http(void) { httpd_register_uri_handler(server, &uri); }
"""
    from app.indexer import FirmwareIndexer

    indexed = FirmwareIndexer().index([{"path": "src/http.c", "content": source}], project_id="FS-HTTP")
    entries = [item for item in indexed.relations if item["relation_kind"] == "REGISTERED_HANDLER"]
    assert len(entries) == 1 and entries[0]["relation_state"] == "OBSERVED"
    assert entries[0]["target_name"] is None
    files = [{"path": "src/http.c", "content": source}]
    finding = _finding(file="src/http.c", function="unsafe_copy", line=2, category="MEMORY_SAFETY")
    current = _result("STILL_PRESENT", remaining=[_evidence("src/http.c", 2, "void unsafe_copy(void) { shared_buffer[index] = value; }", "The current shared buffer write remains.", "unsafe_copy")])
    current = current.model_copy(update={"current_path_edges": _indexed_path_to(indexed, "unsafe_copy")})
    checked = validate_fix_verification(current, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT"


def test_callback_struct_initializer_in_another_function_is_not_reused_as_registration_evidence():
    from app.indexer import FirmwareIndexer

    source = """void timer_callback(void) { unsafe_copy(); }
void local_setup(void) { esp_timer_create_args_t args = { .callback = timer_callback }; }
void later_start(void) { esp_timer_create(&args, &timer); }
void unsafe_copy(void) { shared_buffer[index] = value; }
"""
    indexed = FirmwareIndexer().index([{"path": "src/timer.c", "content": source}], project_id="FS-TIMER-SCOPE")

    assert not any(item["relation_kind"] == "TIMER_ENTRY" for item in indexed.relations)


def test_aa_unresolved_callback_registration_cannot_ground_still_present():
    source = """void unsafe_copy(void) { shared_buffer[index] = value; }
void mqtt_event_handler(void) { unsafe_copy(); }
void register_mqtt(void) { esp_mqtt_client_register_event(client, event, callback_pointer, NULL); }
"""
    files, indexed, finding = _indexed_state(source, finding_line=1)
    registration = next(item for item in indexed.relations if item["relation_kind"] == "CALLBACK_ENTRY")
    assert registration["relation_state"] == "INFERRED"
    claimed = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 1, files[0]["content"].splitlines()[0], "The current sink is reachable.", "unsafe_copy")])
    sink_id = next(item["id"] for item in indexed.symbols if item["name"] == "unsafe_copy")
    calls = [item for item in indexed.relations if item["relation_kind"] in {"CALL", "CALLS"} and item["relation_state"] == "OBSERVED"]
    call_graph = {}
    for item in calls:
        call_graph.setdefault(item["source_symbol_id"], []).append(item)
    handler_id = next(item["id"] for item in indexed.symbols if item["name"] == "mqtt_event_handler")
    handler_call = next(item for item in call_graph[handler_id] if item["target_symbol_id"])
    assert handler_call["target_symbol_id"] == sink_id
    claimed = claimed.model_copy(update={"current_path_edges": [_indexed_edge(registration, indexed.symbols), _indexed_edge(handler_call, indexed.symbols)]})
    checked = validate_fix_verification(claimed, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "INCONCLUSIVE"


def test_ab_unrelated_event_group_does_not_weaken_bounds_failure():
    source = """#include <esp_err.h>
void unsafe_copy(void) {
    xEventGroupWaitBits(events, READY, pdFALSE, pdTRUE, portMAX_DELAY);
    shared_buffer[index] = input;
}
void app_main(void) { unsafe_copy(); }
"""
    files, indexed, finding = _indexed_state(source, finding_line=4)
    result = _still_result(files, indexed, finding, "unsafe_copy", 4, "The indexed write uses an unchecked index.")
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT", checked.validation_reasons


def test_ac_unrelated_mutex_does_not_mitigate_bounds_failure():
    source = """#include <esp_err.h>
void unsafe_copy(void) {
    xSemaphoreTake(logging_mutex, portMAX_DELAY);
    shared_buffer[index] = input;
    xSemaphoreGive(logging_mutex);
}
void app_main(void) { unsafe_copy(); }
"""
    files, indexed, finding = _indexed_state(source, finding_line=4)
    result = _still_result(files, indexed, finding, "unsafe_copy", 4, "The indexed write uses an unchecked index.")
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT", checked.validation_reasons


def _race_before_source():
    return """void app_main(void) {
    xTaskCreate(task_a, "a", 1024, 0, 1, 0);
    xTaskCreate(task_b, "b", 1024, 0, 1, 0);
}
void task_a(void) { unsafe_write(); }
void task_b(void) { unsafe_write(); }
void unsafe_write(void) { shared_state++; }
"""


def _race_finding_with_baseline(source):
    from app.fix_verification import capture_finding_baseline
    from app.indexer import FirmwareIndexer

    files = [{"path": "src/race.c", "content": source}]
    indexer = FirmwareIndexer()
    indexed = indexer.index(files, project_id="FS-RACE-V5")
    finding = _finding(file="src/race.c", function="unsafe_write", line=7, category="CONCURRENCY")
    baseline = capture_finding_baseline(
        finding, [{**files[0], "content_hash": indexer.digest(source)}], indexed.symbols, indexed.relations,
        source_snapshot_hash=indexer.digest(source), topology_fingerprint=indexed.relation_fingerprint,
    )
    return finding.model_copy(update={"verification_baseline": baseline}), files, indexed


def _race_current_source(protected_tasks):
    lines = [
        "void app_main(void) {",
        '    xTaskCreate(task_a, "a", 1024, 0, 1, 0);',
        '    xTaskCreate(task_b, "b", 1024, 0, 1, 0);',
        "}",
    ]
    for task in ("task_a", "task_b"):
        lines.append(f"void {task}(void) {{")
        if task in protected_tasks:
            lines.append("    xSemaphoreTake(shared_state_mutex, portMAX_DELAY);")
        lines.append("    unsafe_write();")
        if task in protected_tasks:
            lines.append("    xSemaphoreGive(shared_state_mutex);")
        lines.append("}")
    lines.append("void unsafe_write(void) { shared_state++; }")
    return "\n".join(lines) + "\n"


def test_ad_matching_mutex_blocks_sink_only_still_present():
    from app.indexer import FirmwareIndexer

    source = _race_current_source({"task_a", "task_b"})
    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-RACE-MUTEX")
    finding = _finding(file="src/race.c", function="unsafe_write", line=15, category="CONCURRENCY")
    result = _result("STILL_PRESENT", remaining=[_evidence("src/race.c", 15, "shared_state++;", "The shared_state write remains.", "unsafe_write")]).model_copy(update={"current_path_edges": _indexed_path_to(indexed, "unsafe_write")})
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "INCONCLUSIVE"


def test_ae_one_fixed_and_one_unprotected_baseline_path_is_not_fixed():
    from app.indexer import FirmwareIndexer

    finding, _, _ = _race_finding_with_baseline(_race_before_source())
    source = _race_current_source({"task_a"})
    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-RACE-PARTIAL")
    result = _result("FIXED", failure="Two tasks previously wrote shared_state without synchronization.", mitigations=[_evidence("src/race.c", 6, "    xSemaphoreTake(shared_state_mutex, portMAX_DELAY);", "task_a now takes the shared_state mutex.", "task_a")])
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict != "FIXED"
    assert {item.status for item in checked.original_path_coverage} == {"MITIGATED", "STILL_UNSAFE"}


def test_af_all_baseline_callers_with_same_mutex_can_be_fixed():
    from app.indexer import FirmwareIndexer

    finding, _, _ = _race_finding_with_baseline(_race_before_source())
    source = _race_current_source({"task_a", "task_b"})
    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-RACE-FULL")
    result = _result("FIXED", failure="Two tasks previously wrote shared_state without synchronization.", mitigations=[_evidence("src/race.c", 6, "    xSemaphoreTake(shared_state_mutex, portMAX_DELAY);", "task_a and task_b now use the same shared_state mutex.", "task_a")])
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "FIXED", checked.validation_reasons
    assert len(checked.original_path_coverage) == 2
    assert {item.status for item in checked.original_path_coverage} == {"MITIGATED"}


def test_ag_unrelated_queue_receive_does_not_mitigate_shared_state_race():
    source = """void unsafe_write(void) { shared_state++; }
void task_a(void) { xQueueReceive(command_queue, &command, portMAX_DELAY); unsafe_write(); }
void app_main(void) { xTaskCreate(task_a, "a", 1024, 0, 1, 0); }
"""
    files, indexed, finding = _indexed_state(source, "unsafe_write", "CONCURRENCY", 1)
    result = _still_result(files, indexed, finding, "unsafe_write", 1)
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "STILL_PRESENT"


def test_ah_queue_copy_isolation_accounts_for_an_original_writer_path():
    from app.indexer import FirmwareIndexer

    finding, _, _ = _race_finding_with_baseline(_race_before_source())
    source = """void app_main(void) {
    xTaskCreate(task_a, "a", 1024, 0, 1, 0);
    xTaskCreate(task_b, "b", 1024, 0, 1, 0);
}
void task_a(void) { unsafe_write(); }
void task_b(void) { QueueMessage copy; xQueueReceive(state_queue, &copy, portMAX_DELAY); consume(copy); }
void unsafe_write(void) { shared_state++; }
"""
    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-RACE-QUEUE")
    result = _result("FIXED", failure="Two tasks previously wrote shared_state without synchronization.", mitigations=[_evidence("src/race.c", 6, "void task_b(void) { QueueMessage copy; xQueueReceive(state_queue, &copy, portMAX_DELAY); consume(copy); }", "task_b now consumes a copied queue message and no longer mutates shared_state.", "task_b")], alternative=True)
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "FIXED", checked.validation_reasons
    assert {item.status for item in checked.original_path_coverage} == {"MITIGATED", "REDIRECTED_SAFE"}


def test_ak_callback_named_function_without_registration_is_not_entry():
    from app.indexer import FirmwareIndexer

    source = "void mqtt_event_handler(void) { handle_event(); }\nvoid handle_event(void) {}\n"
    indexed = FirmwareIndexer().index([{"path": "src/main.c", "content": source}], project_id="FS-NO-CALLBACK")
    assert not any(item["relation_kind"] in {"CALLBACK_ENTRY", "EVENT_HANDLER_ENTRY", "REGISTERED_HANDLER"} for item in indexed.relations)


def test_ai_three_original_paths_are_accounted_for_when_two_are_removed_and_one_is_locked():
    from app.fix_verification import capture_finding_baseline
    from app.indexer import FirmwareIndexer

    before = """void app_main(void) {
    xTaskCreate(task_a, "a", 1024, 0, 1, 0);
    xTaskCreate(task_b, "b", 1024, 0, 1, 0);
    xTaskCreate(task_c, "c", 1024, 0, 1, 0);
}
void task_a(void) { unsafe_write(); }
void task_b(void) { unsafe_write(); }
void task_c(void) { unsafe_write(); }
void unsafe_write(void) { shared_state++; }
"""
    source = """void app_main(void) {
    xTaskCreate(task_a, "a", 1024, 0, 1, 0);
    xTaskCreate(task_b, "b", 1024, 0, 1, 0);
    xTaskCreate(task_c, "c", 1024, 0, 1, 0);
}
void task_a(void) { return; }
void task_b(void) { return; }
void task_c(void) {
    xSemaphoreTake(shared_state_mutex, portMAX_DELAY);
    unsafe_write();
    xSemaphoreGive(shared_state_mutex);
}
void unsafe_write(void) { shared_state++; }
"""
    indexer = FirmwareIndexer()
    old_files = [{"path": "src/race.c", "content": before, "content_hash": indexer.digest(before)}]
    old_index = indexer.index(old_files, project_id="FS-THREE-PATHS")
    finding = _finding(file="src/race.c", function="unsafe_write", line=10, category="CONCURRENCY")
    baseline = capture_finding_baseline(
        finding, old_files, old_index.symbols, old_index.relations,
        source_snapshot_hash=indexer.digest(before), topology_fingerprint=old_index.relation_fingerprint,
    )
    finding = finding.model_copy(update={"verification_baseline": baseline})
    files = [{"path": "src/race.c", "content": source}]
    current_index = indexer.index(files, project_id="FS-THREE-PATHS")
    result = _result(
        "FIXED", failure="Three task paths previously wrote shared_state without synchronization.",
        mitigations=[
            _evidence("src/race.c", 6, "void task_a(void) { return; }", "task_a no longer calls unsafe_write; its path returns.", "task_a"),
            _evidence("src/race.c", 7, "void task_b(void) { return; }", "task_b no longer calls unsafe_write; its path returns.", "task_b"),
            _evidence("src/race.c", 9, "    xSemaphoreTake(shared_state_mutex, portMAX_DELAY);", "task_c uses the mutex protecting shared_state.", "task_c"),
        ],
    )
    checked = validate_fix_verification(result, files, current_index.symbols, finding, current_relations=current_index.relations)
    assert checked.verdict == "FIXED", checked.validation_reasons
    assert len(checked.original_path_coverage) == 3
    assert {item.status for item in checked.original_path_coverage} == {"MITIGATED", "REDIRECTED_SAFE"}


def test_aj_unresolved_original_entry_caller_prevents_fixed_verdict():
    finding, _, _ = _race_finding_with_baseline(_race_before_source())
    source = """void app_main(void) {
    xTaskCreate(task_a, "a", 1024, 0, 1, 0);
}
void task_a(void) { return; }
void unsafe_write(void) { shared_state++; }
"""
    from app.indexer import FirmwareIndexer

    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-RACE-V5")
    result = _result("FIXED", failure="Both original task paths no longer reach the unsafe operation.", mitigations=[_evidence("src/race.c", 5, "void task_a(void) { return; }", "task_a no longer calls unsafe_write.", "task_a")])
    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)
    assert checked.verdict == "INCONCLUSIVE"
    assert any(item.status == "UNRESOLVED" for item in checked.original_path_coverage)


def test_al_unrelated_mutex_does_not_mitigate_concurrency_invariant():
    from app.indexer import FirmwareIndexer

    source = """void unsafe_write(void) { shared_state++; }
void task_a(void) {
    xSemaphoreTake(logging_mutex, portMAX_DELAY);
    unsafe_write();
    xSemaphoreGive(logging_mutex);
}
void app_main(void) { xTaskCreate(task_a, "a", 1024, 0, 1, 0); }
"""
    files = [{"path": "src/race.c", "content": source}]
    indexed = FirmwareIndexer().index(files, project_id="FS-UNRELATED-LOCK")
    finding = _finding(file="src/race.c", function="unsafe_write", line=1, category="CONCURRENCY")
    evidence = _evidence("src/race.c", 1, "void unsafe_write(void) { shared_state++; }", "The unsynchronized shared_state write remains reachable.", "unsafe_write")
    result = _result("STILL_PRESENT", remaining=[evidence]).model_copy(update={"current_path_edges": _indexed_path_to(indexed, "unsafe_write")})

    checked = validate_fix_verification(result, files, indexed.symbols, finding, current_relations=indexed.relations)

    assert checked.verdict == "STILL_PRESENT"


def test_am_resource_relations_retain_api_operation_and_resource_identity():
    from app.indexer import FirmwareIndexer

    source = """void guarded(void) {
    xSemaphoreTake(shared_mutex, portMAX_DELAY);
    unsafe_write();
    xSemaphoreGive(shared_mutex);
}
void unsafe_write(void) { shared_state++; }
"""
    indexed = FirmwareIndexer().index([{"path": "src/race.c", "content": source}], project_id="FS-RESOURCE-META")
    operations = [item for item in indexed.relations if item["relation_kind"] == "USES_RESOURCE"]

    assert [(item["metadata"]["api"], item["metadata"]["operation"], item["metadata"]["resource"]) for item in operations] == [
        ("xSemaphoreTake", "LOCK", "shared_mutex"),
        ("xSemaphoreGive", "UNLOCK", "shared_mutex"),
    ]
    assert [item["line"] for item in operations] == [2, 4]
