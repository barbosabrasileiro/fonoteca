#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Josuel Barbosa
"""
Instalador e verificador da Fonoteca
------------------------------------
Assistente gráfico (GTK3, estilo "avançar, avançar, concluir") para instalar,
atualizar e diagnosticar a Fonoteca em qualquer distro Linux.

  Instalar    dependências do sistema, yt-dlp oficial, arquivos, ícone e atalho
  Atualizar   yt-dlp (o que mais quebra), arquivos da Fonoteca e, se quiser,
              mpv/ffmpeg pelo gerenciador de pacotes
  Diagnosticar  verifica tudo, explica o que está errado e corrige com um clique
  Desinstalar   remove o app, os atalhos e o ícone (seus dados só saem se você pedir)

O instalador se copia para a pasta de instalação e cria o atalho "Instalador da
Fonoteca" no menu. Também dá para abri-lo pela aba "Sobre" da própria Fonoteca.

Depois de uma atualização, uma Fonoteca que já estava aberta continua rodando o código
antigo. O instalador percebe isso, avisa para reiniciar e oferece fechar e reabrir.

Se o GTK ainda não estiver instalado (ou não houver ambiente gráfico), o mesmo
programa funciona no terminal:

  python3 fonoteca-installer.py --install
  python3 fonoteca-installer.py --update
  python3 fonoteca-installer.py --diagnose
  python3 fonoteca-installer.py --uninstall    (--remove-data, --remove-ytdlp, --yes)
  python3 fonoteca-installer.py --cli          (menu interativo no terminal)

Este programa é software livre (GPL-3.0-or-later), sem qualquer garantia.
"""

import argparse
import ast
import datetime
import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

    HAVE_GTK = True
except Exception:  # sem PyGObject/GTK: cai para o modo terminal
    HAVE_GTK = False

# ----------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------
INSTALLER_VERSION = "3.0.0"
APP_NAME = "Fonoteca"
APP_ID = "fonoteca"
APP_TAGLINE = "Uma biblioteca musical para descobrir, organizar e ouvir música."

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
LOCAL_BIN = os.path.join(HOME, ".local", "bin")
DEFAULT_INSTALL_DIR = os.path.join(HOME, ".local", "share", APP_ID)
STATE_DIR = os.path.join(HOME, ".local", "share", "fonoteca-installer")
STATE_FILE = os.path.join(STATE_DIR, "install.json")
CONFIG_DIR = os.path.join(HOME, ".config", APP_ID)
LEGACY_CONFIG_DIR = os.path.join(HOME, ".config", "yt_music_player")
APPS_DIR = os.path.join(HOME, ".local", "share", "applications")
ICON_PNG_DIR = os.path.join(HOME, ".local", "share", "icons", "hicolor", "256x256", "apps")
ICON_SVG_DIR = os.path.join(HOME, ".local", "share", "icons", "hicolor", "scalable", "apps")
# O app cria um socket do mpv por processo (fonoteca_mpv_<pid>.sock); versões antigas usavam
# fonoteca_mpv.sock. O padrão cobre os dois, senão sobra mpv tocando depois de reiniciar o app.
MPV_SOCKET_GLOB = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpv*.sock")
MPV_PROC_PATTERN = f"{APP_ID}_mpv"

# Repositório usado só se a pasta original (clone) não existir mais na hora de atualizar.
# Deve ser o mesmo repositório do APP_URL do fonoteca.py.
REPO_URL = "https://github.com/barbosabrasileiro/fonoteca.git"
SOURCE_CLONE_DIR = os.path.join(STATE_DIR, "source")
INSTALLER_FILE = "fonoteca-installer.py"
INSTALLER_LAUNCHER = "fonoteca-instalador.sh"
INSTALLER_DESKTOP = "fonoteca-installer.desktop"

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
YTDLP_MAX_AGE_DAYS = 60
USER_AGENT = f"Fonoteca-Installer/{INSTALLER_VERSION}"

PAYLOAD_FILES = ("fonoteca.py", INSTALLER_FILE, "fonoteca.png", "fonoteca.svg", "LICENSE", "README.md", "THIRD_PARTY_NOTICES.md")
PAYLOAD_DIRS = ("docs",)
DATA_FILES = ("config.json", "queue.json", "history.json", "playlists.json", "profile.json")

NETWORK_HOSTS = (
    ("YouTube", "https://www.youtube.com/"),
    ("Deezer", "https://api.deezer.com/"),
    ("LRCLIB", "https://lrclib.net/"),
    ("Wikipédia", "https://pt.wikipedia.org/"),
    ("Radio-Browser", "https://de1.api.radio-browser.info/json/stats"),
    ("GitHub (yt-dlp)", "https://github.com/yt-dlp/yt-dlp"),
)

# Pacotes por gerenciador. Cada grupo é uma lista de alternativas (tenta na ordem).
PKG_GROUPS = {
    "apt": {
        "core": [["python3", "python3-gi", "gir1.2-gtk-3.0", "mpv"]],
        "ffmpeg": [["ffmpeg"]],
        "notify": [["libnotify-bin"]],
        "cairo": [["python3-gi-cairo"]],
    },
    "dnf": {
        "core": [["python3", "python3-gobject", "gtk3", "mpv"]],
        "ffmpeg": [["ffmpeg"], ["ffmpeg-free"]],
        "notify": [["libnotify"]],
    },
    "pacman": {
        "core": [["python", "python-gobject", "gtk3", "mpv"]],
        "ffmpeg": [["ffmpeg"]],
        "notify": [["libnotify"]],
    },
    "zypper": {
        "core": [["python3", "python3-gobject", "python3-gobject-Gdk", "typelib-1_0-Gtk-3_0", "mpv"]],
        "ffmpeg": [["ffmpeg"]],
        "notify": [["libnotify-tools"]],
        "cairo": [["python3-gobject-cairo"]],
    },
    "xbps": {
        "core": [["python3", "python3-gobject", "gtk+3", "mpv"]],
        "ffmpeg": [["ffmpeg"]],
        "notify": [["libnotify"]],
    },
    "apk": {
        "core": [["python3", "py3-gobject3", "gtk+3.0", "mpv"]],
        "ffmpeg": [["ffmpeg"]],
        "notify": [["libnotify"]],
    },
}
PKG_UPGRADE_NAMES = ["mpv", "ffmpeg"]
GROUP_LABELS = {
    "core": "mpv, Python e GTK",
    "ffmpeg": "ffmpeg",
    "notify": "notify-send (opcional)",
    "cairo": "integração PyGObject-cairo (arrastar músicas na fila)",
}


# ----------------------------------------------------------------------
# Utilidades gerais
# ----------------------------------------------------------------------
class Result:
    """Resultado de uma etapa ou verificação. status: ok | warn | fail | info | restart."""

    __slots__ = ("status", "title", "detail", "fix")

    def __init__(self, status, title, detail="", fix=None):
        self.status = status
        self.title = title
        self.detail = detail
        self.fix = fix  # "deps" | "ytdlp" | "socket" | None


STATUS_TAG = {"ok": "OK", "warn": "AVISO", "fail": "FALHA", "info": "INFO", "restart": "REINICIE"}
STATUS_MARK = {"ok": "✔", "warn": "!", "fail": "✘", "info": "i", "restart": "↻"}


def capture(cmd, timeout=15, env=None):
    """Executa um comando curto e devolve (código, saída). Nunca levanta exceção."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", timeout=timeout, env=env,
        )
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "tempo esgotado"
    except Exception as e:
        return 127, str(e)


def search_path():
    return LOCAL_BIN + os.pathsep + os.environ.get("PATH", "")


def which(name):
    return shutil.which(name, path=search_path())


def read_os_release():
    data = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.rstrip("\n").split("=", 1)
                        data[k] = v.strip().strip('"')
            break
        except OSError:
            continue
    return data


def detect_pkg_manager():
    for name, exe in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"),
                      ("zypper", "zypper"), ("xbps", "xbps-install"), ("apk", "apk")):
        if shutil.which(exe):
            return name
    return None


def is_immutable_system():
    return os.path.exists("/run/ostree-booted")


def sysinfo_lines():
    osr = read_os_release()
    return [
        f"Distro: {osr.get('PRETTY_NAME', platform.system())}",
        f"Kernel: {platform.release()}",
        f"Python (instalador): {platform.python_version()}",
        f"Sessão: {os.environ.get('XDG_SESSION_TYPE', '?')} · {os.environ.get('XDG_CURRENT_DESKTOP', '?')}",
        f"Gerenciador de pacotes: {detect_pkg_manager() or 'não reconhecido'}",
    ]


def read_app_version(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def syntax_ok(path):
    try:
        with open(path, encoding="utf-8") as f:
            ast.parse(f.read(), filename=path)
        return True, ""
    except Exception as e:
        return False, str(e)


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("install_dir") else None
    except Exception:
        return None


def write_state(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def unsafe_path_reason(path):
    """Caracteres que quebrariam o arquivo .desktop ou o launcher."""
    if not os.path.isabs(path):
        return "o caminho precisa ser absoluto"
    if re.search(r'[\n\r"`$\\%]', path):
        return 'o caminho não pode conter aspas, crase, $, \\ ou %'
    return None


def desktop_dir():
    rc, out = capture(["xdg-user-dir", "DESKTOP"], timeout=5)
    if rc == 0 and out and os.path.isdir(out):
        return out
    for cand in ("Desktop", "Área de Trabalho", "Área de trabalho"):
        p = os.path.join(HOME, cand)
        if os.path.isdir(p):
            return p
    return None


def parse_ytdlp_date(version):
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def same_path(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def has_app_source(path):
    return bool(path) and os.path.isfile(os.path.join(path, "fonoteca.py"))


def is_git_repo(path):
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def file_sha256(path):
    """SHA-256 de um arquivo, ou None se não der para ler."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


SELF_HASH = file_sha256(os.path.abspath(__file__))  # código que este instalador carregou ao abrir


def running_app_pids():
    """PIDs das Fonotecas abertas (python rodando fonoteca.py) deste usuário, sem contar este processo."""
    pids, me, uid = [], os.getpid(), os.getuid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            with open(f"/proc/{name}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if len(argv) < 2 or b"python" not in os.path.basename(argv[0]).lower():
            continue
        script = next((a for a in argv[1:] if a and not a.startswith(b"-")), b"")
        if os.path.basename(script) == b"fonoteca.py":
            pids.append(int(name))
    return pids


def proc_start_time(pid):
    """Momento (epoch) em que o processo começou, ou None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        ticks = int(raw[raw.rindex(")") + 2:].split()[19])
        with open("/proc/stat", encoding="utf-8") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def outdated_app_pids(app_py):
    """Fonotecas abertas que começaram antes da última alteração do fonoteca.py (rodam código antigo)."""
    try:
        mtime = os.path.getmtime(app_py)
    except OSError:
        return []
    out = []
    for pid in running_app_pids():
        started = proc_start_time(pid)
        if started and started + 2 < mtime:
            out.append(pid)
    return out


def restart_app(install_dir):
    """Fecha as Fonotecas abertas e abre de novo. Devolve (ok, mensagem)."""
    launcher = os.path.join(install_dir, "fonoteca.sh")
    if not os.access(launcher, os.X_OK):
        return False, f"Launcher não encontrado em {launcher}. Rode Atualizar para recriá-lo."
    pids = running_app_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as e:
            return False, f"Não consegui fechar a Fonoteca (processo {pid}): {e}"
    deadline = time.time() + 8
    while time.time() < deadline and set(pids) & set(running_app_pids()):
        time.sleep(0.2)
    for pid in set(pids) & set(running_app_pids()):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    # mpv que possa ter sobrado (app morto à força não encerra o mpv): fecha os da Fonoteca
    if shutil.which("pkill"):
        capture(["pkill", "-f", MPV_PROC_PATTERN], timeout=5)
        time.sleep(0.3)
    for sock_path in glob.glob(MPV_SOCKET_GLOB):
        try:
            os.remove(sock_path)
        except OSError:
            pass
    try:
        subprocess.Popen([launcher], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"A Fonoteca foi fechada, mas não consegui abri-la de novo: {e}"
    return True, "Fonoteca reaberta com a versão nova."


# ----------------------------------------------------------------------
# Motor (independe de interface: usado pelo assistente gráfico e pelo terminal)
# ----------------------------------------------------------------------
class Engine:
    def __init__(self, log=print, interactive=False):
        self.log = log
        self.interactive = interactive  # True no terminal: permite sudo com senha
        self.results = []
        self.restart_needed = False  # há uma Fonoteca aberta rodando código antigo

    # ---------- registro ----------
    def add(self, status, title, detail="", fix=None):
        r = Result(status, title, detail, fix)
        self.results.append(r)
        line = f"[{STATUS_TAG[status]}] {title}"
        if detail:
            line += f": {detail.splitlines()[0]}"
        self.log(line)
        return r

    def step(self, text):
        self.log(f"\n▶ {text}")

    # ---------- execução de processos ----------
    def run(self, cmd, timeout=None, env=None):
        """Executa mostrando a saída linha a linha no log. Devolve (código, saída)."""
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=None if self.interactive else subprocess.DEVNULL,
                text=True, errors="replace", bufsize=1, env=env,
            )
        except FileNotFoundError as e:
            self.log(f"    {e}")
            return 127, str(e)
        except Exception as e:
            self.log(f"    {e}")
            return 1, str(e)
        timer = None
        if timeout:
            timer = threading.Timer(timeout, p.kill)
            timer.start()
        lines = []
        for raw in p.stdout:
            line = raw.rstrip("\r\n")
            if line.strip():
                lines.append(line)
                self.log("    " + line)
        p.wait()
        if timer:
            timer.cancel()
        return p.returncode, "\n".join(lines)

    def run_privileged(self, script, timeout=1800):
        """Roda um script sh como root: direto, sudo (terminal) ou pkexec (gráfico)."""
        if os.geteuid() == 0:
            cmd = ["sh", "-c", script]
        elif self.interactive and shutil.which("sudo"):
            cmd = ["sudo", "sh", "-c", script]
        elif shutil.which("pkexec"):
            cmd = ["pkexec", "sh", "-c", script]
        else:
            return 127, "nem pkexec nem sudo estão disponíveis"
        return self.run(cmd, timeout=timeout)

    # ---------- estado do sistema ----------
    def gtk_available_for_python3(self):
        if not shutil.which("python3"):
            return False
        rc, _ = capture(["python3", "-c",
                         "import gi;gi.require_version('Gtk','3.0');from gi.repository import Gtk,GdkPixbuf"])
        return rc == 0

    @staticmethod
    def cairo_available_for_python3():
        """PyGObject com suporte a cairo (pacote separado em Debian/Ubuntu/openSUSE). Sem ele, o desenho
        da linha de inserção ao arrastar músicas na fila gera erros no terminal."""
        if not shutil.which("python3"):
            return False
        rc, _ = capture(["python3", "-c", "import gi;gi.require_foreign('cairo')"])
        return rc == 0

    @staticmethod
    def sqlite_fts5_available():
        """Busca instantânea da biblioteca (SQLite FTS5). Sem ela a Fonoteca usa uma busca simples."""
        if not shutil.which("python3"):
            return False
        rc, _ = capture(["python3", "-c",
                         "import sqlite3;sqlite3.connect(':memory:').execute('create virtual table t using fts5(a)')"])
        return rc == 0

    def missing_groups(self):
        groups = []
        if not (shutil.which("python3") and shutil.which("mpv") and self.gtk_available_for_python3()):
            groups.append("core")
        elif "cairo" in PKG_GROUPS.get(detect_pkg_manager() or "", {}) and not self.cairo_available_for_python3():
            groups.append("cairo")
        if not shutil.which("ffmpeg"):
            groups.append("ffmpeg")
        if not shutil.which("notify-send"):
            groups.append("notify")
        return groups

    # ---------- dependências do sistema ----------
    @staticmethod
    def _install_cmd(mgr, pkgs):
        p = " ".join(shlex.quote(x) for x in pkgs)
        return {
            "apt": f"DEBIAN_FRONTEND=noninteractive apt-get install -y {p}",
            "dnf": f"dnf install -y {p}",
            "pacman": f"pacman -S --needed --noconfirm {p}",
            "zypper": f"zypper --non-interactive install {p}",
            "xbps": f"xbps-install -y {p}",
            "apk": f"apk add {p}",
        }[mgr]

    @staticmethod
    def _refresh_cmd(mgr):
        return {
            "apt": "apt-get update",
            "zypper": "zypper --non-interactive refresh",
            "xbps": "xbps-install -S -y",
            "apk": "apk update",
        }.get(mgr)

    @staticmethod
    def _upgrade_cmd(mgr, pkgs):
        p = " ".join(shlex.quote(x) for x in pkgs)
        return {
            "apt": f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --only-upgrade -y {p}",
            "dnf": f"dnf upgrade -y {p}",
            "zypper": f"zypper --non-interactive update {p}",
            "xbps": f"xbps-install -Su -y {p}",
            "apk": f"apk add -u {p}",
        }.get(mgr)

    def _system_pkg_precheck(self):
        """Devolve o gerenciador, ou None (já registrando o motivo)."""
        if is_immutable_system():
            self.add("warn", "Dependências do sistema",
                     "Este sistema é imutável (rpm-ostree/ostree). Instale mpv e ffmpeg numa caixa "
                     "(toolbox/distrobox) ou como camada (rpm-ostree install) e rode o instalador de novo.")
            return None
        mgr = detect_pkg_manager()
        if not mgr:
            self.add("fail", "Dependências do sistema",
                     "Gerenciador de pacotes não reconhecido. Instale manualmente: mpv, ffmpeg, "
                     "Python 3 com PyGObject e GTK 3.", fix=None)
            return None
        return mgr

    def install_deps(self, groups):
        self.step("Instalando dependências do sistema")
        mgr = self._system_pkg_precheck()
        if not mgr:
            return
        labels = ", ".join(GROUP_LABELS[g] for g in groups)
        self.log(f"    Gerenciador: {mgr} · itens: {labels}")
        self.log("    Uma janela do sistema vai pedir sua senha.")

        lines = []
        refresh = self._refresh_cmd(mgr)
        if refresh:
            lines.append(f"{refresh} || true")
        for g in groups:
            alts = PKG_GROUPS[mgr][g]
            expr = " || ".join(f"( {self._install_cmd(mgr, a)} )" for a in alts)
            lines.append(f"{expr}; echo \"@@RC {g} $?\"")
        script = "\n".join(lines)

        rc, out = self.run_privileged(script)
        codes = dict(re.findall(r"@@RC (\w+) (\d+)", out))
        if not codes and rc in (126, 127):
            self.add("fail", "Dependências do sistema",
                     "Autorização negada ou cancelada. Tente de novo ou instale pelo terminal.", fix="deps")
            return

        for g in groups:
            ok = codes.get(g) == "0"
            if ok:
                self.add("ok", f"Instalado: {GROUP_LABELS[g]}")
            elif g == "core":
                tip = " No Arch, rode 'sudo pacman -Syu' e tente de novo." if mgr == "pacman" else ""
                self.add("fail", f"Falha ao instalar: {GROUP_LABELS[g]}",
                         "Veja o registro detalhado." + tip, fix="deps")
            elif g == "cairo":
                self.add("warn", "Integração PyGObject-cairo não foi instalada",
                         "A Fonoteca funciona, mas arrastar músicas na fila pode mostrar erros no terminal.", fix="deps")
            elif g == "ffmpeg":
                self.add("warn", "ffmpeg não foi instalado",
                         "A Fonoteca toca normalmente, mas não converte downloads para MP3. "
                         "Em algumas distros o ffmpeg completo vem de um repositório extra (RPM Fusion, Packman).",
                         fix="deps")
            else:
                self.add("info", "notify-send não foi instalado", "Só afeta as notificações do desktop.")

    def upgrade_system(self):
        self.step("Atualizando mpv e ffmpeg pelo sistema")
        mgr = self._system_pkg_precheck()
        if not mgr:
            return
        cmd = self._upgrade_cmd(mgr, PKG_UPGRADE_NAMES)
        if not cmd:
            self.add("info", "Atualização do sistema",
                     "No Arch/Manjaro não atualizamos pacotes soltos (isso quebra o sistema). "
                     "Use 'sudo pacman -Syu' quando quiser.")
            return
        installed = [n for n in PKG_UPGRADE_NAMES if shutil.which(n)]
        if not installed:
            self.add("info", "Atualização do sistema", "mpv e ffmpeg não estão instalados.")
            return
        cmd = self._upgrade_cmd(mgr, installed)
        rc, _ = self.run_privileged(cmd)
        if rc == 0:
            self.add("ok", "mpv e ffmpeg verificados/atualizados pelo sistema")
        else:
            self.add("warn", "Não foi possível atualizar mpv/ffmpeg", "Veja o registro detalhado.")

    # ---------- yt-dlp ----------
    def ytdlp_path(self):
        return which("yt-dlp")

    def ytdlp_version(self, path):
        rc, out = capture([path, "--version"], timeout=25)
        return out.splitlines()[0].strip() if rc == 0 and out else None

    def _download(self, url, dest, label):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done, next_mark = 0, 25
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and done * 100 // total >= next_mark:
                    self.log(f"    {label}: {done * 100 // total}%")
                    next_mark += 25
        return done

    def _expected_sha256(self, filename):
        try:
            req = urllib.request.Request(YTDLP_SUMS_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", "replace")
            for line in text.splitlines():
                m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$", line.strip())
                if m and m.group(2) == filename:
                    return m.group(1).lower()
        except Exception as e:
            self.log(f"    Não foi possível obter a lista de hashes: {e}")
        return None

    def install_ytdlp(self, update=False):
        self.step("Atualizando o yt-dlp" if update else "Instalando o yt-dlp oficial")
        dest = os.path.join(LOCAL_BIN, "yt-dlp")
        os.makedirs(LOCAL_BIN, exist_ok=True)
        old = self.ytdlp_version(dest) if os.path.exists(dest) else None

        if old and not update:
            self.add("ok", "yt-dlp", f"já instalado em {dest} (versão {old})")
            return

        if old and update:
            self.log("    Tentando 'yt-dlp -U'…")
            rc, out = self.run([dest, "-U"], timeout=120)
            managed = re.search(r"\bpip\b|package manager", out, re.I)
            if rc == 0 and not managed:
                new = self.ytdlp_version(dest) or "?"
                if new == old:
                    self.add("ok", "yt-dlp", f"já está na versão mais recente ({new})")
                else:
                    self.add("ok", "yt-dlp atualizado", f"{old} → {new}")
                return
            self.log("    Atualização interna indisponível; baixando o binário oficial…")

        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".yt-dlp-", dir=LOCAL_BIN)
            os.close(fd)
            size = self._download(YTDLP_URL, tmp, "Baixando yt-dlp")
            expected = self._expected_sha256("yt-dlp")
            actual = file_sha256(tmp)
            if expected and actual != expected:
                self.add("fail", "yt-dlp", "O SHA-256 do arquivo baixado não confere; instalação cancelada.")
                return
            warn_hash = not expected
            os.chmod(tmp, 0o755)
            new = self.ytdlp_version(tmp)
            if not new:
                self.add("fail", "yt-dlp", "O arquivo baixado não executa. O Python 3 está instalado?", fix="ytdlp")
                return
            os.replace(tmp, dest)
            tmp = None
            self.log(f"    {size // 1024} KB instalados em {dest}")
            if warn_hash:
                self.add("warn", "yt-dlp instalado sem checagem de integridade",
                         f"versão {new}; não foi possível baixar a lista de hashes SHA-256.")
            elif old:
                self.add("ok", "yt-dlp atualizado", f"{old} → {new} (SHA-256 conferido)")
            else:
                self.add("ok", "yt-dlp instalado", f"versão {new} em {dest} (SHA-256 conferido)")
        except Exception as e:
            self.add("fail", "yt-dlp", f"Falha no download: {e}", fix="ytdlp")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---------- arquivos, launcher, ícones e atalhos ----------
    def install_app(self, install_dir, menu=True, desktop=False, update=False, installer_menu=True, source=None):
        self.step("Copiando arquivos da Fonoteca" if not update else "Atualizando arquivos da Fonoteca")
        src_dir = source or HERE
        src_py = os.path.join(src_dir, "fonoteca.py")
        if not os.path.exists(src_py):
            self.add("fail", "Arquivos da Fonoteca",
                     "fonoteca.py não está na mesma pasta do instalador. Extraia o pacote completo.")
            return False
        reason = unsafe_path_reason(install_dir)
        if reason:
            self.add("fail", "Pasta de instalação", reason)
            return False
        src_ok, src_err = syntax_ok(src_py)
        if not src_ok:
            self.add("fail", "fonoteca.py novo com erro de sintaxe",
                     f"{src_err}\nNada foi alterado: a versão instalada continua intacta.")
            return False

        old_version = read_app_version(os.path.join(install_dir, "fonoteca.py"))
        new_version = read_app_version(src_py) or "?"
        try:
            os.makedirs(install_dir, exist_ok=True)
            if not same_path(install_dir, src_dir):
                target_py = os.path.join(install_dir, "fonoteca.py")
                if os.path.exists(target_py) and file_sha256(target_py) != file_sha256(src_py):
                    shutil.copy2(target_py, target_py + ".bak")  # só quando algo muda: não perde a versão anterior
                for name in PAYLOAD_FILES:
                    s = os.path.join(src_dir, name)
                    if not os.path.exists(s):
                        continue
                    if name == INSTALLER_FILE and not syntax_ok(s)[0]:
                        self.add("warn", "Instalador novo ignorado", f"{INSTALLER_FILE} da origem tem erro de sintaxe.")
                        continue
                    shutil.copy2(s, os.path.join(install_dir, name))
                for name in PAYLOAD_DIRS:
                    s = os.path.join(src_dir, name)
                    if os.path.isdir(s):
                        shutil.copytree(s, os.path.join(install_dir, name), dirs_exist_ok=True)
            else:
                self.log("    O instalador já está na pasta de destino; nada a copiar.")
        except Exception as e:
            self.add("fail", "Cópia de arquivos", str(e))
            return False

        app_py = os.path.join(install_dir, "fonoteca.py")
        ok, err = syntax_ok(app_py)
        if not ok:
            self.add("fail", "fonoteca.py", f"erro de sintaxe: {err}")
            return False
        os.chmod(app_py, os.stat(app_py).st_mode | stat.S_IXUSR)

        launcher = os.path.join(install_dir, "fonoteca.sh")
        try:
            with open(launcher, "w", encoding="utf-8") as f:
                f.write(
                    "#!/bin/sh\n"
                    "# Gerado pelo instalador da Fonoteca\n"
                    'PATH="$HOME/.local/bin:$PATH"\n'
                    "export PATH\n"
                    f"exec python3 {shlex.quote(app_py)} \"$@\"\n"
                )
            os.chmod(launcher, 0o755)
        except Exception as e:
            self.add("fail", "Launcher", str(e))
            return False

        icon_png = os.path.join(install_dir, "fonoteca.png")
        icon_svg = os.path.join(install_dir, "fonoteca.svg")
        try:
            if os.path.exists(icon_png):
                os.makedirs(ICON_PNG_DIR, exist_ok=True)
                shutil.copy2(icon_png, os.path.join(ICON_PNG_DIR, "fonoteca.png"))
            if os.path.exists(icon_svg):
                os.makedirs(ICON_SVG_DIR, exist_ok=True)
                shutil.copy2(icon_svg, os.path.join(ICON_SVG_DIR, "fonoteca.svg"))
        except Exception as e:
            self.add("warn", "Ícone", f"não foi possível registrar o ícone no tema: {e}")

        entry = self._desktop_entry(launcher, icon_png if os.path.exists(icon_png) else APP_ID)
        menu_path = os.path.join(APPS_DIR, "fonoteca.desktop")
        if menu:
            try:
                os.makedirs(APPS_DIR, exist_ok=True)
                with open(menu_path, "w", encoding="utf-8") as f:
                    f.write(entry)
                os.chmod(menu_path, 0o644)
                if shutil.which("update-desktop-database"):
                    capture(["update-desktop-database", APPS_DIR], timeout=15)
                if shutil.which("gtk-update-icon-cache"):
                    capture(["gtk-update-icon-cache", "-f", "-t", os.path.join(HOME, ".local", "share", "icons", "hicolor")], timeout=15)
                self.add("ok", "Atalho no menu de aplicativos", menu_path)
            except Exception as e:
                self.add("warn", "Atalho no menu", str(e))
        elif os.path.exists(menu_path):
            try:
                os.remove(menu_path)
            except OSError:
                pass

        desk_path = None
        if desktop:
            d = desktop_dir()
            if not d:
                self.add("warn", "Ícone na Área de Trabalho", "pasta da Área de Trabalho não encontrada")
            else:
                try:
                    desk_path = os.path.join(d, "Fonoteca.desktop")
                    with open(desk_path, "w", encoding="utf-8") as f:
                        f.write(entry)
                    os.chmod(desk_path, 0o755)
                    if shutil.which("gio"):
                        capture(["gio", "set", desk_path, "metadata::trusted", "true"], timeout=10)
                    self.add("ok", "Ícone na Área de Trabalho", desk_path)
                except Exception as e:
                    desk_path = None
                    self.add("warn", "Ícone na Área de Trabalho", str(e))

        installer_ok = self._setup_installer_shortcut(
            install_dir, installer_menu, icon_png if os.path.exists(icon_png) else APP_ID)

        previous = read_state() or {}
        write_state({
            "install_dir": install_dir,
            "version": new_version,
            "menu": bool(menu),
            "desktop": bool(desk_path),
            "installer_menu": bool(installer_ok),
            "source_dir": src_dir,
            "in_place": bool(same_path(install_dir, src_dir) or (update and previous.get("in_place"))),
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })

        if update and old_version and old_version != new_version:
            self.add("ok", "Fonoteca atualizada", f"{old_version} → {new_version} em {install_dir} (cópia anterior: fonoteca.py.bak)")
        elif update:
            self.add("ok", "Arquivos da Fonoteca reaplicados", f"versão {new_version} em {install_dir}")
        else:
            self.add("ok", "Fonoteca instalada", f"versão {new_version} em {install_dir}")
        return True

    def _setup_installer_shortcut(self, install_dir, enabled, icon):
        """Cria (ou remove) o atalho 'Instalador da Fonoteca' no menu. Devolve True se existe."""
        installer = os.path.join(install_dir, INSTALLER_FILE)
        launcher = os.path.join(install_dir, INSTALLER_LAUNCHER)
        entry_path = os.path.join(APPS_DIR, INSTALLER_DESKTOP)
        if not enabled or not os.path.exists(installer):
            if os.path.exists(entry_path):
                try:
                    os.remove(entry_path)
                except OSError:
                    pass
            if enabled:
                self.add("warn", "Atalho do instalador",
                         f"{INSTALLER_FILE} não estava na pasta de origem; o atalho não foi criado.")
            return False
        try:
            with open(launcher, "w", encoding="utf-8") as f:
                f.write(
                    "#!/bin/sh\n"
                    "# Gerado pelo instalador da Fonoteca\n"
                    'PATH="$HOME/.local/bin:$PATH"\n'
                    "export PATH\n"
                    f"exec python3 {shlex.quote(installer)} \"$@\"\n"
                )
            os.chmod(launcher, 0o755)
            os.makedirs(APPS_DIR, exist_ok=True)
            with open(entry_path, "w", encoding="utf-8") as f:
                f.write(self._installer_desktop_entry(launcher, icon))
            os.chmod(entry_path, 0o644)
            if shutil.which("update-desktop-database"):
                capture(["update-desktop-database", APPS_DIR], timeout=15)
            self.add("ok", "Atalho do instalador no menu",
                     entry_path + f"\nPara atualizar ou desinstalar depois, abra \"Instalador da {APP_NAME}\" "
                     "no menu ou use o botão na aba Sobre da Fonoteca.")
            return True
        except Exception as e:
            self.add("warn", "Atalho do instalador", str(e))
            return False

    @staticmethod
    def _installer_desktop_entry(launcher, icon):
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name=Instalador da {APP_NAME}\n"
            "GenericName=Atualizar e desinstalar\n"
            f"Comment=Atualizar, diagnosticar ou desinstalar a {APP_NAME}\n"
            f'Exec="{launcher}"\n'
            f"Icon={icon}\n"
            "Terminal=false\n"
            "Categories=System;Settings;\n"
            "StartupWMClass=fonoteca-installer\n"
            "Keywords=fonoteca;atualizar;instalar;desinstalar;\n"
        )

    @staticmethod
    def _desktop_entry(launcher, icon):
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "GenericName=Player de música\n"
            f"Comment={APP_TAGLINE}\n"
            f'Exec="{launcher}"\n'
            f"Icon={icon}\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Audio;Player;\n"
            f"StartupWMClass={APP_ID}\n"
            "Keywords=música;player;letras;playlist;\n"
        )

    # ---------- fluxos completos ----------
    def run_install(self, install_dir=DEFAULT_INSTALL_DIR, menu=True, desktop=False, deps=True, ytdlp=True,
                    installer_menu=True):
        self.results = []
        self.log(f"{APP_NAME} · instalação")
        if deps:
            groups = self.missing_groups()
            if groups:
                self.install_deps(groups)
            else:
                self.step("Dependências do sistema")
                self.add("ok", "Dependências do sistema", "mpv, ffmpeg, Python e GTK já estão instalados")
        if ytdlp:
            self.install_ytdlp(update=False)
        self.install_app(install_dir, menu=menu, desktop=desktop, installer_menu=installer_menu)
        self._verify_essentials()

    def run_update(self, app=True, ytdlp=True, system=False):
        self.results = []
        self.restart_needed = False
        self.log(f"{APP_NAME} · atualização")
        if system:
            self.upgrade_system()
        if ytdlp:
            self.install_ytdlp(update=True)
        if app:
            state = read_state()
            if not state:
                self.step("Arquivos da Fonoteca")
                self.add("fail", "Fonoteca não está instalada",
                         "Use a opção Instalar primeiro (o instalador não encontrou uma instalação anterior).")
            else:
                src = self.find_source(state["install_dir"], state) or self.clone_source()
                if not src:
                    self.step("Arquivos da Fonoteca")
                    self.add("fail", "Não achei o código-fonte para atualizar",
                             "A pasta de onde a Fonoteca foi instalada não existe mais. Baixe de novo com "
                             "'git clone' e rode 'python3 fonoteca-installer.py --update' dentro dela.")
                else:
                    app_py = os.path.join(state["install_dir"], "fonoteca.py")
                    hash_before = file_sha256(app_py)
                    ver_before = read_app_version(app_py) or "?"
                    self.refresh_source(src)
                    done = self.install_app(state["install_dir"], menu=state.get("menu", True),
                                            desktop=state.get("desktop", False), update=True,
                                            installer_menu=state.get("installer_menu", True), source=src)
                    if done:
                        self._check_restart(app_py, hash_before, ver_before, state["install_dir"])
        self._verify_essentials()

    def _check_restart(self, app_py, hash_before, ver_before, install_dir):
        """Após atualizar pelo git: avisa se a Fonoteca aberta ainda roda o código antigo."""
        self.step("Conferindo se é preciso reiniciar")
        hash_after = file_sha256(app_py)
        ver_after = read_app_version(app_py) or "?"
        if hash_after and hash_after != hash_before:
            if running_app_pids():
                self.restart_needed = True
                change = f"{ver_before} → {ver_after}" if ver_before != ver_after else f"versão {ver_after} (código alterado)"
                self.add("restart", "Reinicie a Fonoteca para usar a versão nova",
                         f"{change}. A janela que está aberta continua na versão antiga até ser fechada e aberta de novo.")
            else:
                self.add("ok", "Nada para reiniciar", "A Fonoteca não está aberta; a próxima abertura já usa a versão nova.")
        else:
            self.add("ok", "Nada para reiniciar", "O código da Fonoteca não mudou.")
        inst_new = file_sha256(os.path.join(install_dir, INSTALLER_FILE))
        if SELF_HASH and inst_new and inst_new != SELF_HASH:
            self.add("info", "O instalador também foi atualizado",
                     "Feche e abra o Instalador da Fonoteca de novo para usar a versão nova dele.")

    # ---------- origem das atualizações ----------
    def find_source(self, install_dir, state):
        """Pasta com os arquivos mais novos: a do instalador em uso, a registrada ou a clonada por ele."""
        candidates = []
        if has_app_source(HERE) and not same_path(HERE, install_dir):
            candidates.append(HERE)
        if state and state.get("source_dir"):
            candidates.append(state["source_dir"])
        candidates.append(SOURCE_CLONE_DIR)
        for c in candidates:
            if has_app_source(c) and not same_path(c, install_dir):
                return c
        if has_app_source(install_dir) and is_git_repo(install_dir):
            return install_dir  # instalada dentro do próprio clone
        return None

    def clone_source(self):
        if "SEU-USUARIO" in REPO_URL or not shutil.which("git"):
            return None
        self.step("Baixando o código da Fonoteca")
        try:
            if os.path.isdir(SOURCE_CLONE_DIR):
                shutil.rmtree(SOURCE_CLONE_DIR, ignore_errors=True)
            os.makedirs(STATE_DIR, exist_ok=True)
        except OSError:
            return None
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        rc, _ = self.run(["git", "clone", "--depth", "1", REPO_URL, SOURCE_CLONE_DIR], timeout=300, env=env)
        return SOURCE_CLONE_DIR if rc == 0 and has_app_source(SOURCE_CLONE_DIR) else None

    def refresh_source(self, src):
        """Baixa novidades do GitHub quando a origem é um repositório git."""
        self.step("Buscando novidades da Fonoteca")
        if not is_git_repo(src):
            self.add("info", "Código da Fonoteca",
                     f"{src} não é um repositório git; vou usar os arquivos como estão. Para receber novidades, "
                     "baixe a versão nova e rode o instalador de dentro dela.")
            return
        if not shutil.which("git"):
            self.add("warn", "git não encontrado", "Instale o git para baixar novidades automaticamente.")
            return
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")

        def head():
            rc, out = capture(["git", "-C", src, "rev-parse", "HEAD"], timeout=15, env=env)
            return out.strip() if rc == 0 else None

        before_rev = head()
        before = read_app_version(os.path.join(src, "fonoteca.py")) or "?"
        if same_path(src, SOURCE_CLONE_DIR):
            # clone privado do instalador (raso): pode ser sobrescrito, o que também resolve histórico reescrito
            rc, _ = self.run(["git", "-C", src, "fetch", "--depth", "1", "origin", "HEAD"], timeout=180, env=env)
            if rc == 0:
                rc, _ = self.run(["git", "-C", src, "reset", "--hard", "FETCH_HEAD"], timeout=60, env=env)
        else:
            rc, _ = self.run(["git", "-C", src, "pull", "--ff-only"], timeout=180, env=env)
        if rc != 0:
            self.add("warn", "Não foi possível baixar as novidades",
                     "Verifique a internet ou alterações locais no repositório ('git status'). "
                     "Os arquivos que já estão na pasta serão reaplicados.")
            return
        after_rev = head()
        after = read_app_version(os.path.join(src, "fonoteca.py")) or "?"
        if before_rev and after_rev and before_rev == after_rev:
            self.add("ok", "Código da Fonoteca", f"já está na versão mais recente ({after})")
        elif before != after:
            self.add("ok", "Novidades baixadas", f"{before} → {after}")
        else:
            self.add("ok", "Novidades baixadas", f"versão {after} (código atualizado)")

    # ---------- desinstalação ----------
    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _is_own_desktop_file(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().lower()
        except OSError:
            return False
        return "[desktop entry]" in text and "fonoteca" in text

    def run_uninstall(self, remove_data=False, remove_ytdlp=False):
        self.results = []
        self.log(f"{APP_NAME} · desinstalação")
        state = read_state()
        install_dir = state["install_dir"] if state else DEFAULT_INSTALL_DIR

        if running_app_pids():
            self.add("info", f"A {APP_NAME} está aberta",
                     "A janela continua na tela até você fechá-la; os arquivos e atalhos são removidos mesmo assim.")

        # arquivos
        self.step("Removendo os arquivos")
        home_real, dir_real = os.path.realpath(HOME), os.path.realpath(install_dir)
        if dir_real in ("/", home_real) or not os.path.isabs(install_dir):
            self.add("fail", "Pasta de instalação suspeita", f"{install_dir}: não vou remover nada dela.")
        elif not os.path.isdir(install_dir) or not (state or has_app_source(install_dir)):
            self.add("info", "Arquivos da Fonoteca", f"Nenhuma instalação encontrada em {install_dir}")
        else:
            in_place = bool(state and state.get("in_place")) or is_git_repo(install_dir)
            for name in ("fonoteca.sh", INSTALLER_LAUNCHER, "fonoteca.py.bak"):
                self._rm(os.path.join(install_dir, name))
            if in_place:
                self.add("ok", "Arquivos gerados removidos",
                         f"{install_dir} é a sua pasta original (clone ou pacote extraído): mantive os arquivos "
                         "dela. Apague a pasta à mão se quiser.")
            else:
                for name in PAYLOAD_FILES:
                    self._rm(os.path.join(install_dir, name))
                for name in PAYLOAD_DIRS + ("__pycache__",):
                    shutil.rmtree(os.path.join(install_dir, name), ignore_errors=True)
                try:
                    os.rmdir(install_dir)
                    self.add("ok", "Arquivos removidos", install_dir)
                except OSError:
                    self.add("warn", "Pasta mantida",
                             f"{install_dir} tem outros arquivos que não foram criados pela {APP_NAME}.")

        # atalhos e ícones
        self.step("Removendo atalhos e ícones")
        removed = []
        for name in ("fonoteca.desktop", INSTALLER_DESKTOP):
            path = os.path.join(APPS_DIR, name)
            if self._is_own_desktop_file(path) and self._rm(path):
                removed.append(path)
        d = desktop_dir()
        if d:
            path = os.path.join(d, "Fonoteca.desktop")
            if self._is_own_desktop_file(path) and self._rm(path):
                removed.append(path)
        for path in (os.path.join(ICON_PNG_DIR, "fonoteca.png"), os.path.join(ICON_SVG_DIR, "fonoteca.svg")):
            if self._rm(path):
                removed.append(path)
        if removed:
            if shutil.which("update-desktop-database"):
                capture(["update-desktop-database", APPS_DIR], timeout=15)
            if shutil.which("gtk-update-icon-cache"):
                capture(["gtk-update-icon-cache", "-f", "-t",
                         os.path.join(HOME, ".local", "share", "icons", "hicolor")], timeout=15)
            self.add("ok", "Atalhos e ícones removidos", "\n".join(removed))
        else:
            self.add("info", "Atalhos e ícones", "nenhum encontrado")

        socks = glob.glob(MPV_SOCKET_GLOB)
        if socks:
            rc, _ = capture(["pgrep", "-f", MPV_PROC_PATTERN], timeout=5)
            if rc != 0:
                for sock_path in socks:
                    self._rm(sock_path)

        # yt-dlp (opcional)
        if remove_ytdlp:
            self.step("Removendo o yt-dlp")
            path = os.path.join(LOCAL_BIN, "yt-dlp")
            if self._rm(path):
                self.add("ok", "yt-dlp removido", path)
            else:
                self.add("info", "yt-dlp", f"não encontrado em {LOCAL_BIN}")

        # dados (opcional)
        self.step("Seus dados")
        if remove_data:
            if os.path.isdir(CONFIG_DIR):
                try:
                    shutil.rmtree(CONFIG_DIR)
                    self.add("ok", "Dados apagados", CONFIG_DIR)
                except OSError as e:
                    self.add("warn", "Não foi possível apagar os dados", f"{CONFIG_DIR}: {e}")
            else:
                self.add("info", "Dados", "não havia dados para apagar")
        elif os.path.isdir(CONFIG_DIR):
            self.add("ok", "Seus dados foram mantidos",
                     f"{CONFIG_DIR} (playlists, histórico e perfil). Se instalar de novo, tudo volta.")

        # registro do instalador
        try:
            shutil.rmtree(STATE_DIR)
        except OSError:
            pass

        mgr = detect_pkg_manager()
        hints = {
            "apt": "sudo apt remove mpv ffmpeg",
            "dnf": "sudo dnf remove mpv ffmpeg",
            "pacman": "sudo pacman -Rs mpv ffmpeg",
            "zypper": "sudo zypper remove mpv ffmpeg",
            "xbps": "sudo xbps-remove mpv ffmpeg",
            "apk": "sudo apk del mpv ffmpeg",
        }
        self.add("info", "Programas do sistema não foram removidos",
                 "mpv, ffmpeg, Python e GTK podem ser usados por outros programas."
                 + (f"\nSe quiser removê-los: {hints[mgr]}" if mgr in hints else ""))

    def _verify_essentials(self):
        """Ao final de instalar/atualizar, acusa o que ainda impede a Fonoteca de abrir."""
        self.step("Conferindo o essencial")
        problems = 0
        if not shutil.which("mpv"):
            self.add("fail", "mpv não encontrado", "sem ele não há reprodução", fix="deps")
            problems += 1
        if not which("yt-dlp"):
            self.add("fail", "yt-dlp não encontrado", "sem ele não há busca nem streams", fix="ytdlp")
            problems += 1
        if not self.gtk_available_for_python3():
            self.add("fail", "Python 3 sem GTK/PyGObject", "a janela não vai abrir", fix="deps")
            problems += 1
        if not problems:
            self.add("ok", "Essenciais presentes", "mpv, yt-dlp e GTK encontrados")

    # ---------- diagnóstico ----------
    def diagnose(self):
        self.results = []
        self.restart_needed = False
        self.log(f"{APP_NAME} · diagnóstico")
        self.step("Sistema")
        self.add("info", "Sistema", "\n".join(sysinfo_lines()))
        if is_immutable_system():
            self.add("info", "Sistema imutável", "Pacotes do sistema não são instalados por este instalador.")

        self.step("Python e GTK")
        if shutil.which("python3"):
            rc, out = capture(["python3", "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"])
            ver = out.strip()
            try:
                major_minor = tuple(int(x) for x in ver.split(".")[:2])
            except ValueError:
                major_minor = (0, 0)
            if major_minor >= (3, 8):
                self.add("ok", "Python 3", ver)
            else:
                self.add("fail", "Python 3 muito antigo", f"{ver}; a Fonoteca precisa do 3.8 ou superior")
        else:
            self.add("fail", "Python 3 não encontrado", "", fix="deps")
        if self.gtk_available_for_python3():
            self.add("ok", "GTK 3 e PyGObject", "disponíveis para o python3")
            if "cairo" in PKG_GROUPS.get(detect_pkg_manager() or "", {}) and not self.cairo_available_for_python3():
                self.add("warn", "PyGObject sem suporte a cairo",
                         "arrastar músicas na fila gera erros. Instale python3-gi-cairo (Debian/Ubuntu) ou "
                         "python3-gobject-cairo (openSUSE); o conserto automático faz isso.", fix="deps")
        else:
            self.add("fail", "GTK 3 / PyGObject ausentes no python3", "a janela da Fonoteca não abre", fix="deps")
        if shutil.which("python3"):
            if self.sqlite_fts5_available():
                self.add("ok", "SQLite com busca FTS5", "a busca instantânea da biblioteca está disponível")
            else:
                self.add("info", "SQLite/FTS5 indisponível no python3",
                         "a Fonoteca continua funcionando: busca simples da biblioteca e, sem SQLite, dados em arquivos JSON")

        self.step("Programas")
        mpv = shutil.which("mpv")
        if mpv:
            _, out = capture([mpv, "--version"])
            self.add("ok", "mpv", (out.splitlines() or [mpv])[0])
        else:
            self.add("fail", "mpv não encontrado", "obrigatório para tocar músicas", fix="deps")

        yt = self.ytdlp_path()
        if yt:
            ver = self.ytdlp_version(yt)
            if not ver:
                self.add("fail", "yt-dlp não executa", f"{yt}", fix="ytdlp")
            else:
                d = parse_ytdlp_date(ver)
                age = (datetime.date.today() - d).days if d else None
                if age is not None and age > YTDLP_MAX_AGE_DAYS:
                    self.add("warn", "yt-dlp desatualizado",
                             f"versão {ver} ({age} dias). O YouTube muda sempre e versões antigas param de funcionar.\n{yt}",
                             fix="ytdlp")
                else:
                    self.add("ok", "yt-dlp", f"versão {ver}\n{yt}")
        else:
            self.add("fail", "yt-dlp não encontrado", "obrigatório para busca, streams e downloads", fix="ytdlp")

        if shutil.which("ffmpeg"):
            _, out = capture(["ffmpeg", "-version"])
            self.add("ok", "ffmpeg", (out.splitlines() or ["ok"])[0])
        else:
            self.add("warn", "ffmpeg não encontrado", "sem ele os downloads não viram MP3", fix="deps")

        if shutil.which("notify-send"):
            self.add("ok", "notify-send", "notificações do desktop disponíveis")
        else:
            self.add("info", "notify-send ausente", "opcional; só afeta as notificações")

        if yt and os.path.dirname(yt) == LOCAL_BIN and LOCAL_BIN not in os.environ.get("PATH", "").split(os.pathsep):
            self.add("info", "~/.local/bin fora do PATH deste terminal",
                     "O atalho da Fonoteca já corrige isso sozinho. Só importa se você abrir pelo terminal "
                     "com 'python3 fonoteca.py'; nesse caso use ./fonoteca.sh.")

        self.step("Instalação da Fonoteca")
        self._diagnose_install()

        self.step("Seus dados")
        self._diagnose_data()

        self.step("Rede")
        yt_net_ok = self._diagnose_network()

        if yt and self.ytdlp_version(yt) and yt_net_ok:
            self.step("Teste de busca (yt-dlp → YouTube)")
            rc, out = self.run([yt, "--no-warnings", "--skip-download", "--print", "title", "ytsearch1:música"], timeout=45)
            if rc == 0 and out.strip():
                self.add("ok", "Busca no YouTube funcionando", out.strip().splitlines()[-1][:80])
            else:
                last = (out.strip().splitlines() or ["sem detalhes"])[-1][:160]
                self.add("warn", "A busca de teste falhou", f"{last}\nGeralmente se resolve atualizando o yt-dlp.", fix="ytdlp")

    def _diagnose_install(self):
        state = read_state()
        entry = os.path.join(APPS_DIR, "fonoteca.desktop")
        if not state:
            if os.path.exists(entry):
                self.add("info", "Atalho existe, mas o instalador não registrou a instalação", entry)
            else:
                extra = " Você pode abrir direto com 'python3 fonoteca.py'." if os.path.exists(os.path.join(HERE, "fonoteca.py")) else ""
                self.add("info", "Fonoteca ainda não instalada", "Use a opção Instalar." + extra)
            return
        d = state["install_dir"]
        app_py = os.path.join(d, "fonoteca.py")
        if not os.path.exists(app_py):
            self.add("fail", "Arquivos da Fonoteca sumiram", f"esperado em {d}. Rode Instalar de novo.")
            return
        ok, err = syntax_ok(app_py)
        if not ok:
            self.add("fail", "fonoteca.py com erro", err)
            return
        installed = read_app_version(app_py) or "?"
        source = read_app_version(os.path.join(HERE, "fonoteca.py"))
        if source and source != installed:
            self.add("info", "Há uma versão diferente neste pacote", f"instalada {installed} · pacote {source}. Use Atualizar.")
        else:
            self.add("ok", "Fonoteca instalada", f"versão {installed} em {d}")
        if outdated_app_pids(app_py):
            self.restart_needed = True
            self.add("restart", "A Fonoteca aberta está rodando código antigo",
                     "Os arquivos foram atualizados depois que ela abriu. Feche e abra de novo para usar a versão nova.")
        launcher = os.path.join(d, "fonoteca.sh")
        if not os.access(launcher, os.X_OK):
            self.add("warn", "Launcher ausente ou sem permissão", launcher + "\nUse Atualizar para recriar.")
        if state.get("installer_menu", True) and not os.path.exists(os.path.join(APPS_DIR, INSTALLER_DESKTOP)):
            self.add("warn", "Atalho do instalador ausente", "Use Atualizar para recriá-lo.")
        src = state.get("source_dir")
        if src and not has_app_source(src) and not same_path(src, d):
            self.add("info", "A pasta de onde a Fonoteca foi instalada não existe mais",
                     f"{src}\nO Atualizar tenta baixar o código de novo pelo git; se não der, rode o instalador "
                     "de dentro de um clone novo.")
        if state.get("menu", True):
            if not os.path.exists(entry):
                self.add("warn", "Atalho do menu ausente", "Use Atualizar para recriá-lo.")
            else:
                with open(entry, encoding="utf-8") as fh:
                    m = re.search(r'^Exec="?([^"\n]+)"?', fh.read(), re.M)
                if m and not os.path.exists(m.group(1)):
                    self.add("warn", "O atalho aponta para um arquivo que não existe", m.group(1) + "\nUse Atualizar para corrigir.")
                else:
                    self.add("ok", "Atalho do menu", entry)

    def _diagnose_data(self):
        if not os.path.isdir(CONFIG_DIR):
            if os.path.isdir(LEGACY_CONFIG_DIR):
                self.add("info", "Dados do YT Music Player encontrados",
                         "Serão copiados sozinhos para a Fonoteca na primeira abertura.")
            else:
                self.add("info", "Ainda sem dados", f"{CONFIG_DIR} será criada ao abrir a Fonoteca.")
            return
        bad = []
        found = 0
        for name in DATA_FILES:
            p = os.path.join(CONFIG_DIR, name)
            if os.path.exists(p):
                found += 1
                try:
                    with open(p, encoding="utf-8") as f:
                        json.load(f)
                except Exception:
                    bad.append(name)
        db_path = os.path.join(CONFIG_DIR, "library.db")
        if os.path.exists(db_path):
            try:
                import sqlite3
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                try:
                    verdict = con.execute("PRAGMA quick_check").fetchone()[0]
                    n_tracks = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
                finally:
                    con.close()
            except Exception as e:
                verdict, n_tracks = str(e)[:80], 0
            if verdict == "ok":
                found += 1
                self.add("ok", "Banco de dados (library.db)",
                         f"íntegro · {n_tracks} faixas indexadas (playlists, favoritas e histórico)")
            else:
                self.add("warn", "Banco de dados (library.db) com problema", f"{verdict}\n"
                         f"Em {CONFIG_DIR}. Restaure um backup (Perfil › Importar). Dica: se existirem arquivos "
                         "playlists.json.bak, favorites.json.bak e history.json.bak, tire o '.bak' do nome e apague "
                         "o library.db: a Fonoteca recria o banco e reimporta esses dados na próxima abertura.")
        if bad:
            self.add("warn", "Arquivos de dados corrompidos", ", ".join(bad) +
                     f"\nEm {CONFIG_DIR}. Restaure um backup (Perfil › Importar) ou renomeie o arquivo para a Fonoteca recriá-lo.")
        else:
            self.add("ok", "Dados do usuário", f"{found} arquivo(s) íntegros em {CONFIG_DIR}")

        socks = glob.glob(MPV_SOCKET_GLOB)
        if socks:
            rc, _ = capture(["pgrep", "-f", MPV_PROC_PATTERN], timeout=5)
            if rc == 0 and running_app_pids():
                self.add("info", "Fonoteca parece estar aberta agora", "há um mpv em execução usando o socket dela")
            elif rc == 0:
                self.add("warn", "mpv da Fonoteca tocando sem o app aberto",
                         "sobrou de um fechamento forçado; o conserto encerra esse mpv", fix="socket")
            else:
                self.add("warn", "Socket do mpv abandonado",
                         f"{socks[0]} sobrou de uma execução anterior", fix="socket")

    def _diagnose_network(self):
        oks, fails = [], []
        yt_ok = False
        for name, url in NETWORK_HOSTS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=8):
                    pass
                oks.append(name)
                if name == "YouTube":
                    yt_ok = True
            except urllib.error.HTTPError:
                oks.append(name)  # respondeu: o servidor é alcançável
                if name == "YouTube":
                    yt_ok = True
            except Exception as e:
                fails.append(f"{name} ({str(e)[:50]})")
        if not fails:
            self.add("ok", "Conexão com os serviços", " · ".join(oks))
        elif not oks:
            self.add("warn", "Sem acesso à internet", "; ".join(fails))
        else:
            self.add("warn", "Alguns serviços não responderam", "; ".join(fails) + (f"\nOK: {' · '.join(oks)}" if oks else ""))
        return yt_ok

    # ---------- correções ----------
    def apply_fixes(self):
        """Aplica as correções sugeridas pelos resultados atuais. Depois, reverifica."""
        fixes = {r.fix for r in self.results if r.fix and r.status in ("warn", "fail")}
        self.log("\n=== Corrigindo problemas encontrados ===")
        if "deps" in fixes:
            groups = self.missing_groups()
            if groups:
                self.results = []
                self.install_deps(groups)
        if "ytdlp" in fixes:
            self.results = []
            self.install_ytdlp(update=bool(os.path.exists(os.path.join(LOCAL_BIN, "yt-dlp"))))
        if "socket" in fixes:
            self.step("Removendo socket abandonado")
            rc, _ = capture(["pgrep", "-f", MPV_PROC_PATTERN], timeout=5)
            if rc == 0 and running_app_pids():
                self.log("    A Fonoteca está aberta; feche-a e tente de novo.")
            else:
                if rc == 0 and shutil.which("pkill"):
                    capture(["pkill", "-f", MPV_PROC_PATTERN], timeout=5)   # mpv órfão: sem app dono
                    time.sleep(0.3)
                for sock_path in glob.glob(MPV_SOCKET_GLOB):
                    try:
                        os.remove(sock_path)
                    except OSError as e:
                        self.log(f"    {e}")
                self.log("    Removido.")
        self.log("\n=== Reverificando ===")
        self.diagnose()

    def has_fixable(self):
        return any(r.fix and r.status in ("warn", "fail") for r in self.results)

    # ---------- relatório ----------
    def report_text(self, title):
        out = [
            f"{APP_NAME} · {title}",
            f"Gerado em {datetime.datetime.now():%d/%m/%Y %H:%M} · instalador {INSTALLER_VERSION}",
            "",
            *sysinfo_lines(),
            "",
        ]
        for r in self.results:
            out.append(f"[{STATUS_TAG[r.status]}] {r.title}")
            for line in (r.detail or "").splitlines():
                out.append(f"      {line}")
        return "\n".join(out) + "\n"

    def overall(self):
        if any(r.status == "fail" for r in self.results):
            return "fail"
        if any(r.status == "warn" for r in self.results):
            return "warn"
        return "ok"


# ----------------------------------------------------------------------
# Modo terminal
# ----------------------------------------------------------------------
def print_summary(engine, title):
    print("\n" + "=" * 60)
    print(engine.report_text(title))
    verdict = {"ok": "Tudo certo.", "warn": "Concluído com avisos.", "fail": "Concluído com problemas."}
    print(verdict[engine.overall()])
    if engine.restart_needed:
        print(f"\n↻ Reinicie a {APP_NAME}: a janela aberta ainda roda a versão antiga.")


def offer_restart(eng):
    """No terminal: se a Fonoteca aberta está desatualizada, oferece fechar e reabrir."""
    if not eng.restart_needed:
        return
    state = read_state()
    if state and sys.stdin.isatty():
        resp = input("Fechar e reabrir a Fonoteca agora? A música em reprodução será interrompida. [s/N] ")
        if resp.strip().lower().startswith("s"):
            ok, msg = restart_app(state["install_dir"])
            print(("✔ " if ok else "✘ ") + msg)
            eng.restart_needed = not ok


def cli_run(args):
    eng = Engine(log=print, interactive=True)
    if args.diagnose:
        eng.diagnose()
        print_summary(eng, "diagnóstico")
        if eng.has_fixable() and sys.stdin.isatty():
            if input("\nTentar corrigir agora? [s/N] ").strip().lower().startswith("s"):
                eng.apply_fixes()
                print_summary(eng, "diagnóstico (após correções)")
        offer_restart(eng)
        return 1 if eng.overall() == "fail" else 0
    if args.uninstall:
        if not args.yes:
            if not sys.stdin.isatty():
                print("Para desinstalar sem terminal interativo, adicione --yes.")
                return 2
            print("\nVai remover a Fonoteca, os atalhos e o ícone.")
            print("Dados (playlists, histórico, perfil): " + ("SERÃO APAGADOS" if args.remove_data else "serão mantidos"))
            print("yt-dlp: " + ("será removido" if args.remove_ytdlp else "será mantido"))
            if not input("Continuar? [s/N] ").strip().lower().startswith("s"):
                print("Cancelado.")
                return 0
        eng.run_uninstall(remove_data=args.remove_data, remove_ytdlp=args.remove_ytdlp)
        print_summary(eng, "desinstalação")
        return 1 if eng.overall() == "fail" else 0
    if args.install:
        eng.run_install(install_dir=os.path.abspath(os.path.expanduser(args.dir)), menu=not args.no_menu,
                        desktop=args.desktop_icon, deps=not args.no_deps, ytdlp=not args.no_ytdlp,
                        installer_menu=not args.no_installer_shortcut)
        print_summary(eng, "instalação")
        return 1 if eng.overall() == "fail" else 0
    if args.update:
        eng.run_update(app=not args.no_app, ytdlp=not args.no_ytdlp, system=args.system)
        print_summary(eng, "atualização")
        offer_restart(eng)
        return 1 if eng.overall() == "fail" else 0
    return 0


def cli_menu(args):
    print(f"\n{APP_NAME} · instalador ({INSTALLER_VERSION})\n{APP_TAGLINE}\n")
    print("  1) Instalar\n  2) Atualizar\n  3) Diagnosticar\n  4) Desinstalar\n  0) Sair")
    choice = input("\nEscolha: ").strip()
    if choice == "1":
        args.install = True
        d = input(f"Pasta de instalação [{DEFAULT_INSTALL_DIR}]: ").strip()
        args.dir = d or DEFAULT_INSTALL_DIR
    elif choice == "2":
        args.update = True
    elif choice == "3":
        args.diagnose = True
    elif choice == "4":
        args.uninstall = True
        args.remove_data = input("Apagar também seus dados (playlists, histórico, perfil)? [s/N] ").strip().lower().startswith("s")
        args.remove_ytdlp = input("Remover também o yt-dlp de ~/.local/bin? [s/N] ").strip().lower().startswith("s")
    else:
        return 0
    return cli_run(args)


# ----------------------------------------------------------------------
# Interface gráfica (assistente)
# ----------------------------------------------------------------------
if HAVE_GTK:

    ICON_BY_STATUS = {
        "ok": "object-select-symbolic",
        "warn": "dialog-warning-symbolic",
        "fail": "dialog-error-symbolic",
        "info": "dialog-information-symbolic",
        "restart": "view-refresh-symbolic",
    }

    class InstallerWizard(Gtk.Assistant):
        P_INTRO, P_OPTIONS, P_CONFIRM, P_PROGRESS, P_SUMMARY = range(5)

        def __init__(self):
            super().__init__()
            self.set_title(f"Instalador da {APP_NAME}")
            self.set_default_size(700, 580)
            self.set_position(Gtk.WindowPosition.CENTER)
            icon = os.path.join(HERE, "fonoteca.png")
            if os.path.exists(icon):
                try:
                    self.set_icon_from_file(icon)
                except Exception:
                    pass

            self.log_buffer = Gtk.TextBuffer()
            self._log_end = self.log_buffer.create_mark("end", self.log_buffer.get_end_iter(), False)
            self.log_views = []
            self.engine = Engine(log=self._log_threadsafe, interactive=False)
            self.running = False
            self.action = "install"
            self.title_done = "Instalação"
            self._pulse_id = None
            self._opts = {}

            self.page_intro = self._build_intro()
            self.page_options = self._build_options()
            self.page_confirm = self._build_confirm()
            self.page_progress = self._build_progress()
            self.page_summary = self._build_summary()

            pages = (
                (self.page_intro, Gtk.AssistantPageType.INTRO, "Bem-vindo"),
                (self.page_options, Gtk.AssistantPageType.CONTENT, "Opções"),
                (self.page_confirm, Gtk.AssistantPageType.CONFIRM, "Confirmar"),
                (self.page_progress, Gtk.AssistantPageType.PROGRESS, "Trabalhando…"),
                (self.page_summary, Gtk.AssistantPageType.SUMMARY, "Resultado"),
            )
            for widget, ptype, title in pages:
                self.append_page(widget)
                self.set_page_type(widget, ptype)
                self.set_page_title(widget, title)
            for widget in (self.page_intro, self.page_options, self.page_confirm, self.page_summary):
                self.set_page_complete(widget, True)
            self.set_page_complete(self.page_progress, False)

            self.set_forward_page_func(self._forward, None)
            self.connect("prepare", self.on_prepare)
            self.connect("apply", self.on_apply)
            self.connect("cancel", self.on_cancel)
            self.connect("close", lambda *_: self._quit())
            self.connect("delete-event", self.on_delete)

        # ----- construção das páginas -----
        def _dim(self, text, wrap=True):
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.set_line_wrap(wrap)
            lbl.get_style_context().add_class("dim-label")
            return lbl

        def _build_intro(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_border_width(24)

            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            logo = Gtk.Image()
            png = os.path.join(HERE, "fonoteca.png")
            try:
                logo.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(png, 80, 80))
            except Exception:
                logo.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
                logo.set_pixel_size(64)
            head.pack_start(logo, False, False, 0)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title = Gtk.Label(xalign=0)
            title.set_markup(f'<span size="xx-large" weight="bold">{APP_NAME}</span>')
            txt.pack_start(title, False, False, 0)
            txt.pack_start(self._dim(APP_TAGLINE), False, False, 0)
            head.pack_start(txt, True, True, 0)
            box.pack_start(head, False, False, 6)

            state = read_state()
            if state:
                status = f"Instalada: versão {state.get('version', '?')} em {state['install_dir']}"
            else:
                status = "Ainda não instalada neste usuário."
            box.pack_start(self._dim(status), False, False, 0)

            q = Gtk.Label(xalign=0)
            q.set_markup("<b>O que você quer fazer?</b>")
            q.set_margin_top(12)
            box.pack_start(q, False, False, 0)

            self.radios = {}
            first = None
            choices = (
                ("install", "Instalar", "Instala as dependências, o yt-dlp oficial, os arquivos, o ícone e o atalho no menu."),
                ("update", "Atualizar", "Atualiza o yt-dlp (o que mais quebra), os arquivos da Fonoteca e, se quiser, mpv e ffmpeg."),
                ("diagnose", "Diagnosticar problemas", "Verifica tudo, explica o que está errado e corrige com um clique."),
                ("uninstall", "Desinstalar", "Remove o app, os atalhos e o ícone. Seus dados ficam guardados, a menos que você peça para apagá-los."),
            )
            default = "update" if state else "install"
            for key, label, desc in choices:
                radio = Gtk.RadioButton.new_with_label_from_widget(first, label) if first else Gtk.RadioButton.new_with_label(None, label)
                if first is None:
                    first = radio
                radio.set_active(key == default)
                self.radios[key] = radio
                box.pack_start(radio, False, False, 0)
                d = self._dim(desc)
                d.set_margin_start(28)
                d.set_margin_bottom(6)
                box.pack_start(d, False, False, 0)
            return box

        def _build_options(self):
            self.stack = Gtk.Stack()

            # --- instalar ---
            inst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            inst.set_border_width(20)
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>Pasta de instalação</b>")
            inst.pack_start(lbl, False, False, 0)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.entry_dir = Gtk.Entry(text=DEFAULT_INSTALL_DIR)
            self.entry_dir.connect("changed", self._on_dir_changed)
            btn_dir = Gtk.Button(label="Escolher…")
            btn_dir.connect("clicked", self._choose_dir)
            row.pack_start(self.entry_dir, True, True, 0)
            row.pack_start(btn_dir, False, False, 0)
            inst.pack_start(row, False, False, 0)
            self.lbl_dir_err = Gtk.Label(xalign=0)
            inst.pack_start(self.lbl_dir_err, False, False, 0)

            self.chk_menu = Gtk.CheckButton(label="Criar atalho no menu de aplicativos")
            self.chk_menu.set_active(True)
            self.chk_desktop = Gtk.CheckButton(label="Criar ícone na Área de Trabalho")
            self.chk_inst_shortcut = Gtk.CheckButton(label="Criar atalho do instalador no menu (para atualizar ou desinstalar depois)")
            self.chk_inst_shortcut.set_active(True)
            self.chk_ytdlp = Gtk.CheckButton(label="Instalar o yt-dlp oficial (recomendado)")
            self.chk_ytdlp.set_active(True)
            self.chk_deps = Gtk.CheckButton(label="Instalar as dependências que faltam (pede sua senha)")
            self.chk_deps.set_active(True)
            self.lbl_deps = self._dim("")
            self.lbl_deps.set_margin_start(28)
            for w in (self.chk_menu, self.chk_desktop, self.chk_inst_shortcut, self.chk_ytdlp, self.chk_deps, self.lbl_deps):
                inst.pack_start(w, False, False, 0)
            self.stack.add_named(inst, "install")

            # --- atualizar ---
            upd = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            upd.set_border_width(20)
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>O que atualizar</b>")
            upd.pack_start(lbl, False, False, 0)
            self.chk_up_ytdlp = Gtk.CheckButton(label="yt-dlp")
            self.chk_up_ytdlp.set_active(True)
            self.lbl_up_ytdlp = self._dim("")
            self.lbl_up_ytdlp.set_margin_start(28)
            self.chk_up_app = Gtk.CheckButton(label="Arquivos da Fonoteca (baixa as novidades pelo git, se possível)")
            self.chk_up_app.set_active(True)
            self.lbl_up_app = self._dim("")
            self.lbl_up_app.set_margin_start(28)
            self.chk_up_sys = Gtk.CheckButton(label="mpv e ffmpeg pelo gerenciador de pacotes (pede sua senha)")
            for w in (self.chk_up_ytdlp, self.lbl_up_ytdlp, self.chk_up_app, self.lbl_up_app, self.chk_up_sys):
                upd.pack_start(w, False, False, 0)
            self.stack.add_named(upd, "update")

            # --- desinstalar ---
            uni = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            uni.set_border_width(20)
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>O que remover</b>")
            uni.pack_start(lbl, False, False, 0)
            uni.pack_start(self._dim("Sempre removidos: os arquivos da Fonoteca, o atalho, o ícone e o atalho do instalador."), False, False, 0)
            self.chk_un_data = Gtk.CheckButton(label="Apagar também meus dados (playlists, histórico e perfil)")
            self.chk_un_ytdlp = Gtk.CheckButton(label="Remover também o yt-dlp instalado em ~/.local/bin")
            d1 = self._dim(f"Os dados ficam em {CONFIG_DIR}. Se não apagar, tudo volta ao reinstalar.")
            d1.set_margin_start(28)
            for w in (self.chk_un_data, d1, self.chk_un_ytdlp):
                uni.pack_start(w, False, False, 0)
            uni.pack_start(self._dim("mpv, ffmpeg, Python e GTK não são removidos, pois outros programas podem usá-los."), False, False, 6)
            self.stack.add_named(uni, "uninstall")

            self.stack.add_named(Gtk.Box(), "diagnose")
            return self.stack

        def _build_confirm(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_border_width(24)
            self.lbl_confirm_title = Gtk.Label(xalign=0)
            box.pack_start(self.lbl_confirm_title, False, False, 0)
            self.lbl_confirm = Gtk.Label(xalign=0, yalign=0)
            self.lbl_confirm.set_line_wrap(True)
            self.lbl_confirm.set_selectable(True)
            box.pack_start(self.lbl_confirm, True, True, 0)
            return box

        def _make_log_view(self):
            view = Gtk.TextView(buffer=self.log_buffer)
            view.set_editable(False)
            view.set_cursor_visible(False)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self.log_views.append(view)
            sw = Gtk.ScrolledWindow()
            sw.set_shadow_type(Gtk.ShadowType.IN)
            sw.add(view)
            return sw

        def _build_progress(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_border_width(18)
            self.lbl_progress = Gtk.Label(xalign=0)
            box.pack_start(self.lbl_progress, False, False, 0)
            self.progress_bar = Gtk.ProgressBar()
            box.pack_start(self.progress_bar, False, False, 0)
            sw = self._make_log_view()
            sw.set_size_request(-1, 300)
            box.pack_start(sw, True, True, 0)
            return box

        def _build_summary(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_border_width(16)

            self.bar_restart = Gtk.InfoBar()
            self.bar_restart.set_message_type(Gtk.MessageType.WARNING)
            self.lbl_restart = Gtk.Label(xalign=0)
            self.lbl_restart.set_line_wrap(True)
            self.lbl_restart.set_markup(
                f"<b>Reinicie a {APP_NAME}</b>\nA janela que está aberta ainda usa a versão antiga. "
                "Feche e abra de novo para aplicar a atualização.")
            self.bar_restart.get_content_area().add(self.lbl_restart)
            self.btn_restart = self.bar_restart.add_button("Reiniciar agora", Gtk.ResponseType.ACCEPT)
            self.bar_restart.connect("response", self.on_restart)
            self.bar_restart.set_no_show_all(True)
            box.pack_start(self.bar_restart, False, False, 0)

            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            self.img_overall = Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.DIALOG)
            head.pack_start(self.img_overall, False, False, 0)
            self.lbl_overall = Gtk.Label(xalign=0)
            head.pack_start(self.lbl_overall, True, True, 0)
            box.pack_start(head, False, False, 0)

            self.results_list = Gtk.ListBox()
            self.results_list.set_selection_mode(Gtk.SelectionMode.NONE)
            sw = Gtk.ScrolledWindow()
            sw.set_shadow_type(Gtk.ShadowType.IN)
            sw.set_size_request(-1, 250)
            sw.add(self.results_list)
            box.pack_start(sw, True, True, 0)

            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.btn_fix = Gtk.Button(label="Corrigir problemas")
            self.btn_fix.connect("clicked", self.on_fix)
            self.btn_copy = Gtk.Button(label="Copiar relatório")
            self.btn_copy.connect("clicked", self.on_copy)
            self.btn_save = Gtk.Button(label="Salvar relatório…")
            self.btn_save.connect("clicked", self.on_save)
            self.btn_open = Gtk.Button(label=f"Abrir a {APP_NAME}")
            self.btn_open.connect("clicked", self.on_open_app)
            for b in (self.btn_fix, self.btn_copy, self.btn_save, self.btn_open):
                bar.pack_start(b, False, False, 0)
            box.pack_start(bar, False, False, 0)

            exp = Gtk.Expander(label="Registro detalhado")
            self.expander = exp
            exp.add(self._make_log_view())
            box.pack_start(exp, False, False, 0)
            return box

        # ----- navegação -----
        def _forward(self, current, _data):
            if current == self.P_INTRO:
                return self.P_CONFIRM if self._selected_action() == "diagnose" else self.P_OPTIONS
            return current + 1

        def _selected_action(self):
            for key, radio in self.radios.items():
                if radio.get_active():
                    return key
            return "install"

        def on_prepare(self, _assistant, page):
            if page is self.page_options:
                self.action = self._selected_action()
                self.stack.set_visible_child_name(self.action)
                if self.action == "install":
                    self.set_page_title(page, "Opções de instalação")
                    missing = self.engine.missing_groups()
                    if missing:
                        names = ", ".join(GROUP_LABELS[g] for g in missing)
                        self.lbl_deps.set_text(f"Faltando neste sistema: {names}")
                        self.chk_deps.set_sensitive(True)
                        self.chk_deps.set_active(True)
                    else:
                        self.lbl_deps.set_text("Todas as dependências já estão instaladas.")
                        self.chk_deps.set_active(False)
                        self.chk_deps.set_sensitive(False)
                    self.set_page_complete(page, self._validate_dir() is None)
                elif self.action == "update":
                    self.set_page_title(page, "Opções de atualização")
                    self.set_page_complete(page, True)
                    yt = self.engine.ytdlp_path()
                    ver = self.engine.ytdlp_version(yt) if yt else None
                    self.lbl_up_ytdlp.set_text(f"Versão atual: {ver}" if ver else "yt-dlp não encontrado; será instalado.")
                    state = read_state()
                    if state:
                        cur = read_app_version(os.path.join(state["install_dir"], "fonoteca.py")) or "?"
                        src = self.engine.find_source(state["install_dir"], state)
                        if src:
                            new = read_app_version(os.path.join(src, "fonoteca.py")) or "?"
                            self.lbl_up_app.set_text(f"Instalada: {cur} · origem: {src} (versão {new})")
                        else:
                            self.lbl_up_app.set_text(f"Instalada: {cur} · a pasta de origem sumiu; tentarei baixar pelo git.")
                        self.chk_up_app.set_sensitive(True)
                    else:
                        self.lbl_up_app.set_text("A Fonoteca ainda não foi instalada por este instalador.")
                        self.chk_up_app.set_active(False)
                        self.chk_up_app.set_sensitive(False)
                else:
                    self.set_page_title(page, "Opções de desinstalação")
                    self.set_page_complete(page, True)
            elif page is self.page_confirm:
                self.action = self._selected_action()
                self._fill_confirm()

        def _fill_confirm(self):
            lines = []
            if self.action == "install":
                self.title_done = "Instalação"
                self.lbl_confirm_title.set_markup("<b>Pronto para instalar</b>")
                lines.append(f"• Instalar em: {self.entry_dir.get_text().strip()}")
                if self.chk_deps.get_active():
                    lines.append("• Instalar as dependências que faltam (uma janela pedirá sua senha)")
                if self.chk_ytdlp.get_active():
                    lines.append(f"• Instalar o yt-dlp oficial em {LOCAL_BIN}")
                if self.chk_menu.get_active():
                    lines.append("• Criar atalho no menu de aplicativos")
                if self.chk_desktop.get_active():
                    lines.append("• Criar ícone na Área de Trabalho")
                if self.chk_inst_shortcut.get_active():
                    lines.append("• Criar o atalho \"Instalador da Fonoteca\" no menu")
            elif self.action == "update":
                self.title_done = "Atualização"
                self.lbl_confirm_title.set_markup("<b>Pronto para atualizar</b>")
                if self.chk_up_sys.get_active():
                    lines.append("• Atualizar mpv e ffmpeg pelo sistema (uma janela pedirá sua senha)")
                if self.chk_up_ytdlp.get_active():
                    lines.append("• Atualizar o yt-dlp")
                if self.chk_up_app.get_active():
                    lines.append("• Atualizar os arquivos da Fonoteca (a versão anterior fica em fonoteca.py.bak)")
                if not lines:
                    lines.append("Nada selecionado.")
            elif self.action == "uninstall":
                self.title_done = "Desinstalação"
                self.lbl_confirm_title.set_markup("<b>Pronto para desinstalar</b>")
                lines += [
                    "• Remover os arquivos da Fonoteca, o atalho, o ícone e o atalho do instalador",
                    f"• APAGAR seus dados em {CONFIG_DIR} (não dá para desfazer)" if self.chk_un_data.get_active()
                    else f"• Manter seus dados em {CONFIG_DIR}",
                    "• Remover o yt-dlp de ~/.local/bin" if self.chk_un_ytdlp.get_active() else "• Manter o yt-dlp",
                ]
            else:
                self.title_done = "Diagnóstico"
                self.lbl_confirm_title.set_markup("<b>Pronto para diagnosticar</b>")
                lines += [
                    "• Python, GTK, mpv, yt-dlp, ffmpeg e SQLite",
                    "• Instalação, atalho e seus dados (inclui o banco library.db)",
                    "• Conexão com YouTube, Deezer, LRCLIB, Wikipédia e Radio-Browser",
                    "• Um teste real de busca com o yt-dlp",
                    "",
                    "Nada será alterado agora. Se algo for encontrado, você poderá corrigir no fim.",
                ]
            lines += ["", "Clique em Aplicar para começar."]
            self.lbl_confirm.set_text("\n".join(lines))

        # ----- pasta de instalação -----
        def _choose_dir(self, _btn):
            dlg = Gtk.FileChooserDialog(title="Escolher pasta de instalação", parent=self,
                                        action=Gtk.FileChooserAction.SELECT_FOLDER)
            dlg.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Escolher", Gtk.ResponseType.OK)
            cur = self.entry_dir.get_text().strip()
            probe = cur
            while probe and not os.path.isdir(probe):
                probe = os.path.dirname(probe)
            if probe:
                dlg.set_current_folder(probe)
            if dlg.run() == Gtk.ResponseType.OK:
                chosen = dlg.get_filename()
                if chosen:
                    if os.path.basename(chosen.rstrip("/")).lower() not in (APP_ID,):
                        chosen = os.path.join(chosen, APP_ID)
                    self.entry_dir.set_text(chosen)
            dlg.destroy()

        def _validate_dir(self):
            path = self.entry_dir.get_text().strip()
            reason = unsafe_path_reason(path) if path else "informe uma pasta"
            if reason:
                self.lbl_dir_err.set_markup(f'<span foreground="#c01c28">{GLib.markup_escape_text(reason)}</span>')
            else:
                self.lbl_dir_err.set_text("")
            return reason

        def _on_dir_changed(self, _entry):
            reason = self._validate_dir()
            if self.action == "install":
                self.set_page_complete(self.page_options, reason is None)

        # ----- execução -----
        def _log_threadsafe(self, text):
            GLib.idle_add(self._append_log, text)

        def _append_log(self, text):
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, text + "\n")
            self.log_buffer.move_mark(self._log_end, self.log_buffer.get_end_iter())
            for view in self.log_views:
                view.scroll_mark_onscreen(self._log_end)
            return False

        def on_apply(self, _assistant):
            self.action = self._selected_action()
            self.log_buffer.set_text("")
            self.running = True
            self._opts = {
                "dir": os.path.abspath(os.path.expanduser(self.entry_dir.get_text().strip() or DEFAULT_INSTALL_DIR)),
                "menu": self.chk_menu.get_active(),
                "desktop": self.chk_desktop.get_active(),
                "deps": self.chk_deps.get_active(),
                "ytdlp": self.chk_ytdlp.get_active(),
                "up_ytdlp": self.chk_up_ytdlp.get_active(),
                "up_app": self.chk_up_app.get_active(),
                "up_sys": self.chk_up_sys.get_active(),
                "inst_shortcut": self.chk_inst_shortcut.get_active(),
                "un_data": self.chk_un_data.get_active(),
                "un_ytdlp": self.chk_un_ytdlp.get_active(),
            }
            self.lbl_progress.set_markup(f"<b>{self.title_done} em andamento…</b>")
            self._start_pulse()
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            eng, o = self.engine, self._opts
            try:
                if self.action == "install":
                    eng.run_install(install_dir=o["dir"], menu=o["menu"], desktop=o["desktop"], deps=o["deps"],
                                    ytdlp=o["ytdlp"], installer_menu=o["inst_shortcut"])
                elif self.action == "update":
                    eng.run_update(app=o["up_app"], ytdlp=o["up_ytdlp"], system=o["up_sys"])
                elif self.action == "uninstall":
                    eng.run_uninstall(remove_data=o["un_data"], remove_ytdlp=o["un_ytdlp"])
                else:
                    eng.diagnose()
            except Exception as e:
                eng.add("fail", "Erro inesperado", repr(e))
            GLib.idle_add(self._finish)

        def _start_pulse(self):
            if self._pulse_id is None:
                self._pulse_id = GLib.timeout_add(120, self._pulse)

        def _pulse(self):
            self.progress_bar.pulse()
            return True

        def _stop_pulse(self):
            if self._pulse_id is not None:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = None

        def _finish(self):
            self.running = False
            self._stop_pulse()
            self.set_page_complete(self.page_progress, True)
            self._populate_summary()
            self.set_current_page(self.P_SUMMARY)
            return False

        # ----- resumo -----
        def _populate_summary(self):
            for child in list(self.results_list.get_children()):
                self.results_list.remove(child)
            for r in self.engine.results:
                self.results_list.add(self._result_row(r))
            self.results_list.show_all()

            overall = self.engine.overall()
            if overall == "fail":
                icon, text = "dialog-error-symbolic", f"{self.title_done} concluído com problemas"
            elif overall == "warn":
                icon, text = "dialog-warning-symbolic", f"{self.title_done} concluído com avisos"
            else:
                icon, text = "object-select-symbolic", f"{self.title_done} concluído: tudo certo"
            self.img_overall.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
            self.lbl_overall.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(text)}</span>')

            self.btn_fix.set_sensitive(True)
            self.btn_fix.set_visible(self.engine.has_fixable())
            self.btn_copy.set_sensitive(True)
            self.btn_save.set_sensitive(True)
            state = read_state()
            can_open = (self.action in ("install", "update") and overall != "fail" and state
                        and os.access(os.path.join(state["install_dir"], "fonoteca.sh"), os.X_OK)
                        and not running_app_pids())  # já aberta: evita abrir uma segunda janela
            self.btn_open.set_sensitive(True)
            self.btn_open.set_visible(bool(can_open))
            if self.engine.restart_needed:
                self.btn_restart.set_sensitive(True)
                self.btn_restart.set_label("Reiniciar agora")
                self.bar_restart.set_no_show_all(False)
                self.bar_restart.show_all()
            else:
                self.bar_restart.hide()
            self.expander.set_expanded(overall == "fail" and not self.engine.has_fixable())

        def _result_row(self, r):
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            h.set_border_width(8)
            img = Gtk.Image.new_from_icon_name(ICON_BY_STATUS[r.status], Gtk.IconSize.LARGE_TOOLBAR)
            img.set_valign(Gtk.Align.START)
            h.pack_start(img, False, False, 0)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(xalign=0)
            t.set_markup(f"<b>{GLib.markup_escape_text(r.title)}</b>")
            t.set_line_wrap(True)
            v.pack_start(t, False, False, 0)
            if r.detail:
                d = Gtk.Label(label=r.detail, xalign=0)
                d.set_line_wrap(True)
                d.set_selectable(True)
                d.get_style_context().add_class("dim-label")
                v.pack_start(d, False, False, 0)
            h.pack_start(v, True, True, 0)
            row.add(h)
            return row

        # ----- botões do resumo -----
        def on_fix(self, _btn):
            if self.running:
                return
            self.running = True
            for b in (self.btn_fix, self.btn_copy, self.btn_save, self.btn_open):
                b.set_sensitive(False)
            self.lbl_overall.set_markup('<span size="large" weight="bold">Corrigindo…</span>')
            self.expander.set_expanded(True)

            def work():
                try:
                    self.engine.apply_fixes()
                except Exception as e:
                    self.engine.add("fail", "Erro inesperado", repr(e))
                GLib.idle_add(self._after_fix)

            threading.Thread(target=work, daemon=True).start()

        def _after_fix(self):
            self.running = False
            self.title_done = "Diagnóstico"
            self._populate_summary()
            return False

        def _report(self):
            return self.engine.report_text(self.title_done.lower())

        def on_copy(self, _btn):
            clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clip.set_text(self._report(), -1)
            clip.store()
            self._toast(self.btn_copy, "Copiado!", "Copiar relatório")

        def _toast(self, btn, temp, original):
            btn.set_label(temp)
            GLib.timeout_add(1500, lambda: (btn.set_label(original), False)[1])

        def on_save(self, _btn):
            dlg = Gtk.FileChooserDialog(title="Salvar relatório", parent=self, action=Gtk.FileChooserAction.SAVE)
            dlg.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Salvar", Gtk.ResponseType.OK)
            dlg.set_do_overwrite_confirmation(True)
            dlg.set_current_name(f"fonoteca-relatorio-{datetime.date.today().isoformat()}.txt")
            if dlg.run() == Gtk.ResponseType.OK:
                path = dlg.get_filename()
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(self._report())
                except OSError as e:
                    m = Gtk.MessageDialog(parent=self, flags=0, message_type=Gtk.MessageType.ERROR,
                                          buttons=Gtk.ButtonsType.OK, text=f"Não foi possível salvar: {e}")
                    m.run()
                    m.destroy()
            dlg.destroy()

        def on_open_app(self, _btn):
            state = read_state()
            if not state:
                return
            try:
                subprocess.Popen([os.path.join(state["install_dir"], "fonoteca.sh")], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._quit()
            except Exception as e:
                m = Gtk.MessageDialog(parent=self, flags=0, message_type=Gtk.MessageType.ERROR,
                                      buttons=Gtk.ButtonsType.OK, text=f"Não foi possível abrir: {e}")
                m.run()
                m.destroy()

        def on_restart(self, _bar, response):
            if response != Gtk.ResponseType.ACCEPT or self.running:
                return
            state = read_state()
            if not state:
                return
            m = Gtk.MessageDialog(parent=self, flags=0, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO, text=f"Fechar e reabrir a {APP_NAME}?",
                                  secondary_text="A música em reprodução será interrompida.")
            answer = m.run()
            m.destroy()
            if answer != Gtk.ResponseType.YES:
                return
            self.running = True  # impede fechar o instalador no meio do reinício
            self.btn_restart.set_sensitive(False)
            self.btn_restart.set_label("Reiniciando…")

            def work():
                ok, msg = restart_app(state["install_dir"])
                GLib.idle_add(self._after_restart, ok, msg)

            threading.Thread(target=work, daemon=True).start()

        def _after_restart(self, ok, msg):
            self.running = False
            if ok:
                self.engine.restart_needed = False
                self.bar_restart.hide()
                self.btn_open.set_visible(False)
                self.lbl_overall.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(msg)}</span>')
            else:
                self.btn_restart.set_sensitive(True)
                self.btn_restart.set_label("Reiniciar agora")
                m = Gtk.MessageDialog(parent=self, flags=0, message_type=Gtk.MessageType.ERROR,
                                      buttons=Gtk.ButtonsType.OK, text=msg)
                m.run()
                m.destroy()
            return False

        # ----- fechar -----
        def _busy_message(self):
            m = Gtk.MessageDialog(parent=self, flags=0, message_type=Gtk.MessageType.INFO,
                                  buttons=Gtk.ButtonsType.OK,
                                  text="Aguarde a conclusão",
                                  secondary_text="Fechar agora deixaria a instalação pela metade. Espere terminar.")
            m.run()
            m.destroy()

        def on_cancel(self, _assistant):
            if self.running:
                self._busy_message()
            else:
                self._quit()

        def on_delete(self, _w, _event):
            if self.running:
                self._busy_message()
                return True
            self._quit()
            return True

        def _quit(self):
            self._stop_pulse()
            Gtk.main_quit()


def run_gui():
    GLib.set_prgname("fonoteca-installer")
    GLib.set_application_name(f"Instalador da {APP_NAME}")
    wizard = InstallerWizard()
    wizard.show_all()
    Gtk.main()
    return 0


# ----------------------------------------------------------------------
# Entrada
# ----------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description=f"Instalador e verificador da {APP_NAME}.")
    p.add_argument("--install", action="store_true", help="instala a Fonoteca (sem interface gráfica)")
    p.add_argument("--update", action="store_true", help="atualiza yt-dlp e arquivos (sem interface gráfica)")
    p.add_argument("--diagnose", action="store_true", help="diagnostica o sistema (sem interface gráfica)")
    p.add_argument("--uninstall", action="store_true", help="remove a Fonoteca, os atalhos e o ícone")
    p.add_argument("--remove-data", action="store_true", help="(com --uninstall) apaga também seus dados")
    p.add_argument("--remove-ytdlp", action="store_true", help="(com --uninstall) remove também o yt-dlp de ~/.local/bin")
    p.add_argument("--yes", action="store_true", help="(com --uninstall) não pedir confirmação")
    p.add_argument("--no-installer-shortcut", action="store_true", help="não criar o atalho do instalador no menu")
    p.add_argument("--cli", action="store_true", help="menu interativo no terminal")
    p.add_argument("--dir", default=DEFAULT_INSTALL_DIR, help="pasta de instalação (com --install)")
    p.add_argument("--no-menu", action="store_true", help="não criar atalho no menu")
    p.add_argument("--desktop-icon", action="store_true", help="criar ícone na Área de Trabalho")
    p.add_argument("--no-deps", action="store_true", help="não instalar dependências do sistema")
    p.add_argument("--no-ytdlp", action="store_true", help="não mexer no yt-dlp")
    p.add_argument("--no-app", action="store_true", help="(com --update) não atualizar os arquivos da Fonoteca")
    p.add_argument("--system", action="store_true", help="(com --update) atualizar mpv/ffmpeg pelo sistema")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.install or args.update or args.diagnose or args.uninstall:
        return cli_run(args)
    if args.cli:
        return cli_menu(args)
    if HAVE_GTK:
        try:
            if Gdk.Display.get_default() is not None:
                return run_gui()
        except Exception as e:
            print(f"Não foi possível abrir a interface gráfica: {e}")
    print("Interface gráfica indisponível (GTK/PyGObject ausentes ou sem ambiente gráfico). Usando o terminal.")
    return cli_menu(args)


if __name__ == "__main__":
    sys.exit(main())
