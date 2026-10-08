from fastapi.testclient import TestClient
from datetime import UTC, datetime

from app.context_builder import ContextBuilder
from app.fix_verification import capture_finding_baseline
from app.flow_service import FlowService
from app.indexer import FirmwareIndexer
from app.knowledge_retriever import KnowledgeRetriever
from app.main import create_app
from app.platform_repository import PlatformRepository
from app.platform_schemas import FindingRead, ProjectCreate
from app.project_service import ProjectService
from app.repository import MemoryRepository


def _project(tmp_path, files, name="Flow fixture"):
    repository = PlatformRepository(str(tmp_path / f"{name.replace(' ', '-')}.db"))
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name=name, description=""))
    projects.add_files(project.id, files)
    projects.index(project.id)
    return repository, projects, project


def test_flow_a_cross_file_calls_form_one_entry_rooted_flow(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y\n",
        "src/main.c": '#include "esp_err.h"\nvoid bar(void); void foo(void); void app_main(void) { foo(); }',
        "src/foo.c": "void bar(void); void foo(void) { bar(); }",
        "src/sink.c": "void unsafe_write(void) {} void bar(void) { unsafe_write(); }",
    })
    flows = FlowService(repository).build(project.id)
    match = next(item for item in flows.scenarios if item.path == ["app_main", "foo", "bar", "unsafe_write"])
    assert match.entry_kind == "APP_ENTRY"
    assert all(edge.relation_state == "OBSERVED" for edge in match.edges)
    assert len({node.file for node in match.nodes if node.file}) == 3


def test_flow_b_mqtt_callback_registration_roots_scenario(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/main.c": "void mqtt_event_handler(void); void init(void) { esp_mqtt_client_register_event(client, 0, mqtt_event_handler, 0); }",
        "src/mqtt.c": "void unsafe_copy(void) {} void parse_payload(void) { unsafe_copy(); } void mqtt_event_handler(void) { parse_payload(); }",
    })
    flows = FlowService(repository).build(project.id)
    assert any(item.entry_kind == "CALLBACK_ENTRY" and item.path == ["mqtt_event_handler", "parse_payload", "unsafe_copy"] for item in flows.scenarios)


def test_flow_g_enumerates_each_task_caller_reaching_shared_sink(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/tasks.c": "void unsafe_write(void) {} void task_a(void *p) { unsafe_write(); } void task_b(void *p) { unsafe_write(); } void app_main(void) { xTaskCreate(task_a, \"a\", 1, 0, 1, 0); xTaskCreate(task_b, \"b\", 1, 0, 1, 0); }",
    })
    paths = FlowService(repository).paths_to_symbol(project.id, "unsafe_write")
    assert {item.entry_name for item in paths} == {"task_a", "task_b"}


def test_flow_c_and_d_queue_async_flow_requires_exact_queue_identity(tmp_path):
    source = '''
typedef void *QueueHandle_t;
QueueHandle_t measurement_queue;
QueueHandle_t other_queue;
void sensor_task(void *arg) { int copy = 1; xQueueSend(measurement_queue, &copy, 0); }
void mqtt_task(void *arg) { int local; xQueueReceive(measurement_queue, &local, 0); }
void unrelated_task(void *arg) { int local; xQueueReceive(other_queue, &local, 0); }
void app_main(void) { measurement_queue = xQueueCreate(4, sizeof(int)); xTaskCreate(sensor_task, "sensor", 1, 0, 1, 0); xTaskCreate(mqtt_task, "mqtt", 1, 0, 1, 0); xTaskCreate(unrelated_task, "other", 1, 0, 1, 0); }
'''
    repository, _, project = _project(tmp_path, {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y", "src/queue.c": source})
    flows = FlowService(repository).build(project.id)
    sensor = next(item for item in flows.scenarios if item.path[-1:] == ["sensor_task"])
    kinds = {edge.kind for edge in sensor.async_edges}
    assert kinds == {"PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE"}
    assert {edge.metadata["resource"] for edge in sensor.async_edges} == {"measurement_queue"}
    assert all(edge.metadata.get("copy_semantics") == "FREERTOS_VALUE_COPY" for edge in sensor.async_edges)
    measurement_queue = next(item for item in flows.resources if item.identity == "measurement_queue")
    assert any(edge.kind == "CREATES_RESOURCE" for edge in measurement_queue.operations)
    other = next(item for item in flows.resources if item.identity == "other_queue")
    assert {edge.kind for edge in other.operations} == {"RECEIVES_FROM_QUEUE"}


def test_flow_e_unresolved_function_pointer_is_exposed_not_traversed(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/main.c": "void (*callback)(void); void app_main(void) { callback(); }",
    })
    flows = FlowService(repository).build(project.id)
    assert flows.unresolved_edges
    assert all(edge.relation_state in {"INFERRED", "UNKNOWN"} for edge in flows.unresolved_edges)
    assert not any("callback" in scenario.path for scenario in flows.scenarios)
    assert any(node.kind == "UNKNOWN_TARGET" for scenario in flows.scenarios for node in scenario.nodes)


def test_flow_f_and_h_i_k_validation_global_state_and_writers(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/state.c": '''
static int global_config = 0;
static int system_state = 0;
void writer_a(void) { global_config = 1; system_state = READY; }
void writer_b(void) { global_config++; }
int reader(void) { if (length <= sizeof(buffer)) return global_config; return 0; }
void app_main(void) { writer_a(); writer_b(); reader(); }
''',
    })
    indexed = repository.list_source_relations(project.id)
    assert any(item["relation_kind"] == "VALIDATES" and "length" in item["metadata"].get("identifiers", "") for item in indexed)
    assert any(item["relation_kind"] == "DATA_SINK" for item in indexed) is False
    flows = FlowService(repository).build(project.id)
    state = next(item for item in flows.shared_state if item.name == "global_config")
    assert set(state.writers) == {"writer_a", "writer_b"}
    assert "reader" in state.readers
    system_state = next(item for item in flows.shared_state if item.name == "system_state")
    assignment = next(item for item in system_state.accesses if item.kind == "WRITES")
    assert assignment.metadata.get("new_state") == "READY"
    assert any(item["relation_kind"] == "CHANGES_STATE" and item["metadata"].get("new_state") == "READY" for item in indexed)


def test_flow_j_untrusted_input_is_labelled_from_api_evidence(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/input.c": "void mqtt_event_handler(void) { recv(socket_fd, payload, length, 0); } void app_main(void) { esp_mqtt_client_register_event(client, 0, mqtt_event_handler, 0); }",
    })
    relations = repository.list_source_relations(project.id)
    facts = [item for item in relations if item["relation_kind"] == "UNTRUSTED_INPUT"]
    assert any(item["metadata"].get("source_kind") == "SOCKET" for item in facts)
    assert any(item["metadata"].get("source_kind") == "MQTT_EVENT" for item in facts)


def test_flow_j_callback_looking_name_requires_registration_and_detects_http_handler(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/http.c": '''
typedef struct { void (*handler)(void); } httpd_uri_t;
void unsafe_parse(void) {}
void handler(void) { unsafe_parse(); }
void mqtt_event_handler(void) { unsafe_parse(); }
void init(void) {
  httpd_uri_t uri = { .handler = handler };
  httpd_register_uri_handler(server, &uri);
}
void app_main(void) { init(); }
''',
    })
    flows = FlowService(repository).build(project.id)
    assert any(item.entry_kind == "REGISTERED_HANDLER" and "handler" in item.path for item in flows.scenarios)
    assert not any(item.entry_name == "mqtt_event_handler" for item in flows.scenarios)


def test_flow_m_cycles_terminate_and_n_scenario_cap_is_explicit(tmp_path):
    cycle = "void app_main(void) { a(); } void a(void) { b(); } void b(void) { c(); } void c(void) { a(); }"
    repository, _, project = _project(tmp_path, {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y", "src/cycle.c": cycle})
    flows = FlowService(repository).build(project.id)
    assert len(flows.scenarios) == 1
    assert flows.scenarios[0].path == ["app_main", "a", "b", "c"]
    many = "void app_main(void) { f0(); }" + "".join(f"void f{i}(void) {{ f{i+1}(); }}" for i in range(18)) + "void f18(void) {}"
    repository, _, project = _project(tmp_path, {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y", "src/branch.c": many}, "Bounded flow")
    flows = FlowService(repository).build(project.id)
    assert len(flows.scenarios) <= 128
    assert flows.truncated is True
    assert any(item.max_depth_reached for item in flows.scenarios)


def test_flow_n_high_fanout_respects_per_entry_path_cap(tmp_path):
    leaves = "".join(f"void leaf_{index}(void) {{}}" for index in range(16))
    calls = "".join(f"leaf_{index}();" for index in range(16))
    source = f"void app_main(void) {{ branch(); }} void branch(void) {{ {calls} }} {leaves}"
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/branch.c": source,
    })
    flows = FlowService(repository).build(project.id)
    assert len(flows.scenarios) <= 8
    assert flows.truncated is True


def test_flow_o_and_p_context_builder_includes_path_and_validation_facts(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/http.c": "void sink(char *buf) { memcpy(dst, buf, len); } int parse(char *payload) { if (len < 8) sink(payload); return len; } void http_handler(void) { char payload[32]; recv(fd, payload, 32, 0); char *normalized = payload; int result = parse(normalized); (void)result; } void app_main(void) { http_handler(); }",
    })
    builder = ContextBuilder(repository, KnowledgeRetriever(MemoryRepository(repository.database_path), repository))
    context = builder.build(project.id, "flow validation", symbols=["sink"], max_chars=8000)["text"]
    assert "app_main -> http_handler -> parse -> sink" in context
    assert all(kind in context for kind in ("UNTRUSTED_INPUT", "PARAMETER", "ASSIGNS", "PROPAGATES_ARGUMENT", "RETURNS_VALUE", "CAPTURES_RETURN", "VALIDATES", "DATA_SINK"))
    flow = FlowService(repository).build(project.id)
    scenario = next(item for item in flow.scenarios if item.path[-1] == "sink")
    assert {edge.kind for edge in scenario.data_edges} >= {"VALIDATES", "DATA_SINK"}


def test_flow_api_exposes_scenarios_and_isolates_projects(tmp_path):
    api = TestClient(create_app(database_path=str(tmp_path / "api.db")))
    project_a = api.post("/api/projects", json={"name": "Flow A", "description": ""}).json()
    project_b = api.post("/api/projects", json={"name": "Flow B", "description": ""}).json()
    source = {"sdkconfig": "CONFIG_IDF_TARGET_ESP32=y", "src/main.c": "void app_main(void) { reached(); } void reached(void) {}"}
    assert api.post(f"/api/projects/{project_a['id']}/files", json={"files": source}).status_code == 200
    assert api.post(f"/api/projects/{project_b['id']}/files", json={"files": {"src/empty.c": "void idle(void) {}"}}).status_code == 200
    response = api.get(f"/api/projects/{project_a['id']}/flows")
    assert response.status_code == 200
    assert response.json()["scenarios"][0]["path"] == ["app_main", "reached"]
    assert api.get(f"/api/projects/{project_b['id']}/flows").json()["scenarios"] == []
    paths = api.get(f"/api/projects/{project_a['id']}/flow/paths-to-symbol", params={"symbol": "reached"})
    assert paths.status_code == 200 and len(paths.json()) == 1
    exact_file = api.get(f"/api/projects/{project_a['id']}/flow/paths-to-symbol", params={"symbol": "reached", "file": "src/other.c"})
    assert exact_file.status_code == 200 and exact_file.json() == []


def test_flow_baseline_is_reused_when_a_finding_is_created(tmp_path):
    repository, _, project = _project(tmp_path, {
        "sdkconfig": "CONFIG_IDF_TARGET_ESP32=y",
        "src/main.c": "void unsafe_read(void) {} void app_main(void) { unsafe_read(); }",
    })
    symbols = repository.list_indexed_symbols(project.id)
    relations = repository.list_source_relations(project.id)
    flow = FlowService(repository).build(project.id)
    finding = FindingRead.model_validate({
        "id": "FS-001", "project_id": project.id, "review_id": "REV-1",
        "title": "Unsafe shared access", "classification": "CONFIRMED_BUG", "severity": "high",
        "category": "CONCURRENCY", "confidence": 0.9,
        "location": {"file": "src/main.c", "function": "unsafe_read", "line_start": 1, "line_end": 1},
        "summary": "A reachable path can access shared mutable state unsafely.",
        "evidence": [{"description": "The operation is unguarded.", "file": "src/main.c", "line": 1}],
        "execution_path": ["app_main", "unsafe_read"], "runtime_scenario": "The path executes during normal boot.",
        "impact": "Shared state can be corrupted.", "assumptions": [], "recommendation": "Protect the shared state.",
        "verification": {"status": "PASSED", "notes": "Candidate survived."}, "decision": "ACCEPTED",
        "topology_path": [], "lifetime_evidence": [], "decision_reason": None, "resolution": "OPEN",
        "remediation": {"status": "UNVERIFIED"}, "created_at": datetime.now(UTC).isoformat(),
    })
    baseline = capture_finding_baseline(
        finding, repository.raw_files(project.id), symbols, relations,
        source_snapshot_hash="snapshot-original", flow_scenarios=flow.scenarios,
    )
    assert baseline.relevant_paths
    assert baseline.relevant_paths[0].entry_name == "app_main"
    assert baseline.relevant_paths[0].entry_relation == "APP_ENTRY"
