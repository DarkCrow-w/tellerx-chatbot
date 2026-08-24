"""Regression guards for the public HTTP surface after router decomposition."""

from app.main import app

EXPECTED_PATHS = {
    "/health/live",
    "/health/ready",
    "/api/v1/projects",
    "/api/v1/documents",
    "/api/v1/ingestion-jobs/{job_id}",
    "/api/v1/ingestion-jobs/{job_id}/retry",
    "/api/v1/documents/{document_id}/versions",
    "/api/v1/document-versions/{version_id}/approve",
    "/api/v1/document-versions/{version_id}/deprecate",
    "/api/v1/sources/{chunk_id}",
    "/api/v1/documents/{document_id}/download",
    "/api/v1/documents/{document_id}",
    "/api/v1/chat",
    "/api/v1/feedback",
    "/api/v1/index/status",
    "/api/v1/admin/indexes/reconcile",
    "/api/v1/models/usage",
    "/api/v1/internal/diagnostics/qwen",
}


def test_public_api_paths_remain_stable() -> None:
    schema = app.openapi()
    assert set(schema["paths"]) == EXPECTED_PATHS


def test_operation_ids_are_unique_and_routes_are_capability_tagged() -> None:
    schema = app.openapi()
    operations = [
        operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    assert len(operation_ids) == len(set(operation_ids))
    assert {tag for operation in operations for tag in operation.get("tags", [])} == {
        "chat",
        "documents",
        "health",
        "operations",
    }
