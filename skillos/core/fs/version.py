"""Git 封装（04 §2.2.3，FR-1.0.10）。全部 subprocess 调用 git，不自研 VCS。

接口收紧（OUT-14）：
- 只暴露具名方法（init/commit/log/show/snapshot_at/diff/rollback），任务表即白名单。
- 不提供 run(cmd, args) 通用透传 —— daemon 参数来自云端，通用透传等于
  「云端触发 → 本地任意 git 执行」通道。
- 块内三方合并（git merge-file --diff3）本期不实现（OUT-11）。

跨平台（05 P0-1 / P2-5）：
- 所有 git 调用显式传 -c 参数，不继承用户全局配置。
- 回滚策略：checkout + 新建 commit，不 rewrite history。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from skillos.core.algorithm.hash import is_foldable_text
from skillos.core.algorithm.parse import build_meta, parse_frontmatter
from skillos.core.model import Commit, FileEntry, SkillMeta, SkillSnapshot

# 05 P0-1：显式覆盖用户全局 git 配置，保证行为可预期
GIT_BASE_ARGS: tuple[str, ...] = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
    "-c",
    "core.quotepath=false",  # 中文文件名正常显示，否则是八进制乱码
    "-c",
    "core.longpaths=true",  # Windows 260 字符路径上限
)

DEFAULT_AUTHOR = "Skill OS <skillos@local>"

# canonical 仓库布局：skills/<slug>/...
SKILLS_DIR = "skills"


class GitError(Exception):
    """git 调用失败（非零退出码 / git 不存在）。"""


def _parse_author(author: str) -> tuple[str, str]:
    """"Name <email>" → (name, email)；无尖括号时整体作 name。"""
    if "<" in author and author.endswith(">"):
        name, _, email = author.partition("<")
        return name.strip(), email[:-1].strip()
    return author.strip(), "skillos@local"


def _build_meta(content: bytes):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fm, _body = parse_frontmatter(text)
    return build_meta(fm) if fm else None


class GitRepo:
    """canonical 仓库（SSOT）的具名操作集。"""

    def __init__(self, repo_root: Path) -> None:
        # T1: repo_root 由调用方显式传入，不从环境变量 / 单例读取
        self.repo_root = repo_root

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        cmd = ["git", *GIT_BASE_ARGS, *args]
        try:
            proc = subprocess.run(  # noqa: S603 —— 参数白名单由具名方法约束（OUT-14）
                cmd, cwd=self.repo_root, capture_output=True, check=False,
                # 隔离 stdin：MCP stdio 场景下防 git 读取/阻塞 JSON-RPC 管道
                stdin=subprocess.DEVNULL,
                # 超时兜底：git 卡死（杀毒/锁）不能拖垮调用方——daemon 的
                # 全扫在循环里跑，单次无界等待 = 整条链路假死
                timeout=30,
            )
        except FileNotFoundError as e:
            raise GitError("git not found in PATH") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args)} timed out after 30s") from e
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}")
        return proc

    # ---- 具名方法（新增能力加具名方法，禁止加通用透传）----

    def init(self) -> None:
        """git init（07 G1/G2）：显式 -b main --ref-format=files，不吃用户全局配置。

        旧版 git 不支持时逐级降级（-b → symbolic-ref），保证初始分支仍为 main。
        """
        try:
            self._git("init", "-b", "main", "--ref-format=files")
        except GitError:
            try:
                self._git("init", "-b", "main")
            except GitError:
                self._git("init")
                self._git("symbolic-ref", "HEAD", "refs/heads/main")
        self._git("config", "core.autocrlf", "false")  # repo 级兜底（05 P2-5）
        # 分发备份落 <repo>/.skillos/backups/（不随 git 版本化）；
        # rollback 的 add -A 会扫全树，没有 .gitignore 会把备份提交进仓库
        gitignore = Path(self.repo_root) / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if ".skillos/backups/" not in lines:
            lines.append(".skillos/backups/")
            gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def remote_head(self, url: str, ref: str | None = None, *,
                    timeout: int = 20) -> str | None:
        """`git ls-remote` 取上游 ref 的 commit（E-08 上游检查的唯一 IO 出口）。

        **具名领域操作**，不是通用透传：参数只有 url + ref，命令固定。
        `ref=None` 时取 HEAD。网络失败会抛 `GitError`，由调用方计失败次数
        （E-08.4：满 3 次才置 error），本方法不吞异常。
        """
        if not url or not url.strip():
            raise GitError("上游 url 为空")
        target = (ref or "HEAD").strip()
        # ls-remote 不需要工作树，cwd 仍固定在 repo（避免在用户某个随机目录跑 git）
        proc = subprocess.run(  # noqa: S603 —— 参数由本方法固定
            ["git", *GIT_BASE_ARGS, "ls-remote", url.strip(), target],
            cwd=self.repo_root, capture_output=True, check=False,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
        if proc.returncode != 0:
            raise GitError(
                f"git ls-remote 失败: {proc.stderr.decode(errors='replace').strip()[:200]}"
            )
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].endswith(target if target.startswith("refs/") else ""):
                return parts[0]
        # 空输出 = ref 不存在（不是网络问题）：交给调用方判 outdated / unknown
        return None

    def commit(self, paths: list[str], message: str, author: str = DEFAULT_AUTHOR) -> str:
        """提交指定路径，返回 commit sha。"""
        name, email = _parse_author(author)
        self._git("add", "--", *paths)
        self._git(
            "-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-m", message,
        )
        return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def log(self, path: str | None = None, limit: int = 20,
            follow: bool = False) -> list[Commit]:
        """提交历史（可按路径过滤；follow 跨改名追踪单文件 —— P4 文件级历史）。"""
        fmt = "%H%x00%an%x00%aI%x00%B%x00%x1e"
        args = ["log", f"--pretty=format:{fmt}", "-n", str(limit)]
        if follow and path:
            args.append("--follow")
        if path:
            args += ["--", path]
        out = self._git(*args).stdout.decode("utf-8", errors="replace")
        commits: list[Commit] = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            parts = record.split("\x00", 3)
            if len(parts) != 4:
                continue
            sha, author, at, message = parts
            # 尾部 \x00：fmt 中 %B 与记录分隔符 %x1e 之间的 %x00 会残留在 message 末尾
            commits.append(Commit(sha=sha, message=message.strip("\n\x00"),
                                  author=author, at=at))
        return commits

    def show(self, commit: str, path: str) -> bytes:
        """取某 commit 下某文件的字节内容（autocrlf=false：字节级不变）。"""
        return self._git("show", f"{commit}:{path}").stdout

    def snapshot_at(self, commit: str, slug: str) -> SkillSnapshot:
        """取某一 commit 下某 Skill 的 canonical 快照 —— base 重建的唯一入口（NFR-3.5）。

        base 不落盘，靠 render(canonical@installed_commit, installed_adapter_version) 现算（T6）。
        """
        prefix = f"{SKILLS_DIR}/{slug}/"
        proc = self._git("ls-tree", "-r", "--name-only", commit, "--", prefix)
        rel_paths = [ln for ln in proc.stdout.decode("utf-8").splitlines() if ln]
        files: list[FileEntry] = []
        meta: SkillMeta | None = None
        for rel in rel_paths:
            content = self.show(commit, rel)
            inner = rel[len(prefix):]
            if inner == "SKILL.md":
                meta = _build_meta(content)
            files.append(
                FileEntry(
                    path=inner, content=content, in_git=True, is_text=is_foldable_text(content)
                )
            )
        if meta is None:
            # frontmatter 结构非法（缺 name/description）：占位空 meta，由 gate
            # 的 fm.*.required 报 error。base 重建只发生在通过 gate 的快照上，
            # 此分支仅覆盖"从未通过 gate 的历史快照"（只读路径，不参与渲染）。
            meta = SkillMeta(name="", description="")
        return SkillSnapshot(skill_id=slug, slug=slug, meta=meta, files=tuple(files))

    def snapshot_all(self, commit: str) -> dict[str, SkillSnapshot]:
        """取某一 commit 下**全部** Skill 快照 —— status_matrix 专用批量入口。

        对比逐 skill 调 snapshot_at：N skill × M 文本文件 = N×(1+M) 次 git
        spawn（27 skill ≈ 100+ 次，Windows 上 20s+）；批量 = 1 次 ls-tree +
        1 次 cat-file --batch（2 次 spawn）。语义与 snapshot_at 一致：
        缺 SKILL.md 的 skill 给占位空 meta（gate 报 error，不炸扫描）。
        """
        proc = self._git("ls-tree", "-r", commit, "--", SKILLS_DIR)
        entries: list[tuple[str, str, str]] = []  # (blob_sha, slug, inner_path)
        prefix = f"{SKILLS_DIR}/"
        for ln in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            meta_part, _, path = ln.partition("\t")
            parts = meta_part.split()
            if len(parts) < 3 or parts[1] != "blob" or not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            slug, sep, inner = rest.partition("/")
            if not sep:  # skills/ 直下无文件，只有目录
                continue
            entries.append((parts[2], slug, inner))
        if not entries:
            return {}

        # cat-file --batch：去重后一次进程取回全部 blob
        shas = list(dict.fromkeys(sha for sha, _, _ in entries))
        batch = subprocess.run(  # noqa: S603 —— 参数固定白名单（cat-file --batch）
            ["git", *GIT_BASE_ARGS, "cat-file", "--batch"],
            cwd=self.repo_root, input=("\n".join(shas) + "\n").encode(),
            capture_output=True, check=False, timeout=120,
        )
        if batch.returncode != 0:
            raise GitError(f"git cat-file --batch failed: "
                           f"{batch.stderr.decode(errors='replace')}")
        contents: dict[str, bytes] = {}
        data, pos = batch.stdout, 0
        for sha in shas:
            nl = data.find(b"\n", pos)
            if nl < 0:
                raise GitError(f"cat-file --batch 输出截断（{sha}）")
            header = data[pos:nl].decode("utf-8", errors="replace").split()
            pos = nl + 1
            if len(header) < 3:  # "<sha> missing"：HEAD 下不该发生，防御
                contents[sha] = b""
                continue
            size = int(header[2])
            contents[sha] = data[pos:pos + size]
            pos += size + 1  # 内容后有一个换行

        by_slug: dict[str, list[FileEntry]] = {}
        meta_by_slug: dict[str, SkillMeta | None] = {}
        for sha, slug, inner in entries:
            content = contents[sha]
            by_slug.setdefault(slug, []).append(
                FileEntry(path=inner, content=content, in_git=True,
                          is_text=is_foldable_text(content))
            )
            if inner == "SKILL.md":
                meta_by_slug[slug] = _build_meta(content)
        out: dict[str, SkillSnapshot] = {}
        for slug, files in by_slug.items():
            meta = meta_by_slug.get(slug)
            if meta is None:
                meta = SkillMeta(name="", description="")
            out[slug] = SkillSnapshot(skill_id=slug, slug=slug, meta=meta,
                                     files=tuple(files))
        return out

    def diff(self, base: str, head: str, path: str | None = None) -> str:
        """unified diff 文本。二进制文件第一版只显示「已变更」（T5）。"""
        args = ["diff", base, head]
        if path:
            args += ["--", path]
        return self._git(*args).stdout.decode("utf-8", errors="replace")

    def resolve_ref(self, ref: str) -> str:
        """解析 ref（分支/tag）为 commit sha。"""
        return self._git("rev-parse", f"{ref}^{{commit}}").stdout.decode().strip()

    def has_commits(self) -> bool:
        """HEAD 是否存在（空仓库/刚 init 时为 False）。

        单独一个具名探测，而不是让调用方去 catch `GitError` 再猜错误文本 ——
        「仓库还没有提交」是正常状态，不是异常（体检测量时尤其如此）。
        """
        proc = subprocess.run(  # noqa: S603 —— 参数固定，无用户输入
            ["git", *GIT_BASE_ARGS, "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=self.repo_root, capture_output=True, check=False,
            stdin=subprocess.DEVNULL, timeout=30,
        )
        return proc.returncode == 0

    def create_tag(self, name: str, message: str, commit: str = "HEAD") -> None:
        """建附注 tag（P4）。tag 名合法性交给 git 判定，失败抛 GitError。"""
        self._git(
            "-c", "user.name=Skill OS", "-c", "user.email=skillos@local",
            "tag", "-a", name, "-m", message, commit,
        )

    def list_tags(self) -> list[dict]:
        """tag 清单（name + 所指 commit sha），时间轴徽标数据源。

        附注 tag 的 objectname 是 tag 对象本身，须取解引用的 %(*objectname)；
        轻量 tag 无解引用值，回退 objectname（即 commit）。
        """
        fmt = "%(refname:short)%00%(*objectname)%00%(objectname)"
        out = self._git("tag", f"--format={fmt}", "--sort=-creatordate").stdout
        tags: list[dict] = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            name, deref, direct = (line.split("\x00") + [""])[:3]
            tags.append({"name": name, "sha": deref or direct})
        return tags

    def rollback(self, commit: str, paths: list[str],
                 author: str = "Skill OS <skillos@local>") -> str:
        """先自动建安全快照（含未提交改动），再 checkout <commit> -- <paths> 并提交新 commit。

        不重写历史（07 N23）：回滚本身也是破坏性操作，快照用户可见、可再回滚。
        回滚 commit 即溯源记录（FR-1.0.22）：author + 目标版本写进 git 历史，不可篡改。

        坑（实测）：`git checkout <commit> -- <path>` 只会**恢复/覆盖**目标版本中存在的
        文件，HEAD 有而目标没有的文件纹丝不动 → 回滚后残留新版本文件。必须显式
        `git rm` 掉目标版本中不存在的路径，回滚才是完整一致状态。
        """
        # 1. 安全快照：把当前工作区全部状态（含 dirty）先存下来
        self._git("add", "-A")
        status = self._git("status", "--porcelain").stdout
        if status.strip():
            self._git(
                "-c", "user.name=Skill OS", "-c", "user.email=skillos@local",
                "commit", "-m", "skillos: snapshot before rollback",
            )
        # 2. 计算回滚要删除的文件（HEAD 有、目标没有），显式 rm
        for pathspec in paths:
            head_files = self._ls_tree_files("HEAD", pathspec)
            tgt_files = self._ls_tree_files(commit, pathspec)
            doomed = head_files - tgt_files
            if doomed:
                self._git("rm", "-f", "--", *sorted(doomed))
        # 3. checkout 旧版本路径 + 新 commit（不 rewrite history）
        self._git("checkout", commit, "--", *paths)
        self._git("add", "--", *paths)
        staged = self._git("status", "--porcelain").stdout
        if not staged.strip():
            # 目标与当前一致：不产生空 commit，直接返回 HEAD
            return self._git("rev-parse", "HEAD").stdout.decode().strip()
        name, email = _parse_author(author)
        self._git(
            "-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-m", f"skillos: rollback {', '.join(paths)} to {commit}",
        )
        return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def _ls_tree_files(self, ref: str, pathspec: str) -> set[str]:
        """列 ref 下 pathspec 内的受控文件路径（回滚删除集计算用）。"""
        out = self._git(
            "ls-tree", "-r", "--name-only", "-z", ref, "--", pathspec,
        ).stdout.decode("utf-8", errors="replace")
        return {p for p in out.split("\x00") if p}
