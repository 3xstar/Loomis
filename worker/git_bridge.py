import asyncio
from pathlib import Path
from time import time_ns
from uuid import uuid4


class GitBridge:
    def __init__(self, workspace: Path, project_id: int):
        self.workspace = workspace
        self.branch = f"loomis/project-{project_id}"
        self.tags = f"loomis/project-{project_id}/"

    async def git(self, *args, allowed=(0,)):
        process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.hooksPath=.git/loomis-disabled-hooks", *args, cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 45)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            raise
        if process.returncode not in allowed:
            raise RuntimeError(err.decode(errors="replace").strip() or "Git operation failed")
        return out.decode(errors="replace").strip()

    async def initialize(self):
        if not (self.workspace / ".git").exists():
            await self.git("init")
        if await self.git("rev-parse", "--show-prefix"):
            raise RuntimeError("The workspace must be the repository root")

    async def link_origin(self, url: str):
        await self.initialize()
        remotes = (await self.git("remote")).splitlines()
        action = "set-url" if "origin" in remotes else "add"
        await self.git("remote", action, "--", "origin", url)

    async def prepare(self):
        await self.initialize()
        if await self.git("branch", "--show-current") != self.branch:
            existing = await self.git("branch", "--list", self.branch)
            if existing:
                await self.git("switch", self.branch)
            else:
                await self.git("switch", "-c", self.branch)

    async def clear_stale_index_lock(self):
        git_dir = self.workspace / ".git"
        lock = git_dir / "index.lock"
        if git_dir.is_file():
            lock = self.workspace / await self.git("rev-parse", "--git-path", "index.lock")
        try:
            if time_ns() - lock.stat().st_mtime_ns > 10_000_000_000:
                lock.unlink()
        except FileNotFoundError:
            pass

    async def commit(self, message="wip: handoff"):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        if await self.git("status", "--porcelain") or not await self.git("rev-parse", "--verify", "HEAD", allowed=(0, 128)):
            await self.clear_stale_index_lock()
            await self.git("-c", "user.name=Loomis", "-c", "user.email=loomis@localhost",
                           "commit", "--allow-empty", "-m", message)

    async def checkpoint(self):
        await self.commit()
        tag = f"{self.tags}{time_ns()}-{uuid4().hex}"
        await self.git("tag", tag)
        return tag

    async def restore(self, source):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        if await self.git("status", "--porcelain") or source != "HEAD":
            await self.git("restore", f"--source={source}", "--staged", "--worktree", "--", ".")

    async def rollback_last_step(self):
        await self.clear_stale_index_lock()
        tags = (await self.git("tag", "--list", f"{self.tags}*", "--sort=-refname")).splitlines()
        if not tags:
            raise ValueError("Нет точки отката.")
        tag = tags[0]
        await self.restore(tag)
        await self.commit("wip: rollback")
        await self.git("tag", "-d", tag)

    async def diff(self, checkpoint):
        return await self.git("diff", "--no-ext-diff", checkpoint, "--", ".")

    async def synchronize(self):
        remote = await self.git("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}")
        if remote:
            await self.pull()

    async def pull(self):
        await self.git("pull", "--ff-only", "origin", self.branch)

    async def push(self):
        await self.git("push", "origin", f"HEAD:refs/heads/{self.branch}")
