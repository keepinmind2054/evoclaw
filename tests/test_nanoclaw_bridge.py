"""Tests for NanoClaw IPC bridge."""
import json
import os
import sys
import unittest
from pathlib import Path
import tempfile

# Allow running without installed package
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestNanoclawBridge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _import_bridge(self, data_dir="", group_folder="", discord_jid=""):
        # reload with fresh env vars
        import importlib
        env_patch = {
            "NANOCLAW_DATA_DIR": data_dir,
            "NANOCLAW_GROUP_FOLDER": group_folder,
            "NANOCLAW_DISCORD_JID": discord_jid,
        }
        original = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        try:
            import host.channels.nanoclaw_bridge as mod
            importlib.reload(mod)
            return mod
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_is_configured_false_when_env_missing(self):
        mod = self._import_bridge()
        self.assertFalse(mod.is_configured())

    def test_is_configured_true_when_all_set(self):
        mod = self._import_bridge(self.data_dir, "telegram_test", "dc:123")
        self.assertTrue(mod.is_configured())

    def test_send_returns_false_when_not_configured(self):
        mod = self._import_bridge()
        result = mod.send("hello")
        self.assertFalse(result)

    def test_send_writes_json_file(self):
        mod = self._import_bridge(self.data_dir, "telegram_test", "dc:123")
        result = mod.send("hello world")
        self.assertTrue(result)

        ipc_dir = Path(self.data_dir) / "ipc" / "telegram_test" / "messages"
        files = list(ipc_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["chatJid"], "dc:123")
        self.assertEqual(data["text"], "hello world")

    def test_send_no_tmp_file_left_on_success(self):
        mod = self._import_bridge(self.data_dir, "telegram_test", "dc:123")
        mod.send("test")
        ipc_dir = Path(self.data_dir) / "ipc" / "telegram_test" / "messages"
        tmp_files = list(ipc_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_send_custom_jid(self):
        mod = self._import_bridge(self.data_dir, "telegram_test", "dc:default")
        mod.send("msg", chat_jid="dc:override")
        ipc_dir = Path(self.data_dir) / "ipc" / "telegram_test" / "messages"
        files = list(ipc_dir.glob("*.json"))
        data = json.loads(files[0].read_text())
        self.assertEqual(data["chatJid"], "dc:override")

    def test_send_unique_filenames(self):
        mod = self._import_bridge(self.data_dir, "telegram_test", "dc:123")
        for _ in range(5):
            mod.send("msg")
        ipc_dir = Path(self.data_dir) / "ipc" / "telegram_test" / "messages"
        files = list(ipc_dir.glob("*.json"))
        self.assertEqual(len(files), 5)
        # All filenames are unique
        names = [f.name for f in files]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
