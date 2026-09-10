#!/usr/bin/env python3
"""Small internal web console for the Telegram check-in runner."""

import asyncio
import base64
import json
import logging
import os
import random
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from collections import deque

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # Keep local syntax checks usable without optional login dependency.
    curl_requests = None
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from tg_checkin import (
    BotTask,
    ensure_user_authorized,
    parse_tasks,
    proxy_from_settings,
    run_task,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("TG_CONFIG", str(ROOT / "bots.json")))
SESSION_PATH = os.getenv("TG_SESSION", str(ROOT / "tg_checkin.session"))
SETTINGS_PATH = Path(os.getenv("TG_SETTINGS", str(ROOT / "settings.json")))
SCHEDULE_STATE_PATH = Path(
    os.getenv("TG_SCHEDULE_STATE", str(ROOT / "data" / "schedule.json"))
)
HISTORY_PATH = Path(
    os.getenv("TG_CHECKIN_HISTORY", str(ROOT / "data" / "checkin_history.json"))
)
WEBSITE_SETTINGS_PATH = Path(
    os.getenv("TG_WEBSITE_SETTINGS", str(ROOT / "data" / "website_checkin.json"))
)
WEBSITE_HISTORY_PATH = Path(
    os.getenv("TG_WEBSITE_HISTORY", str(ROOT / "data" / "website_checkin_history.json"))
)
WEBSITE_SCHEDULE_STATE_PATH = Path(
    os.getenv("TG_WEBSITE_SCHEDULE_STATE", str(ROOT / "data" / "website_schedule.json"))
)
PROXY_SETTINGS_PATH = Path(
    os.getenv("TG_PROXY_SETTINGS", str(ROOT / "data" / "proxy_settings.json"))
)
MIHOMO_CONFIG_PATH = Path(
    os.getenv("MIHOMO_CONFIG_PATH", str(ROOT / "data" / "mihomo" / "config.yaml"))
)
MIHOMO_API_URL = os.getenv("MIHOMO_API_URL", "http://mihomo:9090").strip().rstrip("/")
MIHOMO_API_SECRET = os.getenv("MIHOMO_API_SECRET", "").strip()
MIHOMO_PROXY_HOST = os.getenv("MIHOMO_PROXY_HOST", "mihomo").strip() or "mihomo"
try:
    MIHOMO_PROXY_PORT = int(os.getenv("MIHOMO_PROXY_PORT", "7890"))
except ValueError:
    MIHOMO_PROXY_PORT = 7890
MIHOMO_PROVIDER_NAME = "tg-subscription"
MIHOMO_GROUP_NAME = "TG-Proxy"
MIHOMO_HEALTHCHECK_URL = (
    os.getenv("MIHOMO_HEALTHCHECK_URL", "https://telegram.org").strip()
    or "https://telegram.org"
)
MIHOMO_HEALTHCHECK_EXPECTED_STATUS = (
    os.getenv("MIHOMO_HEALTHCHECK_EXPECTED_STATUS", "200-399").strip()
    or "200-399"
)
WEB_DIR = ROOT / "web"
SCHEDULE_TIMEZONE_NAME = os.getenv("TG_SCHEDULE_TIMEZONE", "Asia/Shanghai").strip()
SCHEDULE_TIMEZONE = ZoneInfo(SCHEDULE_TIMEZONE_NAME)
SCHEDULE_WINDOW_MINUTES = 20
NODESEEK_BASE_URL = "https://www.nodeseek.com"
NODESEEK_LOGIN_URL = NODESEEK_BASE_URL + "/signIn.html"
NODESEEK_CHECKIN_URL = NODESEEK_BASE_URL + "/api/attendance?random=false"
NODESEEK_RANDOM_CHECKIN_URL = NODESEEK_BASE_URL + "/api/attendance?random=true"
NODESEEK_IMPERSONATE = os.getenv("NODESEEK_IMPERSONATE", "chrome136").strip() or "chrome136"
NODESEEK_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": NODESEEK_BASE_URL,
    "Referer": NODESEEK_BASE_URL + "/board",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        state["logs"].append({
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "message": self.format(record),
        })


state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "current": None,
    "results": [],
    "logs": deque(maxlen=250),
    "auth_required": None,
    "auth_message": None,
    "trigger": None,
    "next_scheduled_at": None,
}
run_lock = asyncio.Lock()
website_run_lock = asyncio.Lock()
website_login_lock = asyncio.Lock()
profile_lock = asyncio.Lock()
run_task_handle: asyncio.Task = None
scheduler_task: Optional[asyncio.Task] = None
auth_future: Optional[asyncio.Future] = None
website_run_task_handle: Optional[asyncio.Task] = None
website_login_task_handle: Optional[asyncio.Task] = None
website_scheduler_task: Optional[asyncio.Task] = None
bot_profile_cache: Dict[str, Dict[str, Any]] = {}


def load_schedule_state() -> Dict[str, Any]:
    try:
        data = json.loads(SCHEDULE_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        LOGGER.warning("自动签到状态文件无效，将重新开始记录")
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if isinstance(key, str)}


handler = MemoryLogHandler()
handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
logging.getLogger("tg_checkin").addHandler(handler)
LOGGER = logging.getLogger("tg_web")
LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)
logging.getLogger("tg_checkin").setLevel(logging.INFO)

schedule_state = load_schedule_state()


def load_bot_history() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(bot): entry
        for bot, entry in data.items()
        if isinstance(bot, str) and isinstance(entry, dict)
    }


def write_bot_history() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = HISTORY_PATH.with_suffix(HISTORY_PATH.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(bot_history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, HISTORY_PATH)


bot_history = load_bot_history()


def nodeseek_checkin_url(random_checkin: bool) -> str:
    return NODESEEK_RANDOM_CHECKIN_URL if random_checkin else NODESEEK_CHECKIN_URL


def load_website_settings() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "name": "NodeSeek 签到",
        "enabled": False,
        "login_url": NODESEEK_LOGIN_URL,
        "checkin_url": NODESEEK_CHECKIN_URL,
        "random_checkin": False,
        "method": "POST",
        "cookie": "",
        "headers": NODESEEK_HEADERS.copy(),
        "body": "",
        "timeout": 25,
        "success_keyword": "",
        "schedule_enabled": False,
        "schedule_time": "08:00",
        "updated_at": None,
    }
    try:
        data = json.loads(WEBSITE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    # Keep the persisted cookie and scheduling switches, but never allow the
    # generic website fields to override the built-in NodeSeek protocol.
    defaults["enabled"] = data.get("enabled") if isinstance(data.get("enabled"), bool) else False
    defaults["cookie"] = data.get("cookie") if isinstance(data.get("cookie"), str) else ""
    defaults["random_checkin"] = (
        data.get("random_checkin")
        if isinstance(data.get("random_checkin"), bool)
        else False
    )
    defaults["checkin_url"] = nodeseek_checkin_url(defaults["random_checkin"])
    defaults["schedule_enabled"] = (
        data.get("schedule_enabled")
        if isinstance(data.get("schedule_enabled"), bool)
        else False
    )
    defaults["schedule_time"] = (
        data.get("schedule_time")
        if isinstance(data.get("schedule_time"), str)
        and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", data.get("schedule_time"))
        else "08:00"
    )
    defaults["updated_at"] = data.get("updated_at") if isinstance(data.get("updated_at"), str) else None
    return defaults



def load_website_history() -> Dict[str, Any]:
    try:
        data = json.loads(WEBSITE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Ignore history written by the earlier generic-site implementation.
    return data if isinstance(data, dict) and data.get("name") == "NodeSeek 签到" else {}


def load_website_schedule_state() -> Dict[str, Any]:
    try:
        data = json.loads(WEBSITE_SCHEDULE_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_website_history() -> None:
    WEBSITE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = WEBSITE_HISTORY_PATH.with_suffix(WEBSITE_HISTORY_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(website_history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, WEBSITE_HISTORY_PATH)


def write_website_schedule_state() -> None:
    WEBSITE_SCHEDULE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = WEBSITE_SCHEDULE_STATE_PATH.with_suffix(WEBSITE_SCHEDULE_STATE_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(website_schedule_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, WEBSITE_SCHEDULE_STATE_PATH)


website_settings = load_website_settings()
website_history = load_website_history()
website_schedule_state = load_website_schedule_state()
website_runtime: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "status": "idle",
    "message": "",
    "status_code": None,
    "response_preview": "",
    "trigger": None,
    "next_scheduled_at": None,
}
website_login_runtime: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "status": "idle",
    "message": "",
    "cookie_configured": False,
}
website_login_runtime["cookie_configured"] = bool(str(website_settings.get("cookie", "")).strip())


def load_proxy_state() -> Dict[str, Any]:
    defaults = {
        "enabled": False,
        "url": "",
        "name": "Telegram 订阅",
        "selected": "DIRECT",
        "host": MIHOMO_PROXY_HOST,
        "port": MIHOMO_PROXY_PORT,
        "updated_at": None,
    }
    try:
        data = json.loads(PROXY_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    state_data = defaults.copy()
    state_data.update({key: value for key, value in data.items() if key in defaults})
    state_data["host"] = MIHOMO_PROXY_HOST
    state_data["port"] = MIHOMO_PROXY_PORT
    return state_data


def write_proxy_state() -> None:
    PROXY_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = PROXY_SETTINGS_PATH.with_suffix(PROXY_SETTINGS_PATH.suffix + ".tmp")
    data = {key: value for key, value in proxy_state.items() if key not in {"host", "port"}}
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, PROXY_SETTINGS_PATH)


proxy_state = load_proxy_state()


def validate_subscription_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("订阅地址必须是有效的 HTTP 或 HTTPS 链接")
    return url


def validate_website_url(value: Any, label: str, required: bool = False) -> str:
    url = str(value or "").strip()
    if not url and not required:
        return ""
    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("{}必须是有效的 HTTP 或 HTTPS 链接".format(label))
    return url


def parse_website_headers(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, str):
        parsed_pairs = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" not in line:
                raise RuntimeError("请求头每行都应使用“名称: 值”格式")
            parsed_pairs.append(tuple(line.split(":", 1)))
        pairs = parsed_pairs
    else:
        raise RuntimeError("请求头格式无效")

    headers: Dict[str, str] = {}
    header_name_pattern = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
    for raw_name, raw_value in pairs:
        name = str(raw_name).strip()
        header_value = str(raw_value).strip()
        if not name or not header_name_pattern.fullmatch(name):
            raise RuntimeError("请求头名称无效")
        if "\r" in header_value or "\n" in header_value:
            raise RuntimeError("请求头值不能包含换行")
        if name.casefold() == "cookie":
            raise RuntimeError("请使用单独的 Cookie 字段，不要在请求头中填写 Cookie")
        if len(header_value) > 4096:
            raise RuntimeError("请求头值过长")
        headers[name] = header_value
        if len(headers) > 30:
            raise RuntimeError("请求头不能超过 30 个")
    return headers


def website_header_text(headers: Any) -> str:
    if not isinstance(headers, dict):
        return ""
    return "\n".join("{}: {}".format(key, value) for key, value in headers.items())


def write_website_settings(payload: Dict[str, Any]) -> None:
    global website_settings
    current = website_settings

    clear_cookie = payload.get("clear_cookie", False)
    if not isinstance(clear_cookie, bool):
        raise RuntimeError("clear_cookie 必须是布尔值")
    incoming_cookie = payload.get("cookie")
    if clear_cookie:
        cookie = ""
    elif incoming_cookie is None or (isinstance(incoming_cookie, str) and not incoming_cookie.strip()):
        cookie = str(current.get("cookie", ""))
    else:
        cookie = str(incoming_cookie).strip()
    if len(cookie) > 16384 or "\r" in cookie or "\n" in cookie:
        raise RuntimeError("Cookie 格式无效或长度过长")

    enabled = payload.get("enabled", current.get("enabled", False))
    schedule_enabled = payload.get("schedule_enabled", current.get("schedule_enabled", False))
    if not isinstance(enabled, bool) or not isinstance(schedule_enabled, bool):
        raise RuntimeError("启用选项必须是布尔值")
    random_checkin = payload.get("random_checkin", current.get("random_checkin", False))
    if not isinstance(random_checkin, bool):
        raise RuntimeError("签到模式必须是布尔值")
    schedule_time = str(payload.get("schedule_time", current.get("schedule_time", "08:00"))).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time):
        raise RuntimeError("自动签到时间必须是 HH:MM 格式")

    website_settings = {
        "name": "NodeSeek 签到",
        "enabled": enabled,
        "login_url": NODESEEK_LOGIN_URL,
        "checkin_url": nodeseek_checkin_url(random_checkin),
        "random_checkin": random_checkin,
        "method": "POST",
        "cookie": cookie,
        "headers": NODESEEK_HEADERS.copy(),
        "body": "",
        "timeout": 25,
        "success_keyword": "",
        "schedule_enabled": schedule_enabled,
        "schedule_time": schedule_time,
        "updated_at": now_iso(),
    }
    WEBSITE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = WEBSITE_SETTINGS_PATH.with_suffix(WEBSITE_SETTINGS_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(website_settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, WEBSITE_SETTINGS_PATH)


def write_website_cookie(cookie: str) -> None:
    write_website_settings({"cookie": cookie})



def decode_website_body(raw: bytes, headers: Any = None) -> str:
    charset = None
    if headers is not None:
        try:
            charset = headers.get_content_charset()
        except AttributeError:
            charset = None
    return raw.decode(charset or "utf-8", errors="replace")


def perform_website_request(settings: Dict[str, Any]) -> Dict[str, Any]:
    if curl_requests is None:
        raise RuntimeError("NodeSeek 登录/签到需要 curl_cffi 依赖")
    cookie = str(settings.get("cookie", "")).strip()
    if not cookie:
        raise RuntimeError("未配置 NodeSeek Cookie")
    headers = NODESEEK_HEADERS.copy()
    headers["Cookie"] = cookie
    checkin_url = str(settings.get("checkin_url") or NODESEEK_CHECKIN_URL)
    try:
        response = curl_requests.post(
            checkin_url,
            headers=headers,
            json={},
            impersonate=NODESEEK_IMPERSONATE,
            timeout=25,
        )
    except Exception as exc:
        raise RuntimeError("NodeSeek 请求失败：{}".format(str(exc)[:300])) from exc
    body = response.text[:1024 * 1024]
    try:
        data = response.json()
    except ValueError:
        data = {}
    message = str(data.get("message", "")) if isinstance(data, dict) else ""
    success = bool(data.get("success")) if isinstance(data, dict) else False
    already = "已完成签到" in message
    return {
        "status_code": response.status_code,
        "final_url": checkin_url,
        "body": body,
        "message": message,
        "success": success or already,
        "already": already,
    }


def _redact_login_error(message: Any, password: str) -> str:
    text = str(message or "登录失败")[:300]
    return text.replace(password, "[已隐藏]") if password else text


def perform_nodeseek_login(username: str, password: str) -> str:
    if curl_requests is None:
        raise RuntimeError("NodeSeek 登录需要 curl_cffi 依赖")
    session = curl_requests.Session(impersonate=NODESEEK_IMPERSONATE)
    try:
        session.get(NODESEEK_LOGIN_URL, headers=NODESEEK_HEADERS, timeout=25)
        login_headers = NODESEEK_HEADERS.copy()
        login_headers.update({
            "Referer": NODESEEK_LOGIN_URL,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        response = session.post(
            NODESEEK_BASE_URL + "/api/account/signIn",
            json={"username": username, "password": password},
            headers=login_headers,
            timeout=25,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not data.get("success"):
            message = data.get("message") if isinstance(data, dict) else response.text
            raise RuntimeError(_redact_login_error(message, password))
        cookies = session.cookies.get_dict()
        cookie = "; ".join("{}={}".format(name, value) for name, value in cookies.items())
        if not cookie:
            raise RuntimeError("登录成功但未获取到 Cookie")
        return cookie
    except Exception as exc:
        raise RuntimeError(_redact_login_error(exc, password)) from None
    finally:
        session.close()


def mihomo_config() -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "mixed-port": MIHOMO_PROXY_PORT,
        # The app reaches Mihomo over the private Docker network. No Mihomo
        # proxy port is published to the host, so this is not internet-facing.
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "info",
        "external-controller": "0.0.0.0:9090",
        "secret": MIHOMO_API_SECRET,
        "proxy-groups": [{
            "name": MIHOMO_GROUP_NAME,
            "type": "select",
            "proxies": ["DIRECT"],
        }],
        "rules": [f"MATCH,{MIHOMO_GROUP_NAME}"],
    }
    if proxy_state.get("url"):
        config["proxy-providers"] = {
            MIHOMO_PROVIDER_NAME: {
                "type": "http",
                "url": proxy_state["url"],
                "path": f"./proxy_providers/{MIHOMO_PROVIDER_NAME}.yaml",
                "interval": 86400,
                "health-check": {
                    "enable": True,
                    # Check the service this proxy is used for. A generic
                    # Google endpoint can be blocked even when Telegram works.
                    "url": MIHOMO_HEALTHCHECK_URL,
                    "interval": 300,
                    "expected-status": MIHOMO_HEALTHCHECK_EXPECTED_STATUS,
                },
            }
        }
        config["proxy-groups"][0]["use"] = [MIHOMO_PROVIDER_NAME]
    return config


def write_mihomo_config() -> None:
    MIHOMO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = MIHOMO_CONFIG_PATH.with_suffix(MIHOMO_CONFIG_PATH.suffix + ".tmp")
    # JSON is valid YAML and avoids adding a parser solely for generated config.
    temp_path.write_text(json.dumps(mihomo_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, MIHOMO_CONFIG_PATH)


def mihomo_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 3.0) -> Any:
    url = f"{MIHOMO_API_URL}/{path.lstrip('/')}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if MIHOMO_API_SECRET:
        headers["Authorization"] = f"Bearer {MIHOMO_API_SECRET}"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {"raw": raw.decode("utf-8", errors="replace")}


async def mihomo_call(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 3.0) -> Any:
    return await asyncio.to_thread(mihomo_request, method, path, payload, timeout)


def proxy_node_json(node: Dict[str, Any]) -> Dict[str, Any]:
    history = node.get("history") if isinstance(node.get("history"), list) else []
    delay = None
    if history and isinstance(history[-1], dict):
        delay = history[-1].get("delay")
    return {
        "name": str(node.get("name", "")),
        "type": str(node.get("type", "")),
        "alive": node.get("alive"),
        "delay": delay,
        "provider": node.get("provider-name") or MIHOMO_PROVIDER_NAME,
    }


def proxy_runtime_settings() -> Dict[str, Any]:
    return {
        "enabled": bool(proxy_state.get("enabled")),
        "host": MIHOMO_PROXY_HOST,
        "port": MIHOMO_PROXY_PORT,
    }


try:
    write_mihomo_config()
except OSError as exc:
    LOGGER.warning("无法写入 Mihomo 初始配置: %s", exc)

app = FastAPI(title="Telegram Check-in Console", docs_url=None, redoc_url=None)


def read_config() -> List[Dict[str, Any]]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="配置文件不存在") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="配置文件不是有效 JSON") from exc
    items = data.get("bots") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(status_code=500, detail="配置必须是数组或包含 bots 数组")
    return items


def write_config(items: List[Dict[str, Any]]) -> None:
    parse_tasks(items)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, CONFIG_PATH)


def load_settings_file() -> Dict[str, Any]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("API 配置文件不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("API 配置必须是 JSON 对象")
    return data


def effective_settings() -> Dict[str, str]:
    saved = load_settings_file()
    return {
        "api_id": str(os.getenv("TG_API_ID", "").strip() or saved.get("api_id", "")).strip(),
        "api_hash": str(os.getenv("TG_API_HASH", "").strip() or saved.get("api_hash", "")).strip(),
        "phone": str(os.getenv("TG_PHONE", "").strip() or saved.get("phone", "")).strip(),
    }


def write_settings(payload: Dict[str, Any]) -> None:
    current = effective_settings()
    api_id_value = payload.get("api_id", current["api_id"])
    try:
        api_id = int(str(api_id_value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("API ID 必须是整数") from exc
    if api_id <= 0:
        raise RuntimeError("API ID 必须大于 0")

    api_hash = str(payload.get("api_hash", "")).strip() or current["api_hash"]
    if not api_hash:
        raise RuntimeError("请填写 API Hash")
    phone = str(payload.get("phone", current["phone"])).strip()
    data = {"api_id": api_id, "api_hash": api_hash, "phone": phone}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, SETTINGS_PATH)


async def wait_for_auth_input(stage: str, message: str) -> str:
    """Pause the Web run until the browser submits a login value."""
    global auth_future
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    auth_future = future
    state["auth_required"] = stage
    state["auth_message"] = message
    try:
        return await future
    finally:
        if auth_future is future:
            auth_future = None
            state["auth_required"] = None
            state["auth_message"] = None


async def web_login_code() -> str:
    return await wait_for_auth_input("code", "验证码已发送到 Telegram，请输入验证码")


async def web_two_factor_password() -> str:
    return await wait_for_auth_input("password", "账号已开启两步验证，请输入 2FA 密码")


def task_json(task: BotTask) -> Dict[str, Any]:
    return {
        "bot": task.bot,
        "command": task.command,
        "button": task.button,
        "timeout": task.timeout,
        "click_wait": task.click_wait,
        "pause_after": task.pause_after,
        "enabled": task.enabled,
    }


def profile_key(value: Any) -> str:
    return str(value or "").strip().lstrip("@").casefold()


def entity_display_name(entity: Any, fallback: str) -> str:
    parts = [str(getattr(entity, key, "") or "").strip() for key in ("first_name", "last_name")]
    name = " ".join(part for part in parts if part)
    return name or str(getattr(entity, "title", "") or "").strip() or fallback


def photo_data_url(photo: Optional[bytes]) -> Optional[str]:
    if not photo:
        return None
    if photo.startswith(b"\x89PNG"):
        mime = "image/png"
    elif photo.startswith(b"GIF8"):
        mime = "image/gif"
    elif photo.startswith(b"RIFF") and photo[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(photo).decode('ascii')}"


async def lookup_bot_profiles(names: List[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve public bot names without prompting for a Telegram login."""
    unique = {profile_key(name): str(name).strip() for name in names if profile_key(name)}
    if run_is_active():
        return {key: bot_profile_cache[key] for key in unique if key in bot_profile_cache and bot_profile_cache[key].get("available")}
    missing = [key for key in unique if key not in bot_profile_cache]
    if not missing:
        return {key: bot_profile_cache[key] for key in unique if bot_profile_cache[key].get("available")}

    async with profile_lock:
        missing = [key for key in unique if key not in bot_profile_cache]
        if missing:
            client = None
            try:
                settings = effective_settings()
                if not settings["api_id"] or not settings["api_hash"] or not SESSION_PATH:
                    raise RuntimeError("Telegram API 尚未配置")
                client = TelegramClient(
                    SESSION_PATH,
                    int(settings["api_id"]),
                    settings["api_hash"],
                    proxy=proxy_from_settings(proxy_runtime_settings()),
                    request_retries=1,
                    connection_retries=1,
                    flood_sleep_threshold=0,
                )
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Telegram session 尚未登录")
                for key in missing:
                    raw_name = unique[key]
                    try:
                        entity = await client.get_entity(raw_name)
                        photo = await client.download_profile_photo(entity, file=bytes)
                        username = str(getattr(entity, "username", "") or raw_name.lstrip("@"))
                        bot_profile_cache[key] = {
                            "available": True,
                            "username": f"@{username}",
                            "name": entity_display_name(entity, raw_name),
                            "photo": photo_data_url(photo),
                        }
                    except Exception as exc:
                        LOGGER.info("无法读取机器人资料 %s: %s", raw_name, exc)
                        bot_profile_cache[key] = {"available": False}
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                LOGGER.info("机器人资料查询暂不可用: %s", exc)
                for key in missing:
                    bot_profile_cache[key] = {"available": False}
            finally:
                if client is not None:
                    await client.disconnect()

    return {key: bot_profile_cache[key] for key in unique if bot_profile_cache[key].get("available")}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/bot-profiles")
async def get_bot_profiles(payload: Dict[str, Any]) -> Dict[str, Any]:
    names = payload.get("bots")
    if not isinstance(names, list):
        raise HTTPException(status_code=422, detail="请求体需要 bots 数组")
    safe_names = [str(name).strip() for name in names if str(name).strip()]
    return {"profiles": await lookup_bot_profiles(safe_names)}


@app.get("/api/tasks")
async def get_tasks() -> Dict[str, Any]:
    items = read_config()
    try:
        tasks = parse_tasks(items)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"tasks": [task_json(task) for task in tasks]}


@app.put("/api/tasks")
async def update_tasks(payload: Dict[str, Any]) -> Dict[str, Any]:
    items = payload.get("tasks")
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="请求体需要 tasks 数组")
    try:
        write_config(items)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    LOGGER.info("配置已保存，共 %d 个任务", len(items))
    return {"ok": True}


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    try:
        settings = effective_settings()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "api_id": settings["api_id"],
        "phone": settings["phone"],
        "api_hash_configured": bool(settings["api_hash"]),
    }


@app.put("/api/settings")
async def update_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        write_settings(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    LOGGER.info("Telegram API 配置已保存")
    return {"ok": True}


@app.get("/api/website/settings")
async def get_website_settings() -> Dict[str, Any]:
    return {
        "name": "NodeSeek 签到",
        "enabled": bool(website_settings.get("enabled")),
        "login_url": NODESEEK_LOGIN_URL,
        "checkin_url": website_settings.get("checkin_url", NODESEEK_CHECKIN_URL),
        "random_checkin": bool(website_settings.get("random_checkin", False)),
        "method": "POST",
        "cookie_configured": bool(str(website_settings.get("cookie", "")).strip()),
        "schedule_enabled": bool(website_settings.get("schedule_enabled")),
        "schedule_time": website_settings.get("schedule_time", "08:00"),
        "updated_at": website_settings.get("updated_at"),
        "login_runtime": website_login_runtime.copy(),
    }


@app.put("/api/website/settings")
async def update_website_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        write_website_settings(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    website_login_runtime["cookie_configured"] = bool(website_settings.get("cookie"))
    LOGGER.info("NodeSeek 签到配置已保存")
    return {"ok": True, "cookie_configured": bool(website_settings.get("cookie"))}


@app.get("/api/website/status")
async def get_website_status() -> Dict[str, Any]:
    return {
        "name": "NodeSeek 签到",
        "enabled": bool(website_settings.get("enabled")),
        "configured": True,
        "login_url": NODESEEK_LOGIN_URL,
        "checkin_url": website_settings.get("checkin_url", NODESEEK_CHECKIN_URL),
        "random_checkin": bool(website_settings.get("random_checkin", False)),
        "method": "POST",
        "cookie_configured": bool(str(website_settings.get("cookie", "")).strip()),
        "login_runtime": website_login_runtime.copy(),
        "runtime": website_runtime.copy(),
        "last_result": website_history.copy() if website_history else None,
        "schedule": {
            "enabled": bool(website_settings.get("schedule_enabled")),
            "time": website_settings.get("schedule_time", "08:00"),
            "timezone": SCHEDULE_TIMEZONE_NAME,
            "last_run_date": website_schedule_state.get("last_run_date"),
            "next_run_at": website_runtime.get("next_scheduled_at"),
        },
    }


@app.post("/api/website/login", status_code=202)
async def start_website_login_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    if website_login_is_active():
        raise HTTPException(status_code=409, detail="NodeSeek 登录正在运行")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not username.strip() or len(username.strip()) > 256:
        raise HTTPException(status_code=422, detail="请输入有效的 NodeSeek 用户名")
    if not isinstance(password, str) or not password or len(password) > 4096:
        raise HTTPException(status_code=422, detail="请输入有效的 NodeSeek 密码")
    launch_website_login(username.strip(), password)
    return {"ok": True, "message": "NodeSeek 登录已开始"}


@app.post("/api/website/run", status_code=202)
async def start_website_run() -> Dict[str, Any]:
    if website_run_is_active():
        raise HTTPException(status_code=409, detail="网站签到正在运行")
    if not website_settings.get("enabled"):
        raise HTTPException(status_code=422, detail="请先在网站设置中启用网站签到")
    if not website_settings.get("cookie"):
        raise HTTPException(status_code=422, detail="请先登录 NodeSeek 获取 Cookie")
    launch_website_run("manual")
    return {"ok": True, "message": "网站签到已开始"}


def proxy_url_preview(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}"


@app.get("/api/proxy/status")
async def get_proxy_status() -> Dict[str, Any]:
    nodes = [{"name": "DIRECT", "type": "DIRECT", "alive": True, "delay": 0, "provider": "built-in"}]
    mihomo = {"running": False, "version": None, "selected": proxy_state.get("selected", "DIRECT")}
    errors: List[str] = []
    try:
        version = await mihomo_call("GET", "version")
        mihomo["running"] = True
        mihomo["version"] = version.get("version") if isinstance(version, dict) else None
    except (HTTPError, URLError, OSError) as exc:
        errors.append("Mihomo 未连接: {}".format(getattr(exc, "reason", exc)))

    if proxy_state.get("url"):
        try:
            provider = await mihomo_call(
                "GET", f"providers/proxies/{quote(MIHOMO_PROVIDER_NAME, safe='')}"
            )
            provider_nodes = provider.get("proxies", []) if isinstance(provider, dict) else []
            nodes.extend(
                proxy_node_json(node)
                for node in provider_nodes
                if isinstance(node, dict) and node.get("name")
            )
        except (HTTPError, URLError, OSError) as exc:
            errors.append("订阅节点暂未加载: {}".format(getattr(exc, "reason", exc)))

    try:
        group = await mihomo_call("GET", f"proxies/{quote(MIHOMO_GROUP_NAME, safe='')}")
        if isinstance(group, dict) and group.get("now"):
            mihomo["selected"] = group["now"]
            proxy_state["selected"] = group["now"]
    except (HTTPError, URLError, OSError) as exc:
        errors.append("代理组状态不可用: {}".format(getattr(exc, "reason", exc)))

    return {
        "enabled": bool(proxy_state.get("enabled")),
        "name": proxy_state.get("name", "Telegram 订阅"),
        "configured": bool(proxy_state.get("url")),
        "url_preview": proxy_url_preview(str(proxy_state.get("url", ""))),
        "selected": mihomo["selected"],
        "nodes": nodes,
        "mihomo": mihomo,
        "error": "；".join(errors) if errors else None,
    }


@app.put("/api/proxy/subscription")
async def import_proxy_subscription(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        url = validate_subscription_url(payload.get("url"))
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    name = str(payload.get("name") or "Telegram 订阅").strip()[:80] or "Telegram 订阅"
    proxy_state.update({
        "url": url,
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "selected": "DIRECT",
        "updated_at": now_iso(),
    })
    write_proxy_state()
    write_mihomo_config()
    try:
        await mihomo_call("PUT", "configs?force=true", {})
        reloaded = True
    except (HTTPError, URLError, OSError) as exc:
        reloaded = False
        LOGGER.warning("Mihomo 配置已保存，但重载失败: %s", exc)
    return {
        "ok": True,
        "reloaded": reloaded,
        "message": "订阅已导入" if reloaded else "订阅已保存，Mihomo 尚未连接",
    }


@app.delete("/api/proxy/subscription")
async def delete_proxy_subscription() -> Dict[str, Any]:
    proxy_state.update({"url": "", "name": "Telegram 订阅", "enabled": False, "selected": "DIRECT", "updated_at": now_iso()})
    write_proxy_state()
    write_mihomo_config()
    try:
        await mihomo_call("PUT", "configs?force=true", {})
    except (HTTPError, URLError, OSError):
        pass
    return {"ok": True}


@app.put("/api/proxy/settings")
async def update_proxy_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled 必须是布尔值")
    if enabled and not proxy_state.get("url"):
        raise HTTPException(status_code=422, detail="请先导入订阅链接")
    proxy_state["enabled"] = enabled
    write_proxy_state()
    return {"ok": True, "enabled": enabled}


@app.post("/api/proxy/refresh")
async def refresh_proxy_subscription() -> Dict[str, Any]:
    if not proxy_state.get("url"):
        raise HTTPException(status_code=422, detail="请先导入订阅链接")
    try:
        await mihomo_call(
            "PUT", f"providers/proxies/{quote(MIHOMO_PROVIDER_NAME, safe='')}", {}
        )
    except (HTTPError, URLError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Mihomo 暂不可用: {}".format(getattr(exc, "reason", exc))) from exc
    return {"ok": True, "message": "节点刷新已触发"}


@app.put("/api/proxy/select")
async def select_proxy_node(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 256 or any(char in name for char in "\r\n"):
        raise HTTPException(status_code=422, detail="节点名称无效")
    try:
        await mihomo_call(
            "PUT",
            f"proxies/{quote(MIHOMO_GROUP_NAME, safe='')}",
            {"name": name},
        )
    except (HTTPError, URLError, OSError) as exc:
        raise HTTPException(status_code=503, detail="节点切换失败: {}".format(getattr(exc, "reason", exc))) from exc
    proxy_state["selected"] = name
    write_proxy_state()
    return {"ok": True, "selected": name}


@app.post("/api/auth")
async def submit_auth(payload: Dict[str, Any]) -> Dict[str, Any]:
    global auth_future
    stage = state["auth_required"]
    if stage not in {"code", "password"} or auth_future is None or auth_future.done():
        raise HTTPException(status_code=409, detail="当前不需要登录输入")
    value = payload.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=422, detail="请输入登录信息")
    if stage == "code":
        value = value.strip()
        if not value:
            raise HTTPException(status_code=422, detail="请输入验证码")
    auth_future.set_result(value)
    return {"ok": True}


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return {
        "running": state["running"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "current": state["current"],
        "results": state["results"],
        "logs": list(state["logs"]),
        "auth_required": state["auth_required"],
        "auth_message": state["auth_message"],
        "trigger": state["trigger"],
        "bot_history": list(bot_history.values()),
        "schedule": {
            "enabled": True,
            "timezone": SCHEDULE_TIMEZONE_NAME,
            "window": "00:00-00:20",
            "next_run_at": state["next_scheduled_at"],
            "last_run_date": schedule_state.get("last_run_date"),
        },
    }


def schedule_window(day) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime_time.min, tzinfo=SCHEDULE_TIMEZONE)
    return start, start + timedelta(minutes=SCHEDULE_WINDOW_MINUTES)


def schedule_now() -> datetime:
    return datetime.now(SCHEDULE_TIMEZONE)


def write_schedule_state() -> None:
    SCHEDULE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SCHEDULE_STATE_PATH.with_suffix(SCHEDULE_STATE_PATH.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(schedule_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, SCHEDULE_STATE_PATH)


def run_is_active() -> bool:
    return state["running"] or (
        run_task_handle is not None and not run_task_handle.done()
    )


def launch_run(trigger: str) -> None:
    global run_task_handle
    run_task_handle = asyncio.create_task(execute_all(trigger))


def website_run_is_active() -> bool:
    return website_runtime["running"] or (
        website_run_task_handle is not None and not website_run_task_handle.done()
    )


def website_login_is_active() -> bool:
    return website_login_runtime["running"] or (
        website_login_task_handle is not None and not website_login_task_handle.done()
    )


def launch_website_login(username: str, password: str) -> None:
    global website_login_task_handle
    website_login_task_handle = asyncio.create_task(
        execute_website_login(username, password)
    )


def launch_website_run(trigger: str) -> None:
    global website_run_task_handle
    website_run_task_handle = asyncio.create_task(execute_website_checkin(trigger))


async def execute_website_login(username: str, password: str) -> None:
    async with website_login_lock:
        website_login_runtime.update({
            "running": True,
            "started_at": now_iso(),
            "finished_at": None,
            "status": "running",
            "message": "正在请求 NodeSeek 登录接口",
            "cookie_configured": False,
        })
        try:
            cookie = await asyncio.to_thread(perform_nodeseek_login, username, password)
            write_website_cookie(cookie)
            website_login_runtime.update({
                "status": "success",
                "message": "NodeSeek 登录成功，Cookie 已保存",
                "cookie_configured": True,
            })
        except Exception as exc:
            website_login_runtime.update({
                "status": "failed",
                "message": str(exc)[:300] or "NodeSeek 登录失败",
                "cookie_configured": bool(website_settings.get("cookie")),
            })
        finally:
            website_login_runtime["running"] = False
            website_login_runtime["finished_at"] = now_iso()


async def auto_schedule_loop() -> None:
    """Run once at a random time in each day's local midnight window."""
    while True:
        now = schedule_now()
        today = now.date()
        start, end = schedule_window(today)
        last_run_date = schedule_state.get("last_run_date")

        if last_run_date == today.isoformat() or now >= end:
            start, end = schedule_window(today + timedelta(days=1))
            target = start + timedelta(seconds=random.uniform(0, SCHEDULE_WINDOW_MINUTES * 60))
        else:
            target = now + timedelta(seconds=random.uniform(0, max(0, (end - now).total_seconds())))

        state["next_scheduled_at"] = target.isoformat(timespec="seconds")
        LOGGER.info(
            "自动签到已安排在 %s（%s）",
            state["next_scheduled_at"],
            SCHEDULE_TIMEZONE_NAME,
        )
        await asyncio.sleep(max(0, (target - schedule_now()).total_seconds()))

        target_date = target.date().isoformat()
        if schedule_state.get("last_run_date") == target_date:
            continue

        schedule_state["last_run_date"] = target_date
        write_schedule_state()
        state["next_scheduled_at"] = None
        if run_is_active():
            LOGGER.warning("自动签到时间到达，但已有签到运行中，跳过本次自动签到")
            continue
        LOGGER.info("开始自动签到（日期 %s）", target_date)
        launch_run("scheduled")


async def website_schedule_loop() -> None:
    """Run the website check-in once per day at the configured local time."""
    while True:
        settings = website_settings.copy()
        if not settings.get("enabled") or not settings.get("checkin_url") or not settings.get("cookie") or not settings.get("schedule_enabled"):
            website_runtime["next_scheduled_at"] = None
            await asyncio.sleep(30)
            continue

        now = schedule_now()
        hour, minute = map(int, str(settings.get("schedule_time", "08:00")).split(":"))
        target = datetime.combine(
            now.date(),
            datetime_time(hour, minute),
            tzinfo=SCHEDULE_TIMEZONE,
        )
        today = now.date().isoformat()
        if target <= now or website_schedule_state.get("last_run_date") == today:
            target = target + timedelta(days=1)
        website_runtime["next_scheduled_at"] = target.isoformat(timespec="seconds")
        await asyncio.sleep(max(0, (target - schedule_now()).total_seconds()))

        target_date = target.date().isoformat()
        if website_schedule_state.get("last_run_date") == target_date:
            continue
        if not website_settings.get("enabled") or not website_settings.get("schedule_enabled"):
            continue
        website_schedule_state["last_run_date"] = target_date
        write_website_schedule_state()
        website_runtime["next_scheduled_at"] = None
        if website_run_is_active():
            LOGGER.warning("网站自动签到时间到达，但已有网站签到运行中，跳过本次自动签到")
            continue
        LOGGER.info("开始网站自动签到（日期 %s）", target_date)
        launch_website_run("scheduled")


async def execute_website_checkin(trigger: str = "manual") -> None:
    global website_history
    async with website_run_lock:
        started_at = now_iso()
        website_runtime.update({
            "running": True,
            "started_at": started_at,
            "finished_at": None,
            "status": "running",
            "message": "正在发送签到请求",
            "status_code": None,
            "response_preview": "",
            "trigger": trigger,
        })
        result: Dict[str, Any] = {
            "name": website_settings.get("name", "网站签到"),
            "status": "failed",
            "message": "",
            "status_code": None,
            "response_preview": "",
            "finished_at": None,
            "trigger": trigger,
        }
        try:
            settings = website_settings.copy()
            if not settings.get("enabled"):
                raise RuntimeError("网站签到未启用")
            if not settings.get("checkin_url"):
                raise RuntimeError("未配置签到地址")
            if not settings.get("cookie"):
                raise RuntimeError("未配置 Cookie，请先登录网站并保存 Cookie")
            response = await asyncio.to_thread(perform_website_request, settings)
            body = str(response.get("body", ""))
            status_code = int(response.get("status_code", 0))
            message = str(response.get("message", "")).strip()
            if 200 <= status_code < 400 and response.get("success"):
                status = "success"
                result_message = message or "NodeSeek 签到成功"
            elif 200 <= status_code < 400:
                status = "failed"
                result_message = message or "NodeSeek 返回了未成功的响应"
            else:
                status = "failed"
                result_message = message or "NodeSeek 返回 HTTP {}".format(status_code)
            result.update({
                "status": status,
                "message": result_message,
                "status_code": status_code,
                "response_preview": body[:2000],
            })
        except HTTPError as exc:
            body = decode_website_body(exc.read(65536), exc.headers)
            result.update({
                "status": "failed",
                "message": "网站返回 HTTP {}".format(exc.code),
                "status_code": exc.code,
                "response_preview": body[:2000],
            })
        except (URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            result.update({"status": "failed", "message": "网站请求失败：{}".format(reason)})
        except Exception as exc:
            LOGGER.exception("website check-in failed")
            result.update({"status": "failed", "message": str(exc)})
        finally:
            result["finished_at"] = now_iso()
            website_history = result
            website_runtime.update({
                "running": False,
                "finished_at": result["finished_at"],
                "status": result["status"],
                "message": result["message"],
                "status_code": result["status_code"],
                "response_preview": result["response_preview"],
                "trigger": trigger,
            })
            write_website_history()


async def execute_all(trigger: str = "manual") -> None:
    global auth_future
    async with run_lock:
        state["running"] = True
        state["started_at"] = now_iso()
        state["finished_at"] = None
        state["current"] = None
        state["results"] = []
        state["auth_required"] = None
        state["auth_message"] = None
        state["trigger"] = trigger
        auth_future = None
        client = None
        try:
            items = read_config()
            tasks = [task for task in parse_tasks(items) if task.enabled]
            if not tasks:
                LOGGER.warning("没有启用的签到任务")
                return
            settings = effective_settings()
            try:
                api_id = int(settings["api_id"])
            except ValueError as exc:
                raise RuntimeError("请先在 Web 设置中填写有效的 API ID") from exc
            if not settings["api_hash"]:
                raise RuntimeError("请先在 Web 设置中填写 API Hash")

            client = TelegramClient(
                SESSION_PATH,
                api_id,
                settings["api_hash"],
                proxy=proxy_from_settings(proxy_runtime_settings()),
                request_retries=3,
                connection_retries=3,
                flood_sleep_threshold=60,
            )
            await ensure_user_authorized(
                client,
                phone=settings["phone"],
                code_callback=web_login_code,
                password_callback=web_two_factor_password,
            )
            for position, task in enumerate(tasks):
                state["current"] = task.bot
                result = {"bot": task.bot, "command": task.command, "status": "running", "started_at": now_iso()}
                state["results"].append(result)
                points = None
                try:
                    outcome = await run_task(client, task)
                    if isinstance(outcome, dict):
                        points = outcome.get("points")
                except asyncio.TimeoutError:
                    result.update(status="timeout", message="等待回复超时")
                except FloodWaitError as exc:
                    result.update(status="failed", message="Telegram 要求等待 {} 秒".format(exc.seconds))
                except RPCError as exc:
                    result.update(status="failed", message="Telegram RPC 错误: {}".format(exc))
                except Exception as exc:
                    LOGGER.exception("[%s] web check-in failed", task.bot)
                    result.update(status="failed", message=str(exc))
                else:
                    result["status"] = "success"
                    result["message"] = "完成"
                result["finished_at"] = now_iso()
                result["points"] = points
                bot_history[task.bot] = {
                    "bot": task.bot,
                    "status": result["status"],
                    "message": result.get("message", ""),
                    "points": points,
                    "last_run_at": result["finished_at"],
                    "trigger": trigger,
                }
                write_bot_history()
                if position < len(tasks) - 1 and task.pause_after:
                    await asyncio.sleep(task.pause_after + random.uniform(0, min(1.0, task.pause_after)))
        except Exception as exc:
            LOGGER.exception("web check-in run failed")
            state["results"].append({"bot": "系统", "status": "failed", "message": str(exc), "finished_at": now_iso()})
        finally:
            state["running"] = False
            state["current"] = None
            state["finished_at"] = now_iso()
            state["auth_required"] = None
            state["auth_message"] = None
            if auth_future is not None and not auth_future.done():
                auth_future.cancel()
            auth_future = None
            if client is not None:
                await client.disconnect()


@app.post("/api/run", status_code=202)
async def start_run() -> Dict[str, Any]:
    if run_is_active():
        raise HTTPException(status_code=409, detail="签到正在运行")
    launch_run("manual")
    return {"ok": True, "message": "签到任务已开始"}


@app.on_event("startup")
async def start_scheduler() -> None:
    global scheduler_task, website_scheduler_task
    scheduler_task = asyncio.create_task(auto_schedule_loop())
    website_scheduler_task = asyncio.create_task(website_schedule_loop())


@app.on_event("shutdown")
async def stop_scheduler() -> None:
    global scheduler_task, website_scheduler_task, website_login_task_handle
    active_tasks = [
        task
        for task in (scheduler_task, website_scheduler_task, website_login_task_handle)
        if task is not None
    ]
    for task in active_tasks:
        task.cancel()
    for task in active_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    scheduler_task = None
    website_scheduler_task = None
    website_login_task_handle = None


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}
