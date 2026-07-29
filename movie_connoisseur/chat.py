"""A synchronous wrapper around the ADK runner.

ADK is async; Streamlit and a terminal REPL are not. This module owns a single
event loop for the lifetime of a conversation so callers can just do:

    chat = MovieChat()
    print(chat.send("What thrillers are on Netflix?").text)
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from movie_connoisseur.agents import root_agent

APP_NAME = "movie_connoisseur"


@dataclass
class ToolCall:
    """One tool invocation made while answering a turn."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    error: str = ""


@dataclass
class Turn:
    """The result of one user message."""

    text: str
    agent: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str = ""
    retry_after: int = 0

    @property
    def failed_tools(self) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.status == "error"]


def _describe_model_error(exc: Exception) -> tuple[str, int]:
    """Turn a model-provider exception into a user-facing message and retry delay.

    ADK wraps model errors in private exception types, so match on the message
    rather than the class. Messages stay provider-neutral because the model can
    be Gemini, OpenAI or NVIDIA NIM.
    """
    text = str(exc)

    # Weaker models sometimes route to the agent they already are; ADK rejects
    # it. Recoverable — the user just needs to try again.
    if "cannot transfer to itself" in text:
        return (
            "I got confused about which specialist should handle that. "
            "Please ask again, slightly rephrased.",
            0,
        )

    if "RESOURCE_EXHAUSTED" in text or "429" in text or "rate limit" in text.lower():
        match = re.search(r"[Rr]etry in (\d+(?:\.\d+)?)s", text)
        wait = int(float(match.group(1))) + 1 if match else 60
        return (
            f"The model provider's rate limit was hit. Try again in about "
            f"{wait} seconds, or switch MODEL_NAME / MODEL_PROVIDER in .env.",
            wait,
        )

    if "404" in text and ("no longer available" in text or "not found" in text.lower()):
        return (
            "The configured model is not available on this API key. "
            "Set MODEL_NAME in .env to a current model.",
            0,
        )

    if any(s in text for s in ("401", "API_KEY_INVALID", "PERMISSION_DENIED", "AuthenticationError")):
        return ("The provider rejected the API key. Check the key in .env.", 0)

    return (f"Something went wrong talking to the model: {text[:300]}", 0)


class MovieChat:
    """One conversation with the Movie Connoisseur agent tree."""

    def __init__(self, user_id: str = "local_user", session_id: str = "") -> None:
        self.user_id = user_id
        self.session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
        self._runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)

        # A dedicated loop keeps any loop-bound client inside the runner valid
        # across calls; asyncio.run() would build and tear down a new one each
        # time.
        self._loop = asyncio.new_event_loop()
        self._run(
            self._runner.session_service.create_session(
                app_name=APP_NAME, user_id=self.user_id, session_id=self.session_id
            )
        )

    def _run(self, coro):
        asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coro)

    async def _send_async(self, message: str) -> Turn:
        content = types.Content(role="user", parts=[types.Part(text=message)])

        chunks: list[str] = []
        calls: dict[str, ToolCall] = {}
        answering_agent = ""

        try:
            async for event in self._runner.run_async(
                user_id=self.user_id,
                session_id=self.session_id,
                new_message=content,
            ):
                if not event.content or not event.content.parts:
                    continue

                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        # transfer_to_agent is ADK's routing mechanism, not a
                        # domain tool — surface it as the handling agent instead.
                        if fc.name == "transfer_to_agent":
                            answering_agent = (fc.args or {}).get("agent_name", "")
                            continue
                        calls[fc.id or fc.name] = ToolCall(
                            name=fc.name, args=dict(fc.args or {})
                        )

                    elif getattr(part, "function_response", None):
                        fr = part.function_response
                        call = calls.get(fr.id or fr.name)
                        if call is None:
                            continue
                        response = fr.response if isinstance(fr.response, dict) else {}
                        call.status = str(response.get("status", ""))
                        call.error = str(response.get("error_message", ""))

                    elif getattr(part, "text", None) and event.is_final_response():
                        # Reasoning models emit chain-of-thought as parts marked
                        # thought=True. That is internal planning, not an answer
                        # — showing it leaks "Okay, the user asked for…" into
                        # the reply.
                        if getattr(part, "thought", False):
                            continue
                        chunks.append(part.text)
                        if event.author:
                            answering_agent = event.author

        except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
            message, retry_after = _describe_model_error(exc)
            return Turn(
                text="".join(chunks).strip() or message,
                agent=answering_agent,
                tool_calls=list(calls.values()),
                error=message,
                retry_after=retry_after,
            )

        return Turn(
            text="".join(chunks).strip(),
            agent=answering_agent,
            tool_calls=list(calls.values()),
        )

    def send(self, message: str) -> Turn:
        """Send one user message and return the agent's reply."""
        return self._run(self._send_async(message))

    def close(self) -> None:
        """Release the event loop owned by this conversation.

        The genai client leaves background aiohttp cleanup tasks pending, so
        drain them before closing or asyncio prints "Task was destroyed".
        """
        if self._loop.is_closed():
            return

        asyncio.set_event_loop(self._loop)
        try:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        finally:
            self._loop.close()
