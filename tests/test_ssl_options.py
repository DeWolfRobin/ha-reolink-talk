"""Check every device session callback and persisted-option reload hook."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

ROOT = Path("custom_components/reolink_talk")

class SSLOptionTests(IsolatedAsyncioTestCase):
    def test_all_four_device_callbacks_honor_options(self):
        tree = ast.parse((ROOT / "media_player.py").read_text())
        callbacks = [n for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "aiohttp_get_session_callback"]
        self.assertEqual(len(callbacks), 4)
        for value in (True, False):
            for option in callbacks:
                session = Mock()
                hass = object()
                ns = dict(async_get_clientsession=session, hass=hass,
                          entry=SimpleNamespace(options={"verify_ssl": value}),
                          CONF_VERIFY_SSL="verify_ssl",
                          self=SimpleNamespace(hass=hass, _target=SimpleNamespace(verify_ssl=value)))
                callback = eval(compile(ast.Expression(option.value), "callback", "eval"), ns)
                callback()
                session.assert_called_once_with(hass, verify_ssl=value)
        # Existing entries have no option and keep verification enabled.
        ns["entry"].options.clear()
        callback = eval(compile(ast.Expression(next(c.value for c in callbacks if any(isinstance(n, ast.Name) and n.id == "entry" for n in ast.walk(c.value)))), "callback", "eval"), ns)
        ns["async_get_clientsession"].reset_mock()
        callback()
        ns["async_get_clientsession"].assert_called_once_with(hass, verify_ssl=True)

    async def test_options_update_reloads_entry(self):
        tree = ast.parse((ROOT / "__init__.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_options_updated")
        ns = dict(HomeAssistant=object, ConfigEntry=object)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "reload", "exec"), ns)
        reload = AsyncMock()
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_reload=reload))
        await ns[node.name](hass, SimpleNamespace(entry_id="entry"))
        reload.assert_awaited_once_with("entry")
