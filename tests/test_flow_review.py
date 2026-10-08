"""Regression tests for flow-first Investigator review planning and context."""

import json

from app.flow_review_service import FlowReviewPlanner
from app.flow_service import FlowService
from app.indexer import FirmwareIndexer
from app.platform_repository import PlatformRepository
from app.platform_schemas import ProjectCreate, ReviewCreate
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


def _setup(tmp_path, files, name="Flow review"):
    database = str(tmp_path / f"{name.replace(' ', '-')}.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name=name, description=""))
    projects.add_files(project.id, files)
    projects.index(project.id)
    flow = FlowService(repository)
    planner = FlowReviewPlanner(repository, flow)
    units = planner.plan(project.id, repository.raw_files(project.id))
    return database, repository, projects, project, flow, units


def test_fr_a_cross_file_flow_is_one_multi_symbol_review_unit(tmp_path):
    _, _, _, project, _, units = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/main.c": "void foo(void); void app_main(void) { foo(); }\n",
        "src/foo.c": "void bar(void); void foo(void) { bar(); }\n",
        "src/sink.c": "void unsafe_write(void) {} void bar(void) { unsafe_write(); }\n",
    })
    flow = next(unit for unit in units if unit.unit_kind.startswith("FLOW") and unit.entry_name == "app_main" and "unsafe_write" in {segment.symbol for segment in unit.source_segments})
    assert flow.project_id == project.id
    assert {segment.symbol for segment in flow.source_segments} >= {"app_main", "foo", "bar", "unsafe_write"}
    assert {segment.file for segment in flow.source_segments} == {"src/main.c", "src/foo.c", "src/sink.c"}
    assert [edge.kind for edge in flow.execution_edges] == ["CALLS", "CALLS", "CALLS"]


def test_fr_b_and_investigator_context_carries_validation_and_cross_file_sink(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/http.c": "void parse(char *data); void http_handler(void) { char payload[32]; int len = recv(fd, payload, 32, 0); if (len < 32) parse(payload); } void app_main(void) { http_handler(); }\n",
        "src/parser.c": "void parse(char *data) { memcpy(dst, data, 64); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    batch = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "parse" in (item.flow_symbols or []))
    assert "http_handler" in batch.context and "memcpy" in batch.context
    assert "VALIDATES" in batch.context and "src/http.c" in batch.context and "src/parser.c" in batch.context


def test_fr_c_other_observed_caller_is_added_to_verifier_context(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/http.c": "void parse(char *data); void http_handler(void) { if (len < 8) parse(payload); } void app_main(void) { http_handler(); mqtt_handler(); }\n",
        "src/mqtt.c": "void parse(char *data); void mqtt_handler(void) { parse(payload); }\n",
        "src/parse.c": "void parse(char *data) { memcpy(dst, data, 64); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    batch = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "parse" in (item.flow_symbols or []))
    from app.platform_schemas import FindingCandidate
    candidate = FindingCandidate.model_validate({
        "title": "Parser copy reads beyond capacity", "classification": "CONFIRMED_BUG", "severity": "high", "category": "MEMORY_SAFETY", "confidence": .9,
        "location": {"file": "src/parse.c", "function": "parse", "line_start": 1, "line_end": 1}, "summary": "The parser copies a fixed oversized amount.",
        "evidence": [{"description": "copy size exceeds the buffer", "file": "src/parse.c", "line": 1}], "execution_path": ["parse", "memcpy"],
        "runtime_scenario": "An external payload reaches the fixed-size parser copy.", "impact": "The destination buffer can be overwritten.", "assumptions": [], "recommendation": "bound the copy",
    })
    context = service._cross_flow_support(project.id, candidate, batch)
    assert "HTTP" in context or "app_main" in context
    assert "mqtt_handler" in context


def test_fr_d_async_queue_continues_to_consumer_and_e_no_unrelated_queue_join(tmp_path):
    _, _, _, project, _, units = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/tasks.c": "typedef void *QueueHandle_t; QueueHandle_t queue_a, queue_b; void sensor_task(void *p) { int value = 1; xQueueSend(queue_a, &value, 0); } void mqtt_task(void *p) { int local; xQueueReceive(queue_a, &local, 0); build_payload(); } void build_payload(void) { mqtt_publish(); } void other_task(void *p) { int local; xQueueReceive(queue_b, &local, 0); } void app_main(void) { xTaskCreate(sensor_task, \"s\", 1, 0, 1, 0); xTaskCreate(mqtt_task, \"m\", 1, 0, 1, 0); xTaskCreate(other_task, \"o\", 1, 0, 1, 0); }\n",
    })
    composite = [unit for unit in units if len(unit.async_edges) == 2 and {edge.metadata.get("resource") for edge in unit.async_edges} == {"queue_a"}]
    assert composite
    assert any("mqtt_task" in {segment.symbol for segment in unit.source_segments} for unit in composite)
    assert not any({edge.metadata.get("resource") for edge in unit.async_edges} == {"queue_a", "queue_b"} for unit in units)


def test_fr_f_async_cycle_is_bounded_and_g_orphan_source_has_fallback_unit(tmp_path):
    _, _, _, project, _, units = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/tasks.c": "typedef void *QueueHandle_t; QueueHandle_t q1, q2; void task_a(void *p) { int x; xQueueReceive(q2, &x, 0); xQueueSend(q1, &x, 0); } void task_b(void *p) { int x; xQueueReceive(q1, &x, 0); xQueueSend(q2, &x, 0); } void orphan_helper(void) { int value = 4; (void)value; } void app_main(void) { xTaskCreate(task_a, \"a\", 1, 0, 1, 0); xTaskCreate(task_b, \"b\", 1, 0, 1, 0); }\n",
    })
    assert len(units) < 64
    assert any(unit.async_edges and unit.truncated for unit in units)
    assert any(unit.unit_kind == "ORPHAN_SOURCE" and "orphan_helper" in "".join(segment.content for segment in unit.source_segments) for unit in units)


def test_fr_h_source_lines_all_covered_by_flow_or_fallback_units(tmp_path):
    _, repository, _, project, _, units = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/mixed.c": "void reached(void) { work(); } void work(void) {} void app_main(void) { reached(); }\nvoid helper(void) { isolated(); }\n",
        "src/unresolved.c": "void (*dynamic_target)(void); void dispatcher(void) { dynamic_target(); }\n",
    })
    covered = {segment.file: set() for unit in units for segment in unit.source_segments}
    for unit in units:
        for segment in unit.source_segments:
            covered.setdefault(segment.file, set()).update(range(segment.line_start, segment.line_end + 1))
    for file in repository.raw_files(project.id):
        if file["path"] == "sdkconfig":
            continue
        assert set(range(1, len(file["content"].splitlines()) + 1)) <= covered[file["path"]]


def test_fr_i_value_flow_only_links_observed_identifier_propagation(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/net.c": "void parse(char *data); void socket_handler(void) { char payload[64]; int len = recv(fd, payload, 64, 0); if (len > 0) parse(payload); } void parse(char *data) { if (data != NULL) memcpy(dst, data, 48); } void app_main(void) { socket_handler(); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    batch = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "parse" in (item.flow_symbols or []))
    assert "SOCKET:payload" in batch.context
    assert "memcpy" in batch.context
    assert "src/net.c:" in batch.context
    assert "SOCKET:payload" in batch.context and "parse:data" in batch.context
    assert "VALIDATED_BEFORE_SINK" in batch.context


def test_fr_j_value_flow_stops_at_complex_argument_transform(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/net.c": "void parse(char *data); void socket_handler(void) { char payload[64]; recv(fd, payload, 64, 0); parse(transform(payload)); } void parse(char *data) { memcpy(dst, data, 48); } void app_main(void) { socket_handler(); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    batch = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "parse" in (item.flow_symbols or []))
    assert "SOCKET" in batch.context and "memcpy" in batch.context
    assert "SOCKET:payload @" in batch.context
    assert "→ memcpy" not in batch.context


def test_fr_k_validation_fact_respects_source_order(tmp_path):
    source_before = "void handler(void) { char payload[16]; int len = recv(fd, payload, 64, 0); if (len <= 16) memcpy(dst, payload, len); } void app_main(void) { handler(); }\n"
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n", "src/net.c": source_before,
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    before = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "handler" in (item.flow_symbols or []))
    assert "VALIDATED_BEFORE_SINK" in before.context

    projects.add_files(project.id, {"src/net.c": "void handler(void) { char payload[16]; int len = recv(fd, payload, 64, 0); memcpy(dst, payload, len); if (len <= 16) { (void)len; } } void app_main(void) { handler(); }\n"})
    projects.index(project.id)
    after = next(item for item in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if item.unit_kind.startswith("FLOW") and "handler" in (item.flow_symbols or []))
    assert "NO_TRACKED_VALIDATION_BEFORE_SINK" in after.context


def test_fr_o_review_units_are_project_scoped_for_identical_symbols(tmp_path):
    database = str(tmp_path / "flow-isolation.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    flow = FlowService(repository)
    first = projects.create(ProjectCreate(name="Project A", description=""))
    second = projects.create(ProjectCreate(name="Project B", description=""))
    projects.add_files(first.id, {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n", "src/main.c": "void shared_helper(void) { int marker_a = 1; (void)marker_a; } void app_main(void) { shared_helper(); }\n"})
    projects.add_files(second.id, {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n", "src/main.c": "void shared_helper(void) { int marker_b = 2; (void)marker_b; } void app_main(void) { shared_helper(); }\n"})
    projects.index(first.id)
    projects.index(second.id)

    first_units = FlowReviewPlanner(repository, flow).plan(first.id, repository.raw_files(first.id))
    second_units = FlowReviewPlanner(repository, flow).plan(second.id, repository.raw_files(second.id))
    first_unit = next(unit for unit in first_units if unit.unit_kind.startswith("FLOW") and unit.entry_name == "app_main")
    second_unit = next(unit for unit in second_units if unit.unit_kind.startswith("FLOW") and unit.entry_name == "app_main")
    assert first_unit.project_id == first.id and second_unit.project_id == second.id
    assert "marker_a" in "".join(segment.content for segment in first_unit.source_segments)
    assert "marker_b" in "".join(segment.content for segment in second_unit.source_segments)
    assert "marker_b" not in "".join(segment.content for segment in first_unit.source_segments)
    assert "marker_a" not in "".join(segment.content for segment in second_unit.source_segments)


def test_fr_m_topology_change_changes_flow_unit_cache_identity(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/main.c": "void callback(void) {} void app_main(void) { callback(); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    before = next(unit for unit in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if unit.unit_kind.startswith("FLOW"))
    projects.add_files(project.id, {"src/main.c": "void callback(void) {} void app_main(void) { callback(); callback(); }\n"})
    projects.index(project.id)
    after = next(unit for unit in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if unit.unit_kind.startswith("FLOW"))
    assert before.unit_id != after.unit_id
    assert before.flow_fingerprint != after.flow_fingerprint


def test_fr_m_topology_only_change_invalidates_scenario_cache_identity(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/main.c": "void callback(void) {} void app_main(void) { callback(); }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    before = next(unit for unit in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if unit.unit_kind.startswith("FLOW") and "app_main" in (unit.flow_symbols or []))
    relations = repository.list_source_relations(project.id)
    call = next(item for item in relations if item["relation_kind"] == "CALLS")
    call["relation_state"] = "INFERRED"
    call["confidence"] = 0.35
    snapshot = repository.topology_snapshot(project.id)
    repository.replace_topology(
        project.id, repository.list_indexed_symbols(project.id), relations,
        repository.list_allocation_events(project.id),
        source_snapshot_hash=snapshot["source_snapshot_hash"],
        relation_fingerprint="topology-only-change", indexed_at="2026-10-08T00:00:00+00:00",
    )
    after = next(unit for unit in service._review_units(project.id, "Flow review", [], repository.raw_files(project.id)) if unit.unit_kind.startswith("FLOW") and "app_main" in (unit.flow_symbols or []))
    assert before.unit_id != after.unit_id


def test_fr_n_orphan_source_change_preserves_unrelated_flow_cache_identity(tmp_path):
    database, repository, projects, project, flow, _ = _setup(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/main.c": "void reachable(void) {} void app_main(void) { reachable(); } void orphan_helper(void) { int state = 1; (void)state; }\n",
    })
    service = ReviewService(repository, projects, lambda role: None, MemoryService(MemoryRepository(database)), flow_service=flow)
    before_units = service._review_units(project.id, "Flow review", [], repository.raw_files(project.id))
    before_flow = next(unit for unit in before_units if unit.unit_kind.startswith("FLOW") and "app_main" in (unit.flow_symbols or []))
    before_orphan = next(unit for unit in before_units if unit.unit_kind == "ORPHAN_SOURCE" and "orphan_helper" in unit.context)
    projects.add_files(project.id, {"src/main.c": "void reachable(void) {} void app_main(void) { reachable(); } void orphan_helper(void) { int state = 2; (void)state; }\n"})
    projects.index(project.id)
    after_units = service._review_units(project.id, "Flow review", [], repository.raw_files(project.id))
    after_flow = next(unit for unit in after_units if unit.unit_kind.startswith("FLOW") and "app_main" in (unit.flow_symbols or []))
    after_orphan = next(unit for unit in after_units if unit.unit_kind == "ORPHAN_SOURCE" and "orphan_helper" in unit.context)
    assert before_flow.unit_id == after_flow.unit_id
    assert before_orphan.unit_id != after_orphan.unit_id


class CapturingProvider:
    name = "capture"

    def __init__(self):
        self.prompts = []
        self.model = "fixture"

    def available(self):
        return True

    def chat(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        if "Investigator" in system_prompt:
            return json.dumps({"findings": []})
        if "Verifier" in system_prompt:
            return json.dumps({"verdict": "REJECTED", "notes": "No candidate was supplied for verification."})
        return "{}"


def test_flow_first_review_sends_cross_file_unit_to_investigator_and_persists_plan(tmp_path, monkeypatch):
    provider = CapturingProvider()
    captured = []
    def structured(_provider, schema, _system, user_prompt, **_kwargs):
        if schema.__name__ == "InvestigatorResult":
            captured.append(user_prompt)
            from app.platform_schemas import InvestigatorResult
            return InvestigatorResult(findings=[])
        from app.platform_schemas import VerifierResult
        return VerifierResult(verdict="REJECTED", notes="No candidate was present to verify.")
    monkeypatch.setattr("app.review_service.structured_response", structured)
    database = str(tmp_path / "review-run.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Flow review run", description=""))
    files = {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/http.c": "void parse(char *data); void handler(void) { char payload[32]; int len = recv(fd, payload, 32, 0); if (len < 32) parse(payload); } void app_main(void) { handler(); }\n",
        "src/parser.c": "void parse(char *data) { memcpy(dst, data, 48); }\n",
    }
    projects.add_files(project.id, files)
    projects.index(project.id)
    flow = FlowService(repository)
    reviews = ReviewService(repository, projects, lambda _role: provider, MemoryService(MemoryRepository(database)), flow_service=flow)
    reviews._run_learning = lambda *args, **kwargs: None
    reviews._sync_knowledge_vault = lambda *args, **kwargs: None
    review = reviews.begin(project.id, ReviewCreate(scope="Full Project", focus=["memory"]))
    reviews.execute(review.id)
    flow_request = next(user for user in captured if "FLOW REVIEW UNIT" in user)
    assert "src/http.c" in flow_request and "src/parser.c" in flow_request
    assert "VALIDATES" in flow_request and "memcpy" in flow_request
    persisted = repository.get_review(review.id)
    assert persisted["review_units"]
    assert any(unit["unit_kind"].startswith("FLOW") for unit in persisted["review_units"])
    assert persisted["source_coverage"]["source_coverage_percent"] == 100
    assert persisted["source_coverage"]["flow_coverage_percent"] > 0
