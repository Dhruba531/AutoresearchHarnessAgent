"""Push notifications through ntfy (https://ntfy.sh or a self-hosted server).

Subscribe to your topic in the ntfy phone app; Relay posts when it needs an
approval or finishes a task. Pick a long, unguessable topic name — anyone who
knows it can read the notifications. Notifications carry summaries only;
approving happens in the Relay app or Telegram.
"""

from __future__ import annotations

import httpx


class Ntfy:
    def __init__(self, server: str, topic: str, app_url: str | None = None, transport=None):
        self.url = f"{server.rstrip('/')}/{topic}"
        self.app_url = app_url
        self.transport = transport

    async def _post(self, title: str, body: str, priority: str, tags: str) -> None:
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": priority, "Tags": tags}
        if self.app_url:
            headers["Click"] = self.app_url
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as http:
            await http.post(self.url, content=body.encode(), headers=headers)

    async def approval(self, approval: dict, task: dict) -> None:
        kind = "Question" if approval["kind"] == "question" else f"Approve? ({approval['risk']})"
        await self._post(f"Relay · {kind}", f"{approval['summary']}\n\nTask: {task['title']}", "high", "raised_hand")

    async def finished(self, task: dict) -> None:
        if task["status"] not in {"done", "failed", "blocked"}:
            return
        icon = {"done": "white_check_mark", "failed": "x", "blocked": "warning"}[task["status"]]
        body = (task.get("result") or task.get("error") or "")[:1500]
        await self._post(f"Relay · {task['status']}: {task['title']}", body, "default", icon)
