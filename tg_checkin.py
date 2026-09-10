#!/usr/bin/env python3
"""Run configured, user-authorized Telegram bot check-ins.

This uses Telegram's MTProto API through Telethon. It is intentionally a
small command/button runner: each bot may have a different conversation flow.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError


LOGGER = logging.getLogger("tg_checkin")


@dataclass(frozen=True)
class BotTask:
    bot: str
    command: str
    button: Optional[str] = None
    timeout: float = 30.0
    click_wait: float = 0.0
    pause_after: float = 2.0
    enabled: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Telegram bot check-ins")
    parser.add_argument("--config", default=os.getenv("TG_CONFIG", "bots.json"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print enabled tasks without sending messages",
    )
    return parser.parse_args()


def parse_tasks(data: Any) -> List[BotTask]:
    """Validate JSON data and convert it to executable tasks."""
    items = data.get("bots") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("Config must be a JSON list or an object with a 'bots' list")

    tasks: List[BotTask] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RuntimeError("Task #{} must be a JSON object".format(index))
        try:
            task = BotTask(
                bot=str(item["bot"]).strip(),
                command=str(item["command"]).strip(),
                button=(str(item["button"]) if item.get("button") is not None else None),
                timeout=float(item.get("timeout", 30)),
                click_wait=float(item.get("click_wait", 0)),
                pause_after=float(item.get("pause_after", 2)),
                enabled=bool(item.get("enabled", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid task #{}: {}".format(index, exc)) from exc

        if not task.bot or not task.command:
            raise RuntimeError("Task #{} needs non-empty 'bot' and 'command'".format(index))
        if task.timeout <= 0 or task.click_wait < 0 or task.pause_after < 0:
            raise RuntimeError("Task #{} has an invalid timeout or pause value".format(index))
        tasks.append(task)

    if not any(task.enabled for task in tasks):
        LOGGER.warning("No enabled tasks in config")
    return tasks


def load_tasks(path: Path) -> List[BotTask]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("Config file not found: {}".format(path)) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON in {}: {}".format(path, exc)) from exc

    return parse_tasks(data)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError("Missing environment variable {}".format(name))
    return value


def proxy_from_settings(settings: Any) -> Optional[Tuple[Any, str, int]]:
    """Build a Telethon SOCKS5 proxy tuple from persisted proxy settings."""
    if not isinstance(settings, dict):
        return None
    enabled = settings.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    try:
        port = int(settings.get("port", 7890))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("代理端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("代理端口必须在 1 到 65535 之间")
    try:
        import socks
    except ImportError as exc:
        raise RuntimeError("启用 Telegram 代理需要安装 PySocks") from exc
    return (socks.SOCKS5, host, port)


def load_saved_settings(path: Path) -> dict:
    """Load optional settings written by the Web console."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON in {}".format(path)) from exc
    if not isinstance(data, dict):
        raise RuntimeError("Settings file must contain a JSON object")
    return data


async def ensure_user_authorized(
    client: TelegramClient,
    phone: Optional[str] = None,
    code_callback: Optional[Callable[[], Awaitable[str]]] = None,
    password_callback: Optional[Callable[[], Awaitable[str]]] = None,
) -> None:
    """Log in once, optionally using async callbacks for non-interactive callers."""
    await client.connect()
    if await client.is_user_authorized():
        return

    phone = (phone or "").strip() or required_env("TG_PHONE")
    if code_callback is None:
        LOGGER.info("No authorized session found; Telegram will ask for the login code")
        await client.start(phone=phone)
        return

    LOGGER.info("No authorized session found; requesting a Telegram login code")
    await client.send_code_request(phone)
    code = (await code_callback()).strip()
    if not code:
        raise RuntimeError("Telegram login code cannot be empty")
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        if password_callback is None:
            raise RuntimeError("Telegram account requires a 2FA password")
        password = await password_callback()
        if not password:
            raise RuntimeError("Telegram 2FA password cannot be empty")
        await client.sign_in(password=password)


def message_text(message: Any) -> str:
    text = getattr(message, "raw_text", None) or getattr(message, "text", None)
    if not text:
        return "<non-text response>"
    return " ".join(str(text).split())


def extract_points(*texts: str) -> Optional[float]:
    """Extract the current check-in reward from bot response text."""
    preferred = re.compile(
        r"(?:本次|此次|今日|获得|奖励|增加|赠送|领取|加)[^\d+\-]{0,16}"
        r"([+\-]?\d+(?:\.\d+)?)\s*(?:积分|分|points?)",
        re.IGNORECASE,
    )
    fallback = re.compile(
        r"([+\-]?\d+(?:\.\d+)?)\s*(?:积分|分|points?)|"
        r"(?<!前)(?<!累计)(?:积分|points?)\s*[:：]?\s*([+\-]?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    for text in texts:
        value = " ".join(str(text or "").split())
        match = preferred.search(value) or fallback.search(value)
        if match:
            number = float(match.group(1) or match.group(2))
            return int(number) if number.is_integer() else number
    return None


def button_text_key(text: Any) -> str:
    """Normalize button labels for matching without requiring decorative icons."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = " ".join(value.split()).casefold()
    # Telegram buttons commonly prefix the actionable label with an emoji.
    # Ignore symbol characters for the comparison, while preserving normal
    # punctuation and CJK text.
    value = "".join(
        char for char in value
        if not unicodedata.category(char).startswith(("So", "Sk"))
    )
    return " ".join(value.split()).strip()


def find_button(response: Any, requested: str) -> Any:
    """Find a configured button, allowing decorative icon prefixes."""
    buttons = [button for row in (response.buttons or []) for button in row]
    requested_key = button_text_key(requested)
    matches = []
    for button in buttons:
        actual = getattr(button, "text", "")
        actual_key = button_text_key(actual)
        if actual_key == requested_key or actual_key.endswith(" " + requested_key):
            matches.append(button)

    if not matches:
        available = ", ".join(repr(getattr(button, "text", "")) for button in buttons)
        raise RuntimeError(
            "button {!r} not found; available buttons: {}".format(requested, available or "<none>")
        )
    if len(matches) > 1:
        labels = ", ".join(repr(getattr(button, "text", "")) for button in matches)
        raise RuntimeError("button {!r} is ambiguous; matches: {}".format(requested, labels))
    return matches[0]


async def run_task(client: TelegramClient, task: BotTask) -> Dict[str, Any]:
    LOGGER.info("[%s] sending %s", task.bot, task.command)
    async with client.conversation(
        task.bot,
        timeout=task.timeout,
        exclusive=True,
    ) as conversation:
        sent = await conversation.send_message(task.command)
        response = await conversation.get_response(sent, timeout=task.timeout)
        response_text = message_text(response)
        response_texts = [response_text]
        LOGGER.info("[%s] response: %s", task.bot, response_text[:500])

        if task.button:
            if not response.buttons:
                raise RuntimeError(
                    "response has no buttons; check the configured button text"
                )
            button = find_button(response, task.button)
            callback_answer = await button.click()
            actual_button = getattr(button, "text", task.button)
            LOGGER.info("[%s] clicked button %r", task.bot, actual_button)
            callback_message = getattr(callback_answer, "message", None)
            if callback_message:
                response_texts.append(" ".join(str(callback_message).split()))
                LOGGER.info(
                    "[%s] button response: %s",
                    task.bot,
                    " ".join(str(callback_message).split())[:500],
                )

            # Some bots send a follow-up message, while others edit the same
            # message or only acknowledge the callback. Waiting is optional.
            if task.click_wait:
                try:
                    follow_up = await conversation.get_response(timeout=task.click_wait)
                except asyncio.TimeoutError:
                    LOGGER.info("[%s] no follow-up within %.1fs", task.bot, task.click_wait)
                else:
                    response_texts.append(message_text(follow_up))
                    LOGGER.info("[%s] follow-up: %s", task.bot, message_text(follow_up)[:500])

        return {"points": extract_points(*response_texts), "texts": response_texts}


async def run(config_path: Path, dry_run: bool = False) -> int:
    tasks = load_tasks(config_path)
    enabled_tasks = [task for task in tasks if task.enabled]
    if dry_run:
        for task in enabled_tasks:
            LOGGER.info(
                "dry-run: %s -> %s%s",
                task.bot,
                task.command,
                " (button: {})".format(task.button) if task.button else "",
            )
        return 0

    saved = load_saved_settings(Path(os.getenv("TG_SETTINGS", "./settings.json")))
    api_id_value = os.getenv("TG_API_ID", "").strip() or str(saved.get("api_id", "")).strip()
    try:
        api_id = int(api_id_value)
    except ValueError as exc:
        raise RuntimeError("TG_API_ID must be an integer") from exc
    api_hash = os.getenv("TG_API_HASH", "").strip() or str(saved.get("api_hash", "")).strip()
    if not api_hash:
        raise RuntimeError("Missing environment variable TG_API_HASH or Web API settings")
    phone = os.getenv("TG_PHONE", "").strip() or str(saved.get("phone", "")).strip()
    session_path = os.getenv("TG_SESSION", "./tg_checkin.session")
    proxy = proxy_from_settings({
        "enabled": os.getenv("TG_PROXY_ENABLED", "false"),
        "host": os.getenv("TG_PROXY_HOST", "127.0.0.1"),
        "port": os.getenv("TG_PROXY_PORT", "7890"),
    })

    client = TelegramClient(
        session_path,
        api_id,
        api_hash,
        proxy=proxy,
        request_retries=3,
        connection_retries=3,
        flood_sleep_threshold=60,
    )
    failed = False
    try:
        await ensure_user_authorized(client, phone=phone)
        for position, task in enumerate(enabled_tasks):
            try:
                await run_task(client, task)
            except asyncio.TimeoutError:
                failed = True
                LOGGER.error("[%s] timed out after %.1fs", task.bot, task.timeout)
            except FloodWaitError as exc:
                failed = True
                LOGGER.error("[%s] Telegram flood wait: %ss", task.bot, exc.seconds)
            except RPCError as exc:
                failed = True
                LOGGER.error("[%s] Telegram RPC error: %s", task.bot, exc)
            except Exception:
                failed = True
                LOGGER.exception("[%s] check-in failed", task.bot)

            if position < len(enabled_tasks) - 1 and task.pause_after:
                # A small jitter avoids sending every bot request at the exact
                # same second when this script is run on a schedule.
                delay = task.pause_after + random.uniform(0, min(1.0, task.pause_after))
                await asyncio.sleep(delay)
    finally:
        await client.disconnect()
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    try:
        return asyncio.run(run(Path(args.config), args.dry_run))
    except (RuntimeError, KeyboardInterrupt) as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Telegram check-in run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
