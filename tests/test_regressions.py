import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import digest, put, unit, validate
from formats import collect, rewrite, rpg_strings
from locales import select
from patcher import perform
from vision import alike


class TranslationTests(unittest.TestCase):
    def test_tokens_and_proper_names(self):
        u = unit(r"\N[1] found %1 of {count}.", "event")
        validate(u, r"\N[1] 找到了 {count} 个中的 %1 个。")
        with self.assertRaises(ValueError):
            validate(u, r"\N[1] 找到了 {count} 个。")
        validate(unit("DejaVu Sans", "font"), "DejaVu Sans")
        for bad in ("{}", '{"translation":"中文"}', ""):
            with self.assertRaises(ValueError):
                validate(unit("Open the door.", "gate"), bad)

    def test_game_database_references(self):
        sample = {
            "displayName": "River Crossing",
            "bgm": {"name": "Field1"},
            "events": [
                None,
                {"name": "GateTrigger", "pages": [{"list": [{"code": 401, "parameters": ["Open."]}]}]},
            ],
        }
        self.assertEqual([v for _, v in rpg_strings(sample, "Map001")], ["River Crossing", "Open."])
        system = {"gameTitle": "Castle", "titleBgm": {"name": "Theme1"}, "terms": {"commands": ["Save"]}}
        self.assertEqual([v for _, v in rpg_strings(system, "System")], ["Castle", "Save"])

    def test_partial_ocr_and_unrelated_sign(self):
        self.assertTrue(
            alike(
                "Apencilwithblackleadwritesbest.",
                "A pencil with black-led right's best, the lamp shone with a steady green flame.",
            )
        )
        self.assertFalse(alike("NORTH GATE", "The lamp shone with a steady green flame."))

    def test_select_language_pair(self):
        base = Path("X:/Game")
        en, zh, ja = [base / "i18n" / name / "Game.locres" for name in ("en", "zh-Hans", "ja")]
        skip, pairs = select(base, [en, zh, ja])
        self.assertEqual(skip, {zh, ja})
        self.assertEqual(pairs, {en: zh})

    def test_gettext_uses_rendered_source_and_preserves_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            src = d / "source.po"
            dst = d / "out.po"
            src.write_text(
                'msgid ""\nmsgstr ""\n"Language: en\\n"\n\nmsgctxt "river"\nmsgid "ACTION_04"\nmsgstr "Raise the drawbridge"\n',
                encoding="utf-8",
            )
            us = []
            rows = collect("po", src, "menu", us)
            self.assertEqual(us[0]["source"], "Raise the drawbridge")
            rewrite("po", src, dst, rows, {us[0]["id"]: "升起吊桥"})
            import polib

            target = polib.pofile(str(dst))[0]
            self.assertEqual(
                (target.msgctxt, target.msgid, target.msgstr), ("river", "ACTION_04", "升起吊桥")
            )


class PatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "game"
        self.root.mkdir()
        dest = Path(self.tmp.name) / "patch"
        self.pay = dest / "payload"
        self.pay.mkdir(parents=True)
        (self.root / "a.txt").write_bytes(b"before")
        (self.pay / "a.txt").write_bytes(b"after")
        (self.pay / "b.txt").write_bytes(b"added")
        self.spec = {
            "game": str(self.root),
            "files": [
                {
                    "path": "a.txt",
                    "before": digest(self.root / "a.txt"),
                    "after": digest(self.pay / "a.txt"),
                },
                {"path": "b.txt", "before": None, "after": digest(self.pay / "b.txt")},
            ],
        }
        self.manifest = dest / "patch.json"
        put(self.manifest, self.spec)

    def test_apply_restore_idempotent(self):
        for cmd in ("apply", "apply", "restore", "restore"):
            perform(cmd, self.manifest)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"before")
        self.assertFalse((self.root / "b.txt").exists())

    def test_conflict_preflight_preserves_all_files(self):
        (self.root / "b.txt").write_bytes(b"user's file")
        with self.assertRaises(ValueError):
            perform("apply", self.manifest)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"before")

    def test_damaged_payload_and_path_escape(self):
        (self.pay / "b.txt").write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            perform("apply", self.manifest)
        self.spec["files"][0]["path"] = "../outside.txt"
        put(self.manifest, self.spec)
        with self.assertRaises(ValueError):
            perform("apply", self.manifest)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"before")


if __name__ == "__main__":
    unittest.main()
