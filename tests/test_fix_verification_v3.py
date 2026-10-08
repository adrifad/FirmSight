from __future__ import annotations

from datetime import UTC, datetime

from app.fix_verification import build_differential_context, validate_fix_verification
from app.platform_schemas import FindingRead, FixVerificationEvidence, FixVerificationResult


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


def _symbol(name: str, file: str, start: int, end: int, *, kind: str = "function", source_hash: str = "a" * 64) -> dict:
    return {"id": f"{file}:{name}", "name": name, "kind": kind, "file": file, "line_start": start, "line_end": end, "source_hash": source_hash}


def _relation(source: str, target: str, file: str, line: int, kind: str = "CALL") -> dict:
    return {"id": f"{source}>{target}", "source_symbol_id": source, "target_symbol_id": target, "target_name": target.split(":")[-1], "relation_kind": kind, "relation_state": "OBSERVED", "file": file, "line": line}


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
    assert checked.verdict == "FIXED"
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
    source = "void unsafe_read(void) {\n  shared_state++;\n}\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("unsafe_read", "src/main.c", 1, 3)]
    result = _result("STILL_PRESENT", remaining=[_evidence("src/main.c", 2, "  shared_state++;", "The current function mutates shared state.", "unsafe_read")])
    assert validate_fix_verification(result, files, symbols, _finding(line=2)).verdict == "STILL_PRESENT"


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
    source = "void renamed_unsafe_read(void) { shared_state++; }\n"
    files = [{"path": "src/moved.c", "content": source}]
    symbols = [_symbol("renamed_unsafe_read", "src/moved.c", 1, 1)]
    finding = _finding(file="src/old.c", function="unsafe_read")
    context = build_differential_context(finding, [{"path": "src/old.c", "content": "void unsafe_read(void) { shared_state++; }\n"}], files, current_symbols=symbols)
    assert "renamed_unsafe_read" in context
    result = _result("STILL_PRESENT", remaining=[_evidence("src/moved.c", 1, source, "Moved code retains the unsafe shared write.", "renamed_unsafe_read")])
    assert validate_fix_verification(result, files, symbols, finding).verdict == "STILL_PRESENT"


def test_h_removed_feature_has_current_caller_evidence_for_fix():
    source = "esp_err_t app_main(void) {\n    return ESP_OK;\n}\n"
    files = [{"path": "src/main.c", "content": source}]
    symbols = [_symbol("app_main", "src/main.c", 1, 3)]
    finding = _finding(function="unsafe_read", line=2)
    context = build_differential_context(
        finding,
        [{"path": "src/main.c", "content": "esp_err_t app_main(void) { unsafe_read(); }\nvoid unsafe_read(void) { shared_state++; }\n"}],
        files,
        current_symbols=symbols,
    )
    assert "unsafe_read" in context
    result = _result("FIXED", failure="The removed feature previously reached an unsafe shared write.", current_path=["app_main now returns without entering the removed path."], mitigations=[_evidence("src/main.c", 2, "return ESP_OK;", "Current entry point no longer enters the vulnerable feature.", "app_main")])
    assert validate_fix_verification(result, files, symbols, finding).verdict == "FIXED"


def test_i_api_representation_change_can_break_original_invalid_length_condition():
    source = "void bounded_view(Span input) { copy_span(input); }\n"
    files = [{"path": "src/buffer.c", "content": source}]
    symbols = [_symbol("bounded_view", "src/buffer.c", 1, 1)]
    result = _result("FIXED", failure="An invalid caller length previously reached the buffer copy.", current_path=["caller -> bounded_view(Span) -> copy_span uses the validated span length"], mitigations=[_evidence("src/buffer.c", 1, source, "Span carries the bounded extent into the copy operation.", "bounded_view")], alternative=True)
    checked = validate_fix_verification(result, files, symbols, _finding(function="bounded_view", line=1))
    assert checked.verdict == "FIXED"
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
