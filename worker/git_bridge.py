import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4


def is_github_repo(url: str) -> bool:
    return bool(re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", url, re.IGNORECASE)
                and url.rsplit("/", 1)[-1] not in {".", ".."})


class GitBridge:
    def __init__(self, workspace: Path, project_id: int):
        self.workspace = workspace
        self.branch = f"loomis/project-{project_id}"
        self.tags = f"loomis/project-{project_id}/"

    async def git(self, *args, allowed=(0,), env=None):
        process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.hooksPath=.git/loomis-disabled-hooks", *args, cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
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

    async def commit(self, message="wip: handoff", *, allow_empty=False, author="Loomis"):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        if allow_empty or await self.git("status", "--porcelain") or not await self.head():
            await self.clear_stale_index_lock()
            await self.git("-c", "user.name=Loomis", "-c", "user.email=loomis@localhost",
                           "commit", "--allow-empty", "--author", f"{author} <loomis@localhost>", "-m", message)

    async def head(self):
        return await self.git("rev-parse", "--verify", "HEAD", allowed=(0, 128))

    async def snapshot(self):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        tree = await self.git("write-tree")
        return await self.git("-c", "user.name=Loomis", "-c", "user.email=loomis@localhost",
                              "commit-tree", tree, "-m", "Loomis workspace checkpoint")

    async def checkpoint(self):
        source = await self.snapshot()
        tag = f"{self.tags}{time_ns()}-{uuid4().hex}"
        await self.git("tag", tag, source)
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
        await self.git("tag", "-d", tag)

    async def commits(self):
        if not (self.workspace / ".git").exists() or not await self.head():
            return []
        output = await self.git("log", "-z", "--format=%H%x00%h%x00%B%x00%an%x00%cI", "HEAD", "--")
        fields = output.rstrip("\0").split("\0")
        return [
            {"hash": full, "short_hash": short, "message": message.rstrip(), "author": author,
             "created_at": datetime.fromisoformat(date)}
            for full, short, message, author, date in zip(*(iter(fields),) * 5)
        ]

    async def reset(self, commit_hash):
        await self.clear_stale_index_lock()
        await self.git("reset", "--hard", commit_hash)

    async def diff(self, checkpoint):
        return await self.git("diff", "--no-ext-diff", checkpoint, "--", ".")

    async def open_transport(self):
        ref = f"refs/heads/{self.branch}-work-{uuid4().hex}"
        source = await self.snapshot()
        await self.git("push", "origin", f"{source}:{ref}")
        return ref

    async def fetch_transport(self, ref):
        pattern = rf"refs/heads/{re.escape(self.branch)}-work-[a-f0-9]{{32}}"
        if not re.fullmatch(pattern, ref):
            raise ValueError("Недопустимая ссылка рабочего снимка.")
        await self.git("fetch", "--no-tags", "origin", ref)

    async def close_transport(self, ref):
        await self.fetch_transport(ref)
        await self.restore("FETCH_HEAD")
        await self.git("push", "origin", "--delete", ref)

    async def pull(self):
        await self.git("pull", "--ff-only", "origin", self.branch)

    async def push(self):
        await self.git("push", "origin", f"HEAD:refs/heads/{self.branch}")

    async def push_main(self, url):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        await self.git("push", "--", url, "HEAD:refs/heads/main", env=env)

    async def push_github_main(self, url: str, token: str):
        if not is_github_repo(url):
            raise ValueError("Для автопуша укажите HTTPS-адрес репозитория на github.com.")
        try:
            # Use only the workspace's committed objects. Its Git config can be edited by agents.
            head = await self.git("rev-parse", "--verify", "HEAD^{commit}")
            objects = await self.git("rev-parse", "--path-format=absolute", "--git-path", "objects")
            if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", head):
                raise ValueError("Не удалось определить коммит для GitHub.")
            with TemporaryDirectory(prefix="loomis-github-") as directory:
                workspace = Path(directory)
                askpass = workspace / "askpass.sh"
                askpass.write_bytes(
                    b'#!/bin/sh\ncase "$1" in\n'
                    b'  Username*) printf "%s\\n" x-access-token ;;\n'
                    b'  Password*) printf "%s\\n" "$LOOMIS_GITHUB_PAT" ;;\n'
                    b'  *) exit 1 ;;\nesac\n'
                )
                askpass.chmod(0o700)
                env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
                env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https", LC_ALL="C")
                isolated = GitBridge(workspace, 0)
                object_format = "sha256" if len(head) == 64 else "sha1"
                await isolated.git("init", "--bare", "--template=", f"--object-format={object_format}", env=env)
                alternates = workspace / "objects" / "info" / "alternates"
                if "\n" in objects or "\r" in objects or not Path(objects).is_absolute():
                    raise ValueError("Не удалось открыть объекты коммита для GitHub.")
                alternates.write_bytes((Path(objects).as_posix() + "\n").encode("utf-8"))
                await isolated.git("update-ref", "refs/heads/main", head, env=env)
                await isolated.git("remote", "add", "origin", url, env=env)
                env.update(GIT_ASKPASS=askpass.as_posix(), LOOMIS_GITHUB_PAT=token)
                await isolated.git("-c", "http.followRedirects=false", "-c", "credential.helper=",
                                   "push", "-u", "origin", "main", env=env)
        except TimeoutError:
            raise RuntimeError("Истекло время ожидания GitHub.") from None
        except (OSError, RuntimeError) as exc:
            error = str(exc).lower()
            if any(word in error for word in ("non-fast-forward", "fetch first", "[rejected]")):
                message = "В GitHub есть изменения в main. Синхронизируйте историю с локальной копией."
            elif any(word in error for word in ("authentication", "denied", "403", "401", "not found")):
                message = "GitHub отказал в доступе. Проверьте токен и права записи в репозиторий."
            else:
                message = "Не удалось отправить коммит. Проверьте сеть и доступность репозитория GitHub."
            raise RuntimeError(message) from None
