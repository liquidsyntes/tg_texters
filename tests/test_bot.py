import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from disense.bot import Bot, Config, Store, _upload_sftp, prepare, publish


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return True

    def download_photo(self, identifier, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"photo")

    def send_document(self, chat_id, path):
        self.calls.append(("sendDocument", {"chat_id": chat_id, "path": path}))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config("test-token", 123, root / "data", "https://example.com/articles", str(root / "published"))
        self.store = Store(self.config.data_dir)
        self.telegram = FakeTelegram()
        self.store.save_settings({"provider": "openrouter", "model": "example/model"})
        self.identifier = "draft01"
        self.store.save(self.identifier, {"status": "language", "message": {
            "rich_message": {"blocks": [
                {"type": "heading", "size": 1, "text": "Про канал"},
                {"type": "paragraph", "text": "Привіт."},
                {"type": "photo", "photo": [{"file_id": "photo1", "width": 500, "height": 300}],
                 "caption": {"text": "Підпис"}},
            ]}
        }})

    @patch("disense.bot.translate_fragments")
    def test_prepare_publish_and_inline_result(self, translate):
        translate.return_value = [
            "<h2>О канале</h2>", "<p>Привет.</p>",
            '<figure><img src="draft01-assets/images/01.jpg" alt="Підпис" loading="lazy"><figcaption>Подпись</figcaption></figure>',
        ]
        draft = prepare(self.config, self.telegram, self.store, self.identifier, "uk")
        self.assertEqual(draft["status"], "review")
        page = (self.store.path(self.identifier) / "site" / "draft01-ru.html").read_text()
        self.assertIn("Привет.", page)
        self.assertIn('href="./draft01-uk.html"', page)
        self.assertIn('href="./draft01-assets/style.css"', page)
        self.assertIn('src="./draft01-assets/brand-logo.png"', page)
        self.assertIn('src="draft01-assets/images/01.jpg"', page)
        self.assertTrue((self.store.path(self.identifier) / "site" / "draft01-assets" / "brand-logo.png").is_file())
        self.assertTrue((self.store.path(self.identifier) / "site" / "draft01-assets" / "images" / "01.jpg").is_file())
        with ZipFile(self.store.path(self.identifier) / "preview.zip") as archive:
            self.assertIn("draft01-assets/images/01.jpg", archive.namelist())
            self.assertIn("draft01-assets/brand-logo.png", archive.namelist())
        Path(self.config.publish_target).mkdir()
        published = publish(self.config, self.store, self.identifier)
        self.assertEqual(published["status"], "published")
        self.assertTrue((Path(self.config.publish_target) / "draft01-ru.html").is_file())
        bot = Bot(self.config)
        bot.api = self.telegram
        bot.inline({"id": "q1", "from": {"id": 123}, "query": self.identifier})
        method, params = self.telegram.calls[-1]
        self.assertEqual(method, "answerInlineQuery")
        result = params["results"][0]
        blocks = result["input_message_content"]["rich_message"]["blocks"]
        self.assertEqual(blocks[0], {"type": "paragraph", "text": [
            {"type": "url", "text": "Читать на русском", "url": published["urls"]["ru"]},
            " · ",
            {"type": "url", "text": "Читати українською", "url": published["urls"]["uk"]},
        ]})
        self.assertEqual(blocks[1]["text"], "Про канал")
        self.assertEqual(blocks[3]["photo"]["media"], "photo1")
        self.assertEqual(result["reply_markup"]["inline_keyboard"][0][0]["url"], published["urls"]["ru"])
        self.assertEqual(result["reply_markup"]["inline_keyboard"][0][1]["url"], published["urls"]["uk"])

    def test_other_user_is_ignored(self):
        bot = Bot(self.config)
        bot.api = self.telegram
        bot.message({"from": {"id": 999}, "chat": {"type": "private"}, "text": "/status"})
        bot.inline({"id": "q2", "from": {"id": 999}, "query": self.identifier})
        self.assertEqual(self.telegram.calls, [])

    @patch("disense.bot.secrets.token_urlsafe", return_value="buttonDraft")
    def test_rich_article_with_button_is_accepted(self, _identifier):
        bot = Bot(self.config)
        bot.api = self.telegram
        bot.message({
            "from": {"id": 123}, "chat": {"type": "private"},
            "rich_message": {"blocks": [
                {"type": "heading", "size": 1, "text": "Стаття"},
                {"type": "paragraph", "text": ["Текст ", {"type": "button", "button": {
                    "text": "Читати", "url": "https://example.com/article"
                }}]},
                {"type": "buttons", "buttons": [{"text": "Сайт", "url": "https://example.com"}]},
            ]},
        })
        self.assertEqual(self.store.load("buttonDraft")["status"], "language")
        self.assertEqual(self.telegram.calls[-1][0], "sendMessage")
        self.assertIn("inline_keyboard", self.telegram.calls[-1][1]["reply_markup"])

    @patch("disense.bot.translate_fragments")
    def test_sftp_upload_uses_pinned_key_and_places_pages_last(self, translate):
        translate.return_value = ["<h2>О канале</h2>", "<p>Привет.</p>",
                                  '<figure><img src="draft01-assets/images/01.jpg" alt="Підпис" loading="lazy"><figcaption>Подпись</figcaption></figure>']
        prepare(self.config, self.telegram, self.store, self.identifier, "uk")
        key = Path(self.temp.name) / "test.ppk"
        key.write_text("dummy")
        sftp = Config("test-token", 123, self.config.data_dir, "https://example.com/static", "",
                      "54.39.231.150", "ubuntu", 22, key, "SHA256:known", "/var/www/static")

        def fake_run(command, **kwargs):
            batch = Path(command[command.index("-b") + 1]).read_text()
            self.assertIn('cd "/var/www/static"', batch)
            self.assertIn('mkdir "draft01-assets"', batch)
            self.assertIn('put "' + str(self.store.path(self.identifier) / "site" / "draft01-assets" / "brand-logo.png"), batch)
            self.assertIn('put "' + str(self.store.path(self.identifier) / "site" / "draft01-assets" / "article.js"), batch)
            self.assertLess(batch.index('put "' + str(self.store.path(self.identifier) / "site" / "draft01-assets" / "style.css")),
                            batch.index('put "' + str(self.store.path(self.identifier) / "site" / "draft01-ru.html")))
            self.assertIn('mv "draft01-ru.html.part" "draft01-ru.html"', batch)
            self.assertIn("SHA256:known", command)
            return SimpleNamespace(returncode=0)

        with patch("disense.bot.subprocess.run", side_effect=fake_run):
            published = publish(sftp, self.store, self.identifier)
        self.assertEqual(published["urls"]["uk"], "https://example.com/static/draft01-uk.html")


if __name__ == "__main__":
    unittest.main()
