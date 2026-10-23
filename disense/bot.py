"""Private Telegram bot: article in, review, upload, user-selected inline post out."""

from __future__ import annotations

from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .article import UnsupportedArticle, article_blocks, article_title, inline_blocks, photos_in, render_block, render_page
from .translation import TranslationError, translate_fragments


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int
    data_dir: Path
    public_base_url: str
    publish_target: str
    sftp_host: str = ""
    sftp_user: str = ""
    sftp_port: int = 22
    sftp_key_path: Path | None = None
    sftp_host_key: str = ""
    sftp_remote_dir: str = ""

    @property
    def upload_ready(self) -> bool:
        return bool(self.public_base_url and (self.publish_target or (
            self.sftp_host and self.sftp_user and self.sftp_key_path and
            self.sftp_host_key and self.sftp_remote_dir
        )))

    @classmethod
    def from_environment(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        owner = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
        if not token or not owner.isdecimal():
            raise ValueError("Задайте TELEGRAM_BOT_TOKEN и числовой TELEGRAM_OWNER_ID")
        base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base and not base.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL должен начинаться с https://")
        key = os.environ.get("SFTP_KEY_PATH", "").strip()
        return cls(token, int(owner), Path(os.environ.get("DISENSE_DATA_DIR", "./data")).resolve(),
                   base, os.environ.get("PUBLISH_TARGET", "").strip(),
                   os.environ.get("SFTP_HOST", "").strip(), os.environ.get("SFTP_USER", "").strip(),
                   int(os.environ.get("SFTP_PORT", "22")), Path(key) if key else None,
                   os.environ.get("SFTP_HOST_KEY", "").strip(), os.environ.get("SFTP_REMOTE_DIR", "").strip())


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method: str, **params: object) -> object:
        request = Request(self.base + method, json.dumps(params).encode("utf-8"), {"Content-Type": "application/json"})
        with urlopen(request, timeout=45) as response:
            value = json.load(response)
        if not value.get("ok"):
            raise RuntimeError(f"Telegram: {method} вернул ошибку {value.get('error_code', '?')}")
        return value["result"]

    def send_document(self, chat_id: int, path: Path) -> None:
        boundary = "disense" + secrets.token_hex(8)
        data = path.read_bytes()
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        request = Request(self.base + "sendDocument", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(request, timeout=60) as response:
            value = json.load(response)
        if not value.get("ok"):
            raise RuntimeError("Telegram не принял HTML-файл")

    def download_photo(self, identifier: str, destination: Path) -> None:
        info = self.call("getFile", file_id=identifier)
        file_path = info["file_path"]
        if info.get("file_size", 0) > 20_000_000:
            raise UnsupportedArticle("Фотография превышает лимит загрузки Bot API (20 МБ)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(self.file_base + quote(file_path, safe="/"), timeout=60) as response:
            data = response.read(20_000_001)
        if len(data) > 20_000_000:
            raise UnsupportedArticle("Фотография превышает лимит загрузки Bot API (20 МБ)")
        destination.write_bytes(data)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, identifier: str) -> Path:
        if not identifier.isascii() or not all(c.isalnum() or c in "_-" for c in identifier):
            raise ValueError("Неверный номер черновика")
        return self.root / identifier

    def save(self, identifier: str, draft: dict) -> None:
        directory = self.path(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "draft.tmp"
        temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "draft.json")

    def load(self, identifier: str) -> dict:
        return json.loads((self.path(identifier) / "draft.json").read_text(encoding="utf-8"))

    def settings(self) -> dict:
        path = self.root / "settings.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"provider": "openrouter", "model": ""}

    def save_settings(self, value: dict) -> None:
        path = self.root / "settings.json"
        temporary = self.root / "settings.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def offset(self) -> int:
        path = self.root / "offset"
        return int(path.read_text()) if path.is_file() else 0

    def save_offset(self, value: int) -> None:
        temporary = self.root / "offset.tmp"
        temporary.write_text(str(value))
        temporary.replace(self.root / "offset")


def prepare(config: Config, telegram: Telegram, store: Store, identifier: str, source: str) -> dict:
    draft = store.load(identifier)
    blocks = article_blocks(draft["message"])
    settings = store.settings()
    if not settings.get("model"):
        raise TranslationError("Сначала выберите модель командой /model provider/model")
    target = "uk" if source == "ru" else "ru"
    directory = store.path(identifier) / "site"
    assets_name = f"{identifier}-assets"
    assets_dir = directory / assets_name
    images: dict[str, str] = {}
    for index, file_id in enumerate(photos_in(blocks), 1):
        relative = f"{assets_name}/images/{index:02d}.jpg"
        telegram.download_photo(file_id, directory / relative)
        images[file_id] = relative
    rendered = [render_block(block, images) for block in blocks]
    translated = translate_fragments(rendered, source, target, settings["provider"], settings["model"])
    has_heading = bool(blocks and blocks[0].get("type") == "heading")
    translated_title = _text_of_html(translated[0]) if has_heading else article_title(blocks, target)
    translated_body = "\n".join(translated[1:] if has_heading else translated)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{identifier}-{source}.html").write_text(render_page(
        blocks, source, images, f"{identifier}-{target}.html", assets_prefix=f"./{assets_name}/"
    ), encoding="utf-8")
    (directory / f"{identifier}-{target}.html").write_text(render_page(
        blocks, target, images, f"{identifier}-{source}.html", title_override=translated_title,
        body_override=translated_body, assets_prefix=f"./{assets_name}/"
    ), encoding="utf-8")
    assets_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("style.css", "favicon.svg", "article.js"):
        shutil.copy2(ROOT / filename, assets_dir / filename)
    shutil.copytree(ROOT / "fonts", assets_dir / "fonts", dirs_exist_ok=True)
    shutil.make_archive(str(directory.parent / "preview"), "zip", root_dir=directory)
    inline_blocks(blocks)  # Reject unsupported blocks before review.
    draft.update({"source": source, "status": "review", "provider": settings["provider"], "model": settings["model"]})
    store.save(identifier, draft)
    return draft


def _text_of_html(fragment: str) -> str:
    from html.parser import HTMLParser
    class Reader(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
        def handle_data(self, data: str) -> None:
            self.parts.append(data)
    parser = Reader()
    parser.feed(fragment)
    return "".join(parser.parts).strip()


def publish(config: Config, store: Store, identifier: str) -> dict:
    if not config.upload_ready:
        raise ValueError("Для загрузки нужны PUBLIC_BASE_URL и настройки SFTP")
    draft = store.load(identifier)
    if draft.get("status") == "published":
        return draft
    if draft.get("status") != "review":
        raise ValueError("Черновик ещё не готов к публикации")
    source = store.path(identifier) / "site"
    if config.sftp_host:
        _upload_sftp(config, source, identifier)
    elif config.publish_target.startswith("/"):
        destination = Path(config.publish_target).resolve()
        if not destination.is_dir():
            raise ValueError("Локальная папка публикации не существует")
        if any((destination / item.name).exists() for item in source.iterdir()):
            raise ValueError("Файлы этой статьи уже существуют на хостинге")
        for item in source.iterdir():
            if item.is_dir():
                shutil.copytree(item, destination / item.name)
            else:
                shutil.copy2(item, destination / item.name)
    else:
        raise ValueError("Не настроен SFTP или локальная папка публикации")
    base = config.public_base_url + "/" + quote(identifier)
    draft.update({"status": "published", "urls": {"ru": base + "-ru.html", "uk": base + "-uk.html"}})
    store.save(identifier, draft)
    return draft


def _upload_sftp(config: Config, source: Path, identifier: str) -> None:
    if not config.sftp_key_path or not config.sftp_key_path.is_file():
        raise ValueError("Файл ключа SFTP не найден")
    if not all((config.sftp_host, config.sftp_user, config.sftp_host_key, config.sftp_remote_dir)):
        raise ValueError("Настройки SFTP не заполнены")
    if not config.sftp_remote_dir.startswith("/"):
        raise ValueError("Путь SFTP должен быть абсолютным")

    def quoted(value: str) -> str:
        if '"' in value or "\n" in value or "\r" in value:
            raise ValueError("Кавычки и переносы строк в пути SFTP не поддерживаются")
        return '"' + value + '"'

    assets_name = f"{identifier}-assets"
    assets = source / assets_name
    remote_dir = config.sftp_remote_dir.rstrip("/")
    commands = [f"cd {quoted(remote_dir)}", f"mkdir {quoted(assets_name)}", f"cd {quoted(assets_name)}"]
    for filename in ("style.css", "favicon.svg"):
        commands.append(f"put {quoted(str(assets / filename))} {quoted(filename)}")
    commands.extend(["mkdir fonts", "cd fonts"])
    for font in sorted((assets / "fonts").iterdir()):
        commands.append(f"put {quoted(str(font))} {quoted(font.name)}")
    commands.append(f"cd {quoted(remote_dir)}")
    images = assets / "images"
    if images.is_dir():
        commands.extend([f"cd {quoted(assets_name)}", "mkdir images", "cd images"])
        for image in sorted(images.iterdir()):
            commands.append(f"put {quoted(str(image))} {quoted(image.name)}")
        commands.append(f"cd {quoted(remote_dir)}")
    for language in ("ru", "uk"):
        filename = f"{identifier}-{language}.html"
        commands.append(f"put {quoted(str(source / filename))} {quoted(filename + '.part')}")
    for language in ("ru", "uk"):
        filename = f"{identifier}-{language}.html"
        commands.append(f"mv {quoted(filename + '.part')} {quoted(filename)}")

    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile(mode="w", encoding="utf-8", delete=True) as batch:
        batch.write("\n".join(commands) + "\n")
        batch.flush()
        try:
            result = subprocess.run(
                ["psftp", "-batch", "-i", str(config.sftp_key_path), "-P", str(config.sftp_port),
                 "-hostkey", config.sftp_host_key, "-b", batch.name, f"{config.sftp_user}@{config.sftp_host}"],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("psftp не установлен на компьютере бота") from exc
    if result.returncode:
        raise RuntimeError("SFTP-загрузка не удалась; проверьте ключ, права и путь /var/www/static/")


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = Telegram(config.token)
        self.store = Store(config.data_dir)

    def tell(self, text: str, **more: object) -> None:
        self.api.call("sendMessage", chat_id=self.config.owner_id, text=text, **more)

    def message(self, message: dict) -> None:
        if message.get("from", {}).get("id") != self.config.owner_id or message.get("chat", {}).get("type") != "private":
            return
        text = message.get("text", "").strip()
        if text.startswith("/start") or text.startswith("/help"):
            self.tell("Пришлите статью Telegram или текст. Затем выберите исходный язык, проверьте HTML-файлы и подтвердите загрузку. Настройки: /provider openrouter|opencode, /model <ID>, /status.")
            return
        if text.startswith("/provider"):
            args = text.split(maxsplit=1)
            if len(args) == 1:
                self.tell("Провайдер: " + self.store.settings()["provider"] + ". Выбор: /provider openrouter или /provider opencode")
                return
            if args[1] not in {"openrouter", "opencode"}:
                self.tell("Доступны openrouter и opencode")
                return
            settings = self.store.settings()
            settings.update({"provider": args[1], "model": ""})
            self.store.save_settings(settings)
            self.tell("Провайдер: " + args[1] + ". Теперь укажите /model <ID модели>.")
            return
        if text.startswith("/model"):
            args = text.split(maxsplit=1)
            if len(args) == 1:
                self.tell("Модель: " + (self.store.settings().get("model") or "не выбрана"))
                return
            model = args[1].strip()
            if len(model) > 150 or not model or any(c.isspace() for c in model):
                self.tell("Укажите один идентификатор модели без пробелов")
                return
            settings = self.store.settings()
            settings["model"] = model
            self.store.save_settings(settings)
            self.tell("Модель: " + model)
            return
        if text.startswith("/status"):
            settings = self.store.settings()
            upload = "настроена" if self.config.upload_ready else "не настроена"
            self.tell(f"Провайдер: {settings['provider']}\nМодель: {settings.get('model') or 'не выбрана'}\nЗагрузка: {upload}")
            return
        try:
            blocks = article_blocks(message)
            render_blocks = [render_block(block, {item: "images/sample.jpg" for item in photos_in(blocks)}) for block in blocks]
            if not render_blocks:
                raise UnsupportedArticle("Статья пуста")
            inline_blocks(blocks)
        except UnsupportedArticle as exc:
            self.tell(str(exc))
            return
        identifier = secrets.token_urlsafe(9)
        content = {key: message[key] for key in ("rich_message", "text") if key in message}
        self.store.save(identifier, {"message": content, "status": "language"})
        self.tell("Статья получена. На каком языке оригинал?", reply_markup={"inline_keyboard": [[
            {"text": "Русский", "callback_data": f"lang:{identifier}:ru"},
            {"text": "Українська", "callback_data": f"lang:{identifier}:uk"},
        ]]})

    def callback(self, query: dict) -> None:
        if query.get("from", {}).get("id") != self.config.owner_id:
            return
        data = query.get("data", "")
        parts = data.split(":")
        self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        if len(parts) < 2:
            return
        action, identifier = parts[:2]
        try:
            if action == "lang" and len(parts) == 3 and parts[2] in {"ru", "uk"}:
                draft = self.store.load(identifier)
                if draft.get("status") != "language":
                    return
                self.tell("Готовлю страницы и перевод. Это может занять несколько минут.")
                prepare(self.config, self.api, self.store, identifier, parts[2])
                self.api.send_document(self.config.owner_id, self.store.path(identifier) / "preview.zip")
                self._review_message(identifier)
            elif action == "retry":
                draft = self.store.load(identifier)
                if draft.get("status") != "review":
                    return
                self.tell("Повторяю перевод выбранной моделью.")
                prepare(self.config, self.api, self.store, identifier, draft["source"])
                self.api.send_document(self.config.owner_id, self.store.path(identifier) / "preview.zip")
                self._review_message(identifier)
            elif action == "publish":
                draft = publish(self.config, self.store, identifier)
                urls = draft["urls"]
                self.tell("Страницы загружены:\n" + urls["ru"] + "\n" + urls["uk"] +
                          "\n\nДля ручной публикации выберите канал кнопкой ниже. Бот сам ничего в канал не отправит.",
                          reply_markup={"inline_keyboard": [[{"text": "Выбрать канал и пост", "switch_inline_query_chosen_chat": {
                              "query": identifier, "allow_channel_chats": True
                          }}]]})
        except (ValueError, RuntimeError, OSError, KeyError, HTTPError, URLError) as exc:
            self.tell("Не удалось: " + str(exc))

    def _review_message(self, identifier: str) -> None:
        self.tell("В архиве обе страницы со стилями и изображениями. Проверьте перевод и оформление. После подтверждения бот загрузит файлы на хостинг.",
                  reply_markup={"inline_keyboard": [[
                      {"text": "Подтвердить загрузку", "callback_data": f"publish:{identifier}"},
                      {"text": "Повторить перевод", "callback_data": f"retry:{identifier}"},
                  ]]})

    def inline(self, query: dict) -> None:
        if query.get("from", {}).get("id") != self.config.owner_id:
            return
        identifier = query.get("query", "").strip()
        results = []
        try:
            draft = self.store.load(identifier)
            if draft.get("status") == "published":
                blocks = inline_blocks(article_blocks(draft["message"]))
                title = article_title(blocks, draft["source"])
                results = [{"type": "article", "id": identifier, "title": title,
                            "description": "Статья Discipline of Senses с двумя ссылками",
                            "input_message_content": {"rich_message": {"blocks": blocks}},
                            "reply_markup": {"inline_keyboard": [[
                                {"text": "Читать на русском", "url": draft["urls"]["ru"]},
                                {"text": "Читати українською", "url": draft["urls"]["uk"]},
                            ]]}}]
        except (OSError, ValueError, KeyError):
            pass
        self.api.call("answerInlineQuery", inline_query_id=query["id"], results=results, is_personal=True, cache_time=0)

    def run(self) -> None:
        offset = self.store.offset()
        while True:
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query", "inline_query"])
                for update in updates:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        self.message(update["message"])
                    elif "callback_query" in update:
                        self.callback(update["callback_query"])
                    elif "inline_query" in update:
                        self.inline(update["inline_query"])
                    self.store.save_offset(offset)
            except KeyboardInterrupt:
                return
            except HTTPError as exc:
                if exc.code in {401, 409}:
                    print(f"Telegram Bot API вернул {exc.code}: проверьте токен или отключите webhook для long polling", flush=True)
                    return
                print(f"Telegram Bot API вернул {exc.code}", flush=True)
                time.sleep(3)
            except Exception as exc:
                print(f"Ошибка обработки Telegram: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(3)


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "owner-id":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Сначала задайте TELEGRAM_BOT_TOKEN")
        code = secrets.token_hex(4)
        print(f"Отправьте своему боту в личный чат: /owner {code}", flush=True)
        api = Telegram(token)
        offset = 0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            updates = api.call("getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                if message.get("chat", {}).get("type") == "private" and message.get("text") == f"/owner {code}":
                    print("TELEGRAM_OWNER_ID=" + str(message["from"]["id"]))
                    return
        raise TimeoutError("Команда не получена за две минуты")
    Bot(Config.from_environment()).run()


if __name__ == "__main__":
    main()
