"""Local SDK server; Java owns the display-capable machine, Python owns MCP and check jobs."""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

WORKSPACE = Path(__file__).resolve().parents[1]
if WORKSPACE.name == ".root":  # running from the mirror (qa tests)
    WORKSPACE = WORKSPACE.parent
ROOT = WORKSPACE / ".root"  #mirrored dev layout, refreshed by qa/sync-root.ps1
LIMIT = 1024 * 1024


def java_directory():
    """Prefer a real JDK over Windows' Java launcher shim."""
    homes = [Path(os.environ["JAVA_HOME"])] if os.environ.get("JAVA_HOME") else []
    if os.name == "nt":
        homes += sorted((Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Java").glob("jdk*"), reverse=True)
    for home in homes:
        if (home / "bin" / ("javac.exe" if os.name == "nt" else "javac")).is_file():
            return home / "bin"
    compiler = shutil.which("javac")
    if not compiler:
        raise RuntimeError("Install JDK 17+ and set JAVA_HOME to its directory.")
    return Path(compiler).resolve().parent


def process_options():
    """Keep owned helpers hidden on Windows and in an owned process group elsewhere."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


async def terminate(process):
    """Stop only an owned helper and its descendants, then reap it."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **process_options())
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def response(data, png=None):
    """Return explicit metadata plus a native MCP image, without writing guest output to stdout."""
    content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=True))]
    if png:
        content.append(ImageContent(type="image", mimeType="image/png", data=base64.b64encode(png).decode("ascii")))
    failed = bool(data.get("error")) or data.get("state") in {"runtime_error", "failed", "timeout"}
    return CallToolResult(content=content, structuredContent=data, isError=failed)


class Runtime:
    """Serialize worker requests and keep a single bounded regression job alongside it."""

    def __init__(self):
        self.worker = None
        self.classes = None
        self.lock = asyncio.Lock()
        self.operation = asyncio.Lock()
        self.job = None

    async def start(self):
        """Compile current source into a private output directory and initialize the idle CPU."""
        sync = await asyncio.create_subprocess_exec(shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-File",
            str(WORKSPACE / "qa" / "sync-root.ps1"), stdout=sys.stderr, stderr=sys.stderr, **process_options())
        await sync.wait()
        builds = ROOT / "builds"
        if not builds.resolve().is_relative_to(ROOT):
            raise RuntimeError("Build output resolves outside the project.")
        builds.mkdir(exist_ok=True)
        self.classes = tempfile.TemporaryDirectory(prefix=".mcp-", dir=builds)
        jdk = java_directory()
        extension = ".exe" if os.name == "nt" else ""
        compiler = await asyncio.create_subprocess_exec(str(jdk / ("javac" + extension)), "--release", "17",
            "-encoding", "UTF-8", "-d", self.classes.name, *(str(p) for d in ("intel8080", "c8080", "vulcanide") for p in (WORKSPACE / d / "src").rglob("*.java")),
            cwd=ROOT, stdout=sys.stderr, stderr=sys.stderr, **process_options())
        try:
            await asyncio.wait_for(compiler.wait(), 90)
        except BaseException:
            await terminate(compiler)
            raise
        if compiler.returncode:
            raise RuntimeError("Vulcan compilation failed; see server stderr.")
        classpath = self.classes.name + os.pathsep + str(ROOT / "src")
        self.worker = await asyncio.create_subprocess_exec(str(jdk / ("java" + extension)), "-Xmx512m",
            "-Djava.awt.headless=true", "-cp", classpath, "VulcanIDE.McpRuntime", str(ROOT), cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=sys.stderr, **process_options())
        await self.request("idle")

    async def close(self):
        """Release an active check worker, guest CPU, and temporary classes on disconnect."""
        if self.job and not self.job["task"].done():
            self.job["stop_reason"] = "stopped"
            await terminate(self.job["process"])
            await self.job["task"]
        if self.worker:
            if self.worker.returncode is None:
                self.worker.stdin.close()
                try:
                    await asyncio.wait_for(self.worker.wait(), 3)
                except TimeoutError:
                    await terminate(self.worker)
        if self.classes:
            self.classes.cleanup()

    async def request(self, *fields):
        """Exchange bounded private frames; SDK code remains solely responsible for MCP framing."""
        async with self.lock:
            if not self.worker or self.worker.returncode is not None:
                raise RuntimeError("The Java runtime exited; reconnect the MCP server.")
            try:
                return await asyncio.wait_for(self._exchange(fields), 60)
            except BaseException:
                await terminate(self.worker)
                raise

    async def _exchange(self, fields):
        encoded = [str(field).encode("utf-8") for field in fields]
        if any(len(field) > 65536 for field in encoded):
            return response({"error": "Input field exceeds 64 KiB."})
        self.worker.stdin.write(struct.pack(">i", len(encoded)) + b"".join(struct.pack(">i", len(field)) + field for field in encoded))
        await self.worker.stdin.drain()

        async def integer():
            return struct.unpack(">i", await self.worker.stdout.readexactly(4))[0]

        count = await integer()
        if count < 0 or count > 100 or count % 2:
            raise RuntimeError("Invalid runtime response.")
        parts = []
        for _ in range(count):
            length = await integer()
            if length < 0 or length > 32 * LIMIT:
                raise RuntimeError("Runtime response exceeded its limit.")
            parts.append(await self.worker.stdout.readexactly(length))
        raw = dict(zip((value.decode("utf-8") for value in parts[::2]), parts[1::2]))
        png = raw.pop("png", None)
        console = raw.pop("console_bytes", None)
        data = {}
        integers = {"cursor", "console_total", "dropped_bytes", "exit_code", "captured_at_ms", "width", "height", "image_width", "image_height"}
        for name, value in raw.items():
            text = value.decode("utf-8")
            if "." in name:
                group, key = name.split(".", 1)
                target = data.setdefault(group, {})
            else:
                target, key = data, name
            if key in integers:
                target[key] = int(text)
            elif key == "accepted":
                target[key] = text == "true"
            else:
                target[key] = text
        if console is not None:
            data["console"] = console.decode("latin-1")
            data["console_encoding"] = "latin-1"
        return response(data, png)

    def checks_active(self):
        return self.job is not None and not self.job["task"].done()

    async def check_all(self, package=False):
        """Launch the existing complete suite (and with package=True, qa/build.ps1) without blocking MCP."""
        if self.checks_active():
            return response({"error": "A check job is already active."})
        idle = await self.request("idle")
        if idle.isError:
            return idle
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return response({"error": "PowerShell is required by Vulcan's build scripts."})
        kind = "package" if package else "checks"
        process = await asyncio.create_subprocess_exec(shell, "-NoProfile", "-File", str(WORKSPACE / ("qa/build.ps1" if package else "qa/run-all.ps1")),
            
            cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **process_options())
        self.job = {"id": kind + "-" + uuid.uuid4().hex, "kind": kind, "process": process, "output": bytearray(),
            "total": 0, "stop_reason": "", "skipped": [], "state": "running"}
        self.job["task"] = asyncio.create_task(self._collect_checks(self.job))
        return self.observe_job(self.job["id"], 0)

    async def _collect_checks(self, job):
        pending = b""

        async def collect():
            nonlocal pending
            while chunk := await job["process"].stdout.read(8192):
                job["output"].extend(chunk)
                job["total"] += len(chunk)
                del job["output"][:-LIMIT]
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()[-4096:]
                for line in lines:
                    if b"SKIPPED" in line and len(job["skipped"]) < 100:
                        job["skipped"].append(line.decode("utf-8", errors="replace"))
            await job["process"].wait()

        try:
            await asyncio.wait_for(collect(), 900)
        except TimeoutError:
            job["stop_reason"] = "timeout"
            await terminate(job["process"])
        job["state"] = job["stop_reason"] or ("completed" if job["process"].returncode == 0 else "failed")

    def observe_job(self, job_id, cursor):
        if not self.job or job_id != self.job["id"]:
            return response({"error": "Unknown or replaced check job ID."})
        job = self.job
        if cursor < 0 or cursor > job["total"]:
            return response({"error": "Check output cursor is outside this job."})
        first = job["total"] - len(job["output"])
        offset = max(0, cursor - first)
        delta = bytes(job["output"][offset:offset + 65536])
        data = {"id": job_id, "kind": job["kind"], "state": job["state"],
            "console": delta.decode("utf-8", errors="replace"),
            "cursor": max(cursor, first) + len(delta), "console_total": job["total"], "dropped_bytes": max(0, first - cursor),
            "exit_code": job["process"].returncode, "full_suite_completed": job["state"] == "completed",
            "skipped": job["skipped"], "suite_policy": "The suite runs every check and reports each result."}
        if job["kind"] == "package" and job["state"] == "completed":
            data["jar"] = ", ".join(str(path) for path in sorted((WORKSPACE / "vulcanide" / "builds").glob("VulcanIDE *.jar")))
        return response(data)


runtime = Runtime()
JOB_PREFIXES = ("checks-", "package-")


@asynccontextmanager
async def lifespan(server):
    """Initialize and reliably release the private display-capable runtime."""
    try:
        await runtime.start()
        yield {}
    finally:
        await runtime.close()


mcp = FastMCP("Vulcan", lifespan=lifespan, log_level="WARNING", instructions=(
    "Vulcan's extended 8080 uses 32-bit registers and 64-bit addresses. All runs use one private display-capable "
    "machine with working OUT 0 console output. Use client filesystem tools to edit files. Run .c, .asm, build.run "
    "or Vulcan VEXE2 .exe files inside this project. Presets: vos, 16x16 Video 2-bit, 32x32 RAM 2-bit. "
    "Use returned IDs and cursors to observe output. Stop an active run before starting another build/run/check job."))


@mcp.tool()
async def run(path: str, preset: str = "vos", timeout_ms: int = 30000, wait_ms: int = 200) -> CallToolResult:
    """Build/run .c, .asm, exact build.run, or Vulcan .exe in display mode. Returns console, CPU status and PNG.

    timeout_ms is a hard 1..60000 ms deadline; wait_ms (0..1000) is only the initial observation delay.
    """
    async with runtime.operation:
        if runtime.checks_active():
            return response({"error": "A check job is active; stop it or wait for completion."})
        return await runtime.request("run", path, preset, timeout_ms, wait_ms)


@mcp.tool()
async def observe(id: str, cursor: int = 0, include_display: bool = True) -> CallToolResult:
    """Read a run's console delta, CPU state and fresh display PNG, or a check job's progress and result."""
    if id.startswith(JOB_PREFIXES):
        return runtime.observe_job(id, cursor)
    return await runtime.request("observe", id, cursor, str(include_display).lower())


@mcp.tool()
async def build(path: str, output_name: str = "program.exe") -> CallToolResult:
    """Build .c, .asm or build.run into builds/mcp-programs; returns a Vulcan executable path and diagnostics."""
    async with runtime.operation:
        if runtime.checks_active():
            return response({"error": "A check job is active; stop it or wait for completion."})
        return await runtime.request("build", path, output_name)


@mcp.tool()
async def send_input(id: str, text: str, line: bool = True) -> CallToolResult:
    """Send Latin-1 keyboard text; line=true appends LF, line=false sends individual bytes without LF."""
    return await runtime.request("input", id, text, str(line).lower())


@mcp.tool()
async def stop(id: str) -> CallToolResult:
    """Stop the identified run or check job and preserve its output. Repeated stops are harmless."""
    if id.startswith(JOB_PREFIXES):
        if not runtime.job or id != runtime.job["id"]:
            return response({"error": "Unknown check job ID."})
        if runtime.checks_active():
            runtime.job["stop_reason"] = "stopped"
            await terminate(runtime.job["process"])
            await runtime.job["task"]
        return runtime.observe_job(id, 0)
    return await runtime.request("stop", id)


@mcp.tool()
async def check_all() -> CallToolResult:
    """Start Vulcan's full regression suite; returns a job ID for observe/stop, with a 15-minute limit."""
    async with runtime.operation:
        return await runtime.check_all()


@mcp.tool()
async def package() -> CallToolResult:
    """Run qa/build.ps1: rebuilds intel8080, c8080 and vulcanide/builds/VulcanIDE <version>.jar, replacing earlier JARs.

    Returns a job ID for observe/stop; a completed job reports the JAR path. Close running Vulcan JARs first.
    """
    async with runtime.operation:
        return await runtime.check_all(package=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")



