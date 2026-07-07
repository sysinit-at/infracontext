"""Tests for the shared slugify helper."""

import pytest

from infracontext.models.node import slugify


class TestSlugify:
    def test_lowercases_and_replaces_separators(self):
        assert slugify("Web Server 01") == "web-server-01"

    def test_strips_leading_trailing_hyphens(self):
        assert slugify("  hello  ") == "hello"
        assert slugify("---weird---") == "weird"

    def test_collapses_duplicate_separators(self):
        assert slugify("a___b   c") == "a-b-c"

    def test_drops_non_alphanumeric(self):
        assert slugify("server#1!") == "server-1"

    def test_empty_string_returns_node(self):
        assert slugify("") == "node"

    def test_punctuation_only_returns_node(self):
        assert slugify("---") == "node"
        assert slugify("!!!") == "node"

    def test_caps_length(self):
        long = "a" * 250
        result = slugify(long)
        assert len(result) == 100

    def test_unicode_is_replaced_not_dropped(self):
        # Non-ASCII chars become hyphens, not silently dropped.
        assert slugify("café-latte") == "caf-latte"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("pve-node-01", "pve-node-01"),
            ("web_server", "web-server"),
            ("DB.Primary", "db-primary"),
        ],
    )
    def test_common_infra_names(self, name, expected):
        assert slugify(name) == expected


class TestSlugifyMatchesRemovedDuplicates:
    """Guard against the old per-module copies drifting from the shared one.

    The proxmox, ssh_config, and import_cmd modules previously each carried a
    private copy of this exact algorithm. They now all delegate to ``slugify``.
    These cases pin the documented behaviour so a future edit to ``slugify``
    can't silently change the slug of an already-synced node (which would
    orphan its YAML file).
    """

    def test_proxmox_style_vm_name(self):
        assert slugify("ct-mail-01.example.com") == "ct-mail-01-example-com"

    def test_ssh_host_alias(self):
        assert slugify("web-prod-01") == "web-prod-01"
