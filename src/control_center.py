from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any


@dataclass
class AgentView:
    agent_id: str
    state: str = "idle"
    query: str = ""
    current_job: str = ""
    heartbeat: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=300))
    paused: bool = False
    stopped: bool = False
    retry_last: bool = False
    skip_current: bool = False
    focus_tab: bool = False
    pending_prompt: dict[str, Any] | None = None
    pending_answer: str | None = None
    current_job_info: dict[str, Any] | None = None


class ControlCenterRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._agents: dict[str, AgentView] = {}
        self._global: dict[str, int] = {
            "found": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "captcha_wait": 0,
        }

    def ensure_agent(self, agent_id: str) -> None:
        with self._lock:
            if agent_id not in self._agents:
                self._agents[agent_id] = AgentView(agent_id=agent_id)
                self._agents[agent_id].logs.append(
                    f"{datetime.utcnow().strftime('%H:%M:%S')} agent initialized"
                )

    def set_state(self, agent_id: str, state: str, query: str = "", current_job: str = "") -> None:
        with self._lock:
            agent = self._agents.setdefault(agent_id, AgentView(agent_id=agent_id))
            agent.state = state
            if query:
                agent.query = query
            if current_job:
                agent.current_job = current_job
            agent.heartbeat = datetime.utcnow().isoformat()

    def append_log(self, agent_id: str, message: str) -> None:
        with self._lock:
            agent = self._agents.setdefault(agent_id, AgentView(agent_id=agent_id))
            agent.logs.append(f"{datetime.utcnow().strftime('%H:%M:%S')} {message}")
            agent.heartbeat = datetime.utcnow().isoformat()

    def inc(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._global[key] = self._global.get(key, 0) + value

    def set_captcha_wait_count(self) -> None:
        with self._lock:
            self._global["captcha_wait"] = sum(1 for a in self._agents.values() if a.state == "captcha_wait")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "global": dict(self._global),
                "agents": [
                    {
                        "agent_id": a.agent_id,
                        "state": a.state,
                        "query": a.query,
                        "current_job": a.current_job,
                        "current_job_title": (a.current_job_info or {}).get("title", ""),
                        "current_job_company": (a.current_job_info or {}).get("company", ""),
                        "heartbeat": a.heartbeat,
                        "paused": a.paused,
                        "stopped": a.stopped,
                        "log_count": len(a.logs),
                    }
                    for a in self._agents.values()
                ],
            }

    def logs(self, agent_id: str) -> list[str]:
        with self._lock:
            agent = self._agents.get(agent_id)
            return list(agent.logs) if agent else []

    def set_prompt(
        self,
        agent_id: str,
        prompt: str,
        options: list[str] | None = None,
        allow_empty: bool = False,
    ) -> None:
        with self._lock:
            agent = self._agents.setdefault(agent_id, AgentView(agent_id=agent_id))
            agent.pending_prompt = {
                "text": prompt,
                "options": options or [],
                "allow_empty": bool(allow_empty),
                "created_at": datetime.utcnow().isoformat(),
            }
            agent.pending_answer = None

    def clear_prompt(self, agent_id: str) -> None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent:
                return
            agent.pending_prompt = None
            agent.pending_answer = None

    def get_prompt(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or not agent.pending_prompt:
                return None
            return dict(agent.pending_prompt)

    def set_answer(self, agent_id: str, answer: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or not agent.pending_prompt:
                return False
            agent.pending_answer = answer
            return True

    def consume_answer(self, agent_id: str) -> str | None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent:
                return None
            answer = agent.pending_answer
            agent.pending_answer = None
            return answer

    def is_stopped(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            return bool(agent and agent.stopped)

    def set_job_info(self, agent_id: str, info: dict[str, Any]) -> None:
        with self._lock:
            agent = self._agents.setdefault(agent_id, AgentView(agent_id=agent_id))
            agent.current_job_info = dict(info)

    def get_job_info(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or not agent.current_job_info:
                return None
            return dict(agent.current_job_info)

    def action(self, agent_id: str, action: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent:
                return False
            if action == "pause":
                agent.paused = True
            elif action == "resume":
                agent.paused = False
            elif action == "stop":
                agent.stopped = True
                agent.state = "stopped"
                agent.heartbeat = datetime.utcnow().isoformat()
            elif action == "retry_last":
                agent.retry_last = True
            elif action == "focus_tab":
                agent.focus_tab = True
            elif action == "continue":
                if agent.pending_prompt is None:
                    return False
                agent.pending_answer = "ok"
            elif action == "skip":
                agent.skip_current = True
                if agent.pending_prompt is not None:
                    agent.pending_answer = "__SKIP_JOB__"
            else:
                return False
            return True

    async def wait_if_paused(self, agent_id: str) -> bool:
        """Return False if stopped while paused."""
        while True:
            with self._lock:
                agent = self._agents.get(agent_id)
                if not agent:
                    return False
                if agent.stopped:
                    return False
                if not agent.paused:
                    return True
            await asyncio.sleep(0.5)

    def consume_retry_flag(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or not agent.retry_last:
                return False
            agent.retry_last = False
            return True

    def consume_focus_flag(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or not agent.focus_tab:
                return False
            agent.focus_tab = False
            return True

    def is_skip_requested(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            return bool(agent and agent.skip_current)

    def clear_skip_request(self, agent_id: str) -> None:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent:
                agent.skip_current = False


REGISTRY = ControlCenterRegistry()
