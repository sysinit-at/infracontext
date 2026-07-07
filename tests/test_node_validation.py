"""Tests for Node id/slug format validation.

The Node model enforces two invariants:
- ``slug`` is a URL/path-safe token (blocks ``/``, ``:``, spaces, ...).
- ``id`` equals ``f"{type}:{slug}"`` so it can't drift from the fields it
  claims to summarize.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from infracontext.models.node import Node, NodeType


class TestSlugValidation:
    @pytest.mark.parametrize(
        "slug",
        ["web", "web-01", "pve-01", "local-db", "writable-vm", "a", "100", "k8s-node-1"],
    )
    def test_accepts_legitimate_slugs(self, slug):
        node = Node(id=f"vm:{slug}", slug=slug, type=NodeType.VM, name="X")
        assert node.slug == slug

    @pytest.mark.parametrize(
        "slug",
        [
            "web/01",  # path separator
            "web:01",  # scope separator
            "web 01",  # whitespace
            "Web",  # uppercase
            "-web",  # leading hyphen
            "..",  # traversal
            "web.yaml",  # dot
            "web\\01",  # backslash
            "",  # empty
        ],
    )
    def test_rejects_unsafe_slugs(self, slug):
        with pytest.raises(ValidationError):
            Node(id=f"vm:{slug}", slug=slug, type=NodeType.VM, name="X")


class TestIdMatchesTypeSlug:
    def test_valid_id(self):
        node = Node(id="physical_host:pve-01", slug="pve-01", type=NodeType.PHYSICAL_HOST, name="X")
        assert node.id == "physical_host:pve-01"

    def test_id_slug_mismatch_rejected(self):
        with pytest.raises(ValidationError, match="does not match type and slug"):
            Node(id="vm:web", slug="db", type=NodeType.VM, name="X")

    def test_id_type_mismatch_rejected(self):
        # id declares a different type than the type field.
        with pytest.raises(ValidationError, match="does not match type and slug"):
            Node(id="application:web", slug="web", type=NodeType.VM, name="X")

    def test_id_with_extra_colon_rejected(self):
        # An id smuggling a second scope separator can't equal type:slug
        # (the slug validator also rejects the ':').
        with pytest.raises(ValidationError):
            Node(id="vm:a:b", slug="a:b", type=NodeType.VM, name="X")

    def test_make_id_round_trips(self):
        slug = "web-01"
        node = Node(id=Node.make_id(NodeType.VM, slug), slug=slug, type=NodeType.VM, name="X")
        assert node.id == "vm:web-01"
