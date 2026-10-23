import unittest

from disense.article import UnsupportedArticle, article_blocks, inline_blocks, plain_text, render_block, render_page
from disense.translation import TranslationError, validate_translation


class ArticleTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [
            {"type": "heading", "size": 1, "text": "Про цей канал"},
            {"type": "paragraph", "text": ["Привіт, ", {"type": "bold", "text": "друже"}, "."]},
            {"type": "photo", "photo": [{"file_id": "small", "width": 100, "height": 100},
                                          {"file_id": "large", "width": 800, "height": 600}],
             "caption": {"text": "Підпис"}},
        ]

    def test_renders_article_and_media_with_language_link(self):
        page = render_page(self.blocks, "uk", {"large": "images/01.jpg"}, "ru.html")
        self.assertIn('<html lang="uk">', page)
        self.assertIn('href="./ru.html"', page)
        self.assertIn('<img src="./brand-logo.png" alt="" width="37" height="37">', page)
        self.assertIn('rel="icon" href="./brand-logo.png"', page)
        self.assertIn('<strong>друже</strong>', page)
        self.assertIn('src="images/01.jpg" alt="Підпис"', page)
        self.assertEqual(page.count("<h1>"), 1)

    def test_telegram_photo_can_be_sent_in_inline_article(self):
        outgoing = inline_blocks(self.blocks)
        self.assertEqual(outgoing[2]["photo"], {"type": "photo", "media": "large"})

    def test_inline_and_row_buttons_are_preserved(self):
        inline = {"type": "button", "button": {"text": "Читати", "url": "https://example.com/read"}}
        row = {"type": "buttons", "align": "center", "buttons": [
            {"text": "Сайт", "url": "https://example.com/?a=1&b=2"},
            {"text": "Копіювати", "copy_text": {"text": "Текст & ще"}},
        ]}
        blocks = [{"type": "paragraph", "text": ["Далі: ", inline]}, row]
        page = render_page(blocks, "uk", {}, "ru.html")
        self.assertIn('class="article-button article-button-inline" href="https://example.com/read"', page)
        self.assertIn('article-button-row-center', page)
        self.assertIn('href="https://example.com/?a=1&amp;b=2"', page)
        self.assertIn('data-copy-text="Текст &amp; ще"', page)
        self.assertIn('src="./article.js"', page)
        self.assertEqual(plain_text(inline), "Читати")
        self.assertEqual(inline_blocks(blocks)[1], row)

    def test_telegram_only_button_and_unsafe_url_fail_closed(self):
        with self.assertRaisesRegex(UnsupportedArticle, "только в Telegram"):
            render_block({"type": "paragraph", "text": {"type": "button", "button": {
                "text": "Дія", "callback_data": "run"
            }}}, {})
        with self.assertRaises(UnsupportedArticle):
            render_block({"type": "buttons", "buttons": [{"text": "Погано", "url": "javascript:alert(1)"}]}, {})

    def test_unsafe_link_and_unsupported_block_fail_closed(self):
        with self.assertRaises(UnsupportedArticle):
            render_block({"type": "paragraph", "text": {"type": "url", "url": "javascript:alert(1)", "text": "click"}}, {})
        with self.assertRaises(UnsupportedArticle):
            inline_blocks([{"type": "table", "cells": []}])

    def test_plain_text_fallback(self):
        self.assertEqual(article_blocks({"text": "Первый\n\nВторой"}), [
            {"type": "paragraph", "text": "Первый"}, {"type": "paragraph", "text": "Второй"}])

    def test_translation_keeps_html_structure_and_urls(self):
        source = ['<p>Привіт, <strong>друже</strong>.</p>', '<figure><img src="images/01.jpg" alt="Підпис" loading="lazy"><figcaption>Підпис</figcaption></figure>']
        translated = ['<p>Привет, <strong>друг</strong>.</p>', '<figure><img src="images/01.jpg" alt="Підпис" loading="lazy"><figcaption>Подпись</figcaption></figure>']
        self.assertEqual(validate_translation(source, translated), translated)
        with self.assertRaises(TranslationError):
            validate_translation(source, [translated[0], translated[1].replace('images/01.jpg', 'https://other.example/x')])


if __name__ == "__main__":
    unittest.main()
