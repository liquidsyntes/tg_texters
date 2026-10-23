"""Convert Telegram rich messages to safe, self-contained article pages."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit


class UnsupportedArticle(ValueError):
    """The article contains a block that cannot be preserved safely."""


TEXT_TAGS = {
    "bold": "strong", "italic": "em", "underline": "u",
    "strikethrough": "s", "spoiler": "span", "code": "code",
    "marked": "mark", "subscript": "sub", "superscript": "sup",
}


def plain_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(plain_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") == "custom_emoji":
            return str(value.get("alternative_text", ""))
        if value.get("type") == "mathematical_expression":
            return str(value.get("expression", ""))
        if value.get("type") == "button":
            return plain_text((value.get("button") or {}).get("text", ""))
        return plain_text(value.get("text", ""))
    return ""


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https", "mailto", "tel", "tg"}:
        raise UnsupportedArticle("Ссылка в статье использует неподдерживаемый протокол")
    return escape(url, quote=True)


def render_button(button: dict, *, inline: bool = False) -> str:
    label = render_text(button.get("text", ""))
    if not plain_text(button.get("text", "")).strip():
        raise UnsupportedArticle("Кнопка без подписи не поддерживается")
    class_name = "article-button article-button-inline" if inline else "article-button"
    actions = [key for key in (
        "url", "copy_text", "disabled", "callback_data", "web_app", "login_url",
        "switch_inline_query", "switch_inline_query_current_chat", "switch_inline_query_chosen_chat",
    ) if key in button]
    if len(actions) != 1:
        raise UnsupportedArticle("У кнопки должно быть ровно одно действие")
    action = actions[0]
    if action == "url":
        return f'<a class="{class_name}" href="{_safe_url(str(button["url"]))}">{label}</a>'
    if action == "copy_text":
        payload = (button.get("copy_text") or {}).get("text")
        if not isinstance(payload, str) or not 1 <= len(payload) <= 256:
            raise UnsupportedArticle("Неверный текст кнопки копирования")
        return f'<button class="{class_name}" type="button" data-copy-text="{escape(payload, quote=True)}">{label}</button>'
    if action == "disabled":
        return f'<span class="{class_name} article-button-disabled" aria-disabled="true">{label}</span>'
    raise UnsupportedArticle(f"Кнопка с действием {action!r} работает только в Telegram")


def render_text(value: object) -> str:
    if isinstance(value, str):
        return escape(value).replace("\n", "<br>")
    if isinstance(value, list):
        return "".join(render_text(item) for item in value)
    if not isinstance(value, dict):
        raise UnsupportedArticle("Неизвестный фрагмент текста")
    kind = value.get("type")
    if kind in TEXT_TAGS:
        tag = TEXT_TAGS[kind]
        content = render_text(value.get("text", ""))
        attr = ' class="spoiler" tabindex="0"' if kind == "spoiler" else ""
        return f"<{tag}{attr}>{content}</{tag}>"
    if kind == "url":
        return f'<a href="{_safe_url(str(value.get("url", "")))}">{render_text(value.get("text", ""))}</a>'
    if kind in {"email_address", "phone_number"}:
        key = "email_address" if kind == "email_address" else "phone_number"
        scheme = "mailto" if kind == "email_address" else "tel"
        return f'<a href="{_safe_url(scheme + ":" + str(value.get(key, "")))}">{render_text(value.get("text", ""))}</a>'
    if kind == "custom_emoji":
        return escape(str(value.get("alternative_text", "")))
    if kind == "button":
        return render_button(value.get("button") or {}, inline=True)
    if kind in {"mention", "hashtag", "cashtag", "bot_command", "date_time", "text_mention"}:
        return render_text(value.get("text", ""))
    raise UnsupportedArticle(f"Форматирование {kind!r} пока не поддерживается")


def photo_id(block: dict) -> str:
    sizes = block.get("photo") or []
    if not sizes:
        raise UnsupportedArticle("У фотографии нет доступного файла")
    best = max(sizes, key=lambda item: item.get("width", 0) * item.get("height", 0))
    return str(best["file_id"])


def photos_in(blocks: list[dict]) -> list[str]:
    found: list[str] = []
    for block in blocks:
        if block.get("type") == "photo":
            identifier = photo_id(block)
            if identifier not in found:
                found.append(identifier)
        for key in ("blocks",):
            if isinstance(block.get(key), list):
                found.extend(i for i in photos_in(block[key]) if i not in found)
        if block.get("type") == "list":
            for item in block.get("items", []):
                found.extend(i for i in photos_in(item.get("blocks", [])) if i not in found)
    return found


def _caption(block: dict) -> str:
    caption = block.get("caption") or {}
    if not caption:
        return ""
    text = render_text(caption.get("text", ""))
    credit = render_text(caption.get("credit", "")) if caption.get("credit") else ""
    return f"<figcaption>{text}{'<cite>' + credit + '</cite>' if credit else ''}</figcaption>" if text or credit else ""


def render_block(block: dict, images: dict[str, str]) -> str:
    kind = block.get("type")
    if kind == "paragraph":
        return f'<p>{render_text(block.get("text", ""))}</p>'
    if kind == "heading":
        size = max(2, min(6, int(block.get("size", 2))))
        return f'<h{size}>{render_text(block.get("text", ""))}</h{size}>'
    if kind == "divider":
        return "<hr>"
    if kind == "buttons":
        align = block.get("align", "left")
        if align not in {"left", "center", "right"}:
            raise UnsupportedArticle("Неизвестное выравнивание кнопок")
        buttons = block.get("buttons") or []
        if not 1 <= len(buttons) <= 8:
            raise UnsupportedArticle("В ряду должно быть от 1 до 8 кнопок")
        content = "".join(render_button(button) for button in buttons)
        return f'<div class="article-button-row article-button-row-{align}">{content}</div>'
    if kind == "blockquote":
        content = "".join(render_block(item, images) for item in block.get("blocks", []))
        credit = f'<cite>{render_text(block["credit"])}</cite>' if block.get("credit") else ""
        return f"<blockquote>{content}{credit}</blockquote>"
    if kind == "pullquote":
        credit = f'<cite>{render_text(block["credit"])}</cite>' if block.get("credit") else ""
        return f'<blockquote class="pullquote"><p>{render_text(block.get("text", ""))}</p>{credit}</blockquote>'
    if kind == "list":
        ordered = any(
            item.get("value") is not None or item.get("type") in {"1", "a", "A", "i", "I"}
            or str(item.get("label", "")).rstrip(". )").isdigit()
            for item in block.get("items", [])
        )
        tag = "ol" if ordered else "ul"
        pieces = []
        for item in block.get("items", []):
            value = f' value="{int(item["value"])}"' if ordered and item.get("value") is not None else ""
            content = "".join(render_block(child, images) for child in item.get("blocks", []))
            pieces.append(f"<li{value}>{content}</li>")
        items = "".join(pieces)
        return f"<{tag}>{items}</{tag}>"
    if kind == "photo":
        if block.get("has_spoiler"):
            raise UnsupportedArticle("Фотографии под спойлером пока не переносятся на сайт")
        identifier = photo_id(block)
        if identifier not in images:
            raise UnsupportedArticle("Фотография не загружена")
        alt = plain_text((block.get("caption") or {}).get("text", ""))
        return f'<figure><img src="{escape(images[identifier], quote=True)}" alt="{escape(alt, quote=True)}" loading="lazy">{_caption(block)}</figure>'
    if kind == "footer":
        return f'<p class="article-note">{render_text(block.get("text", ""))}</p>'
    raise UnsupportedArticle(f"Блок статьи {kind!r} пока не поддерживается")


def article_blocks(message: dict) -> list[dict]:
    rich = message.get("rich_message")
    if rich and isinstance(rich.get("blocks"), list):
        return rich["blocks"]
    if message.get("text"):
        return [{"type": "paragraph", "text": part} for part in message["text"].split("\n\n") if part.strip()]
    raise UnsupportedArticle("Пришлите статью Telegram или текстовое сообщение")


def article_title(blocks: list[dict], language: str) -> str:
    for block in blocks:
        if block.get("type") == "heading":
            return plain_text(block.get("text", "")).strip() or ("Стаття" if language == "uk" else "Статья")
    return "Стаття" if language == "uk" else "Статья"


def render_page(
    blocks: list[dict], language: str, images: dict[str, str], other_name: str,
    *, title_override: str | None = None, body_override: str | None = None,
    assets_prefix: str = "./",
) -> str:
    if language not in {"ru", "uk"}:
        raise ValueError("Язык должен быть ru или uk")
    title = title_override or article_title(blocks, language)
    body_blocks = blocks.copy()
    if body_blocks and body_blocks[0].get("type") == "heading":
        body_blocks = body_blocks[1:]
    body = body_override if body_override is not None else "\n".join(render_block(block, images) for block in body_blocks)
    other_language = "uk" if language == "ru" else "ru"
    other_label = "Українською" if language == "ru" else "По-русски"
    current_label = "RU" if language == "ru" else "UA"
    alternate_label = "UA" if language == "ru" else "RU"
    nav_label = "Язык статьи" if language == "ru" else "Мова статті"
    title_html = escape(title)
    script = (f'  <script src="{escape(assets_prefix, quote=True)}article.js" defer></script>\n'
              if 'data-copy-text=' in body else '')
    return f'''<!doctype html>
<html lang="{language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#15110f">
  <title>{title_html} · Discipline of Senses</title>
  <link rel="icon" href="{escape(assets_prefix, quote=True)}brand-logo.png" type="image/png">
  <link rel="alternate" hreflang="{other_language}" href="./{escape(other_name, quote=True)}">
  <link rel="stylesheet" href="{escape(assets_prefix, quote=True)}style.css">
{script.rstrip()}
</head>
<body>
  <header class="site-header">
    <div class="brand"><span class="brand-mark"><img src="{escape(assets_prefix, quote=True)}brand-logo.png" alt="" width="37" height="37"></span><span class="brand-name">Discipline of Senses</span></div>
    <nav class="languages" aria-label="{nav_label}">
      <span aria-current="page">{current_label}</span><a href="./{escape(other_name, quote=True)}" hreflang="{other_language}" lang="{other_language}">{alternate_label}</a>
    </nav>
  </header>
  <main class="article"><article>
    <header><p class="eyebrow">Discipline of Senses</p><h1>{title_html}</h1><div class="title-rule" aria-hidden="true"></div></header>
    <div class="article-body">{body}</div>
  </article></main>
  <footer class="site-footer"><a href="./{escape(other_name, quote=True)}" hreflang="{other_language}" lang="{other_language}">{other_label}</a></footer>
</body>
</html>
'''


def inline_blocks(blocks: list[dict]) -> list[dict]:
    """Convert received photo sizes to the outgoing InputMediaPhoto shape."""
    result = []
    for original in blocks:
        block = original.copy()
        kind = block.get("type")
        if kind == "photo":
            block["photo"] = {"type": "photo", "media": photo_id(original)}
        elif kind in {"blockquote", "list"}:
            if kind == "blockquote":
                block["blocks"] = inline_blocks(block.get("blocks", []))
            else:
                allowed = {"has_checkbox", "is_checked", "value", "type"}
                block["items"] = [
                    {**{key: value for key, value in item.items() if key in allowed},
                     "blocks": inline_blocks(item.get("blocks", []))}
                    for item in block.get("items", [])
                ]
        elif kind not in {"paragraph", "heading", "divider", "pullquote", "footer", "buttons"}:
            raise UnsupportedArticle(f"Блок {kind!r} нельзя отправить через inline-режим")
        result.append(block)
    return result
