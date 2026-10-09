# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers

from .base import BaseSerializer
from plane.db.models import Page
from plane.utils.content_validator import validate_html_content


def _sanitize_description_html(value):
    is_valid, error_message, sanitized_html = validate_html_content(value)
    if not is_valid:
        raise serializers.ValidationError(error_message)
    return sanitized_html if sanitized_html is not None else value


class PageSerializer(BaseSerializer):
    """
    Read serializer for project pages.

    The collaborative editor state (description_binary / description_json) is
    internal to the live server and is never exposed; description_html is the
    API representation of the page content.
    """

    parent_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Page
        fields = [
            "id",
            "name",
            "description_html",
            "description_stripped",
            "owned_by",
            "access",
            "color",
            "parent_id",
            "is_locked",
            "archived_at",
            "workspace",
            "projects",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        ]
        read_only_fields = fields


class PageCreateSerializer(BaseSerializer):
    """Validates the payload for creating a project page."""

    description_html = serializers.CharField(required=False, default="<p></p>")
    parent_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Page
        fields = [
            "name",
            "description_html",
            "access",
            "color",
            "parent_id",
            "is_locked",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]

    def validate_description_html(self, value):
        return _sanitize_description_html(value) if value else "<p></p>"

    def validate_parent_id(self, value):
        if value is None:
            return value
        parent_exists = Page.objects.filter(
            pk=value,
            workspace_id=self.context["workspace_id"],
            project_pages__project_id=self.context["project_id"],
            project_pages__deleted_at__isnull=True,
        ).exists()
        if not parent_exists:
            raise serializers.ValidationError("Parent page not found in this project")
        return value


class PageUpdateSerializer(BaseSerializer):
    """Validates the payload for updating a project page."""

    description_html = serializers.CharField(required=False)

    class Meta:
        model = Page
        fields = [
            "name",
            "description_html",
            "access",
            "color",
            "is_locked",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]

    def validate_description_html(self, value):
        return _sanitize_description_html(value) if value else "<p></p>"

    def update(self, instance, validated_data):
        if "description_html" in validated_data:
            # The live server rebuilds the editor state from description_html the
            # next time the page is opened, so drop the stale binary and JSON.
            instance.description_binary = None
            instance.description_json = {}
        return super().update(instance, validated_data)
