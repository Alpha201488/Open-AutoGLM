"""Model client for AI inference using OpenAI-compatible API."""

import json
import time
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from phone_agent.config.i18n import get_message

# 保留标签解析核心函数（无输出，仅处理内容）
_TAG_RE = re.compile(r"<[^>]+>")
def _normalize_xmlish_tags(text: str) -> str:
    def repl(m):
        return re.sub(r"\s+", "", m.group(0))
    return _TAG_RE.sub(repl, text)

def _extract_between(text: str, a: str, b: str) -> str:
    i = text.find(a)
    if i == -1:
        return ""
    i += len(a)
    j = text.find(b, i)
    if j == -1:
        return text[i:]
    return text[i:j]

@dataclass
class ModelConfig:
    """Configuration for the AI model."""
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "EMPTY"
    model_name: str = "autoglm-phone-9b"
    max_tokens: int = 3000
    temperature: float = 0.0
    top_p: float = 0.85
    frequency_penalty: float = 0.2
    extra_body: Dict[str, Any] = field(default_factory=dict)
    lang: str = "cn"  # Language for UI messages: 'cn' or 'en'

@dataclass
class ModelResponse:
    """Response from the AI model."""
    thinking: str
    action: str
    raw_content: str
    # Performance metrics
    time_to_first_token: Optional[float] = None  # Time to first token (seconds)
    time_to_thinking_end: Optional[float] = None  # Time to thinking end (seconds)
    total_time: Optional[float] = None  # Total inference time (seconds)

class ModelClient:
    """
    Client for interacting with OpenAI-compatible vision-language models.
    纯调用型：仅流式打印思考内容，无额外输出，所有结果通过返回值传递
    """
    def __init__(self, config: Optional[ModelConfig] = None):
        self.config = config or ModelConfig()
        self.client = OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key
        )
        # 标签追踪状态（仅用于流式内容清洗，无输出）
        self._tag_buffer = ""
        self._in_tag = False

    def _clean_stream_content(self, text: str) -> str:
        """清洗流式内容中的标签（无输出，仅返回纯文本）"""
        clean_chars = []
        i = 0
        n = len(text)

        while i < n:
            char = text[i]
            if char == "<" and not self._in_tag:
                self._in_tag = True
                self._tag_buffer = "<"
                i += 1
                continue
            elif char == ">" and self._in_tag:
                self._in_tag = False
                self._tag_buffer = ""
                i += 1
                continue
            elif self._in_tag:
                self._tag_buffer += char
                i += 1
                continue
            else:
                clean_chars.append(char)
                i += 1

        clean_text = "".join(clean_chars)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        return clean_text

    def request(self, messages: List[Dict[str, Any]]) -> ModelResponse:
        """
        Send a request to the model.
        仅流式打印思考内容，无其他额外输出，所有结果通过ModelResponse返回
        """
        # 初始化计时（仅记录，无输出）
        start_time = time.time()
        time_to_first_token = None
        time_to_thinking_end = None
        first_token_received = False

        # 重置标签状态
        self._tag_buffer = ""
        self._in_tag = False

        stream = self.client.chat.completions.create(
            messages=messages,
            model=self.config.model_name,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            frequency_penalty=self.config.frequency_penalty,
            extra_body=self.config.extra_body,
            stream=True,
        )

        raw_content = ""
        buffer = ""
        action_markers = ["finish(message=", "do(action="]
        in_action_phase = False

        for chunk in stream:
            if len(chunk.choices) == 0:
                continue
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                raw_content += content

                # 记录首Token时间（仅记录，无输出）
                if not first_token_received:
                    time_to_first_token = time.time() - start_time
                    first_token_received = True

                if in_action_phase:
                    continue

                buffer += content

                # 检测动作标记，仅打印思考内容（流式响应）
                marker_found = False
                for marker in action_markers:
                    if marker in buffer:
                        thinking_part = buffer.split(marker, 1)[0]
                        # 清洗标签后流式打印思考内容（仅这处打印，无其他输出）
                        clean_thinking = self._clean_stream_content(thinking_part)
                        if clean_thinking:
                            print(clean_thinking, end="", flush=True)
                        print()  # 思考结束后换行
                        in_action_phase = True
                        marker_found = True

                        # 记录思考完成时间（仅记录，无输出）
                        if time_to_thinking_end is None:
                            time_to_thinking_end = time.time() - start_time
                        break

                if marker_found:
                    continue

                # 检测潜在标记，避免截断（无输出）
                is_potential_marker = False
                for marker in action_markers:
                    for i in range(1, len(marker)):
                        if buffer.endswith(marker[:i]):
                            is_potential_marker = True
                            break
                    if is_potential_marker:
                        break

                if not is_potential_marker:
                    # 安全打印缓冲的思考内容（流式响应）
                    clean_buffer = self._clean_stream_content(buffer)
                    if clean_buffer:
                        print(clean_buffer, end="", flush=True)
                    buffer = ""

        # 计算总耗时（仅记录，无输出）
        total_time = time.time() - start_time

        # 解析思考和动作（无输出，仅返回）
        thinking, action = self._parse_response(raw_content)

        # ========= 新增：打印性能指标（照你原版的格式） =========
        lang = self.config.lang
        print("-" * 50)
        print("=" * 50)
        print("⏱️  {}:".format(get_message("performance_metrics", lang)))
        print("-" * 50)
        if time_to_first_token is not None:
            print("{}: {:.3f}s".format(get_message("time_to_first_token", lang), time_to_first_token))
        if time_to_thinking_end is not None:
            print("{}:        {:.3f}s".format(get_message("time_to_thinking_end", lang), time_to_thinking_end))
        print("{}:          {:.3f}s".format(get_message("total_inference_time", lang), total_time))
        print("=" * 50)
        # =====================================================

        return ModelResponse(
            thinking=thinking,
            action=action,
            raw_content=raw_content,
            time_to_first_token=time_to_first_token,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

    def _parse_response(self, content: str) -> Tuple[str, str]:
        """解析思考和动作（无输出，仅返回纯文本）"""
        c = _normalize_xmlish_tags(content)

        if "<tool_call>" in c:
            # 提取标签内的思考内容（无输出）
            thinking = _extract_between(c, "<think_text>", "</think_text>").strip()
            thinking = re.sub(r"\s+", " ", thinking).strip()

            # 提取标签内的动作内容（无输出）
            action = _extract_between(c, "<tool_call>", "</tool_call>").strip()
            if not action:
                pos = c.find("<tool_call>") + len("<tool_call>")
                action = c[pos:].strip()

            return thinking, action

        # 兼容原有do/finish格式（无输出）
        if "finish(message=" in content:
            parts = content.split("finish(message=", 1)
            thinking = parts[0].strip()
            action = "finish(message=" + parts[1]
            return thinking, action

        if "do(action=" in content:
            parts = content.split("do(action=", 1)
            thinking = parts[0].strip()
            action = "do(action=" + parts[1]
            return thinking, action

        # 兜底（无输出）
        return "", content.strip()

class MessageBuilder:
    """Helper class for building conversation messages (无输出，仅构建消息)"""
    @staticmethod
    def create_system_message(content: str) -> Dict[str, Any]:
        return {"role": "system", "content": content}

    @staticmethod
    def create_user_message(
        text: str, image_base64: Optional[str] = None
    ) -> Dict[str, Any]:
        content = []
        if image_base64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,{}".format(image_base64)},
                }
            )
        content.append({"type": "text", "text": text})
        return {"role": "user", "content": content}

    @staticmethod
    def create_assistant_message(content: str) -> Dict[str, Any]:
        return {"role": "assistant", "content": content}

    @staticmethod
    def remove_images_from_message(message: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(message.get("content"), list):
            message["content"] = [
                item for item in message["content"] if item.get("type") == "text"
            ]
        return message

    @staticmethod
    def build_screen_info(current_app: str, **extra_info) -> str:
        info = {"current_app": current_app, **extra_info}
        return json.dumps(info, ensure_ascii=False)
