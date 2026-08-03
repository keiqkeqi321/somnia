from __future__ import annotations

import unittest

from open_somnia.tools.registry import ToolDefinition, ToolRegistry


def _handler_a(ctx, payload):
    return "a"


def _handler_b(ctx, payload):
    return "b"


def _tool(name: str, handler=_handler_a, description: str = "tool") -> ToolDefinition:
    return ToolDefinition(name, description, {"type": "object", "properties": {}}, handler)


class ToolRegistryCollisionTests(unittest.TestCase):
    def test_register_overwrite_records_warning_and_still_replaces(self) -> None:
        registry = ToolRegistry()
        registry.register(_tool("mcp__alpha__search", _handler_a, "first"))
        registry.register(_tool("mcp__alpha__search", _handler_b, "second"))

        self.assertEqual(registry.schemas()[0]["description"], "second")
        self.assertEqual(len(registry.registration_warnings), 1)
        warning = registry.registration_warnings[0]
        self.assertIn("mcp__alpha__search", warning)
        self.assertIn("alpha", warning)

    def test_builtin_overwrite_warning_names_handler_module(self) -> None:
        registry = ToolRegistry()
        registry.register(_tool("bash", _handler_a))
        registry.register(_tool("bash", _handler_b))

        self.assertEqual(len(registry.registration_warnings), 1)
        self.assertIn("bash", registry.registration_warnings[0])
        self.assertIn(__name__, registry.registration_warnings[0])

    def test_unregister_prefix_then_reregister_emits_no_warning(self) -> None:
        registry = ToolRegistry()
        registry.register(_tool("mcp__alpha__search", _handler_a))
        registry.unregister_prefix("mcp__alpha__")
        registry.register(_tool("mcp__alpha__search", _handler_b, "refreshed"))

        self.assertEqual(registry.registration_warnings, [])
        self.assertEqual(registry.schemas()[0]["description"], "refreshed")

    def test_distinct_registrations_emit_no_warning(self) -> None:
        registry = ToolRegistry()
        registry.register(_tool("bash", _handler_a))
        registry.register(_tool("read_file", _handler_b))

        self.assertEqual(registry.registration_warnings, [])


if __name__ == "__main__":
    unittest.main()
