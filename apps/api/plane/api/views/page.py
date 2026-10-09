# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json

# Django imports
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response
from drf_spectacular.utils import OpenApiResponse, OpenApiRequest

# Module imports
from plane.api.serializers import (
    PageSerializer,
    PageCreateSerializer,
    PageUpdateSerializer,
)
from plane.app.permissions import ProjectPagePermission
from plane.app.views.page.base import unarchive_archive_page_and_descendants
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import (
    Page,
    Project,
    ProjectMember,
    ProjectPage,
    UserFavorite,
    UserRecentVisit,
)
from plane.utils.openapi import page_docs
from .base import BaseAPIView

ADMIN_ROLE = 20
GUEST_ROLE = 5


def _project_page_queryset(slug, project_id, user):
    """Pages linked to the project that the user may see (public, or private and owned)."""
    return (
        Page.objects.filter(
            workspace__slug=slug,
            project_pages__project_id=project_id,
            project_pages__deleted_at__isnull=True,
            projects__archived_at__isnull=True,
        )
        .filter(Q(owned_by=user) | Q(access=Page.PUBLIC_ACCESS))
        .select_related("workspace", "owned_by")
        .prefetch_related("projects")
        .distinct()
    )


def _is_guest_without_full_view(slug, project_id, user):
    project = Project.objects.get(pk=project_id, workspace__slug=slug)
    return (
        ProjectMember.objects.filter(
            workspace__slug=slug,
            project_id=project_id,
            member=user,
            role=GUEST_ROLE,
            is_active=True,
        ).exists()
        and not project.guest_view_all_features
    )


class PageListCreateAPIEndpoint(BaseAPIView):
    """List and create project pages."""

    model = Page
    serializer_class = PageSerializer
    permission_classes = [ProjectPagePermission]
    use_read_replica = True

    def get_queryset(self):
        return _project_page_queryset(self.kwargs["slug"], self.kwargs["project_id"], self.request.user)

    @page_docs(
        operation_id="list_pages",
        summary="List pages",
        description="List pages in a project. Archived pages are excluded unless `archived=true`.",
        responses={200: OpenApiResponse(description="Paginated list of pages", response=PageSerializer)},
    )
    def get(self, request, slug, project_id):
        queryset = self.get_queryset()
        if request.query_params.get("archived", "false").lower() == "true":
            queryset = queryset.filter(archived_at__isnull=False)
        else:
            queryset = queryset.filter(archived_at__isnull=True)

        if _is_guest_without_full_view(slug, project_id, request.user):
            queryset = queryset.filter(owned_by=request.user)

        return self.paginate(
            request=request,
            queryset=queryset.order_by("-created_at", "id"),
            on_results=lambda pages: PageSerializer(pages, many=True, fields=self.fields).data,
        )

    @page_docs(
        operation_id="create_page",
        summary="Create page",
        description="Create a page in a project. `description_html` is the page body.",
        request=OpenApiRequest(request=PageCreateSerializer),
        responses={
            201: OpenApiResponse(description="Page created", response=PageSerializer),
            400: OpenApiResponse(description="Invalid request data"),
        },
    )
    def post(self, request, slug, project_id):
        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        serializer = PageCreateSerializer(
            data=request.data,
            context={"project_id": project_id, "workspace_id": project.workspace_id},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            page = serializer.save(
                owned_by=request.user,
                workspace_id=project.workspace_id,
                description_binary=None,
                description_json={},
            )
            ProjectPage.objects.create(
                workspace_id=project.workspace_id,
                project_id=project_id,
                page_id=page.id,
                created_by_id=page.created_by_id,
                updated_by_id=page.updated_by_id,
            )

        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=page.id,
        )
        page = self.get_queryset().get(pk=page.id)
        return Response(PageSerializer(page).data, status=status.HTTP_201_CREATED)


class PageDetailAPIEndpoint(BaseAPIView):
    """Retrieve, update and delete a project page."""

    model = Page
    serializer_class = PageSerializer
    permission_classes = [ProjectPagePermission]

    def get_queryset(self):
        return _project_page_queryset(self.kwargs["slug"], self.kwargs["project_id"], self.request.user)

    @page_docs(
        operation_id="retrieve_page",
        summary="Retrieve page",
        description="Retrieve a page, including its `description_html` body.",
        responses={200: OpenApiResponse(description="Page details", response=PageSerializer)},
    )
    def get(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)
        if _is_guest_without_full_view(slug, project_id, request.user) and page.owned_by_id != request.user.id:
            return Response(
                {"error": "You are not allowed to view this page"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(PageSerializer(page, fields=self.fields).data, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="update_page",
        summary="Update page",
        description=(
            "Update a page. `description_html` replaces the whole body. Locked and archived pages cannot be updated."
        ),
        request=OpenApiRequest(request=PageUpdateSerializer),
        responses={
            200: OpenApiResponse(description="Page updated", response=PageSerializer),
            400: OpenApiResponse(description="Invalid request data, or page locked or archived"),
        },
    )
    def put(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)

        if page.is_locked:
            return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)
        if page.archived_at:
            return Response({"error": "Page is archived"}, status=status.HTTP_400_BAD_REQUEST)

        # Only the owner can change who can see the page
        if "access" in request.data and request.data["access"] != page.access and page.owned_by_id != request.user.id:
            return Response(
                {"error": "Access cannot be updated since this page is owned by someone else"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_description_html = page.description_html
        serializer = PageUpdateSerializer(page, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        page = serializer.save()

        if "description_html" in serializer.validated_data:
            page_transaction.delay(
                new_description_html=page.description_html,
                old_description_html=old_description_html,
                page_id=page.id,
            )
            track_page_version.delay(
                page_id=page.id,
                existing_instance=json.dumps({"description_html": old_description_html}, cls=DjangoJSONEncoder),
                user_id=request.user.id,
            )

        page = self.get_queryset().get(pk=page.id)
        return Response(PageSerializer(page).data, status=status.HTTP_200_OK)

    def patch(self, request, slug, project_id, page_id):
        return self.put(request, slug, project_id, page_id)

    @page_docs(
        operation_id="delete_page",
        summary="Delete page",
        description="Permanently delete a page. The page must be archived first.",
        responses={
            204: OpenApiResponse(description="Page deleted"),
            400: OpenApiResponse(description="Page is not archived"),
        },
    )
    def delete(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)

        if page.archived_at is None:
            return Response(
                {"error": "The page should be archived before deleting"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        is_admin = ProjectMember.objects.filter(
            workspace__slug=slug,
            project_id=project_id,
            member=request.user,
            role=ADMIN_ROLE,
            is_active=True,
        ).exists()
        if page.owned_by_id != request.user.id and not is_admin:
            return Response(
                {"error": "Only admin or owner can delete the page"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Detach child pages so they are not deleted with their parent
        Page.objects.filter(
            parent_id=page_id,
            project_pages__project_id=project_id,
            workspace__slug=slug,
            project_pages__deleted_at__isnull=True,
        ).update(parent=None)

        page.delete()
        UserFavorite.objects.filter(
            project=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_type="page",
        ).delete()
        UserRecentVisit.objects.filter(
            project_id=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_name="page",
        ).delete(soft=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PageArchiveUnarchiveAPIEndpoint(BaseAPIView):
    """Archive (POST) and restore (DELETE) a project page and its sub-pages."""

    model = Page
    permission_classes = [ProjectPagePermission]

    def get_queryset(self):
        return _project_page_queryset(self.kwargs["slug"], self.kwargs["project_id"], self.request.user)

    def _can_change_archive_state(self, page, project_id, user):
        # Members and guests may only archive pages they own; admins may archive any page.
        is_non_admin = ProjectMember.objects.filter(
            project_id=project_id, member=user, is_active=True, role__lt=ADMIN_ROLE
        ).exists()
        return not is_non_admin or page.owned_by_id == user.id

    @page_docs(
        operation_id="archive_page",
        summary="Archive page",
        description="Archive a page and all of its sub-pages.",
        request=None,
        responses={200: OpenApiResponse(description="Page archived")},
    )
    def post(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)
        if not self._can_change_archive_state(page, project_id, request.user):
            return Response(
                {"error": "Only the owner or admin can archive the page"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        UserFavorite.objects.filter(
            entity_type="page",
            entity_identifier=page_id,
            project_id=project_id,
            workspace__slug=slug,
        ).delete()

        archived_at = timezone.now().date()
        unarchive_archive_page_and_descendants(page_id, archived_at)
        return Response({"archived_at": str(archived_at)}, status=status.HTTP_200_OK)

    @page_docs(
        operation_id="unarchive_page",
        summary="Unarchive page",
        description="Restore an archived page and all of its sub-pages.",
        responses={204: OpenApiResponse(description="Page restored")},
    )
    def delete(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)
        if not self._can_change_archive_state(page, project_id, request.user):
            return Response(
                {"error": "Only the owner or admin can un archive the page"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Restoring under an archived parent would break the hierarchy
        if page.parent_id and page.parent.archived_at:
            page.parent = None
            page.save(update_fields=["parent"])

        unarchive_archive_page_and_descendants(page_id, None)
        return Response(status=status.HTTP_204_NO_CONTENT)
