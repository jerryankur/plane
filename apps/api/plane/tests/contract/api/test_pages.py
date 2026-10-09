# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for the project pages endpoints.

/api/v1/workspaces/<slug>/projects/<project_id>/pages/
/api/v1/workspaces/<slug>/projects/<project_id>/pages/<page_id>/
/api/v1/workspaces/<slug>/projects/<project_id>/pages/<page_id>/archive/
"""

from unittest import mock

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import APIToken, Page, Project, ProjectMember, ProjectPage, User, WorkspaceMember


def _list_url(slug, project_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/pages/"


def _detail_url(slug, project_id, page_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/pages/{page_id}/"


def _archive_url(slug, project_id, page_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/pages/{page_id}/archive/"


@pytest.fixture(autouse=True)
def page_tasks():
    with (
        mock.patch("plane.api.views.page.page_transaction") as transaction_task,
        mock.patch("plane.api.views.page.track_page_version") as version_task,
    ):
        yield {"transaction": transaction_task, "version": version_task}


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Pages Project",
        identifier="PGS",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(
        workspace=workspace,
        project=project,
        member=create_user,
        role=20,
        is_active=True,
    )
    return project


def _make_page(project, owner, **kwargs):
    page = Page.objects.create(
        workspace=project.workspace,
        owned_by=owner,
        name=kwargs.pop("name", "Existing page"),
        description_html=kwargs.pop("description_html", "<p>Existing body</p>"),
        **kwargs,
    )
    ProjectPage.objects.create(workspace=project.workspace, project=project, page=page)
    return page


@pytest.fixture
def member_client(db, workspace, project):
    """API client for a second user who is a regular member of the project."""
    member = User.objects.create(email="member@plane.so", username="page-member", first_name="Member")
    WorkspaceMember.objects.create(workspace=workspace, member=member, role=15)
    ProjectMember.objects.create(workspace=workspace, project=project, member=member, role=15, is_active=True)
    token = APIToken.objects.create(user=member, label="Member token", token="member-api-token-12345")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    client.user = member
    return client


@pytest.mark.contract
class TestPageCreateAndList:
    @pytest.mark.django_db
    def test_create_page_links_it_to_project(self, api_key_client, workspace, project, create_user, page_tasks):
        response = api_key_client.post(
            _list_url(workspace.slug, project.id),
            {"name": "Launch plan", "description_html": "<h1>Plan</h1><p>Ship it</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["name"] == "Launch plan"
        assert "Ship it" in response.data["description_html"]
        assert response.data["owned_by"] == create_user.id
        assert project.id in response.data["projects"]
        assert "description_binary" not in response.data

        page = Page.objects.get(pk=response.data["id"])
        assert page.description_binary is None
        assert ProjectPage.objects.filter(page=page, project=project).exists()
        page_tasks["transaction"].delay.assert_called_once()

    @pytest.mark.django_db
    def test_create_page_sanitizes_html(self, api_key_client, workspace, project):
        response = api_key_client.post(
            _list_url(workspace.slug, project.id),
            {"name": "Unsafe", "description_html": "<p>ok</p><script>alert(1)</script>"},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert "<script>" not in response.data["description_html"]

    @pytest.mark.django_db
    def test_create_page_rejects_parent_from_other_project(self, api_key_client, workspace, project, create_user):
        other_project = Project.objects.create(name="Other", identifier="OTH", workspace=workspace)
        foreign_parent = _make_page(other_project, create_user)

        response = api_key_client.post(
            _list_url(workspace.slug, project.id),
            {"name": "Child", "parent_id": str(foreign_parent.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "parent_id" in response.data

    @pytest.mark.django_db
    def test_list_excludes_archived_unless_requested(self, api_key_client, workspace, project, create_user):
        _make_page(project, create_user, name="Active")
        _make_page(project, create_user, name="Old", archived_at=timezone.now().date())

        active = api_key_client.get(_list_url(workspace.slug, project.id))
        archived = api_key_client.get(_list_url(workspace.slug, project.id), {"archived": "true"})

        assert active.status_code == status.HTTP_200_OK
        assert {page["name"] for page in active.data["results"]} == {"Active"}
        assert {page["name"] for page in archived.data["results"]} == {"Old"}

    @pytest.mark.django_db
    def test_private_pages_of_others_are_hidden(self, member_client, workspace, project, create_user):
        _make_page(project, create_user, name="Public")
        private = _make_page(project, create_user, name="Secret", access=Page.PRIVATE_ACCESS)

        listing = member_client.get(_list_url(workspace.slug, project.id))
        detail = member_client.get(_detail_url(workspace.slug, project.id, private.id))

        assert {page["name"] for page in listing.data["results"]} == {"Public"}
        assert detail.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    @pytest.mark.django_db
    def test_non_member_cannot_list_pages(self, api_key_client, workspace, create_user):
        outsider_project = Project.objects.create(name="No access", identifier="NOA", workspace=workspace)

        response = api_key_client.get(_list_url(workspace.slug, outsider_project.id))

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
class TestPageUpdate:
    @pytest.mark.django_db
    def test_update_body_resets_editor_state(self, api_key_client, workspace, project, create_user, page_tasks):
        page = _make_page(project, create_user, description_binary=b"stale-yjs-state")

        response = api_key_client.put(
            _detail_url(workspace.slug, project.id, page.id),
            {"name": "Renamed", "description_html": "<p>New body</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["name"] == "Renamed"
        assert "New body" in response.data["description_html"]
        page.refresh_from_db()
        assert page.description_binary is None
        assert page.description_json == {}
        page_tasks["transaction"].delay.assert_called_once()
        page_tasks["version"].delay.assert_called_once()

    @pytest.mark.django_db
    def test_rename_keeps_editor_state(self, api_key_client, workspace, project, create_user):
        page = _make_page(project, create_user, description_binary=b"live-yjs-state")

        response = api_key_client.patch(
            _detail_url(workspace.slug, project.id, page.id),
            {"name": "Only the title"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        page.refresh_from_db()
        assert bytes(page.description_binary) == b"live-yjs-state"

    @pytest.mark.django_db
    def test_locked_page_is_refused(self, api_key_client, workspace, project, create_user):
        page = _make_page(project, create_user, is_locked=True)

        response = api_key_client.put(
            _detail_url(workspace.slug, project.id, page.id),
            {"description_html": "<p>Nope</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_archived_page_is_refused(self, api_key_client, workspace, project, create_user):
        page = _make_page(project, create_user, archived_at=timezone.now().date())

        response = api_key_client.put(
            _detail_url(workspace.slug, project.id, page.id),
            {"description_html": "<p>Nope</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_only_owner_can_change_access(self, member_client, workspace, project, create_user):
        page = _make_page(project, create_user)

        response = member_client.put(
            _detail_url(workspace.slug, project.id, page.id),
            {"access": Page.PRIVATE_ACCESS},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        page.refresh_from_db()
        assert page.access == Page.PUBLIC_ACCESS


@pytest.mark.contract
class TestPageArchiveAndDelete:
    @pytest.mark.django_db
    def test_archive_unarchive_includes_sub_pages(self, api_key_client, workspace, project, create_user):
        parent = _make_page(project, create_user, name="Parent")
        child = _make_page(project, create_user, name="Child", parent=parent)

        archived = api_key_client.post(_archive_url(workspace.slug, project.id, parent.id))
        child.refresh_from_db()
        assert archived.status_code == status.HTTP_200_OK
        assert child.archived_at is not None

        restored = api_key_client.delete(_archive_url(workspace.slug, project.id, parent.id))
        child.refresh_from_db()
        assert restored.status_code == status.HTTP_204_NO_CONTENT
        assert child.archived_at is None

    @pytest.mark.django_db
    def test_member_cannot_archive_others_page(self, member_client, workspace, project, create_user):
        page = _make_page(project, create_user)

        response = member_client.post(_archive_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        page.refresh_from_db()
        assert page.archived_at is None

    @pytest.mark.django_db
    def test_delete_requires_archive(self, api_key_client, workspace, project, create_user):
        page = _make_page(project, create_user)

        response = api_key_client.delete(_detail_url(workspace.slug, project.id, page.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert Page.objects.filter(pk=page.id).exists()

    @pytest.mark.django_db
    def test_delete_archived_page_detaches_children(self, api_key_client, workspace, project, create_user):
        parent = _make_page(project, create_user, name="Parent", archived_at=timezone.now().date())
        child = _make_page(project, create_user, name="Child", parent=parent)

        response = api_key_client.delete(_detail_url(workspace.slug, project.id, parent.id))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not Page.objects.filter(pk=parent.id).exists()
        child.refresh_from_db()
        assert child.parent_id is None
