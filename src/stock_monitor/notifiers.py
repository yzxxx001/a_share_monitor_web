from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import requests

from .config import AliyunSmsConfig, WeComConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    channel: str
    sent: bool
    detail: str


class WeComNotifier:
    def __init__(self, config: WeComConfig, dry_run: bool) -> None:
        self.config = config
        self.dry_run = dry_run

    def send(self, markdown: str) -> SendResult:
        if not self.config.enabled:
            return SendResult("wecom", False, "企业微信通知未启用")
        clipped = markdown[: self.config.max_markdown_chars]
        if self.dry_run:
            LOGGER.info("[DRY RUN][企业微信]\n%s", clipped)
            return SendResult("wecom", False, "dry_run：仅输出预览")
        webhook = os.getenv(self.config.webhook_env, "").strip()
        if not webhook:
            return SendResult("wecom", False, f"缺少环境变量 {self.config.webhook_env}")
        msg_type = (self.config.msg_type or "markdown").lower()
        if msg_type == "text":
            text_content = self._markdown_to_text(clipped)
            payload = {"msgtype": "text", "text": {"content": text_content}}
        else:
            payload = {"msgtype": "markdown", "markdown": {"content": clipped}}
        response = requests.post(
            webhook,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode", 0) != 0:
            raise RuntimeError(f"企业微信发送失败：{payload}")
        return SendResult("wecom", True, "已发送")

    @staticmethod
    def _markdown_to_text(markdown: str) -> str:
        lines: list[str] = []
        for line in markdown.splitlines():
            normalized = line.strip()
            if normalized.startswith("###"):
                normalized = normalized.lstrip("#").strip()
            if normalized.startswith(">"):
                normalized = normalized.lstrip(">").strip()
            lines.append(normalized)
        text = "\n".join(lines).strip()
        return text or "通知"


class AliyunSmsNotifier:
    def __init__(self, config: AliyunSmsConfig, dry_run: bool) -> None:
        self.config = config
        self.dry_run = dry_run
        self._client = None

    def _config_error(self) -> str | None:
        missing: list[str] = []
        for field_name, value in (
            ("phone_numbers", self.config.phone_numbers),
            ("sign_name", self.config.sign_name),
            ("template_code", self.config.template_code),
        ):
            if not value.strip():
                missing.append(field_name)
        for env_name in (self.config.access_key_id_env, self.config.access_key_secret_env):
            if not os.getenv(env_name, "").strip():
                missing.append(env_name)
        return None if not missing else "缺少短信配置：" + ", ".join(missing)

    def _create_client(self):
        from alibabacloud_dysmsapi20170525.client import Client as DysmsapiClient
        from alibabacloud_tea_openapi import models as open_api_models

        sdk_config = open_api_models.Config(
            access_key_id=os.environ[self.config.access_key_id_env],
            access_key_secret=os.environ[self.config.access_key_secret_env],
        )
        sdk_config.endpoint = self.config.endpoint
        return DysmsapiClient(sdk_config)

    def send(self, params: dict[str, str]) -> SendResult:
        if not self.config.enabled:
            return SendResult("sms", False, "短信通知未启用")
        if self.dry_run:
            LOGGER.info("[DRY RUN][短信] %s", json.dumps(params, ensure_ascii=False))
            return SendResult("sms", False, "dry_run：仅输出预览")
        error = self._config_error()
        if error:
            return SendResult("sms", False, error)
        from alibabacloud_dysmsapi20170525 import models as dysmsapi_models
        from alibabacloud_tea_util import models as util_models

        if self._client is None:
            self._client = self._create_client()
        request = dysmsapi_models.SendSmsRequest(
            phone_numbers=self.config.phone_numbers,
            sign_name=self.config.sign_name,
            template_code=self.config.template_code,
            template_param=json.dumps(params, ensure_ascii=False),
        )
        response = self._client.send_sms_with_options(request, util_models.RuntimeOptions())
        response_code = getattr(response.body, "code", None)
        if response_code != "OK":
            return SendResult("sms", False, f"阿里云返回：{response_code} {getattr(response.body, 'message', '')}")
        return SendResult("sms", True, "已发送")
