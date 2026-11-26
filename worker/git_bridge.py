import asyncio
import os
import re
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from worker.sandbox import workspace_file


PROJECT_METADATA = ("SPEC.md", "PLAN.md", "Project_context.md")
TEMP_PATTERNS = ("_check*", "check_*.py", "out.txt", "dump.txt", "inspect_*.py", "*.tmp.*", "temp_*")
DRAFT_PATTERNS = ("*_draft.md", "*_sections.md", "*_review.md", "build_*.py", "run_build.py")
FINAL_SUBJECT = "chore: удалены временные скрипты сборки и черновики"
DOCUMENT_EXTENSIONS = {".docx", ".md", ".rst", ".txt", ".pdf", ".odt"}
CONVENTIONAL_SUBJECT = re.compile(
    r"(?:feat|fix|docs|test|refactor|build|chore|perf|style|ci|revert)(?:\([\w.-]+\))?!?: "
    r"([^\x00-\x1f\x7f]{4,110})"
)
FORBIDDEN_SUBJECT_TEXT = re.compile(
    r"ход\s+выполнения|шаг\s+разработки|gpt|claude|codex|gemini|glm|deepseek|"
    r"qwen|llama|mistral|grok|kimi|copilot|"
    r"\b(?:commit_msg|loomis_\w+)\b", re.IGNORECASE,
)
PAST_SUBJECT = re.compile(
    r"(?:добавлен|обновлён|обновлен|удалён|удален|исправлен|изменён|изменен|"
    r"подготовлен|проверен|создан|собран|настроен|перенесён|перенесен|"
    r"сохранён|сохранен|реализован|восстановлен|оптимизирован|заменён|заменен)"
    r"[аоы]?\s+\S", re.IGNORECASE,
)
SUBJECT_DIRECTIVE = re.compile(
    r"\b(?:нужно|надо|необходимо|следует|требуется|"
    r"[а-яё]{3,}(?:ать|ять|ить|еть|уть|оть|ыть|чь|ться|айте|яйте|ите|ься))\b", re.IGNORECASE,
)


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
        await self.clean_project_metadata()

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

    async def clean_project_metadata(self):
        if not self.workspace.is_dir():
            return
        if (self.workspace / ".git").exists():
            await self.initialize()
            exclude = self.workspace / await self.git("rev-parse", "--git-path", "info/exclude")
            exclude.parent.mkdir(parents=True, exist_ok=True)
            current = exclude.read_bytes() if exclude.exists() else b""
            rules = [f"/{name}".encode("ascii") for name in PROJECT_METADATA]
            missing = [rule for rule in rules if rule not in current.splitlines()]
            if missing:
                with exclude.open("ab") as stream:
                    stream.write((b"\n" if current and not current.endswith(b"\n") else b"")
                                 + b"\n".join(missing) + b"\n")
        reserved = {name.casefold() for name in PROJECT_METADATA}
        for path in self.workspace.iterdir():
            if path.name.casefold() in reserved and (path.is_symlink() or path.is_file()):
                path.unlink(missing_ok=True)

    async def commit_metadata_cleanup(self):
        if not (self.workspace / ".git").exists() or not await self.head():
            return False
        reserved = {name.casefold() for name in PROJECT_METADATA}
        tracked = (await self.git("ls-tree", "--name-only", "HEAD")).splitlines()
        paths = [name for name in tracked if name.casefold() in reserved]
        if not paths or not await self.git("status", "--porcelain", "--", *paths):
            return False
        await self.clear_stale_index_lock()
        self.clean_workspace()
        await self.write_commit("commit", "--only", "-m", "chore: удалены служебные документы проекта", "--", *paths)
        return True

    def clean_workspace(self, *, final=False, deliverable=None):
        patterns = TEMP_PATTERNS + DRAFT_PATTERNS if final else TEMP_PATTERNS
        root = self.workspace.resolve(strict=True)
        keep = workspace_file(root, deliverable) if final and deliverable else None
        if keep is not None and (keep != root / deliverable or not keep.is_file()):
            raise ValueError(f"Итоговый файл {deliverable} отсутствует или находится по ссылке.")

        def raise_walk_error(error):
            raise error

        directories = []
        for directory, dirs, files in os.walk(root, onerror=raise_walk_error):
            parent = Path(directory)
            dirs[:] = [name for name in dirs if name.casefold() != ".git"]
            directories.append(parent)
            for name in files + dirs:
                path = parent / name
                if name.casefold() == ".git" or path == keep:
                    continue
                if path.is_dir() and path.resolve() != path and not path.is_symlink():
                    dirs.remove(name)
                    continue
                if path.is_symlink() or path.is_file():
                    if (final and keep is not None) or any(fnmatchcase(name.casefold(), p) for p in patterns):
                        path.unlink(missing_ok=True)
                        if name in dirs:
                            dirs.remove(name)
        if keep is not None:
            for directory in reversed(directories[1:]):
                if not any(directory.iterdir()):
                    directory.rmdir()

    async def write_commit(self, *args):
        name = await self.git("config", "--global", "--get", "user.name", allowed=(0, 1))
        email = await self.git("config", "--global", "--get", "user.email", allowed=(0, 1))
        if not name or not email:
            raise RuntimeError("Укажите user.name и user.email в глобальном профиле Git.")
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        return await self.git("-c", f"user.name={name}", "-c", f"user.email={email}", *args, env=env)

    async def commit(self, subject=None, *, forbidden_names=(), final=False, deliverable=None):
        await self.clear_stale_index_lock()
        await self.clean_project_metadata()
        if final and await self.head():
            await self.git("reset", "HEAD", "--", *(f":(icase,glob)**/{pattern}" for pattern in TEMP_PATTERNS))
        self.clean_workspace(final=final, deliverable=deliverable)
        if not final and not await self.git("status", "--porcelain", "--untracked-files=all"):
            return False
        await self.git("add", "-A")
        if final:
            if deliverable:
                await self.git("--literal-pathspecs", "add", "-f", "--", deliverable)
            await self.write_commit("commit", "--allow-empty", "-m", FINAL_SUBJECT)
            return True
        staged = await self.git("diff", "--cached", "--name-status", "-z", "--no-renames")
        if not staged:
            return False
        forbidden = [FORBIDDEN_SUBJECT_TEXT.pattern]
        forbidden.extend(rf"(?<!\w){re.escape(name)}(?!\w)" for name in forbidden_names if name)
        names = re.compile("|".join(forbidden), re.IGNORECASE)
        subject = (subject or "").strip()
        match = CONVENTIONAL_SUBJECT.fullmatch(subject)
        if not match or names.search(subject) or not PAST_SUBJECT.match(match[1]) or SUBJECT_DIRECTIVE.search(match[1]):
            fields = staged.split("\0")
            groups = {}
            for status, path in zip(fields[::2], fields[1::2]):
                groups.setdefault(status, []).append(Path(path))
            is_docs = all(path.suffix.lower() in DOCUMENT_EXTENSIONS
                          for paths in groups.values() for path in paths)
            kind = "docs" if is_docs else ("feat" if "A" in groups else "fix")
            labels = {"A": "добавлены", "M": "обновлены", "D": "удалены", "T": "изменён тип файлов"}
            parts = []
            for status, paths in groups.items():
                examples = [path.name for path in paths if not names.search(path.name)
                            and path.name.isprintable()]
                files = ", ".join(examples[:3])[:60] or ("документы проекта" if is_docs else "файлы проекта")
                parts.append(f"{labels.get(status, 'обновлены')} {files}")
            subject = f"{kind}: {', '.join(parts)[:110].rstrip(', ')}"
        await self.write_commit("commit", "-m", subject)
        return True

    async def head(self):
        return await self.git("rev-parse", "--verify", "HEAD", allowed=(0, 128))

    async def snapshot(self):
        await self.clean_project_metadata()
        self.clean_workspace()
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        tree = await self.git("write-tree")
        head = await self.head()
        if head and tree == await self.git("rev-parse", "HEAD^{tree}"):
            return head
        return await self.write_commit("commit-tree", tree, "-m", "chore: сохранён снимок рабочей области")

    async def checkpoint(self):
        await self.clean_project_metadata()
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        source = await self.git("write-tree")
        tag = f"{self.tags}{time_ns()}-{uuid4().hex}"
        await self.git("tag", tag, source)
        return tag

    async def restore(self, source):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        if await self.git("write-tree") != await self.git("rev-parse", f"{source}^{{tree}}"):
            await self.git("restore", f"--source={source}", "--staged", "--worktree", "--", ".")
        await self.clean_project_metadata()

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
        await self.clean_project_metadata()

    async def diff(self, checkpoint):
        return await self.git("diff", "--no-ext-diff", checkpoint, "--", ".")

    async def changed_project_files(self, baseline):
        await self.clear_stale_index_lock()
        await self.git("add", "-A")
        changes = await self.git(
            "diff", "--cached", "--name-status", "-z", "--no-renames", "--diff-filter=AMT", baseline, "--", ".",
        )
        fields = changes.split("\0")
        return [
            (status, path) for status, path in zip(fields[::2], fields[1::2])
            if not any(fnmatchcase(Path(path).name.casefold(), pattern) for pattern in TEMP_PATTERNS)
            and (self.workspace / path).is_file() and not (self.workspace / path).is_symlink()
        ]

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
