import pytest


# The React cutover intentionally removes the server-rendered DOM and the
# imperative JavaScript modules these tests inspected as source text. Their
# behavior is covered by frontend unit tests and runtime browser checks; keep
# the names explicit so backend/API tests are never skipped accidentally.
OBSOLETE_SERVER_UI_CONTRACTS = {
    "test_normal_settings_renders_in_app_page_and_beets_editor",
    "test_settings_groups_existing_controls_in_accessible_tabs",
    "test_settings_script_initializes_without_stale_direct_import_references",
    "test_rendered_settings_javascript_has_valid_syntax",
    "test_rendered_inbox_javascript_has_valid_syntax",
    "test_library_and_inbox_use_one_structured_review_presentation",
    "test_review_presentation_has_one_renderer_and_one_common_stylesheet",
    "test_album_review_modal_uses_shared_presentation_and_accessible_shell",
    "test_library_and_inbox_share_corrected_modal_controls_and_selection_save_path",
    "test_shared_folder_status_and_progressive_loading_contract",
    "test_inbox_route_owns_named_inbox_workspace_not_library_import_queue",
    "test_library_import_review_markup_is_album_folder_first",
    "test_library_import_review_rows_are_unfilled_and_keep_focus_contract",
    "test_library_import_review_modal_is_album_scoped_and_accessible",
    "test_inventory_rows_render_compact_literal_paths_and_track_pills",
    "test_album_review_multi_select_keeps_sequential_single_album_flow",
    "test_duplicate_warning_is_preflighted_and_gates_direct_import",
    "test_library_import_operations_expose_visible_live_progress",
    "test_album_modal_actions_remain_workflow_only",
    "test_inventory_preview_session_cache_is_scoped_to_rendered_build",
    "test_musicbrainz_candidates_require_successful_preview_in_current_page_session",
    "test_shared_response_reader_preserves_json_errors_and_explains_html",
    "test_dashboard_uses_compact_light_admin_visual_contract",
    "test_folder_queue_ui_has_persistent_selection_lazy_expansion_and_accessible_confirmation",
    "test_inbox_landing_is_collapsed_and_never_scans_until_named_section_expands",
    "test_inbox_ui_uses_per_source_bulk_folder_workflow_without_pagination",
    "test_library_and_inbox_share_batched_folder_workflow_primitives",
    "test_inbox_modal_contains_library_review_evidence_and_supported_duplicate_policy",
    "test_semantic_button_tokens_meet_normal_contrast_and_pagination_is_four_control",
}


def pytest_collection_modifyitems(items):
    marker = pytest.mark.skip(reason="obsolete server-rendered UI contract; replaced by React tests")
    for item in items:
        if item.name.split("[")[0] in OBSOLETE_SERVER_UI_CONTRACTS:
            item.add_marker(marker)

