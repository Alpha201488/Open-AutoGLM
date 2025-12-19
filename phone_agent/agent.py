"""Main PhoneAgent class for orchestrating phone automation."""

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from phone_agent.actions import ActionHandler
from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.adb import get_current_app, get_screenshot
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder


@dataclass
class AgentConfig:
    """Configuration for the PhoneAgent."""

    max_steps: int = 100
    device_id: Optional[str] = None
    lang: str = "cn"
    system_prompt: Optional[str] = None
    verbose: bool = True
    retry_reminder_threshold: int = 3

    def __post_init__(self) -> None:
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: Optional[Dict[str, Any]]
    thinking: str
    message: Optional[str] = None


class PhoneAgent:
    """
    AI-powered agent for automating Android phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks.
    """

    def __init__(
        self,
        model_config: Optional[ModelConfig] = None,
        agent_config: Optional[AgentConfig] = None,
        confirmation_callback: Optional[Callable[[str], bool]] = None,
        takeover_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or AgentConfig()

        self.model_client = ModelClient(self.model_config)
        self.action_handler = ActionHandler(
            device_id=self.agent_config.device_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: List[Dict[str, Any]] = []
        self._step_count: int = 0
        self._last_action_type: Optional[str] = None
        self._last_action_target: Optional[str] = None
        self._retry_count: int = 0

    def run(self, task: str) -> str:
        """Run the agent to complete a task."""
        self._context = []
        self._step_count = 0
        self._reset_action_tracking()

        result = self._execute_step(task, is_first=True)
        if result.finished:
            return result.message or "Task completed"

        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)
            if result.finished:
                return result.message or "Task completed"

        return "Max steps reached"

    def step(self, task: Optional[str] = None) -> StepResult:
        """Execute a single step of the agent."""
        is_first = len(self._context) == 0
        if is_first and not task:
            raise ValueError("Task is required for the first step")
        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._context = []
        self._step_count = 0
        self._reset_action_tracking()

    def _execute_step(
        self, user_prompt: Optional[str] = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1

        screenshot = get_screenshot(self.agent_config.device_id)
        current_app = get_current_app(self.agent_config.device_id)

        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self.agent_config.system_prompt)
            )
            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = "{}\n\n{}".format(user_prompt, screen_info)
            text_content = self._compose_user_text(text_content)

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = "** Screen Info **\n\n{}".format(screen_info)
            text_content = self._compose_user_text(text_content)

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        try:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print("💭 {}:".format(msgs["thinking"]))
            print("-" * 50)
            response = self.model_client.request(self._context)
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message="Model error: {}".format(e),
            )

        try:
            action = parse_action(response.action)
        except ValueError:
            if self.agent_config.verbose:
                traceback.print_exc()
            action = finish(message=response.action)

        if self.agent_config.verbose:
            print("-" * 50)
            print("🎯 {}:".format(msgs["action"]))
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        try:
            executed_action = action
            result = self.action_handler.execute(
                executed_action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            executed_action = finish(message=str(e))
            result = self.action_handler.execute(
                executed_action, screenshot.width, screenshot.height
            )

        self._context.append(
            MessageBuilder.create_assistant_message(
                "<think>{}</think><answer>{}</answer>".format(
                    response.thinking, response.action
                )
            )
        )

        finished = executed_action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                "✅ {}: {}".format(
                    msgs["task_completed"],
                    result.message or action.get("message", msgs["done"]),
                )
            )
            print("=" * 50 + "\n")

        self._update_last_action_state(executed_action)

        return StepResult(
            success=result.success,
            finished=finished,
            action=executed_action,
            thinking=response.thinking,
            message=result.message or executed_action.get("message"),
        )

    def _reset_action_tracking(self) -> None:
        self._last_action_type = None
        self._last_action_target = None
        self._retry_count = 0

    def _update_last_action_state(self, action: Dict[str, Any]) -> None:
        action_type = action.get("action") if action.get("_metadata") == "do" else action.get("_metadata")
        action_target = self._format_action_target(action)

        if action_type and action_type == self._last_action_type and action_target == self._last_action_target:
            self._retry_count += 1
        else:
            self._last_action_type = action_type
            self._last_action_target = action_target
            self._retry_count = 1 if action_type else 0

    def _format_action_target(self, action: Dict[str, Any]) -> str:
        metadata = action.get("_metadata")
        if metadata == "finish":
            return action.get("message", "")

        if metadata != "do":
            return ""

        action_name = action.get("action")

        if action_name == "Launch":
            return action.get("app", "")

        if action_name in {"Tap", "Double Tap", "Long Press"}:
            element = action.get("element")
            if isinstance(element, list) and len(element) >= 2:
                return f"({element[0]}, {element[1]})"
            return ""

        if action_name == "Swipe":
            start = action.get("start")
            end = action.get("end")
            if (
                isinstance(start, list)
                and isinstance(end, list)
                and len(start) >= 2
                and len(end) >= 2
            ):
                return f"({start[0]}, {start[1]}) -> ({end[0]}, {end[1]})"
            return ""

        if action_name in {"Type", "Type_Name"}:
            text = action.get("text", "")
            return f"text='{self._truncate_text(text)}'"

        if action_name == "Wait":
            return action.get("duration", "")

        if action_name in {"Back", "Home"}:
            return "navigation"

        if action_name == "Take_over":
            return action.get("message", "")

        return ""

    def _build_retry_note(self) -> Optional[str]:
        if not self._last_action_type or self._retry_count <= 0:
            return None

        target = f" {self._last_action_target}" if self._last_action_target else ""
        note = "已尝试 {}{} 共 {} 次，界面未变化/状态仍未达成".format(
            self._last_action_type, target, self._retry_count
        )

        threshold = self.agent_config.retry_reminder_threshold
        if threshold and self._retry_count >= threshold:
            note += "。请考虑其他策略或调用 finish 请求人工接管。"

        return note

    def _compose_user_text(self, base_text: str) -> str:
        retry_note = self._build_retry_note()
        if retry_note:
            return "{}\n\n{}".format(retry_note, base_text)
        return base_text

    @staticmethod
    def _truncate_text(text: str, max_length: int = 50) -> str:
        if len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    @property
    def context(self) -> List[Dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
