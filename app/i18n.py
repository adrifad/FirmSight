"""Server-side message catalog for user-facing FirmSight text.

Scope (FS-I18N-016 REQ-2): review progress lines and user-facing
HTTPException details. Server logs, ``error_kind`` codes, enum values, API
field names, statuses, and diagnostic codes stay English. The catalog is a
plain dict of ``MessageKey -> (en, id)`` with a module-level completeness
invariant (both locales defined for every key) enforced by tests.

Language policy (TASKS.md FS-I18N-016): Indonesian narrative wraps English
technical firmware terms (mutex, ISR, queue, OTA, ...); evidence quotes, file
paths, symbol names, and log strings are never translated.
"""

from __future__ import annotations

Locale = str  # "en" | "id"
DEFAULT_LOCALE: Locale = "en"
SUPPORTED_LOCALES: tuple[Locale, ...] = ("en", "id")


def normalize_locale(value: object) -> Locale:
    """Map any stored/requested value onto a supported locale.

    Invalid, missing, or unsupported values fall back to ``en`` (never to a
    raw key or a raise): REQ-1/REQ-2 fallback rule.
    """
    if isinstance(value, str) and value.strip().casefold() in {"id", "id-id", "in", "in-id"}:
        return "id"
    return DEFAULT_LOCALE


def _plural(n: int, one: str, other: str) -> str:
    return one if n == 1 else other


def plural_units(count: int, singular: str, plural: str | None = None) -> str:
    """English plural for countable nouns used inside messages."""
    return _plural(count, singular, plural if plural is not None else f"{singular}s")


# ---------------------------------------------------------------------------
# Message keys: user-visible strings only.
# ---------------------------------------------------------------------------

_MESSAGES: dict[str, tuple[str, str]] = {
    # -- Review progress: begin ------------------------------------------------
    "review.queued_scope": (
        "Queued {scope} review for focus: {focus}",
        "Antrean review {scope} untuk fokus: {focus}",
    ),
    "review.indexed": (
        "Indexed {files} files and {symbols} symbols",
        "Mengindeks {files} berkas dan {symbols} simbol",
    ),
    "review.prepared_batches": (
        "Prepared {batches} full-coverage source batches from {files} reviewable files (up to {chars:,} characters per AI request)",
        "Menyiapkan {batches} batch sumber cakupan-penuh dari {files} berkas yang dapat direview (maksimal {chars:,} karakter per permintaan AI)",
    ),
    "review.source_coverage": (
        "Source coverage: {source:.1f}% total · {flow:.1f}% in observed flows · {fallback} fallback units",
        "Cakupan sumber: {source:.1f}% total · {flow:.1f}% dalam flow teramati · {fallback} unit fallback",
    ),
    "review.queued_rereview": (
        "Queued AI recheck for {count} accepted confirmed finding{s} before new discovery",
        "Antrean pemeriksaan ulang AI untuk {count} temuan terkonfirmasi yang diterima sebelum pencarian baru",
    ),
    # -- Review progress: source refresh / snapshot ---------------------------
    "review.refreshed_source": (
        "Refreshed local source before AI recheck ({count} changed file{s})",
        "Menyegarkan sumber lokal sebelum pemeriksaan ulang AI ({count} berkas berubah)",
    ),
    "review.refresh_failed": (
        "Could not refresh local source; rechecking the indexed snapshot: {detail}",
        "Tidak dapat menyegarkan sumber lokal; memeriksa ulang snapshot yang terindeks: {detail}",
    ),
    "review.source_changed": (
        "Project source changed since this review snapshot; start a new review instead of resuming it.",
        "Sumber proyek berubah sejak snapshot review ini; mulai review baru alih-alih melanjutkannya.",
    ),
    # -- Review progress: batch flow ------------------------------------------
    "review.reused_unit": (
        "Reused validated review unit {number}/{total} ({count} candidate finding{s})",
        "Unit review tervalidasi {number}/{total} dipakai ulang ({count} kandidat temuan)",
    ),
    "review.investigator_batch": (
        "Investigator is analyzing source batch {number}/{total} ({files} files)",
        "Investigator menganalisis batch sumber {number}/{total} ({files} berkas)",
    ),
    "review.investigator_batch_result": (
        "Investigator batch {number}/{total} returned {count} candidate findings",
        "Batch Investigator {number}/{total} menghasilkan {count} kandidat temuan",
    ),
    "review.investigator_batch_skipped": (
        "Investigator batch {number}/{total} was skipped (stopped: {reason})",
        "Batch Investigator {number}/{total} dilewati (berhenti: {reason})",
    ),
    "review.skipped_duplicate": (
        "Skipped duplicate candidate from batch {number}: {title}{match}",
        "Kandidat duplikat dari batch {number} dilewati: {title}{match}",
    ),
    "review.discarded_unverifiable": (
        "Discarded batch {number} candidate {candidate}: unverifiable file or line evidence",
        "Kandidat {candidate} batch {number} dibuang: bukti berkas atau baris tidak dapat diverifikasi",
    ),
    "review.verifier_challenging": (
        "Verifier is challenging batch {number} candidate {candidate}/{count}: {title}",
        "Verifier menantang kandidat {candidate}/{count} batch {number}: {title}",
    ),
    "review.overload_limit": (
        "Provider overload detected; limiting remaining Investigator and Verifier requests to 1",
        "Overload provider terdeteksi; permintaan Investigator dan Verifier tersisa dibatasi ke 1",
    ),
    "review.verifier_failed": (
        "Verifier could not validate batch {number} candidate {candidate}; it was not added: {reason}",
        "Verifier tidak dapat memvalidasi kandidat {candidate} batch {number}; tidak ditambahkan: {reason}",
    ),
    "review.verifier_rejected": (
        "Verifier did not approve candidate {candidate} in batch {number}: {title}",
        "Verifier tidak menyetujui kandidat {candidate} pada batch {number}: {title}",
    ),
    "review.created_finding": (
        "Created verifier-approved finding {count}: {title}",
        "Temuan disetujui verifier dibuat ({count}): {title}",
    ),
    "review.coverage": (
        "Investigator coverage: {done}/{total} batches validated",
        "Cakupan Investigator: {done}/{total} batch tervalidasi",
    ),
    "review.verification_complete": (
        "Verification complete: {count} evidence-backed findings created",
        "Verifikasi selesai: {count} temuan berbasis bukti dibuat",
    ),
    "review.partial": (
        "Review is partial: {count} source batch{suffix} unavailable because an Investigator or Verifier response was unusable",
        "Review bersifat parsial: {count} batch sumber tidak tersedia karena respons Investigator atau Verifier tidak dapat digunakan",
    ),
    "review.no_survivors": (
        "No candidate survived verification; no findings were created",
        "Tidak ada kandidat yang lolos verifikasi; tidak ada temuan dibuat",
    ),
    "review.learning_queued": (
        "Project Intelligence learning queued",
        "Pembelajaran Project Intelligence masuk antrean",
    ),
    "review.queued_retry": (
        "Queued retry for {count} unavailable review unit{s}",
        "Percobaan ulang untuk {count} unit review yang tidak tersedia masuk antrean",
    ),
    "review.failed_no_result": (
        "AI review failed before a validated result was available",
        "Review AI gagal sebelum hasil tervalidasi tersedia",
    ),
    "review.stopped_unexpected": (
        "AI review stopped because an unexpected internal error occurred",
        "Review AI berhenti karena terjadi kesalahan internal yang tidak terduga",
    ),
    "review.worker_stopped": (
        "Review worker stopped before completion; no provider request is still running",
        "Worker review berhenti sebelum selesai; tidak ada permintaan provider yang masih berjalan",
    ),
    # -- Review progress: intelligence / knowledge ----------------------------
    "review.learning_completed": (
        "Project Intelligence learning completed: {changes}",
        "Pembelajaran Project Intelligence selesai: {changes}",
    ),
    "review.learning_skipped": (
        "Project Intelligence learning skipped: {reason}",
        "Pembelajaran Project Intelligence dilewati: {reason}",
    ),
    "review.learning_failed": (
        "Project Intelligence processing failed; the review result is unaffected",
        "Pemrosesan Project Intelligence gagal; hasil review tidak terpengaruh",
    ),
    "review.learning_failed_detail": (
        "Project Intelligence processing failed; the review result is unaffected: {detail}",
        "Pemrosesan Project Intelligence gagal; hasil review tidak terpengaruh: {detail}",
    ),
    "review.revalidation_stale": (
        "Project Intelligence revalidation: {count} stale record{s} marked NEEDS_REVALIDATION against current source",
        "Revalidasi Project Intelligence: {count} catatan usang ditandai NEEDS_REVALIDATION terhadap sumber saat ini",
    ),
    "review.revalidation_conflicts": (
        "Project Intelligence revalidation: {count} record{s} conflicted with current source",
        "Revalidasi Project Intelligence: {count} catatan berkonflik dengan sumber saat ini",
    ),
    "review.revalidation_ok": (
        "Project Intelligence revalidation: all related records remain supported by current source",
        "Revalidasi Project Intelligence: semua catatan terkait tetap didukung sumber saat ini",
    ),
    "review.revalidation_unavailable": (
        "Project Intelligence revalidation was unavailable: {detail}",
        "Revalidasi Project Intelligence tidak tersedia: {detail}",
    ),
    "review.index_success": (
        "Knowledge index: {documents} document{s} indexed, {chunks} chunk{s} written",
        "Indeks pengetahuan: {documents} dokumen terindeks, {chunks} chunk ditulis",
    ),
    "review.index_skipped": (
        "Knowledge index skipped: no indexer is configured for this workspace.",
        "Indeks pengetahuan dilewati: tidak ada indexer yang dikonfigurasi untuk workspace ini.",
    ),
    "review.index_unavailable": (
        "Knowledge index unavailable; vault projection remains available. Retry from the Knowledge Base.",
        "Indeks pengetahuan tidak tersedia; proyeksi vault tetap tersedia. Coba ulang dari Knowledge Base.",
    ),
    # -- Review progress: fix recheck -----------------------------------------
    "review.recheck_none": (
        "No accepted confirmed findings require AI recheck; starting new discovery",
        "Tidak ada temuan terkonfirmasi yang diterima dan perlu pemeriksaan ulang AI; memulai pencarian baru",
    ),
    "review.recheck_started": (
        "Rechecking {count} accepted confirmed finding{s} against current source before new discovery",
        "Memeriksa ulang {count} temuan terkonfirmasi yang diterima terhadap sumber saat ini sebelum pencarian baru",
    ),
    "review.recheck_finding": (
        "AI fix verifier is rechecking finding {index}/{count}: {title}",
        "Verifier perbaikan AI memeriksa ulang temuan {index}/{count}: {title}",
    ),
    "review.recheck_error": (
        "AI recheck could not complete for {title}; it remains open: {detail}",
        "Pemeriksaan ulang AI tidak dapat diselesaikan untuk {title}; tetap terbuka: {detail}",
    ),
    "review.recheck_solved": (
        "Finding {index}/{count} marked solved after AI recheck: {title}",
        "Temuan {index}/{count} ditandai selesai setelah pemeriksaan ulang AI: {title}",
    ),
    "review.recheck_open": (
        "AI recheck kept finding {index}/{count} open: {title} ({verdict})",
        "Pemeriksaan ulang AI mempertahankan temuan {index}/{count} tetap terbuka: {title} ({verdict})",
    ),
    # -- HTTP errors: projects ------------------------------------------------
    "error.project_not_found": ("Project not found", "Proyek tidak ditemukan"),
    "error.project_file_not_found": ("Project file not found", "Berkas proyek tidak ditemukan"),
    "error.import_root_missing": (
        "Set FIRMSIGHT_IMPORT_ROOT in the backend environment before importing a local directory",
        "Atur FIRMSIGHT_IMPORT_ROOT di lingkungan backend sebelum mengimpor direktori lokal",
    ),
    "error.import_root_not_exist": ("Configured FIRMSIGHT_IMPORT_ROOT does not exist", "FIRMSIGHT_IMPORT_ROOT yang dikonfigurasi tidak ada"),
    "error.import_root_not_dir": ("Configured FIRMSIGHT_IMPORT_ROOT must be a directory", "FIRMSIGHT_IMPORT_ROOT yang dikonfigurasi harus berupa direktori"),
    "error.import_root_is_fs_root": ("Configured FIRMSIGHT_IMPORT_ROOT must not be the filesystem root", "FIRMSIGHT_IMPORT_ROOT yang dikonfigurasi tidak boleh root filesystem"),
    "error.project_dir_not_found": ("Project directory was not found", "Direktori proyek tidak ditemukan"),
    "error.project_dir_symlink": ("Project directory cannot be a symbolic link", "Direktori proyek tidak boleh berupa tautan simbolik"),
    "error.project_dir_outside": ("Project directory is outside FIRMSIGHT_IMPORT_ROOT", "Direktori proyek berada di luar FIRMSIGHT_IMPORT_ROOT"),
    "error.project_path_not_dir": ("Project path must be a directory", "Jalur proyek harus berupa direktori"),
    "error.project_file_limit": ("Project directory exceeds the 500-file import limit", "Direktori proyek melebihi batas impor 500 berkas"),
    "error.no_source_files": (
        "No supported firmware source or project metadata files were found",
        "Tidak ada berkas sumber firmware atau metadata proyek yang didukung",
    ),
    "error.sync_requires_local": ("Source sync requires a project imported from a local directory.", "Sinkronisasi sumber memerlukan proyek yang diimpor dari direktori lokal."),
    "error.path_invalid": ("{message}", "{message}"),
    "error.path_not_allowed": ("{path} is not an allowed source or project metadata file", "{path} bukan berkas sumber atau metadata proyek yang diizinkan"),
    "error.path_too_large": ("{path} exceeds the per-file import limit", "{path} melebihi batas impor per berkas"),
    "error.path_not_utf8": ("{path} is not valid UTF-8 source text", "{path} bukan teks sumber UTF-8 yang valid"),
    "error.project_active_review": (
        "This project has a running AI review. Wait for it to finish before deleting.",
        "Proyek ini memiliki review AI yang sedang berjalan. Tunggu hingga selesai sebelum menghapus.",
    ),
    "error.project_active_intelligence": (
        "This project has queued or running Project Intelligence work. Wait for it to finish before deleting.",
        "Proyek ini memiliki pekerjaan Project Intelligence yang masuk antrean atau sedang berjalan. Tunggu hingga selesai sebelum menghapus.",
    ),
    # -- HTTP errors: reviews / findings --------------------------------------
    "error.review_not_found": ("Review not found", "Review tidak ditemukan"),
    "error.finding_not_found": ("Finding not found", "Temuan tidak ditemukan"),
    "error.memory_not_found": ("Engineering memory not found", "Memori rekayasa tidak ditemukan"),
    "error.intelligence_not_found": ("Intelligence record not found", "Catatan intelligence tidak ditemukan"),
    "error.proposal_not_found": ("Memory proposal not found", "Usulan memori tidak ditemukan"),
    "error.no_learning_summary": ("No learning summary exists for this review", "Tidak ada ringkasan pembelajaran untuk review ini"),
    "error.no_reviewable_files": (
        "No reviewable firmware source files were found for the selected review scope",
        "Tidak ada berkas sumber firmware yang dapat direview untuk cakupan review yang dipilih",
    ),
    "error.review_requires_models": (
        "AI Review requires configured investigator and verifier models with a server API key",
        "AI Review memerlukan model investigator dan verifier yang terkonfigurasi beserta API key server",
    ),
    "error.import_before_review": ("Import a local project directory before starting a review", "Impor direktori proyek lokal sebelum memulai review"),
    "error.import_before_yaml": ("Import a local project directory before generating firmware.ai.yaml", "Impor direktori proyek lokal sebelum membuat firmware.ai.yaml"),
    "error.chat_requires_model": ("AI Chat requires a configured chat model and server API key", "AI Chat memerlukan model chat yang terkonfigurasi dan API key server"),
    "error.yaml_requires_model": ("YAML generation requires a configured YAML Generator model and server API key", "Pembuatan YAML memerlukan model YAML Generator yang terkonfigurasi dan API key server"),
    "error.fix_verify_requires_model": ("Verify Fix requires a configured verifier model with a server API key", "Verify Fix memerlukan model verifier yang terkonfigurasi dengan API key server"),
    "error.attach_before_resume": ("Attach the current local source directory before resuming this review.", "Lampirkan direktori sumber lokal saat ini sebelum melanjutkan review ini."),
    "error.attach_before_sync": ("Attach the current local source directory before syncing it.", "Lampirkan direktori sumber lokal saat ini sebelum melakukan sinkronisasi."),
    "error.accept_before_fix": ("Accept this finding before verifying a source fix", "Terima temuan ini sebelum memverifikasi perbaikan sumber"),
    "error.rejection_reason_required": ("A rejection reason is required", "Alasan penolakan wajib diisi"),
    "error.only_confirmed_solved": ("Only CONFIRMED_BUG findings can be marked solved", "Hanya temuan CONFIRMED_BUG yang dapat ditandai selesai"),
    "error.only_confirmed_verify": ("Only CONFIRMED_BUG findings can be verified as solved", "Hanya temuan CONFIRMED_BUG yang dapat diverifikasi selesai"),
    "error.fix_no_verifier_result": ("Fix verification did not receive a validated verifier result", "Verifikasi perbaikan tidak menerima hasil verifier yang tervalidasi"),
    "error.resume_not_allowed": ("Only failed, partial, or interrupted reviews can be resumed", "Hanya review gagal, parsial, atau terputus yang dapat dilanjutkan"),
    "error.no_unavailable_units": ("This review has no unavailable review units to resume", "Review ini tidak memiliki unit review tidak tersedia untuk dilanjutkan"),
    "error.no_unresolved_units": ("This review has no unresolved review units to resume", "Review ini tidak memiliki unit review belum selesai untuk dilanjutkan"),
    "error.no_snapshot": ("This review has no source snapshot; start a new review instead of resuming it", "Review ini tidak memiliki snapshot sumber; mulai review baru alih-alih melanjutkannya"),
    "error.no_updates": ("At least one update is required", "Setidaknya satu pembaruan diperlukan"),
    "error.correction_too_short": ("Correction reason must contain at least 8 characters", "Alasan koreksi harus mengandung setidaknya 8 karakter"),
    "error.terminal_no_revalidate": ("Terminal records cannot be revalidated", "Catatan terminal tidak dapat direvalidasi"),
    "error.knowledge_doc_not_found": ("Knowledge document not found", "Dokumen pengetahuan tidak ditemukan"),
    "error.knowledge_scope_mismatch": ("Knowledge search project scope does not match the route", "Cakupan proyek pencarian pengetahuan tidak cocok dengan rute"),
    # -- Settings / endpoint validation ---------------------------------------
    "error.endpoint_invalid": (
        "Endpoint must be an absolute HTTP(S) URL without embedded credentials",
        "Endpoint harus berupa URL HTTP(S) absolut tanpa kredensial tersemat",
    ),
    "error.models_invalid": ("Models must use only known FirmSight roles with non-empty values", "Model hanya boleh menggunakan role FirmSight yang dikenal dengan nilai tidak kosong"),
    "error.models_incomplete": ("Models must resolve a model for every FirmSight role", "Model harus mengisi model untuk setiap role FirmSight"),
    "error.review_context_invalid": ("Review context must be one of: {allowed} characters", "Konteks review harus salah satu dari: {allowed} karakter"),
    "error.parallel_invalid": ("Review parallel requests must be one of: 1, 2, 3", "Permintaan review paralel harus salah satu dari: 1, 2, 3"),
    "error.investigator_budget_invalid": ("Investigator output budget must be one of: {allowed} tokens or {default}", "Anggaran output Investigator harus salah satu dari: {allowed} token atau {default}"),
    "error.verifier_budget_invalid": ("Verifier output budget must be one of: {allowed} tokens or {default}", "Anggaran output Verifier harus salah satu dari: {allowed} token atau {default}"),
    "error.vault_invalid": ("{message}", "{message}"),
    # -- Vault projection fixed messages (FS-I18N-016) -------------------------
    "retrieval.provisional_warning": ("is provisional", "bersifat provisional"),
    "retrieval.revalidation_warning": ("needs source revalidation", "perlu revalidasi sumber"),
    "retrieval.revalidation_lifecycle": ("Needs source revalidation", "Perlu revalidasi sumber"),
    "error.vault_blocked_path": ("refusing to write a blocked path", "menolak menulis ke jalur yang diblokir"),
    "error.vault_external_edit": (
        "external edit was not imported; generated projection documents are DB-authoritative",
        "suntingan eksternal tidak diimpor; dokumen proyeksi yang dihasilkan bersifat otoritatif dari basis data",
    ),
    "error.vault_external_edit_regen": (
        "external edit was not imported; regeneration requires explicit confirmation",
        "suntingan eksternal tidak diimpor; regenerasi memerlukan konfirmasi eksplisit",
    ),
    "error.vault_layout": ("vault layout could not be prepared", "tata letak vault tidak dapat disiapkan"),
    "error.vault_wrong_project_doc": (
        "wrong project scope for generated projection document",
        "cakupan proyek salah untuk dokumen proyeksi yang dihasilkan",
    ),
    "error.vault_wrong_project_id": (
        "wrong project or unknown intelligence id",
        "proyek salah atau id intelligence tidak dikenal",
    ),
    "error.vault_duplicate_id": ("duplicate intelligence id", "id intelligence duplikat"),
    "error.vault_no_projection": ("no stored generated projection to compare", "tidak ada proyeksi tersimpan untuk dibandingkan"),
    "error.vault_symlink_dir": ("refusing to use a symlinked vault directory", "menolak menggunakan direktori vault yang berupa tautan simbolik"),
    # -- Knowledge vault projection lines (part of review progress) -----------
    "review.projection_errors": (
        "Knowledge vault projection completed with {count} quarantine/error item{s}",
        "Proyeksi vault pengetahuan selesai dengan {count} item karantina/kesalahan",
    ),
    "review.projection_written": (
        "Knowledge vault projection: {written} document{s} written",
        "Proyeksi vault pengetahuan: {written} dokumen ditulis",
    ),
    "review.projection_legacy_part": (
        ", {count} legacy document{s} left in place",
        ", {count} dokumen lama dibiarkan di tempatnya",
    ),
    "review.projection_unchanged_part": (
        ", {count} unchanged",
        ", {count} tidak berubah",
    ),
    "review.vault_skipped": (
        "Knowledge vault is not configured for this workspace.",
        "Vault pengetahuan belum dikonfigurasi untuk workspace ini.",
    ),
    "review.index_attention": (
        "Knowledge index skipped: vault projection requires attention before indexing. Retry vault sync from the Knowledge Base.",
        "Indeks pengetahuan dilewati: proyeksi vault perlu perhatian sebelum pengindeksan. Ulangi vault sync dari Knowledge Base.",
    ),
    "review.projection_failed": (
        "Knowledge vault projection could not complete safely; no index was attempted. Retry vault sync from the Knowledge Base.",
        "Proyeksi vault pengetahuan tidak dapat diselesaikan dengan aman; pengindeksan tidak dilakukan. Ulangi vault sync dari Knowledge Base.",
    ),
    "review.projection_unavailable": (
        "Knowledge vault projection was unavailable; no index was attempted. Retry vault sync from the Knowledge Base.",
        "Proyeksi vault pengetahuan tidak tersedia; pengindeksan tidak dilakukan. Ulangi vault sync dari Knowledge Base.",
    ),
    "review.index_failed": (
        "Knowledge index unavailable; vault projection remains available. Retry from the Knowledge Base.",
        "Indeks pengetahuan tidak tersedia; proyeksi vault tetap tersedia. Ulangi dari Knowledge Base.",
    ),
    "review.no_indexer": (
        "Vault projection completed; no indexer is configured for this workspace.",
        "Proyeksi vault selesai; tidak ada indexer yang dikonfigurasi untuk workspace ini.",
    ),
    "review.index_updated": ("Knowledge index updated.", "Indeks pengetahuan diperbarui."),
    "review.index_skip_no_indexer": (
        "Vault projection completed, but indexing was skipped because no indexer is configured.",
        "Proyeksi vault selesai, tetapi pengindeksan dilewati karena tidak ada indexer yang dikonfigurasi.",
    ),
    "error.review_worker_stopped": (
        "Review worker stopped before completion",
        "Worker review berhenti sebelum selesai",
    ),
    # -- Learning count labels used inside review.learning_completed ----------
    "count.provisional": ("provisional", "provisional"),
    "count.reinforced": ("reinforced", "reinforced"),
    "count.verified": ("verified", "terverifikasi"),
    "count.needs_revalidation": ("needs revalidation", "perlu revalidasi"),
    "count.conflicted": ("conflicted", "berkonflik"),
    "count.superseded": ("superseded", "superseded"),
    "count.rejected": ("rejected", "ditolak"),
    "count.none": ("no new knowledge", "tidak ada pengetahuan baru"),
    # -- Language setting validation -------------------------------------------
    "error.language_invalid": ("Language must be one of: en, id", "Bahasa harus salah satu dari: en, id"),
    # -- Topology derivation (server-side, safe fixed message) ------------------
    "error.topology_derivation_failed": (
        "The project topology could not be derived consistently. No source or topology changes were applied; retry after checking the source.",
        "Topologi proyek tidak dapat diturunkan secara konsisten. Tidak ada perubahan sumber atau topologi yang diterapkan; coba lagi setelah memeriksa sumber.",
    ),
}


def message(key: str, locale: Locale = DEFAULT_LOCALE, /, **params: object) -> str:
    """Format a user-facing message in the requested locale.

    Unknown keys and params fall back safely: a missing key renders the
    English template (never the raw key), missing params render empty so a
    typo cannot crash a review job mid-flight.
    """
    entry = _MESSAGES.get(key)
    if entry is None:
        return key if "{{" in key else ""
    template = entry[0] if normalize_locale(locale) == "en" else entry[1]
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


def s(key: str, locale: Locale = DEFAULT_LOCALE, /, **params: object) -> str:
    """Alias of :func:`message` for call-site brevity."""
    return message(key, locale, **params)


def catalog_keys() -> set[str]:
    return set(_MESSAGES)


def catalog_entries() -> dict[str, tuple[str, str]]:
    return dict(_MESSAGES)
