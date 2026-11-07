"""Exercise registration with read-only and writable HA collection surfaces."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

source = Path("custom_components/reolink_talk/__init__.py").read_text()
node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_register_lovelace_resource")
namespace = dict(asyncio=asyncio, HomeAssistant=object, DOMAIN="reolink_talk", FRONTEND_URL="/reolink_talk/reolink-talk-button.js", _LOGGER=logging.getLogger(__name__))
exec(compile(ast.Module(body=[node], type_ignores=[]), source, "exec"), namespace)
register = namespace[node.name]

class ResourceTests(IsolatedAsyncioTestCase):
    async def test_yaml_and_existing(self):
        for items in ([], [{"url": namespace["FRONTEND_URL"] + "?v=1"}]):
            resources = SimpleNamespace(loaded=True, async_items=lambda: items)
            await register(SimpleNamespace(data={"lovelace": SimpleNamespace(resources=resources)}))
            self.assertEqual(len(items), 0 if not items else 1)

    async def test_storage_repeated_and_concurrent(self):
        items = []
        async def create(item):
            await asyncio.sleep(0)
            items.append(item)
        resources = SimpleNamespace(loaded=False, async_load=AsyncMock(), async_items=lambda: items, async_create_item=create)
        hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=resources)})
        await asyncio.gather(register(hass), register(hass))
        await register(hass)
        self.assertEqual(len(items), 1)
        resources.async_load.assert_awaited_once()

    async def test_missing_and_failures_do_not_raise(self):
        await register(SimpleNamespace(data={}))
        for method in ("async_load", "async_create_item"):
            resources = SimpleNamespace(loaded=method != "async_load", async_items=lambda: [], async_load=AsyncMock(), async_create_item=AsyncMock())
            setattr(resources, method, AsyncMock(side_effect=RuntimeError("unavailable")))
            await register(SimpleNamespace(data={"lovelace": SimpleNamespace(resources=resources)}))
