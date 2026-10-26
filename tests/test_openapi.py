from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_docs_are_available(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "swagger" in response.text.lower()


def test_openapi_is_published_with_api_version(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "EstudioMusica API"
    app = cast(FastAPI, client.app)
    assert spec["info"]["version"] == app.version
    assert "/api/v1" in spec["info"]["description"]
    assert "X-API-Version" in spec["info"]["description"]
    assert not spec.get("servers")


def test_openapi_documents_errors_and_pagination(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    paths = spec["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/db" in paths
    assert "/api/v1/auth/signup" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/logout" in paths
    assert "/api/v1/auth/me" in paths
    assert "/api/v1/admin/system-roles" in paths
    assert "/api/v1/admin/system-roles/{role_id}" in paths
    assert "/api/v1/categories" in paths
    assert "/api/v1/admin/categories" in paths
    assert "/api/v1/admin/categories/{category_id}" in paths
    assert "/api/v1/courses/{course_id}/me/permissions" in paths
    assert "/api/v1/courses/{course_id}" in paths
    assert "/api/v1/courses/{course_id}/archived" in paths
    assert "/api/v1/admin/courses" in paths
    assert "/api/v1/admin/courses/{course_id}" in paths
    assert "/api/v1/admin/courses/{course_id}/duplicate" in paths
    assert "/api/v1/examples" in paths
    assert "/api/v1/examples/denied" in paths
    assert "/api/v1/examples/too-large" in paths
    assert "/health" not in paths
    assert "/health/db" not in paths

    db_health = paths["/api/v1/health/db"]["get"]
    assert "200" in db_health["responses"]
    assert "503" in db_health["responses"]

    examples_get = paths["/api/v1/examples"]["get"]
    param_names = {param["name"] for param in examples_get["parameters"]}
    assert param_names == {"page", "pageSize"}
    page_size = next(
        param for param in examples_get["parameters"] if param["name"] == "pageSize"
    )
    page_size_schema = page_size.get("schema", page_size)
    assert "maximum" not in page_size_schema

    schemas = spec["components"]["schemas"]
    assert "ErrorResponse" in schemas
    assert "HTTPValidationError" not in schemas
    assert "$defs" not in schemas["ErrorResponse"]
    error_props = schemas["ErrorResponse"]["properties"]
    assert set(error_props) >= {"error", "code", "fields"}
    fields_schema = error_props["fields"]
    items = fields_schema.get("items") or fields_schema.get("anyOf", [{}])[0].get(
        "items"
    )
    assert items is not None
    assert items.get("type") == "string"

    for path_item in paths.values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = cast(dict[str, Any], operation.get("responses") or {})
            assert "422" not in responses

    post_responses = paths["/api/v1/examples"]["post"]["responses"]
    assert "400" in post_responses
    assert post_responses["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

    signup = paths["/api/v1/auth/signup"]["post"]
    assert "201" in signup["responses"]
    assert "400" in signup["responses"]
    assert "409" in signup["responses"]
    login = paths["/api/v1/auth/login"]["post"]
    assert "200" in login["responses"]
    assert "401" in login["responses"]
    logout = paths["/api/v1/auth/logout"]["post"]
    assert "204" in logout["responses"]
    me = paths["/api/v1/auth/me"]["get"]
    assert "200" in me["responses"]
    assert "401" in me["responses"]
    assert "SignUpRequest" in schemas
    assert "LogInRequest" in schemas
    assert "PublicUser" in schemas
    assert "AuthMeResponse" in schemas
    signup_props = schemas["SignUpRequest"]["properties"]
    assert set(signup_props) >= {"name", "email", "password"}
    me_props = schemas["AuthMeResponse"]["properties"]
    assert set(me_props) >= {"user", "systemRole", "isAdmin"}

    roles_get = paths["/api/v1/admin/system-roles"]["get"]
    assert "200" in roles_get["responses"]
    assert "401" in roles_get["responses"]
    assert "403" in roles_get["responses"]
    roles_patch = paths["/api/v1/admin/system-roles/{role_id}"]["patch"]
    assert "409" in roles_patch["responses"]
    assert "200" in roles_patch["responses"]
    assert "400" in roles_patch["responses"]
    roles_delete = paths["/api/v1/admin/system-roles/{role_id}"]["delete"]
    assert "204" in roles_delete["responses"]
    assert "409" in roles_delete["responses"]
    course_perms = paths["/api/v1/courses/{course_id}/me/permissions"]["get"]
    assert "200" in course_perms["responses"]
    assert "401" in course_perms["responses"]
    assert "403" in course_perms["responses"]
    assert "SystemRoleResponse" in schemas
    assert "SystemRoleUpdate" in schemas
    assert "CoursePermissionsResponse" in schemas
    role_props = schemas["SystemRoleResponse"]["properties"]
    assert set(role_props) >= {
        "id",
        "name",
        "sortOrder",
        "isSystem",
        "active",
        "createdAt",
        "updatedAt",
    }
    patch_props = schemas["SystemRoleUpdate"]["properties"]
    assert set(patch_props) == {"name", "active", "sortOrder"}
    perms_props = schemas["CoursePermissionsResponse"]["properties"]
    assert set(perms_props) >= {"courseRole", "permissions"}

    categories_get = paths["/api/v1/categories"]["get"]
    assert "200" in categories_get["responses"]
    assert "401" in categories_get["responses"]
    categories_post = paths["/api/v1/admin/categories"]["post"]
    assert "201" in categories_post["responses"]
    assert "400" in categories_post["responses"]
    assert "401" in categories_post["responses"]
    assert "403" in categories_post["responses"]
    assert "409" in categories_post["responses"]
    assert "patch" not in paths["/api/v1/admin/categories/{category_id}"]
    categories_put = paths["/api/v1/admin/categories/{category_id}"]["put"]
    assert "200" in categories_put["responses"]
    assert "400" in categories_put["responses"]
    assert "404" in categories_put["responses"]
    assert "409" in categories_put["responses"]
    categories_delete = paths["/api/v1/admin/categories/{category_id}"]["delete"]
    assert "204" in categories_delete["responses"]
    assert "409" in categories_delete["responses"]
    assert "CategoryResponse" in schemas
    assert "CategoryCreate" in schemas
    assert "CategoryUpdate" in schemas
    category_props = schemas["CategoryResponse"]["properties"]
    assert set(category_props) >= {
        "id",
        "name",
        "parentId",
        "description",
        "sortOrder",
        "createdAt",
        "updatedAt",
    }
    create_props = schemas["CategoryCreate"]["properties"]
    assert set(create_props) == {"name", "parentId", "description", "sortOrder"}
    update_props = schemas["CategoryUpdate"]["properties"]
    assert set(update_props) == {"name", "parentId", "description", "sortOrder"}

    course_get = paths["/api/v1/courses/{course_id}"]["get"]
    assert "200" in course_get["responses"]
    assert "401" in course_get["responses"]
    assert "403" in course_get["responses"]
    assert "404" in course_get["responses"]
    assert "patch" not in paths["/api/v1/courses/{course_id}"]
    course_put = paths["/api/v1/courses/{course_id}"]["put"]
    assert "200" in course_put["responses"]
    assert "403" in course_put["responses"]
    assert "409" in course_put["responses"]
    archived_put = paths["/api/v1/courses/{course_id}/archived"]["put"]
    assert "200" in archived_put["responses"]
    admin_list = paths["/api/v1/admin/courses"]["get"]
    list_params = {param["name"] for param in admin_list["parameters"]}
    assert {"page", "pageSize", "query", "archived"} <= list_params
    assert "limit" not in list_params
    assert "cursor" not in list_params
    page_size = next(
        param for param in admin_list["parameters"] if param["name"] == "pageSize"
    )
    assert "maximum" not in page_size.get("schema", page_size)
    admin_post = paths["/api/v1/admin/courses"]["post"]
    assert "201" in admin_post["responses"]
    assert "409" in admin_post["responses"]
    admin_dup = paths["/api/v1/admin/courses/{course_id}/duplicate"]["post"]
    assert "201" in admin_dup["responses"]
    admin_delete = paths["/api/v1/admin/courses/{course_id}"]["delete"]
    assert "204" in admin_delete["responses"]
    assert "CourseResponse" in schemas
    assert "CourseCreate" in schemas
    assert "CourseUpdate" in schemas
    assert "CourseArchive" in schemas
    assert "CourseDuplicate" in schemas
    course_props = schemas["CourseResponse"]["properties"]
    assert set(course_props) >= {
        "id",
        "shortName",
        "name",
        "summary",
        "categoryId",
        "startDate",
        "endDate",
        "visible",
        "level",
        "defaultGroupMode",
        "deletedAt",
        "archivedAt",
        "createdAt",
        "updatedAt",
    }
    assert set(schemas["CourseCreate"]["properties"]) == {
        "shortName",
        "name",
        "summary",
        "categoryId",
        "startDate",
        "endDate",
        "visible",
        "level",
        "defaultGroupMode",
    }
    assert set(schemas["CourseDuplicate"]["properties"]) == {
        "shortName",
        "name",
        "startDate",
    }
    assert set(schemas["CourseArchive"]["properties"]) == {"archived"}
    level_schema = course_props["level"]
    level_values = level_schema.get("enum") or level_schema.get("anyOf", [{}])[0].get(
        "enum"
    )
    if level_values is None:
        ref = (level_schema.get("$ref") or "").rsplit("/", 1)[-1]
        level_values = schemas.get(ref, {}).get("enum")
    assert level_values is not None
    assert set(level_values) == {"beginner", "intermediate", "advanced"}
