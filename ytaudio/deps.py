import asyncio
import importlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from discord import gateway as dpy_gateway
from discord import opus as dpy_opus
from discord import voice_client as dpy_voice_client
from discord import voice_state as dpy_voice_state

log = logging.getLogger("red.ytaudio")

WINDOWS = os.name == "nt"
VOICE_PACKAGES = ("PyNaCl>=1.5,<1.6", "davey>=0.1.0")
UPDATE_INTERVAL = 24 * 3600
RETRY_INTERVAL = 15 * 60
INSTALL_COOLDOWN = 5 * 60
BLOCKING = ("python", "yt-dlp", "ffmpeg", "voice")


class Deps:
    def __init__(self, cog, data: Path):
        self.cog = cog
        self.venv = data / "ytdlp-venv"
        self.target = data / "pylib"
        self.voicelib = data / "voicelib"
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.python: Optional[str] = None
        self.env: Dict[str, str] = {}
        self.ytdlp_override: Optional[str] = None
        self.ytdlp_version: Optional[str] = None
        self.ffmpeg: Optional[str] = None
        self.ffmpeg_bundled = False
        self.js: Optional[str] = None
        self.problems: Dict[str, str] = {}
        self._notified: Dict[str, str] = {}
        self._last_install = 0.0
        self._installed_now = False
        self._venv_failed = False

    @property
    def venv_python(self) -> Path:
        return self.venv / ("Scripts/python.exe" if WINDOWS else "bin/python")

    @property
    def ytdlp_cmd(self) -> list:
        return [self.ytdlp_override] if self.ytdlp_override else [self.python or sys.executable, "-m", "yt_dlp"]

    def blocking(self) -> Dict[str, str]:
        return {k: v for k, v in self.problems.items() if k in BLOCKING}

    async def run(self, *cmd: str, timeout: float = 600, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        full_env = {**os.environ, **env} if env else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=full_env
            )
        except OSError as e:
            return 1, str(e)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "timed out"
        return proc.returncode, out.decode(errors="replace").strip()

    async def _py(self, code: str) -> Tuple[int, str]:
        return await self.run(self.python, "-c", code, env=self.env)

    async def _pip(self, *packages: str, pre: bool = False) -> Tuple[int, str]:
        cmd = [self.python, "-m", "pip", "install", "-q", "-U", "--disable-pip-version-check"]
        if pre:
            cmd.append("--pre")
        if self.env:
            cmd += ["--target", str(self.target)]
        code, out = await self.run(*cmd, *packages, env=self.env)
        if code:
            log.warning("pip install %s failed: %s", " ".join(packages), out[-500:])
        return code, out

    async def _setup_python(self) -> bool:
        for attempt in range(2):
            if self.venv_python.exists():
                if (await self.run(str(self.venv_python), "-m", "pip", "--version"))[0] == 0:
                    self.python, self.env = str(self.venv_python), {}
                    return True
                await asyncio.to_thread(shutil.rmtree, self.venv, True)
            if attempt == 0 and not self._venv_failed:
                log.info("Creating Python environment in %s", self.venv)
                code, out = await self.run(sys.executable, "-m", "venv", str(self.venv))
                if code:
                    log.warning("venv creation failed: %s", out[-300:])
        self._venv_failed = True
        if (await self.run(sys.executable, "-m", "pip", "--version"))[0] == 0:
            log.info("Using pip --target %s instead of a venv", self.target)
            self.python, self.env = sys.executable, {"PYTHONPATH": str(self.target)}
            return True
        self.problems["python"] = "Can't install packages: the bot's Python has no pip or venv. Install python3-venv or python3-pip on the host."
        return False

    async def _ytdlp_version(self) -> Optional[str]:
        code, out = await self.run(*self.ytdlp_cmd, "--version", env=self.env, timeout=60)
        return out.splitlines()[-1] if code == 0 and out else None

    async def _find_js(self) -> Optional[str]:
        override = await self.cog.config.js_runtime()
        if override is not None:
            return override or None
        if self.python:
            code, out = await self._py("import deno; print(deno.find_deno_bin())")
            for candidate in ([out.splitlines()[-1]] if code == 0 and out else []) + [
                str(self.target / "bin" / ("deno.exe" if WINDOWS else "deno"))
            ]:
                if Path(candidate).is_file():
                    return f"deno:{candidate}"
        for name in ("deno", "node", "bun"):
            if path := shutil.which(name):
                return f"{name}:{path}"
        return None

    async def _find_ffmpeg(self, install: bool) -> Optional[str]:
        if path := shutil.which("ffmpeg"):
            self.ffmpeg_bundled = False
            return path
        if not self.python:
            return None
        code, out = await self._py("import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
        if code and install:
            log.info("FFmpeg not found, installing a bundled copy")
            await self._pip("imageio-ffmpeg")
            code, out = await self._py("import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
        self.ffmpeg_bundled = code == 0
        return out.splitlines()[-1] if code == 0 and out else None

    def _import_voice(self) -> bool:
        if self.voicelib.exists() and str(self.voicelib) not in sys.path:
            sys.path.append(str(self.voicelib))
        importlib.invalidate_caches()
        try:
            import davey
            import nacl.secret
            import nacl.utils
        except ImportError:
            return False
        dpy_voice_client.nacl = nacl
        dpy_voice_client.has_nacl = dpy_voice_client.has_dave = True
        dpy_voice_state.davey = dpy_gateway.davey = davey
        dpy_voice_state.has_dave = True
        return True

    async def _ensure_voice(self, install: bool) -> Optional[str]:
        if dpy_voice_client.has_nacl and dpy_voice_state.has_dave or self._import_voice():
            return None
        if install:
            log.info("Installing voice libraries into %s", self.voicelib)
            code, out = await self.run(
                sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                "--target", str(self.voicelib), *VOICE_PACKAGES,
            )
            if code:
                log.warning("Voice library install failed: %s", out[-500:])
            if self._import_voice():
                return None
        return "Voice libraries (PyNaCl, davey) are missing and couldn't be installed automatically."

    def opus_available(self) -> bool:
        if dpy_opus.is_loaded():
            return True
        try:
            return dpy_opus._load_default()
        except Exception:
            return False

    async def ensure(self, update: bool = False) -> Dict[str, str]:
        async with self.lock:
            now = time.time()
            install = update or now - self._last_install >= INSTALL_COOLDOWN
            if install:
                self._last_install = now
            self.problems = {}
            self.ytdlp_override = await self.cog.config.ytdlp_path()
            if await self._setup_python():
                version = await self._ytdlp_version()
                if not self.ytdlp_override and install and (update or not version):
                    code, out = await self._pip("yt-dlp[default]", pre=True)
                    new = await self._ytdlp_version()
                    if code and new:
                        self.problems["update"] = f"yt-dlp update failed, still using {new}."
                    self._installed_now |= not version and bool(new)
                    version = new
                self.ytdlp_version = version
                if not version:
                    self.problems["yt-dlp"] = "yt-dlp couldn't be installed. Check the bot's internet connection and logs."
                js = await self._find_js()
                if install and (update or not js) and await self.cog.config.js_runtime() is None:
                    await self._pip("deno")
                    js = await self._find_js()
                self.js = js
                if not js:
                    self.problems["js"] = "No JavaScript runtime found, YouTube may fail. Install deno or node on the host."
            self.ffmpeg = await self._find_ffmpeg(install)
            if not self.ffmpeg:
                self.problems["ffmpeg"] = "FFmpeg is missing and couldn't be installed automatically. Install FFmpeg on the host."
            if voice := await self._ensure_voice(install):
                self.problems["voice"] = voice
            if update and "yt-dlp" not in self.problems and "update" not in self.problems:
                await self.cog.config.last_update.set(now)
            self.ready.set()
        await self._notify()
        return self.problems

    async def recheck(self):
        if self.lock.locked():
            async with self.lock:
                pass
        if not self.ready.is_set() or self.blocking():
            await self.ensure()

    async def loop(self):
        while True:
            try:
                due = await self.cog.config.auto_update() and time.time() - await self.cog.config.last_update() >= UPDATE_INTERVAL
                if due or self.blocking() or not self.ready.is_set():
                    await self.ensure(update=due)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Dependency check failed")
                self.ready.set()
            await asyncio.sleep(RETRY_INTERVAL if self.blocking() else 3600)

    def status(self) -> str:
        ffmpeg = f"{self.ffmpeg} ({'bundled' if self.ffmpeg_bundled else 'system'})" if self.ffmpeg else "missing"
        voice = "ok" if dpy_voice_client.has_nacl and dpy_voice_state.has_dave else "missing"
        lines = [
            f"yt-dlp:     {self.ytdlp_version or 'missing'}",
            f"JS runtime: {self.js or 'none'}",
            f"FFmpeg:     {ffmpeg}",
            f"Voice:      {voice}, {'libopus' if self.opus_available() else 'encoding with FFmpeg'}",
        ]
        return "\n".join(lines + [f"Problem:    {p}" for p in self.problems.values()])

    async def _notify(self):
        messages = []
        if self._installed_now and not self.blocking():
            messages.append(f"**ytaudio** finished setting up and is ready.\n```\n{self.status()}\n```")
        self._installed_now = False
        new = {k: v for k, v in self.problems.items() if self._notified.get(k) != v}
        if new:
            messages.append("**ytaudio** needs attention:\n" + "\n".join(f"- {v}" for v in new.values()))
        elif self._notified and not self.problems:
            messages.append("**ytaudio**: all problems are fixed, music works again.")
        self._notified = dict(self.problems)
        for message in messages:
            try:
                await self.cog.bot.send_to_owners(message)
            except Exception:
                log.exception("Couldn't notify owners")
