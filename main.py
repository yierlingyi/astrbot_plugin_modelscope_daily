"""魔搭社区（ModelScope）每日签到插件。

每天北京时间 06:00 使用配置中的 Cookie 触发一次签到，并把结果主动推送到
配置指定的会话。签到结果只有三种：未配置 Cookie、签到失败、签到成功。

签到原理：请求 ``/api/v1/users/login/info`` 本身会触发当日签到（重复调用幂等），
随后通过 ``/openapi/v1/magicubes/transactions`` 查询当日到账记录确认是否成功。
参考实现：https://github.com/L0NE-6/ModelScope-Daily
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_VERSION = "1.0.1"

SITE = "https://www.modelscope.cn"
LOGIN_INFO_PATH = "/api/v1/users/login/info"
TRANSACTIONS_PATH = "/openapi/v1/magicubes/transactions"
BALANCE_PATH = "/openapi/v1/magicubes/balance"
REFERER = "https://www.modelscope.cn/models?filter=inference_type&page=1&tabKey=task"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"
)

# 魔粒账单里属于「每日签到」的规则，其余 EARN 记录（如活动奖励）不算签到。
DAILY_RULE_KEYS = {"daily_active", "aliyun_bindlogin"}

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
CRON_HOUR = 6
CRON_MINUTE = 0

STATUS_NO_COOKIE = "no_cookie"
STATUS_FAILED = "failed"
STATUS_SUCCESS = "success"

RESULT_TITLES = {
    STATUS_NO_COOKIE: "⚠️ 魔搭每日签到未执行",
    STATUS_FAILED: "❌ 魔搭每日签到失败",
    STATUS_SUCCESS: "✅ 魔搭每日签到成功",
}


def beijing_now() -> datetime:
    """返回当前北京时间。

    Returns:
        带 +08:00 时区信息的当前时间。
    """
    return datetime.now(BEIJING)


def today_in_beijing() -> str:
    """返回北京时间的今天日期字符串。

    Returns:
        形如 ``2026-10-31`` 的日期字符串。
    """
    return beijing_now().strftime("%Y-%m-%d")


def extract_session_id(cookie_text: str) -> str:
    """从用户粘贴的 Cookie 中提取 m_session_id。

    Args:
        cookie_text: 浏览器里复制的整段 Cookie，或单独的 m_session_id / ms- 令牌。

    Returns:
        m_session_id 的值；没有找到时返回空字符串。
    """
    raw = (cookie_text or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    match = re.search(r"(?:^|[;\n])\s*m_session_id\s*=\s*([^;\n]+)", raw)
    if match:
        return match.group(1).strip().strip('"')
    # 容错：用户只粘贴了会话值本身（不含分号与等号）。
    if ";" not in raw and "=" not in raw:
        return raw.split("\n")[0].strip()
    return ""


def build_cookie_header(cookie_text: str, session_id: str) -> str:
    """生成请求头里的 Cookie 值。

    Args:
        cookie_text: 用户填写的整段 Cookie、m_session_id 键值对或 ms- 令牌。
        session_id: 已提取出的 m_session_id。

    Returns:
        可直接放进 Cookie 请求头的字符串。
    """
    raw = (cookie_text or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    raw = re.sub(r"\r?\n", "; ", raw).strip().strip("; ")
    if "=" in raw:
        # 用户填的是整段 Cookie，原样透传，避免丢失其它风控 Cookie。
        return raw
    return f"m_session_id={session_id}"


def record_beijing_date(record: dict[str, Any]) -> str:
    """取出一条魔粒账单记录对应的北京日期。

    魔搭账单以北京时间自然日结算，字段值可能是不带时区的北京时间字符串；
    如果字段带时区（如 Z / +00:00），会先换算成北京时间再取日期。

    Args:
        record: 账单记录。

    Returns:
        形如 ``2026-10-31`` 的日期字符串；无法识别时返回空字符串。
    """
    raw = None
    for key in ("gmt_created", "gmt_create", "created_at", "create_time"):
        value = record.get(key)
        if value is not None and str(value).strip():
            raw = value
            break
    if raw is None:
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            moment = datetime.fromtimestamp(raw, tz=timezone.utc)
            return moment.astimezone(BEIJING).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        # 退化为取前 10 位，兼容 ``2026-10-31 06:00:00`` 这类格式。
        return text[:10]
    if moment.tzinfo is not None:
        moment = moment.astimezone(BEIJING)
    return moment.strftime("%Y-%m-%d")


def envelope_data(payload: dict[str, Any]) -> dict[str, Any]:
    """取出魔搭响应信封中的 Data 字段。

    Args:
        payload: 接口返回的 JSON 对象。

    Returns:
        Data（或 data）字段；不存在或类型不符时返回空字典。
    """
    data = payload.get("Data")
    if not isinstance(data, dict):
        data = payload.get("data")
    return data if isinstance(data, dict) else {}


def envelope_message(payload: dict[str, Any]) -> str:
    """取出魔搭响应信封中的错误信息。

    Args:
        payload: 接口返回的 JSON 对象。

    Returns:
        错误信息文本；没有时返回空字符串。
    """
    for key in ("Message", "message", "msg"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def envelope_ok(payload: dict[str, Any]) -> bool:
    """判断魔搭响应信封是否表示成功。

    Args:
        payload: 接口返回的 JSON 对象。

    Returns:
        Success 为 true 或 Code 为 200 时返回 True。
    """
    for key in ("Success", "success"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    code = payload.get("Code", payload.get("code"))
    if code is None:
        return False
    try:
        return int(code) == 200
    except (TypeError, ValueError):
        return False


def daily_records(records: Any) -> list[dict[str, Any]]:
    """筛选出北京时间今天的签到到账记录。

    Args:
        records: 魔粒账单接口返回的 records 字段。

    Returns:
        当天的签到类型 EARN 记录列表。
    """
    if not isinstance(records, list):
        return []
    today = today_in_beijing()
    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("type") or "").upper() != "EARN":
            continue
        if record_beijing_date(record) != today:
            continue
        if str(record.get("rule_key") or "").lower() not in DAILY_RULE_KEYS:
            continue
        result.append(record)
    return result


def record_credit(records: list[dict[str, Any]] | None) -> float:
    """汇总账单记录里的到账魔粒数量。

    Args:
        records: 账单记录列表。

    Returns:
        所有记录的到账合计（无法解析的金额按 0 处理）。
    """
    credit = 0.0
    for record in records or []:
        if not isinstance(record, dict):
            continue
        amount = record.get("total_amount")
        try:
            if amount is not None:
                credit += float(str(amount).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return credit


def format_credit(value: float) -> str:
    """把魔粒数量格式化成不带多余小数的文本。

    Args:
        value: 魔粒数量。

    Returns:
        整数原样输出，小数保留两位。
    """
    return str(int(value)) if value.is_integer() else str(round(value, 2))


class ModelScopeDailyPlugin(Star):
    """魔搭社区每日签到插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        try:
            self.request_timeout = max(5, int(self.config.get("request_timeout", 30)))
        except (TypeError, ValueError):
            self.request_timeout = 30
        self.scheduler: AsyncIOScheduler | None = None

    async def initialize(self) -> None:
        """启动每天北京时间 06:00 的签到任务。"""
        self._shutdown_scheduler()
        try:
            scheduler = AsyncIOScheduler(timezone=BEIJING)
            scheduler.add_job(
                self._daily_check_in,
                trigger=CronTrigger(
                    hour=CRON_HOUR,
                    minute=CRON_MINUTE,
                    timezone=BEIJING,
                ),
                id="modelscope_daily_check_in",
                name="魔搭每日签到",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
            scheduler.start()
            self.scheduler = scheduler
            logger.info(
                "魔搭每日签到插件 v%s 已启动：每天 %02d:%02d（北京时间）执行。",
                PLUGIN_VERSION,
                CRON_HOUR,
                CRON_MINUTE,
            )
        except Exception:
            self.scheduler = None
            logger.exception("启动魔搭每日签到定时任务失败。")

    async def terminate(self) -> None:
        """停止签到定时任务。"""
        self._shutdown_scheduler()

    def _shutdown_scheduler(self) -> None:
        """关闭并清理定时任务调度器。"""
        scheduler = self.scheduler
        self.scheduler = None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)

    @filter.command("ms签到")
    async def manual_check_in(self, event: AstrMessageEvent):
        """手动立即执行一次魔搭每日签到。"""
        result = await self._run_check_in()
        yield event.plain_result(result["text"])

    async def _daily_check_in(self) -> None:
        """每天 06:00 执行的签到任务，结果推送到配置的会话。"""
        logger.info("开始执行魔搭每日签到。")
        try:
            result = await self._run_check_in()
        except Exception:
            logger.exception("魔搭每日签到执行出错。")
            result = {
                "status": STATUS_FAILED,
                "text": self._build_message(
                    STATUS_FAILED,
                    ["原因：插件内部错误，请查看 AstrBot 日志。"],
                ),
            }
        logger.info("魔搭每日签到结果（%s）：\n%s", result["status"], result["text"])
        await self._notify(result["text"])

    async def _notify(self, text: str) -> bool:
        """把签到结果推送到配置指定的会话。

        Args:
            text: 推送文本。

        Returns:
            是否推送成功；没有配置推送会话时返回 False。
        """
        umo = str(self.config.get("notify_umo") or "").strip()
        if not umo:
            logger.warning("未配置 notify_umo，签到结果只写入日志。")
            return False
        try:
            sent = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            logger.error("推送签到结果到 %s 失败：%s", umo, exc)
            return False
        if not sent:
            logger.error("推送签到结果失败：找不到会话 %s 对应的平台。", umo)
        return sent

    async def _get_json(
        self,
        cookie_header: str,
        path: str,
        what: str,
    ) -> tuple[int, dict[str, Any], str]:
        """请求魔搭接口并解析 JSON 响应。

        Args:
            cookie_header: 放进 Cookie 请求头的字符串。
            path: 接口路径，以 / 开头。
            what: 接口用途，仅用于日志。

        Returns:
            (HTTP 状态码, 解析后的 JSON 对象, 响应文本片段)。

        Raises:
            aiohttp.ClientError: 网络请求失败。
            asyncio.TimeoutError: 请求超时。
        """
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
            "x-modelscope-accept-language": "zh_CN",
            "X-Modelscope-Trace-Id": str(uuid.uuid4()),
            "Cookie": cookie_header,
        }
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
            async with http.get(f"{SITE}{path}") as response:
                status = response.status
                body = (await response.read()).decode("utf-8", errors="replace")
        logger.debug("魔搭接口「%s」返回 HTTP %s", what, status)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return status, payload, body[:200].replace("\n", " ")

    async def _run_check_in(self) -> dict[str, str]:
        """执行一次签到，返回状态与用于推送的文案。

        Returns:
            ``{"status": "no_cookie" | "failed" | "success", "text": "..."}``。
        """
        cookie_text = str(self.config.get("cookie") or "").strip()
        if not cookie_text:
            return {
                "status": STATUS_NO_COOKIE,
                "text": self._build_message(
                    STATUS_NO_COOKIE,
                    ["原因：尚未在插件配置中填写 Cookie。"],
                ),
            }

        session_id = extract_session_id(cookie_text)
        if not session_id:
            return {
                "status": STATUS_FAILED,
                "text": self._build_message(
                    STATUS_FAILED,
                    ["原因：Cookie 中未找到 m_session_id，请重新复制完整 Cookie。"],
                ),
            }
        cookie_header = build_cookie_header(cookie_text, session_id)

        # 0. 先读一次今天的账单，用来区分「本次新到账」和「今天已签到过」。
        # 读失败不影响签到，只是不展示这层区分。
        before_records: list[dict[str, Any]] | None = None
        try:
            before_status, before_payload, _ = await self._get_json(
                cookie_header,
                f"{TRANSACTIONS_PATH}?page=1&page_size=50",
                "签到前账单",
            )
            if before_status == 200 and envelope_ok(before_payload):
                before_data = envelope_data(before_payload)
                before_raw = before_data.get("records")
                if before_raw is None:
                    before_raw = before_data.get("Records")
                if isinstance(before_raw, list):
                    before_records = daily_records(before_raw)
        except Exception as exc:
            logger.debug("签到前查询账单失败，本次不区分重复签到：%s", exc)

        # 1. 请求登录接口，该请求本身会触发当日签到（重复调用幂等）。
        try:
            status, payload, snippet = await self._get_json(
                cookie_header,
                LOGIN_INFO_PATH,
                "登录签到",
            )
        except Exception as exc:
            return {
                "status": STATUS_FAILED,
                "text": self._build_message(
                    STATUS_FAILED,
                    [f"原因：请求魔搭登录接口失败：{exc}"],
                ),
            }

        identity = envelope_data(payload)
        display = ""
        for key in ("NickName", "Name", "nickname", "name", "HavanaId"):
            value = identity.get(key)
            if value is not None and str(value).strip():
                display = str(value).strip()
                break

        # 登录接口使用 {Code, Message, Success, Data} 信封；只要返回了 Data 且没有
        # 明确的 Success=false，就认为登录成功，避免上游信封字段调整后误判。
        explicit_failure = (
            payload.get("Success") is False or payload.get("success") is False
        )
        login_ok = envelope_ok(payload) or (bool(identity) and not explicit_failure)

        if status == 401:
            reason = "Cookie 已失效（HTTP 401），请重新登录魔搭并更新 Cookie。"
        elif status == 403:
            reason = "被魔搭风控拒绝（HTTP 403），请稍后重试或更换网络。"
        elif status < 200 or status >= 300:
            reason = f"登录接口返回 HTTP {status}：{snippet}"
        elif not login_ok:
            detail = envelope_message(payload)
            if not detail:
                code = payload.get("Code", payload.get("code"))
                detail = (
                    f"魔搭返回 Code={code!r}"
                    if payload
                    else f"响应不是有效 JSON：{snippet}"
                )
            reason = f"签到请求被拒绝：{detail}"
        else:
            reason = ""

        if reason:
            return {
                "status": STATUS_FAILED,
                "text": self._build_message(STATUS_FAILED, [f"原因：{reason}"]),
            }

        # 2. 查询当日魔粒账单，确认签到是否真的到账。到账可能有延迟，最多查 3 次。
        records: list[dict[str, Any]] = []
        verify_error = ""
        for attempt in range(3):
            try:
                tx_status, tx_payload, tx_snippet = await self._get_json(
                    cookie_header,
                    f"{TRANSACTIONS_PATH}?page=1&page_size=50",
                    "魔粒账单",
                )
            except Exception as exc:
                verify_error = f"请求魔粒账单接口失败：{exc}"
            else:
                if tx_status in (401, 403):
                    # 鉴权 / 风控问题重试也没用，直接结束。
                    verify_error = (
                        "Cookie 已失效（HTTP 401）。"
                        if tx_status == 401
                        else "被魔搭风控拒绝（HTTP 403）。"
                    )
                    break
                if tx_status < 200 or tx_status >= 300:
                    verify_error = f"魔粒账单接口返回 HTTP {tx_status}。"
                else:
                    tx_data = envelope_data(tx_payload)
                    raw_records = tx_data.get("records")
                    if raw_records is None:
                        raw_records = tx_data.get("Records")
                    records = daily_records(raw_records)
                    if records:
                        verify_error = ""
                        break
                    if isinstance(raw_records, list) and raw_records:
                        logger.debug(
                            "今日账单有 %s 条记录但未匹配签到字段，首条记录：%s",
                            len(raw_records),
                            str(raw_records[0])[:300],
                        )
                    if not envelope_ok(tx_payload):
                        detail = envelope_message(tx_payload)
                        if not detail:
                            code = tx_payload.get("Code", tx_payload.get("code"))
                            detail = (
                                f"魔搭返回 code={code!r}"
                                if tx_payload
                                else f"响应不是有效 JSON：{tx_snippet}"
                            )
                        verify_error = f"魔粒账单接口返回失败：{detail}"
                    else:
                        verify_error = ""
            if attempt < 2:
                await asyncio.sleep(3)

        if not records:
            reason = (
                "未能确认今日签到到账（签到请求已发出，但账单中没有对应记录，"
                "可能是到账延迟或账号不在活动范围，可稍后用 /ms签到 复查）。"
            )
            if verify_error:
                reason = f"未能确认今日签到到账：{verify_error}"
            return {
                "status": STATUS_FAILED,
                "text": self._build_message(STATUS_FAILED, [f"原因：{reason}"]),
            }

        credit = record_credit(records)
        before_credit = record_credit(before_records)
        titles: list[str] = []
        for record in records:
            title = str(record.get("title") or record.get("rule_key") or "").strip()
            if title and title not in titles:
                titles.append(title)

        # 3. 查询余额只用于展示，失败不影响签到成功的结论。
        available: Any = None
        total: Any = None
        try:
            balance_status, balance_payload, _ = await self._get_json(
                cookie_header,
                BALANCE_PATH,
                "魔粒余额",
            )
            if balance_status == 200 and envelope_ok(balance_payload):
                balance = envelope_data(balance_payload)
                available = balance.get("available_balance")
                total = balance.get("total_balance")
        except Exception as exc:
            logger.warning("查询魔搭魔粒余额失败：%s", exc)

        lines: list[str] = []
        if display:
            lines.append(f"用户：{display}")
        title_text = f"（{' + '.join(titles)}）" if titles else ""
        if before_records is None:
            # 签到前的账单没查到，无法判断是不是重复签到，只报告今日合计。
            lines.append(f"今日到账：+{format_credit(credit)} 魔粒{title_text}")
        elif not before_records:
            lines.append(f"本次到账：+{format_credit(credit)} 魔粒{title_text}")
        elif credit > before_credit:
            gained = credit - before_credit
            lines.append(f"本次到账：+{format_credit(gained)} 魔粒{title_text}")
            lines.append(f"今日合计：+{format_credit(credit)} 魔粒")
        else:
            lines.append("今天已签到过，本次无新增到账。")
            lines.append(f"今日合计：+{format_credit(credit)} 魔粒{title_text}")
        if available is not None:
            balance_line = f"当前余额：{available} 魔粒"
            if total is not None:
                balance_line += f"（总计 {total}）"
            lines.append(balance_line)
        return {
            "status": STATUS_SUCCESS,
            "text": self._build_message(STATUS_SUCCESS, lines),
        }

    def _build_message(self, status: str, lines: list[str]) -> str:
        """拼接签到结果文案。

        Args:
            status: 签到结果状态。
            lines: 结果明细行。

        Returns:
            完整的推送文本。
        """
        now = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
        return "\n".join([RESULT_TITLES[status], f"时间：{now}（北京时间）", *lines])
