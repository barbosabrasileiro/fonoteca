#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Josuel Barbosa
"""
Fonoteca (GTK3 + mpv IPC)
-------------------------
Uma biblioteca musical para descobrir, organizar e ouvir música.

Player leve para Linux, usando os widgets e o tema nativo do GTK do sistema
(sem CSS customizado), com playlists,
downloads, letras, equalizador de 10 bandas, reprodução sem pausa (gapless),
biblioteca em SQLite com busca FTS5, rádios web, descoberta de artistas e
navegação rica.

Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo
sob os termos da GNU General Public License, versão 3 ou (a seu critério)
qualquer versão posterior. Ele é distribuído SEM QUALQUER GARANTIA. Veja o
arquivo LICENSE para os detalhes.
"""

import gi
import subprocess
import threading
import os
import re
import sys
import json
import socket
import time
import math
import tempfile
import random
import urllib.request
import urllib.parse
import shutil
import unicodedata
import difflib
import datetime
import weakref
import contextlib
import signal
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed

try:  # Python sem o módulo sqlite3: a Fonoteca segue funcionando com os arquivos JSON
    import sqlite3
except ImportError:  # pragma: no cover
    sqlite3 = None

try:  # opcional: leitura de tags (ID3, Vorbis, MP4...). Sem ele, usa ffprobe e o nome do arquivo.
    import mutagen
except Exception:  # pragma: no cover
    mutagen = None

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Gio

# ----------------------------------------------------------------------
# Identidade do aplicativo
# ----------------------------------------------------------------------
APP_NAME = "Fonoteca"
APP_ID = "fonoteca"
APP_VERSION = "3.0.0"
APP_TAGLINE = "Uma biblioteca musical para descobrir, organizar e ouvir música."
APP_AUTHOR = "Josuel Barbosa"
APP_YEAR = "2026"
APP_LICENSE = "GPL-3.0-or-later"
APP_URL = "https://github.com/barbosabrasileiro/fonoteca"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+{APP_URL})"

# Serviços opcionais. Desligados por padrão por questões de termos de uso:
# o endpoint público "gtx" do Google Tradutor não é uma API oficial.
TRANSLATE_ENABLED = False

# Diretórios e Arquivos de Configuração
CONFIG_DIR = os.path.join(os.path.expanduser("~/.config"), APP_ID)
# Pasta usada pelo nome antigo do app (YT Music Player). Os dados são copiados
# uma única vez para a pasta nova; a antiga permanece intacta.
LEGACY_CONFIG_DIR = os.path.expanduser("~/.config/yt_music_player")


def _migrate_legacy_config():
    if os.path.isdir(CONFIG_DIR) or not os.path.isdir(LEGACY_CONFIG_DIR):
        return
    try:
        shutil.copytree(LEGACY_CONFIG_DIR, CONFIG_DIR)
    except Exception:
        pass


def _find_icon_file():
    """Procura o ícone PNG ao lado do script e nos locais padrão do sistema."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (
        here,
        os.path.join(here, "assets"),
        f"/usr/share/icons/hicolor/256x256/apps",
        os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps"),
        "/usr/share/pixmaps",
    ):
        path = os.path.join(base, f"{APP_ID}.png")
        if os.path.exists(path):
            return path
    return None


def _find_installer_file():
    """Procura o instalador ao lado do script e na pasta registrada por ele."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "fonoteca-installer.py")]
    try:
        state_file = os.path.expanduser("~/.local/share/fonoteca-installer/install.json")
        with open(state_file, encoding="utf-8") as f:
            install_dir = json.load(f).get("install_dir")
        if install_dir:
            candidates.append(os.path.join(install_dir, "fonoteca-installer.py"))
    except Exception:
        pass
    candidates.append(os.path.expanduser(f"~/.local/share/{APP_ID}/fonoteca-installer.py"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_migrate_legacy_config()
os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
QUEUE_FILE = os.path.join(CONFIG_DIR, "queue.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
PLAYLISTS_FILE = os.path.join(CONFIG_DIR, "playlists.json")
PROFILE_FILE = os.path.join(CONFIG_DIR, "profile.json")
FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")
LIBRARY_FILE = os.path.join(CONFIG_DIR, "library.json")   # índice de tags da biblioteca local
DB_FILE = os.path.join(CONFIG_DIR, "library.db")          # SQLite: faixas, playlists, favoritas, histórico (+FTS5)
COVERS_DIR = os.path.join(CONFIG_DIR, "covers")           # capas extraídas dos arquivos
AUDIO_EXTS = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav", ".wma", ".webm", ".mka")

# Formato do arquivo de backup do perfil (exportar/importar).
# Backups feitos pelo nome antigo continuam sendo aceitos na importação.
BACKUP_FORMAT = "fonoteca_backup"
LEGACY_BACKUP_FORMATS = ("yt_music_player_backup",)
BACKUP_VERSION = 1
HISTORY_LIMIT = 50

# Socket por processo: duas instâncias do app não derrubam o mpv uma da outra.
MPV_SOCKET = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpv_{os.getpid()}.sock")
MPV_LOG = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpv_{os.getpid()}.log")

DEEZER_API = "https://api.deezer.com"
WIKIPEDIA_HOSTS = ("https://pt.wikipedia.org", "https://en.wikipedia.org")
TRANSLATE_API = "https://translate.googleapis.com/translate_a/single"

# Palavras que indicam que uma página da Wikipédia é de músico/banda
MUSIC_KEYWORDS = (
    "banda", "cantor", "cantora", "músic", "music", "compositor", "grupo", "dupla",
    "rapper", "duo", "trio", "vocalista", "instrumentista", "produtor", "guitarr",
    "pianista", "violonista", "sertanej", "mpb", "rock", "pop", "funk", "hip hop",
    "singer", "band", "songwriter", "composer", "vocalist", "guitarist",
)
MUSIC_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in MUSIC_KEYWORDS) + ")", re.I)

# Conteúdo entre parênteses/colchetes que é só "lixo" de título do YouTube
JUNK_GROUP_RE = re.compile(
    r"(?:official|oficial|music|video|vídeo|clipe|clip|audio|áudio|lyrics?|letra|hd|4k|mv|visualizer|\s|-)+",
    re.I,
)


TRANSLATE_CARDS = (
    ("Google Tradutor (endpoint público)", "Traduz para português a biografia obtida da Wikipédia em inglês (até cerca de 1800 caracteres).", "translate.googleapis.com"),
) if TRANSLATE_ENABLED else ()


# ----------------------------------------------------------------------
# Biblioteca local: leitura de tags, capa e letra de arquivos de áudio
# ----------------------------------------------------------------------
def _fmt_clock(seconds):
    try:
        seconds = int(float(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clock_to_seconds(txt):
    """'3:25' -> 205. Devolve 0 se não der para interpretar."""
    try:
        parts = [int(x) for x in str(txt or "").split(":")]
    except ValueError:
        return 0
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec


def _first(val):
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    return str(val).strip() if val is not None else ""


def _filename_guess(path):
    """'Artista - Título.mp3' -> (título, artista). Sem ' - ' devolve só o título."""
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", base)  # remove número de faixa no início
    if " - " in base:
        artist, title = base.split(" - ", 1)
        return title.strip(), artist.strip()
    return base.strip(), ""


def _tags_from_mutagen(path):
    f = mutagen.File(path, easy=False)
    if f is None:
        return {}
    out = {}
    info = getattr(f, "info", None)
    if info is not None and getattr(info, "length", None):
        out["seconds"] = info.length
    tags = f.tags
    if not tags:
        return out
    keys = {k.lower(): k for k in tags.keys()}

    def grab(*names):
        for n in names:
            for lk, k in keys.items():
                if lk == n.lower() or lk.startswith(n.lower() + ":"):
                    v = tags[k]
                    if hasattr(v, "text"):          # ID3
                        v = v.text
                    return _first(v)
        return ""

    out["title"] = grab("TIT2", "title", "\xa9nam")
    out["artist"] = grab("TPE1", "artist", "\xa9ART", "TPE2", "albumartist", "aART")
    out["album"] = grab("TALB", "album", "\xa9alb")
    out["year"] = grab("TDRC", "date", "year", "\xa9day", "TYER")[:4]
    tn = grab("TRCK", "tracknumber")
    out["track_no"] = tn.split("/")[0].strip() if tn else ""
    lyr = ""
    for lk, k in keys.items():                      # USLT (ID3) / LYRICS (Vorbis) / ©lyr (MP4)
        if "uslt" in lk or lk in ("lyrics", "unsyncedlyrics", "\xa9lyr") or lk.endswith(":lyrics"):
            v = tags[k]
            if hasattr(v, "text"):
                v = v.text
            lyr = _first(v)
            if lyr:
                break
    out["lyrics"] = lyr
    return out


def _tags_from_ffprobe(path):
    if not shutil.which("ffprobe"):
        return {}
    try:
        raw = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            stderr=subprocess.DEVNULL, timeout=15)
        fmt = json.loads(raw).get("format", {})
    except Exception:
        return {}
    t = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    tn = str(t.get("track", ""))
    return {
        "seconds": fmt.get("duration"),
        "title": _first(t.get("title")),
        "artist": _first(t.get("artist") or t.get("album_artist")),
        "album": _first(t.get("album")),
        "year": _first(t.get("date") or t.get("year"))[:4],
        "track_no": tn.split("/")[0].strip(),
        "lyrics": _first(t.get("lyrics") or t.get("unsyncedlyrics")),
    }


def read_local_track(path):
    """Metadados de um arquivo local: tags embutidas -> ffprobe -> nome do arquivo."""
    tags = {}
    if mutagen is not None:
        try:
            tags = _tags_from_mutagen(path)
        except Exception:
            tags = {}
    if not tags.get("title") or not tags.get("seconds"):
        try:
            for k, v in _tags_from_ffprobe(path).items():
                if v and not tags.get(k):
                    tags[k] = v
        except Exception:
            pass
    g_title, g_artist = _filename_guess(path)
    title = tags.get("title") or g_title
    artist = tags.get("artist") or g_artist
    dur = _fmt_clock(tags.get("seconds"))
    return {
        "id": "",
        "title": title,
        "uploader": artist,
        "artist": artist,
        "album": tags.get("album", ""),
        "year": tags.get("year", ""),
        "track_no": tags.get("track_no", ""),
        "duration": dur,
        "duration_fmt": dur,
        "verified": False,
        "path": os.path.abspath(path),
        "offline": True,
        "has_lyrics": bool(tags.get("lyrics")),
    }


def read_local_lyrics(path):
    """Letra offline: arquivo .lrc/.txt ao lado da faixa -> letra embutida nas tags."""
    base = os.path.splitext(path)[0]
    for ext in (".lrc", ".LRC", ".txt", ".TXT"):
        side = base + ext
        if os.path.isfile(side):
            try:
                if os.path.getsize(side) > 1_000_000:   # letra de verdade não passa disso; evita ler arquivo gigante
                    continue
                with open(side, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                text = re.sub(r"\[\d+:\d+(?:[.:]\d+)?\]\s*", "", text)      # tempos do .lrc
                text = re.sub(r"(?m)^\[(?:ar|ti|al|by|offset|length|re|ve):.*\]\s*$", "", text)
                text = text.strip()
                if text:
                    return text
            except OSError:
                pass
    if mutagen is not None:
        try:
            return _tags_from_mutagen(path).get("lyrics", "")
        except Exception:
            pass
    return _tags_from_ffprobe(path).get("lyrics", "")


def local_cover_bytes(path):
    """Bytes da capa: imagem embutida -> folder/cover.jpg na pasta -> ffmpeg. None se não houver."""
    if mutagen is not None:
        try:
            f = mutagen.File(path)
            if f is not None:
                tags = f.tags
                pics = getattr(f, "pictures", None)           # FLAC
                if pics:
                    return pics[0].data
                if tags is not None:
                    for k in tags.keys():
                        if str(k).startswith("APIC"):          # MP3
                            return tags[k].data
                    if "covr" in tags and tags["covr"]:        # MP4/M4A
                        return bytes(tags["covr"][0])
        except Exception:
            pass
    folder = os.path.dirname(path)
    for name in ("cover", "folder", "front", "album", "Cover", "Folder"):
        for ext in (".jpg", ".jpeg", ".png"):
            cand = os.path.join(folder, name + ext)
            if os.path.isfile(cand):
                try:
                    with open(cand, "rb") as fh:
                        return fh.read()
                except OSError:
                    pass
    if shutil.which("ffmpeg"):
        try:
            return subprocess.check_output(
                ["ffmpeg", "-v", "quiet", "-i", path, "-an", "-frames:v", "1", "-f", "image2pipe",
                 "-vcodec", "mjpeg", "-"], stderr=subprocess.DEVNULL, timeout=15) or None
        except Exception:
            pass
    return None


_TIMEPOS_RE = re.compile(rb'"data":\s*(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)')


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _reap_orphan_mpv():
    """Encerra mpv que sobrou de uma Fonoteca fechada à força (ex.: atualização/reinício,
    queda de energia do app). Só mexe em mpv da Fonoteca cujo app dono não existe mais."""
    uid, me = os.getuid(), os.getpid()
    tmp = tempfile.gettempdir()
    prefix = f"--input-ipc-server={os.path.join(tmp, APP_ID + '_mpv')}"
    legacy = prefix + ".sock"                       # versões antigas: um socket só, sem PID
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return

    def app_alive(pid):
        argv = _proc_cmdline(pid)
        return any(os.path.basename(a) == f"{APP_ID}.py" for a in argv[1:3])

    others_running = any(int(n) != me and app_alive(n) for n in names)
    for n in names:
        pid = int(n)
        if pid == me:
            continue
        try:
            if os.stat(f"/proc/{n}").st_uid != uid:
                continue
        except OSError:
            continue
        argv = _proc_cmdline(n)
        if not argv or os.path.basename(argv[0]) != "mpv":
            continue
        sock_arg = next((a for a in argv if a.startswith(prefix) and a.endswith(".sock")), None)
        if not sock_arg:
            continue
        mid = sock_arg[len(prefix):-len(".sock")]       # "" (antigo) ou "_<pid do app>"
        if sock_arg == legacy:
            orphan = not others_running
        else:
            orphan = mid.startswith("_") and mid[1:].isdigit() and not app_alive(mid[1:])
        if not orphan:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for path in (sock_arg[len("--input-ipc-server="):],
                     os.path.join(tmp, f"{APP_ID}_mpv{mid}.log")):
            try:
                os.remove(path)
            except OSError:
                pass


def _install_exit_handlers(app):
    """Fechar o app por fora (instalador, kill, logout) tem que fechar de verdade: roda o
    on_destroy (salva tudo e encerra o mpv). Sem isso o Python morre direto no SIGTERM e o
    mpv continua tocando sozinho."""
    def _quit():
        if not getattr(app, "_closing", False):
            app.destroy()
        return False

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, _quit)
        except Exception:
            # PyGObject antigo: handler Python + "cutuca" o laço do GTK para ele rodar
            signal.signal(sig, lambda *a: GLib.idle_add(_quit))
            GLib.timeout_add(500, lambda: True)
    atexit.register(app.mpv._cleanup)   # rede de segurança: saída normal do Python nunca deixa o mpv


# ======================================================================
# Normalização de faixas e chave de identidade (usadas pelo app e pelo banco)
# ======================================================================
def normalize_track(item):
    """Esquema único de uma faixa. Mantém campos extras de faixas offline (path...) e de rádios web."""
    if not isinstance(item, dict):
        item = {}
    uploader = str(item.get("uploader") or item.get("artist") or "")
    duration = str(item.get("duration") or item.get("duration_fmt") or "0:00")
    out = {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or "Sem título"),
        "uploader": uploader,
        "artist": uploader,
        "duration": duration,
        "duration_fmt": duration,
        "verified": bool(item.get("verified")),
    }
    if item.get("path"):
        out.update({k: item[k] for k in ("path", "offline", "album", "year", "track_no", "cover_url") if item.get(k)})
    if item.get("stream_url"):   # estação de rádio web (Radio-Browser)
        out.update({k: item[k] for k in ("stream_url", "webradio", "favicon", "country", "tags") if item.get(k)})
    return out


def track_key(t):
    """Chave estável de uma faixa: arquivo local, id (YouTube/rádio) ou título|artista."""
    if t.get("path"):
        return "file:" + t["path"]
    tid = (t.get("id") or "").strip()
    return tid or f"{(t.get('title') or '').lower()}|{(t.get('uploader') or '').lower()}"


def _unaccent_text(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


# ======================================================================
# Banco de dados SQLite (library.db) com busca de texto completo FTS5
# ======================================================================
class LibraryDB:
    """Persistência relacional de faixas, playlists, favoritas e histórico.

    - Tabelas: tracks, playlists, playlist_tracks, favorites, history (+ meta e tracks_fts).
    - FTS5 (índice externo + gatilhos) para buscar por título/artista/álbum em milissegundos.
      Se o SQLite do sistema não tiver FTS5, a busca cai automaticamente para LIKE.
    - Escritas rodam numa única thread em segundo plano (a interface nunca espera o disco) e só
      regravam o que mudou (diff por playlist). A leitura inicial é feita uma vez na abertura.
    - Na primeira abertura importa os .json legados (playlists/favorites/history) de forma
      transparente e os renomeia para *.json.bak (nada é apagado).
    """

    def __init__(self, path, legacy_files=None):
        self.path = path
        self.fts = False
        self._closed = False
        self._lock = threading.RLock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._pl_snap = {}      # nome -> tupla de chaves (último estado gravado)
        self._fav_snap = None
        self._hist_snap = None
        # isolation_level=None: autocommit; as transações são controladas explicitamente em _tx().
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=15)
        try:
            self.conn.create_function("unaccent", 1, lambda s: _unaccent_text(str(s or "")).lower())
            self._init_schema()
            self._migrate_legacy(legacy_files or {})
            with self._tx() as c:
                self._prune(c)
        except Exception:
            self._writer.shutdown(wait=False)
            try:
                self.conn.close()
            except Exception:
                pass
            raise

    # ---------- infraestrutura ----------
    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _submit(self, fn, *args):
        if self._closed:
            return
        try:
            self._writer.submit(self._guard, fn, *args)
        except RuntimeError:
            pass

    @staticmethod
    def _guard(fn, *args):
        try:
            fn(*args)
        except Exception as exc:   # falha de gravação nunca derruba a interface
            print(f"[{APP_NAME}] erro ao gravar no banco: {exc}", file=sys.stderr)

    def _init_schema(self):
        with self._lock:
            c = self.conn
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA cache_size=-1024")      # ~1 MB de cache de páginas (padrão 2 MB): app leve
            c.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS tracks(
                    id     INTEGER PRIMARY KEY,
                    key    TEXT NOT NULL UNIQUE,
                    title  TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    album  TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'online',   -- 'local' (arquivo) | 'online'
                    data   TEXT NOT NULL                     -- faixa completa em JSON
                );
                CREATE TABLE IF NOT EXISTS playlists(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, position INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS playlist_tracks(
                    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                    position    INTEGER NOT NULL,
                    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    PRIMARY KEY(playlist_id, position));
                CREATE TABLE IF NOT EXISTS favorites(
                    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history(
                    position INTEGER PRIMARY KEY,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS idx_pt_track ON playlist_tracks(track_id);
                CREATE INDEX IF NOT EXISTS idx_hist_track ON history(track_id);
                CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source);
            """)
            try:
                self._create_fts()
                self.fts = True
            except sqlite3.Error:
                self.fts = False

    def _create_fts(self):
        c = self.conn
        last = None
        for tok in ("unicode61 remove_diacritics 2", "unicode61"):   # 'remove_diacritics 2' exige SQLite >= 3.27
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5("
                          "title, artist, album, content='tracks', content_rowid='id', "
                          f"tokenize='{tok}')")
                last = None
                break
            except sqlite3.OperationalError as exc:
                last = exc
        if last is not None:
            raise last
        c.executescript("""
            CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
                INSERT INTO tracks_fts(rowid, title, artist, album) VALUES (new.id, new.title, new.artist, new.album);
            END;
            CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                VALUES ('delete', old.id, old.title, old.artist, old.album);
            END;
            CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                VALUES ('delete', old.id, old.title, old.artist, old.album);
                INSERT INTO tracks_fts(rowid, title, artist, album) VALUES (new.id, new.title, new.artist, new.album);
            END;
        """)
        if self._meta("fts_built") != "1":      # banco criado por versão sem FTS: indexa o que já existe
            c.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")
            self._set_meta("fts_built", "1")

    def _meta(self, key):
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        with self._lock:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- escrita de baixo nível (sempre dentro de _tx) ----------
    @staticmethod
    def _upsert_track(c, t):
        """Insere/atualiza a faixa (pela chave) e devolve o id. Só toca no índice FTS se algo mudou."""
        t = normalize_track(t)
        key = track_key(t)
        data = json.dumps(t, ensure_ascii=False, separators=(",", ":"))
        c.execute(
            "INSERT INTO tracks(key, title, artist, album, source, data) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
            "album=excluded.album, data=excluded.data WHERE tracks.data != excluded.data",
            (key, t["title"], t["uploader"], str(t.get("album") or ""),
             "local" if t.get("path") else "online", data))
        return c.execute("SELECT id FROM tracks WHERE key=?", (key,)).fetchone()[0]

    @staticmethod
    def _prune(c):
        """Remove faixas online que nenhuma playlist/favorita/histórico referencia mais."""
        c.execute("DELETE FROM tracks WHERE source != 'local' AND id NOT IN ("
                  "SELECT track_id FROM playlist_tracks UNION SELECT track_id FROM favorites "
                  "UNION SELECT track_id FROM history)")

    def _w_playlists(self, order, changed):
        with self._tx() as c:
            existing = {n: i for i, n in c.execute("SELECT id, name FROM playlists")}
            for name, pid in existing.items():
                if name not in order:
                    c.execute("DELETE FROM playlists WHERE id=?", (pid,))
            for pos, name in enumerate(order):
                pid = existing.get(name)
                if pid is None:
                    pid = c.execute("INSERT INTO playlists(name, position) VALUES(?,?)", (name, pos)).lastrowid
                else:
                    c.execute("UPDATE playlists SET position=? WHERE id=?", (pos, pid))
                if name in changed:
                    c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
                    rows = [(pid, i, self._upsert_track(c, t)) for i, t in enumerate(changed[name])]
                    c.executemany("INSERT INTO playlist_tracks(playlist_id, position, track_id) VALUES(?,?,?)", rows)
            self._prune(c)

    def _w_favorites(self, tracks):
        with self._tx() as c:
            c.execute("DELETE FROM favorites")
            c.executemany("INSERT OR IGNORE INTO favorites(track_id, position) VALUES(?,?)",
                          [(self._upsert_track(c, t), i) for i, t in enumerate(tracks)])
            self._prune(c)

    def _w_history(self, tracks):
        with self._tx() as c:
            c.execute("DELETE FROM history")
            c.executemany("INSERT INTO history(position, track_id) VALUES(?,?)",
                          [(i, self._upsert_track(c, t)) for i, t in enumerate(tracks)])
            self._prune(c)

    def _w_sync_local(self, tracks, sig=""):
        """Delta-sync do espelho da biblioteca local. Três níveis de economia:
        1) `sig` (mtime+tamanho do library.json, que o app só regrava quando algo muda) igual ao da última
           sincronização => não faz NADA (zero CPU, zero leitura, zero escrita) - o caso normal a cada abertura;
        2) senão compara com o banco e só grava faixas novas/alteradas e remove as sumidas;
        3) trabalha em lotes cedendo a GIL, para não competir com a interface."""
        if sig and self._meta("local_sig") == sig:
            return
        fresh = {}
        for n, t in enumerate(tracks, 1):
            t = normalize_track(dict(t))
            fresh[track_key(t)] = (t, json.dumps(t, ensure_ascii=False, separators=(",", ":")))
            if n % 400 == 0:
                time.sleep(0.002)
        with self._lock:
            known = dict(self.conn.execute("SELECT key, data FROM tracks WHERE source='local'").fetchall())
        changed = [t for k, (t, blob) in fresh.items() if known.get(k) != blob]
        stale = [k for k in known if k not in fresh]
        if not changed and not stale:
            if sig:
                self._set_meta("local_sig", sig)
            return
        with self._tx() as c:
            for t in changed:
                self._upsert_track(c, t)
            for n in range(0, len(stale), 500):
                chunk = stale[n:n + 500]
                marks = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM tracks WHERE key IN ({marks}) AND source='local' AND id NOT IN ("
                          "SELECT track_id FROM playlist_tracks UNION SELECT track_id FROM favorites "
                          "UNION SELECT track_id FROM history)", chunk)
            if sig:
                c.execute("INSERT INTO meta(key, value) VALUES('local_sig', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sig,))

    # ---------- API de escrita (chamada pela interface; devolve na hora) ----------
    def save_playlists(self, playlists):
        keys = {str(n): tuple(track_key(t) for t in v) for n, v in playlists.items()}
        if keys == self._pl_snap:
            return
        changed = {n: [dict(t) for t in playlists[n]] for n in keys if self._pl_snap.get(n) != keys[n]}
        self._pl_snap = keys
        self._submit(self._w_playlists, list(keys), changed)

    def save_favorites(self, tracks):
        keys = tuple(track_key(t) for t in tracks)
        if keys == self._fav_snap:
            return
        self._fav_snap = keys
        self._submit(self._w_favorites, [dict(t) for t in tracks])

    def save_history(self, tracks):
        keys = tuple(track_key(t) for t in tracks)
        if keys == self._hist_snap:
            return
        self._hist_snap = keys
        self._submit(self._w_history, [dict(t) for t in tracks])

    def sync_local(self, tracks, sig=""):
        """Espelha o índice da biblioteca local na tabela tracks (para a busca FTS), em segundo plano.
        Recebe só a lista de referências (barato); as cópias e a comparação são feitas na thread do banco."""
        self._submit(self._w_sync_local, list(tracks), sig)

    # ---------- leitura ----------
    @staticmethod
    def _loads(blob):
        try:
            d = json.loads(blob)
            return d if isinstance(d, dict) else None
        except (TypeError, ValueError):
            return None

    def load_playlists(self):
        out = {}
        with self._lock:
            rows = self.conn.execute(
                "SELECT p.name, t.data FROM playlists p "
                "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
                "LEFT JOIN tracks t ON t.id = pt.track_id ORDER BY p.position, p.id, pt.position").fetchall()
        for name, blob in rows:
            lst = out.setdefault(name, [])
            d = self._loads(blob) if blob else None
            if d:
                lst.append(d)
        self._pl_snap = {n: tuple(track_key(t) for t in v) for n, v in out.items()}
        return out

    def _load_ordered(self, sql):
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [d for d in (self._loads(r[0]) for r in rows) if d]

    def load_favorites(self):
        out = self._load_ordered("SELECT t.data FROM favorites f JOIN tracks t ON t.id=f.track_id ORDER BY f.position")
        self._fav_snap = tuple(track_key(t) for t in out)
        return out

    def load_history(self):
        out = self._load_ordered("SELECT t.data FROM history h JOIN tracks t ON t.id=h.track_id ORDER BY h.position")
        self._hist_snap = tuple(track_key(t) for t in out)
        return out

    def search(self, text, limit=200):
        """Busca instantânea por qualquer termo de título, artista ou álbum (prefixo, sem acentos)."""
        toks = re.findall(r"[^\W_]+", text or "")
        if not toks:
            return []
        with self._lock:
            try:
                if self.fts:
                    q = " ".join('"%s"*' % t.replace('"', "") for t in toks)
                    rows = self.conn.execute(
                        "SELECT t.data FROM tracks_fts JOIN tracks t ON t.id = tracks_fts.rowid "
                        "WHERE tracks_fts MATCH ? ORDER BY bm25(tracks_fts, 4.0, 2.0, 1.0) LIMIT ?",
                        (q, limit)).fetchall()
                else:
                    where = " AND ".join(["unaccent(title || ' ' || artist || ' ' || album) LIKE ?"] * len(toks))
                    rows = self.conn.execute(
                        f"SELECT data FROM tracks WHERE {where} LIMIT ?",
                        [f"%{_unaccent_text(t).lower()}%" for t in toks] + [limit]).fetchall()
            except sqlite3.Error:
                return []
        return [d for d in (self._loads(r[0]) for r in rows) if d]

    def count_tracks(self):
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    # ---------- migração dos .json legados ----------
    @staticmethod
    def _read_json(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _migrate_legacy(self, files):
        if self._meta("legacy_migrated") == "1":
            return
        pls_raw = self._read_json(files["playlists"]) if files.get("playlists") else None
        fav_raw = self._read_json(files["favorites"]) if files.get("favorites") else None
        his_raw = self._read_json(files["history"]) if files.get("history") else None

        def clean(lst):
            return [normalize_track(t) for t in lst if isinstance(t, dict)] if isinstance(lst, list) else []

        playlists = ({str(k): clean(v) for k, v in pls_raw.items()} if isinstance(pls_raw, dict) else {})
        favorites, history = clean(fav_raw), clean(his_raw)
        if playlists:
            self._w_playlists(list(playlists), playlists)
        if favorites:
            self._w_favorites(favorites)
        if history:
            self._w_history(history)
        self._set_meta("legacy_migrated", "1")
        for path in files.values():          # só depois do commit: o legado vira .bak (reversível)
            if path and os.path.isfile(path):
                try:
                    os.replace(path, path + ".bak")
                except OSError:
                    pass

    # ---------- encerramento ----------
    def close(self):
        """Espera as gravações pendentes terminarem e fecha o banco."""
        if self._closed:
            return
        self._closed = True
        self._writer.shutdown(wait=True)
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            try:
                self.conn.close()
            except sqlite3.Error:
                pass


# ======================================================================
# Equalizador de 10 bandas (filtro de áudio do mpv)
# ======================================================================
EQ_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)   # Hz
EQ_RANGE_DB = 12.0
EQ_PRESETS = {
    "Flat":       (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "Rock":       (5, 4, 3, 1, -1, -1, 1, 3, 4, 5),
    "Pop":        (-1, 1, 3, 4, 3, 0, -1, -1, 1, 2),
    "Bass Boost": (8, 7, 5, 3, 1, 0, 0, 0, 0, 0),
    "Vocal":      (-3, -2, -1, 1, 3, 4, 4, 2, 0, -1),
    "Jazz":       (3, 2, 1, 2, -2, -2, 0, 1, 2, 3),
}


def clamp_eq(gains):
    """Lista de 10 ganhos em dB, limitada a ±12 (aceita lixo vindo do config.json)."""
    out = []
    for i in range(len(EQ_BANDS)):
        try:
            g = float(gains[i])
        except (TypeError, ValueError, IndexError):
            g = 0.0
        out.append(max(-EQ_RANGE_DB, min(EQ_RANGE_DB, g)))
    return out


def build_eq_filter(gains):
    """Valor da propriedade `af` do mpv. Vazio = sem filtro (zero custo de CPU).

    Nota: o mpv não tem um filtro 'equalizer=g1:...:g10'; o 'equalizer' do libavfilter é de UMA banda
    (f=freq:t=o:w=largura:g=ganho). Por isso a cadeia é montada com um filtro por banda dentro de
    `lavfi=[...]`. Há ainda um pré-ganho negativo igual ao maior reforço, para evitar clipping."""
    gains = clamp_eq(gains)
    parts = [f"equalizer=f={f}:t=o:w=1:g={g:.1f}" for f, g in zip(EQ_BANDS, gains) if abs(g) >= 0.05]
    if not parts:
        return ""
    peak = max(0.0, max(gains))
    if peak >= 0.05:
        parts.append(f"volume=volume=-{peak:.1f}dB")
    return "lavfi=[" + ",".join(parts) + "]"


# ======================================================================
# Radio-Browser (rádios web gratuitas)
# ======================================================================
WEBRADIO_SERVERS = (
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
)
WEBRADIO_COUNTRIES = (   # (rótulo, código ISO-3166; "" = mundo todo)
    ("Brasil", "BR"), ("Portugal", "PT"), ("Estados Unidos", "US"), ("Reino Unido", "GB"),
    ("Argentina", "AR"), ("México", "MX"), ("Espanha", "ES"), ("França", "FR"),
    ("Alemanha", "DE"), ("Itália", "IT"), ("Japão", "JP"), ("Mundo todo", ""),
)
WEBRADIO_TAGS = ("", "rock", "mpb", "notícias", "eletrônica", "pop", "jazz", "clássica",
              "sertanejo", "samba", "forró", "gospel", "hip hop", "reggae", "lofi")


def webradio_search(name="", country_code="", tag="", limit=80, timeout=8):
    """Consulta a API do Radio-Browser (com revezamento entre servidores espelho). Bloqueante: use em thread."""
    params = {"limit": str(limit), "hidebroken": "true", "order": "votes", "reverse": "true"}
    if name.strip():
        params["name"] = name.strip()
    if country_code:
        params["countrycode"] = country_code
    if tag.strip():
        params["tag"] = tag.strip()
    qs = urllib.parse.urlencode(params)
    last_err = None
    for base in WEBRADIO_SERVERS:
        try:
            req = urllib.request.Request(f"{base}/json/stations/search?{qs}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict)]
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Radio-Browser indisponível ({last_err})")


def webradio_to_track(st):
    """Estação do Radio-Browser -> faixa do app (toca como qualquer outra, via fila)."""
    url = (st.get("url_resolved") or st.get("url") or "").strip()
    uuid = (st.get("stationuuid") or "").strip()
    if not url or not uuid:
        return None
    tags = ", ".join([t for t in (st.get("tags") or "").split(",") if t][:4])
    country = st.get("country") or st.get("countrycode") or ""
    return normalize_track({
        "id": f"webradio:{uuid}", "title": (st.get("name") or "Rádio").strip() or "Rádio",
        "uploader": " · ".join(x for x in ("Rádio", country) if x),
        "duration": "AO VIVO", "stream_url": url, "webradio": True,
        "favicon": (st.get("favicon") or "").strip(), "country": country, "tags": tags,
    })


def webradio_register_click(uuid):
    """Boa prática da API: avisa que a estação foi ouvida (ajuda o ranking). Falha em silêncio."""
    for base in WEBRADIO_SERVERS[:2]:
        try:
            req = urllib.request.Request(f"{base}/json/url/{urllib.parse.quote(uuid)}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=6):
                return
        except Exception:
            continue


# ======================================================================
# Pré-carregamento (gapless): resolve a URL direta da próxima faixa com o yt-dlp
# ======================================================================
PRELOAD_MAX_S = 15      # começa a resolver quando faltam <= 15 s ...
PRELOAD_MIN_S = 10      # ... (janela ideal 10-15 s); se o usuário pular para o fim, ainda tenta até 4 s
PRELOAD_LATE_S = 4


def resolve_stream_url(video_url, timeout=30):
    """URL direta do áudio (yt-dlp -g). Bloqueante: use em thread. Devolve None se falhar."""
    try:
        out = subprocess.run(
            ["yt-dlp", "-g", "-f", "bestaudio/best", "--no-playlist", "--no-warnings",
             "--socket-timeout", "10", video_url],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    return None


class MPVController:
    """Controla o processo mpv em modo daemon através de Unix Socket IPC."""

    def __init__(self, callbacks):
        self.proc = None
        self.sock = None
        self.callbacks = callbacks
        self._running = False
        self._listener_thread = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._last_volume = 100
        self._af = ""            # filtro de áudio atual (equalizador); reaplicado se o mpv reiniciar

    def _cleanup(self):
        """Encerra socket e processo (usado ao reiniciar e ao sair)."""
        self._running = False
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            os.remove(MPV_SOCKET)
        except OSError:
            pass

    def alive(self):
        return bool(self._running and self.sock and self.proc and self.proc.poll() is None)

    def restart(self):
        """Reinicia o mpv se ele morreu. Devolve True se voltou a funcionar."""
        self._cleanup()
        return self.start(self._last_volume)

    def start(self, initial_volume=100):
        self._last_volume = initial_volume
        if os.path.exists(MPV_SOCKET):
            try:
                os.remove(MPV_SOCKET)
            except OSError:
                pass

        try:
            log_file = open(MPV_LOG, "w")
        except OSError:
            log_file = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                [
                    "mpv",
                    "--idle=yes",
                    "--no-video",
                    "--no-terminal",
                    "--ytdl=yes",
                    "--ytdl-format=bestaudio/best",
                    # googlevideo: URLs diretas já resolvidas no pré-carregamento (gapless) não passam de novo pelo yt-dlp.
                    # Padrão Lua sem '%' (no mpv, '%' em --script-opts é prefixo de comprimento e quebraria a opção).
                    "--script-opts=ytdl_hook-ytdl_path=yt-dlp,ytdl_hook-exclude=googlevideo[.]com",
                    "--network-timeout=20",      # rede travada vira erro (o watchdog tenta de novo) em vez de pendurar
                    "--audio-display=no",
                    f"--input-ipc-server={MPV_SOCKET}",
                    f"--volume={initial_volume}",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except (FileNotFoundError, OSError):
            return False
        finally:
            if log_file is not subprocess.DEVNULL:
                log_file.close()  # o processo filho já tem a própria cópia do descritor

        for _ in range(50):
            if os.path.exists(MPV_SOCKET):
                break
            time.sleep(0.1)
        else:
            return False

        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(MPV_SOCKET)
        except OSError:
            self.sock = None
            return False

        self._running = True
        self._listener_thread = threading.Thread(target=self._listen, daemon=True)
        self._listener_thread.start()

        # Áudio não precisa dos ~150 MiB de cache padrão do demuxer: reduz a RAM do mpv em PCs fracos.
        # Via IPC (e não como opção de linha de comando) para não derrubar mpv antigo que não conheça a opção.
        self._send(["set_property", "demuxer-max-bytes", "24MiB"])
        self._send(["set_property", "demuxer-max-back-bytes", "4MiB"])
        self._send(["observe_property", 1, "time-pos"])
        self._send(["observe_property", 2, "duration"])
        self._send(["observe_property", 3, "pause"])
        self._send(["observe_property", 4, "idle-active"])
        self._send(["observe_property", 6, "metadata"])         # rádios web: título da música (ICY)
        # Encadeamento sem pausa entre as entradas da playlist interna do mpv (ignorado por mpv antigo).
        self._send(["set_property", "gapless-audio", "yes"])
        self._send(["set_property", "prefetch-playlist", "yes"])
        if self._af:
            self._send(["set_property", "af", self._af])
        return True

    def _listen(self):
        buf = b""
        last_pos = -1.0
        sock = self.sock
        while self._running and sock:
            try:
                data = sock.recv(8192)
            except OSError:
                break
            if not data:
                break
            buf += data
            if len(buf) > 4_000_000:   # linha absurda sem quebra: descarta em vez de crescer sem limite
                buf = b""
            if b"\n" not in buf:
                continue
            lines = buf.split(b"\n")
            buf = lines.pop()          # resto (linha incompleta) fica para a próxima leitura
            for line in lines:
                if not line.strip():
                    continue
                # Caminho rápido: o mpv manda a posição dezenas de vezes por segundo. A interface só
                # usa ~4/s, então essas linhas nem passam pelo json.loads quando serão descartadas.
                if b'"name":"time-pos"' in line and b'"event":"property-change"' in line:
                    m = _TIMEPOS_RE.search(line)
                    pos = float(m.group(1)) if m else 0.0
                    if "on_time_change" in self.callbacks and (pos < last_pos or pos - last_pos >= 0.25):
                        last_pos = pos
                        GLib.idle_add(self.callbacks["on_time_change"], pos)
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    continue

                if msg.get("event") == "end-file" and msg.get("reason") == "eof":
                    if "on_track_end" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_track_end"])

                elif msg.get("event") == "property-change":
                    name = msg.get("name")
                    val = msg.get("data")
                    if name == "time-pos" and "on_time_change" in self.callbacks:
                        pos = val or 0
                        # O mpv avisa a posição muitas vezes por segundo; a interface só precisa de ~4 atualizações/s.
                        if pos < last_pos or pos - last_pos >= 0.25:
                            last_pos = pos
                            GLib.idle_add(self.callbacks["on_time_change"], pos)
                    elif name == "duration" and "on_duration_change" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_duration_change"], val or 0)
                    elif name == "pause" and "on_pause_change" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_pause_change"], val)
                    elif name == "idle-active" and "on_idle_change" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_idle_change"], bool(val))
                    elif name == "metadata" and "on_metadata" in self.callbacks and isinstance(val, dict):
                        GLib.idle_add(self.callbacks["on_metadata"], val)

                if "request_id" in msg and "error" in msg:
                    if msg["error"] != "success" and "on_load_error" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_load_error"], msg["error"])

        # Saiu do laço sem ter sido pedido: o mpv morreu/fechou o socket. Avisa a interface.
        if self._running and sock is self.sock and "on_died" in self.callbacks:
            GLib.idle_add(self.callbacks["on_died"])

    def _send(self, command, request_id=None):
        if not self.sock:
            return
        payload = {"command": command}
        if request_id is not None:
            payload["request_id"] = request_id
        data = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except (OSError, AttributeError):
            pass

    def load(self, url):
        with self._lock:
            self._request_id += 1
            rid = self._request_id
        self._send(["set_property", "pause", False])  # escolher uma faixa sempre toca (mesmo se estava pausado)
        self._send(["loadfile", url, "replace"], request_id=rid)

    def append(self, url):
        """Gapless: enfileira a próxima faixa na playlist interna do mpv (toca assim que a atual acabar)."""
        self._send(["loadfile", url, "append"])

    def playlist_clear(self):
        """Remove as entradas pendentes (pré-carregadas), mantendo a faixa que está tocando."""
        self._send(["playlist-clear"])

    def set_af(self, af):
        """Aplica (ou, com string vazia, remove) o filtro de áudio via IPC: set_property af."""
        self._af = af or ""
        self._send(["set_property", "af", self._af])

    def pause_toggle(self):
        self._send(["cycle", "pause"])

    def stop(self):
        self._send(["stop"])

    def seek(self, position):
        self._send(["seek", position, "absolute"])

    def seek_relative(self, seconds):
        self._send(["seek", seconds, "relative"])

    def set_volume(self, vol):
        self._last_volume = vol
        self._send(["set_property", "volume", vol])

    def set_mute(self, muted):
        self._send(["set_property", "mute", bool(muted)])

    def quit(self):
        self._running = False
        self._send(["quit"])
        self._cleanup()


# ----------------------------------------------------------------------
# MPRIS2 (D-Bus): faz o Fonoteca aparecer nos controles de mídia do sistema
# (applet de som/PulseAudio, GNOME, KDE, teclas multimídia, playerctl...)
# ----------------------------------------------------------------------
def _desktop_quote(arg):
    """Quoting de argumento do campo Exec= (Desktop Entry Spec)."""
    esc = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return f'"{esc}"'


def _ensure_desktop_entry():
    """Garante um fonoteca.desktop visível pelo sistema (applets de som/mídia usam o
    DesktopEntry do MPRIS para achar nome e ícone do player). Não mexe em arquivo que
    o usuário ou um pacote/instalador já tenha criado; só mantém o que ele mesmo gerou."""
    try:
        marker = "X-Fonoteca-Autogenerated=true"
        script = os.path.abspath(__file__)
        exec_line = f"{_desktop_quote(sys.executable or 'python3')} {_desktop_quote(script)}"
        target = os.path.join(GLib.get_user_data_dir(), "applications", f"{APP_ID}.desktop")
        try:
            existing = Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")   # PyGObject levanta TypeError se não existir
        except TypeError:
            existing = None
        if existing is not None:
            try:
                with open(target, encoding="utf-8") as fh:
                    mine = marker in fh.read()
            except OSError:
                mine = False
            if not mine:
                return                      # entrada de outra origem: respeita
            if existing.get_commandline() and script in existing.get_commandline():
                return                      # a nossa, já correta
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Comment={APP_TAGLINE}\n"
            f"Exec={exec_line}\n"
            f"Icon={_find_icon_file() or APP_ID}\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Audio;Player;GTK;\n"
            f"StartupWMClass={APP_ID}\n"
            f"{marker}\n"
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except Exception:
        pass


_MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_MPRIS_PATH = "/org/mpris/MediaPlayer2"
_MPRIS_ROOT = "org.mpris.MediaPlayer2"
_MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
_MPRIS_NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"


class MprisService:
    """Expõe o player via MPRIS2 usando só Gio (sem dependências extras).

    Tudo roda no loop principal do GTK, então chamar os métodos do app é seguro.
    """

    def __init__(self, app):
        self.app = app
        self.conn = None
        self._reg_ids = []
        self._owner_id = 0
        self._art_url = ""
        self._art_key = None
        self._art_toggle = 0
        self._fallback_tried = False
        _ensure_desktop_entry()
        try:
            self._info = Gio.DBusNodeInfo.new_for_xml(_MPRIS_XML)
            self._own(f"org.mpris.MediaPlayer2.{APP_ID}")
        except Exception:
            self._owner_id = 0  # sem D-Bus de sessão: o player segue funcionando normalmente

    def _own(self, name):
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, name, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
            self._on_bus_acquired, None, self._on_name_lost)

    def _on_name_lost(self, connection, name):
        """Outra instância já tem o nome padrão: usa o nome por instância (padrão MPRIS)."""
        if connection is None or self._fallback_tried or getattr(self.app, "_closing", False):
            return
        self._fallback_tried = True
        try:
            self._own(f"org.mpris.MediaPlayer2.{APP_ID}.instance{os.getpid()}")
        except Exception:
            pass

    # ---------- registro no barramento ----------
    def _on_bus_acquired(self, connection, name):
        self.conn = connection
        if self._reg_ids:
            return
        try:
            for iface in self._info.interfaces:
                rid = connection.register_object(
                    _MPRIS_PATH, iface, self._on_method_call, self._on_get_property, self._on_set_property)
                self._reg_ids.append(rid)
        except Exception:
            pass

    def close(self):
        try:
            if self.conn is not None:
                for rid in self._reg_ids:
                    self.conn.unregister_object(rid)
            if self._owner_id:
                Gio.bus_unown_name(self._owner_id)
        except Exception:
            pass
        self._reg_ids = []
        self._owner_id = 0
        self.conn = None
        for n in (0, 1):
            try:
                os.remove(os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpris_art_{os.getpid()}_{n}.jpg"))
            except OSError:
                pass

    # ---------- estado lido do app ----------
    def _item(self):
        a = self.app
        if 0 <= a.current_index < len(a.queue):
            return a.queue[a.current_index]
        return None

    def _status(self):
        a = self.app
        if a._mpv_idle:
            return "Stopped"
        return "Paused" if a._mpv_paused else "Playing"

    def _loop(self):
        return "Track" if self.app.is_repeat else "None"

    def _volume(self):
        a = self.app
        if a.is_muted:
            return 0.0
        return max(0.0, min(1.0, a.vol_scale.get_value() / 100.0))

    def _position_us(self):
        return int(max(0.0, float(getattr(self.app, "_mpris_pos", 0) or 0)) * 1_000_000)

    def _can_next(self):
        a = self.app
        if not a.queue:
            return False
        return (a.is_shuffle and len(a.queue) > 1) or a.current_index + 1 < len(a.queue) or a._radio is not None

    def _can_prev(self):
        return self.app.current_index > 0

    def _metadata(self):
        item = self._item()
        if item is None:
            return {"mpris:trackid": GLib.Variant("o", _MPRIS_NO_TRACK)}
        a = self.app
        md = {"mpris:trackid": GLib.Variant("o", f"/org/{APP_ID}/track/{max(a._play_token, 0)}")}
        dur = a.track_duration
        if not dur:
            dur = _clock_to_seconds(item.get("duration", "")) if item.get("duration") else 0
        if dur:
            md["mpris:length"] = GLib.Variant("x", int(float(dur) * 1_000_000))
        md["xesam:title"] = GLib.Variant("s", str(item.get("title", "")))
        artist = a._guess_artist(item)
        if artist:
            md["xesam:artist"] = GLib.Variant("as", [artist])
        if item.get("album"):
            md["xesam:album"] = GLib.Variant("s", str(item["album"]))
        if self._art_url and self._art_key == (item.get("id") or item.get("path")):
            md["mpris:artUrl"] = GLib.Variant("s", self._art_url)
        return md

    def _prop(self, iface, name):
        """Devolve o GLib.Variant da propriedade ou None se não existir."""
        if iface == _MPRIS_ROOT:
            table = {
                "CanQuit": ("b", False), "CanRaise": ("b", True), "HasTrackList": ("b", False),
                "Identity": ("s", APP_NAME), "DesktopEntry": ("s", APP_ID),
                "SupportedUriSchemes": ("as", []), "SupportedMimeTypes": ("as", []),
            }
            if name in table:
                t, v = table[name]
                return GLib.Variant(t, v)
            return None
        if iface == _MPRIS_PLAYER:
            if name == "Metadata":
                return GLib.Variant("a{sv}", self._metadata())
            table = {
                "PlaybackStatus": lambda: ("s", self._status()),
                "LoopStatus": lambda: ("s", self._loop()),
                "Rate": lambda: ("d", 1.0),
                "Shuffle": lambda: ("b", bool(self.app.is_shuffle)),
                "Volume": lambda: ("d", self._volume()),
                "Position": lambda: ("x", self._position_us()),
                "MinimumRate": lambda: ("d", 1.0),
                "MaximumRate": lambda: ("d", 1.0),
                "CanGoNext": lambda: ("b", self._can_next()),
                "CanGoPrevious": lambda: ("b", self._can_prev()),
                "CanPlay": lambda: ("b", True),
                "CanPause": lambda: ("b", True),
                "CanSeek": lambda: ("b", not self.app._mpv_idle),
                "CanControl": lambda: ("b", True),
            }
            if name in table:
                t, v = table[name]()
                return GLib.Variant(t, v)
        return None

    # ---------- callbacks do D-Bus ----------
    def _on_get_property(self, connection, sender, path, iface, name):
        try:
            return self._prop(iface, name)
        except Exception:
            return None

    def _on_set_property(self, connection, sender, path, iface, name, value):
        a = self.app
        try:
            if iface != _MPRIS_PLAYER:
                return False
            if name == "LoopStatus":
                a.btn_repeat.set_active(value.get_string() == "Track")
                return True
            if name == "Shuffle":
                a.btn_shuffle.set_active(bool(value.get_boolean()))
                return True
            if name == "Volume":
                a.vol_scale.set_value(max(0.0, min(1.0, value.get_double())) * 100.0)
                return True
            if name == "Rate":
                return True
        except Exception:
            pass
        return False

    def _on_method_call(self, connection, sender, path, iface, method, params, invocation):
        a = self.app
        try:
            if iface == _MPRIS_ROOT:
                if method == "Raise":
                    a.present()
            elif iface == _MPRIS_PLAYER:
                if method == "Next":
                    a.on_next(None)
                elif method == "Previous":
                    a.on_prev(None)
                elif method == "PlayPause":
                    a.toggle_playback()
                elif method == "Play":
                    if a._mpv_idle or a._mpv_paused:
                        a.toggle_playback()
                elif method == "Pause":
                    if not a._mpv_idle and not a._mpv_paused:
                        a.toggle_playback()
                elif method == "Stop":
                    a.mpv.stop()
                    a._set_mpv_idle(True)
                elif method == "Seek":
                    off = params.unpack()[0]
                    a.mpv.seek_relative(off / 1_000_000.0)
                    GLib.timeout_add(150, self._emit_seeked_later)
                elif method == "SetPosition":
                    tid, pos = params.unpack()
                    if tid == f"/org/{APP_ID}/track/{a._play_token}":
                        a.mpv.seek(pos / 1_000_000.0)
                        a._mpris_pos = pos / 1_000_000.0
                        self.seeked(a._mpris_pos)
                # OpenUri: ignorado de propósito
            invocation.return_value(None)
        except Exception as e:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))

    def _emit_seeked_later(self):
        self.seeked(getattr(self.app, "_mpris_pos", 0))
        return False

    # ---------- avisos para o sistema ----------
    def notify(self, *names):
        """Emite PropertiesChanged para as propriedades do Player listadas."""
        if self.conn is None or not names:
            return
        try:
            changed = {}
            for n in names:
                v = self._prop(_MPRIS_PLAYER, n)
                if v is not None:
                    changed[n] = v
            if changed:
                self.conn.emit_signal(
                    None, _MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (_MPRIS_PLAYER, changed, [])))
        except Exception:
            pass

    def seeked(self, seconds):
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(
                None, _MPRIS_PATH, _MPRIS_PLAYER, "Seeked",
                GLib.Variant("(x)", (int(max(0.0, float(seconds or 0)) * 1_000_000),)))
        except Exception:
            pass

    def set_art(self, src_path, key):
        """Copia a capa para um arquivo alternado (a URL muda a cada faixa) e atualiza o Metadata."""
        try:
            if not src_path or not os.path.isfile(src_path):
                return
            self._art_toggle ^= 1
            dst = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpris_art_{os.getpid()}_{self._art_toggle}.jpg")
            shutil.copyfile(src_path, dst)
            self._art_url = "file://" + urllib.parse.quote(dst)
            self._art_key = key
        except Exception:
            return
        self.notify("Metadata")


# ======================================================================
# Diálogo "Equalizador e Áudio": 10 bandas (Gtk.Scale vertical) + presets + gapless
# ======================================================================
class AudioDialog(Gtk.Window):
    """Janela não modal. Fechar apenas a esconde (o estado fica no app e em config.json)."""

    def __init__(self, app):
        super().__init__(title="Equalizador e Áudio")
        self.app = app
        self.set_transient_for(app)
        self.set_destroy_with_parent(True)
        self.set_default_size(680, 430)
        self.set_border_width(14)
        self._updating = False
        self._apply_id = None
        self.scales, self.val_labels = [], []
        self.connect("delete-event", lambda w, e: (w.hide(), True)[1])

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(root)

        # Linha superior: liga/desliga + nome do preset atual
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.sw = Gtk.Switch()
        self.sw.set_valign(Gtk.Align.CENTER)
        self.sw.set_active(app.eq_enabled)
        self.sw.connect("notify::active", self._on_switch)
        top.pack_start(self.sw, False, False, 0)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="large" weight="bold">Equalizador de 10 bandas</span>')
        top.pack_start(title, False, False, 0)
        self.preset_lbl = Gtk.Label(xalign=1)
        self.preset_lbl.get_style_context().add_class("dim-label")
        top.pack_end(self.preset_lbl, False, False, 0)
        root.pack_start(top, False, False, 0)

        # Presets
        presets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        presets.get_style_context().add_class("linked")
        presets.set_halign(Gtk.Align.START)
        for name in EQ_PRESETS:
            b = Gtk.Button(label=name)
            b.connect("clicked", self._on_preset, name)
            presets.pack_start(b, False, False, 0)
        root.pack_start(presets, False, False, 0)

        # Bandas
        bands = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bands.set_homogeneous(True)
        for i, (freq, gain) in enumerate(zip(EQ_BANDS, app.eq_gains)):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            val = Gtk.Label(label=self._fmt_db(gain))
            val.set_width_chars(6)
            val.get_style_context().add_class("dim-label")
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -EQ_RANGE_DB, EQ_RANGE_DB, 0.5)
            sc.set_inverted(True)                 # +12 dB em cima, -12 dB embaixo
            sc.set_draw_value(False)
            sc.set_has_origin(False)              # sem preenchimento 'a partir do fundo': o EQ é centrado em 0 dB
            sc.set_size_request(-1, 190)
            sc.set_vexpand(True)
            sc.add_mark(0.0, Gtk.PositionType.RIGHT, None)
            sc.set_value(gain)
            sc.connect("value-changed", self._on_band, i)
            sc.connect("button-press-event", self._on_band_press, i)
            sc.set_tooltip_text(f"{self._fmt_freq(freq)} - duplo clique zera esta banda")
            lbl = Gtk.Label(label=self._fmt_freq(freq))
            col.pack_start(val, False, False, 0)
            col.pack_start(sc, True, True, 0)
            col.pack_start(lbl, False, False, 0)
            bands.pack_start(col, True, True, 0)
            self.scales.append(sc)
            self.val_labels.append(val)
        root.pack_start(bands, True, True, 0)

        root.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        self.chk_gapless = Gtk.CheckButton(label="Reprodução sem pausa (gapless): pré-carrega a próxima faixa nos últimos segundos")
        self.chk_gapless.set_active(app.gapless_enabled)
        self.chk_gapless.connect("toggled", lambda b: app.set_gapless(b.get_active()))
        root.pack_start(self.chk_gapless, False, False, 0)
        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_text("Faixa de ±12 dB por banda. Com todas as bandas em 0 dB o filtro é removido (zero custo de CPU). "
                      "Ao reforçar bandas, um pré-ganho automático evita distorção por clipping.")
        hint.get_style_context().add_class("dim-label")
        root.pack_start(hint, False, False, 0)
        self._refresh_preset_label()

    @staticmethod
    def _fmt_db(g):
        return f"{g:+.1f} dB" if abs(g) >= 0.05 else "0 dB"

    @staticmethod
    def _fmt_freq(f):
        return f"{f / 1000:g} kHz" if f >= 1000 else f"{f} Hz"

    def _refresh_preset_label(self):
        name = "Personalizado"
        for n, g in EQ_PRESETS.items():
            if all(abs(a - b) < 0.01 for a, b in zip(self.app.eq_gains, g)):
                name = n
                break
        self.app.eq_preset = name
        self.preset_lbl.set_text(f"Preset: {name}")

    def _set_switch(self, on):
        self._updating = True
        self.sw.set_active(on)
        self._updating = False
        self.app.eq_enabled = on

    def _on_switch(self, sw, _pspec):
        if self._updating:
            return
        self.app.eq_enabled = sw.get_active()
        self.app.apply_eq()

    def _on_band(self, scale, i):
        if self._updating:
            return
        g = scale.get_value()
        self.val_labels[i].set_text(self._fmt_db(g))
        self.app.eq_gains[i] = g
        if not self.sw.get_active():
            self._set_switch(True)
        self._refresh_preset_label()
        if self._apply_id is None:                         # debounce: arrastar o slider não inunda o IPC
            self._apply_id = GLib.timeout_add(60, self._do_apply)

    def _on_band_press(self, scale, event, i):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            scale.set_value(0.0)
            return True
        return False

    def _do_apply(self):
        self._apply_id = None
        self.app.apply_eq()
        return False

    def _on_preset(self, btn, name):
        gains = clamp_eq(EQ_PRESETS[name])
        self._updating = True
        for i, g in enumerate(gains):
            self.scales[i].set_value(g)
            self.val_labels[i].set_text(self._fmt_db(g))
        self._updating = False
        self.app.eq_gains = gains
        self._set_switch(name != "Flat" or self.sw.get_active())
        self._refresh_preset_label()
        self.app.apply_eq()


class MusicPlayerApp(Gtk.Window):
    # Estado da renderização em lotes (listas grandes são montadas aos poucos, só quando visíveis)
    _queue_gen = 0
    _queue_dirty = False
    _queue_building = False
    _hl_idx = -1
    _fav_gen = 0
    _fav_dirty = False
    _lib_gen = 0
    db = None            # LibraryDB (SQLite); None = modo de contingência com arquivos JSON
    CHUNK_FIRST = 40     # linhas criadas de imediato (a lista aparece na hora)
    CHUNK_STEP = 30      # linhas por passada ociosa (mantém a interface fluida)

    def __init__(self):
        super().__init__(title=APP_NAME)

        # Verificação de dependências do sistema antes da inicialização total
        self._check_dependencies()

        icon_path = _find_icon_file()
        if icon_path:
            self.set_icon_from_file(icon_path)

        self.config = self._load_json(CONFIG_FILE, {
            "volume": 100,
            "width": 540,
            "height": 740,
        })
        if not isinstance(self.config, dict):
            self.config = {"volume": 100}
        try:
            self.config["volume"] = max(0, min(100, int(self.config.get("volume", 100))))
        except (TypeError, ValueError):
            self.config["volume"] = 100

        self.set_default_size(self.config.get("win_w", 1040), self.config.get("win_h", 720))
        self.set_position(Gtk.WindowPosition.CENTER)
        self.maximize()  # sempre abre maximizado (não é tela cheia); o tamanho acima vale ao restaurar

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        self.set_titlebar(header)

        # Estado da aplicação
        self._queue_save_id = None
        self.queue = self._clean_track_list(self._load_json(QUEUE_FILE, []))
        self._open_database()   # abre library.db (migra os .json legados na 1ª vez); None => contingência em JSON
        self.history = self._clean_track_list(self._db_load("history"))[:HISTORY_LIMIT]
        _pls = self._db_load("playlists")
        self.playlists = ({str(k): self._clean_track_list(v) for k, v in _pls.items()}
                          if isinstance(_pls, dict) else {})
        self.favorites = [self._normalize_track(t) for t in self._db_load("favorites") if isinstance(t, dict)]
        self._hearts = weakref.WeakSet()
        # Biblioteca local (Músicas Offline)
        _lib = self._load_json(LIBRARY_FILE, {})
        self.lib_index = _lib.get("tracks", {}) if isinstance(_lib, dict) else {}
        self.lib_root = self.config.get("library_root", "")
        self.lib_dir = self.lib_root
        self._lib_album_cache = {}
        self._lib_enriching = False
        self._lib_scan_token = 0
        self._lib_scanning = False
        self._nav_history = []
        self._panel_lock = False
        self._wiki_forced = None
        self.album_artist_name = ""
        self._art_cache = {}
        self._art_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="art")  # capas: no máx. 4 downloads simultâneos
        self._dl_procs = set()
        self._pl_busy = False
        self._busy_pulse_id = None
        self._busy_job = None
        self.profile = self._load_json(PROFILE_FILE, {"name": ""})
        if not isinstance(self.profile, dict):
            self.profile = {"name": ""}
        self._welcome_timeout_id = None
        self.selected_playlist = None
        self.results = []
        self.current_index = -1
        self.is_repeat = False
        self.is_shuffle = False
        # Rádio (fila infinita com músicas parecidas); None = desligada
        self._radio = None
        self._radio_busy = False
        self._radio_token = 0
        self._radio_pending_next = False
        self._radio_ended = False
        self._radio_ui_lock = False
        self.track_duration = 0
        self.user_is_seeking = False
        # Estado do play/pause: ao abrir não há nada tocando (mpv ocioso)
        self._mpv_idle = True
        self._mpv_paused = False
        
        # Gerenciamento de timeouts e cancelamento de buscas
        self._msg_timeout_id = None
        self._msg_fade_id = None
        self.search_token = 0
        self.wiki_token = 0
        self._chart_token = 0           # descarta respostas antigas do "Em alta" ao trocar de país

        self.album_token = 0
        self._album_cache = {}
        self.wiki_artist_name = ""

        # Artista da faixa tocando (usado pela Wiki)
        self.now_artist = ""
        self._artist_stale = {"wiki": False}
        self._loaded_key = {"wiki": ""}

        # Volume / mute
        self.is_muted = False
        self._last_volume = max(int(self.config.get("volume", 100) or 100), 1)
        self._config_save_id = None
        self._queue_save_id = None
        self._closing = False
        self._mute_icon = ""

        # Filtro da lista de playlists
        self._pl_filter_key = ""

        # Watchdog de reprodução
        self._play_token = 0
        self._missing_skips = 0
        self._track_started = False
        self._stall_retry_count = 0
        self._stall_check_id = None

        # Gapless / pré-carregamento da próxima faixa
        self.gapless_enabled = bool(self.config.get("gapless", True))
        self._preload = None             # {"token","idx","key"}: próxima faixa já enfileirada no mpv
        self._preload_fired = None       # token da faixa para a qual o pré-carregamento já foi disparado
        self._preload_gen = 0
        self._gapless_guard = None
        self._preload_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preload")

        # Equalizador
        _eq = self.config.get("eq") if isinstance(self.config.get("eq"), dict) else {}
        self.eq_enabled = bool(_eq.get("enabled", False))
        self.eq_gains = clamp_eq(_eq.get("gains") or [])
        self.eq_preset = str(_eq.get("preset") or "Flat")
        self._audio_dialog = None

        # Rádios e busca na biblioteca (rede/banco sempre em threads)
        self._net_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="net")
        self.webradio_token = 0
        self._webradio_loaded = False
        self.mylib_token = 0
        self._mylib_timer = None

        # Container Principal
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        # Corpo: barra lateral | conteúdo navegável | painel direito (Fila/Letra)
        overlay = Gtk.Overlay()
        self._body_widget = self._build_body()
        self._wire_selection_tracking()
        overlay.add(self._body_widget)
        overlay.add_overlay(self._build_welcome_overlay())
        overlay.add_overlay(self._build_busy_overlay())
        vbox.pack_start(overlay, True, True, 0)

        vbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        # Painel Inferior de Reprodução
        player_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        player_panel.set_border_width(10)

        seek_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.seek_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.seek_scale.set_draw_value(False)
        self.seek_scale.connect("button-press-event", self.on_seek_start)
        self.seek_scale.connect("button-release-event", self.on_seek_finish)
        seek_row.pack_start(self.seek_scale, True, True, 0)

        # Volume: botão Mute (ícone muda conforme o nível) + slider compacto
        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.btn_mute = Gtk.Button()
        self.btn_mute.set_relief(Gtk.ReliefStyle.NONE)
        self.mute_img = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic", Gtk.IconSize.BUTTON)
        self.btn_mute.set_image(self.mute_img)
        self.btn_mute.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.btn_mute.connect("clicked", self.on_toggle_mute)
        self.btn_mute.connect("scroll-event", self.on_volume_scroll)
        vol_box.pack_start(self.btn_mute, False, False, 0)

        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.set_increments(5, 10)
        self.vol_scale.set_size_request(90, -1)
        self.vol_scale.set_value(self.config.get("volume", 100))
        self.vol_scale.connect("value-changed", self.on_volume_changed)
        vol_box.pack_start(self.vol_scale, False, False, 0)

        seek_row.pack_start(vol_box, False, False, 0)
        player_panel.pack_start(seek_row, False, False, 0)

        now_playing_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        self.thumbnail_img = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.thumbnail_img.set_pixel_size(48)
        now_playing_box.pack_start(self.thumbnail_img, False, False, 0)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.now_playing_label = Gtk.Label(label="Nenhuma música em execução", xalign=0)
        self.now_playing_label.set_ellipsize(3)
        self.time_label = Gtk.Label(label="0:00 / 0:00", xalign=0)
        self.time_label.get_style_context().add_class("dim-label")
        # Botão do artista: com moldura e ícones (pessoa + seta) para deixar claro que é clicável
        self.now_artist_btn = Gtk.Button()
        self.now_artist_lbl = Gtk.Label(label="", xalign=0)
        self.now_artist_lbl.set_ellipsize(3)
        artist_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        artist_box.pack_start(Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.MENU), False, False, 0)
        artist_box.pack_start(self.now_artist_lbl, True, True, 0)
        artist_box.pack_start(Gtk.Image.new_from_icon_name("go-next-symbolic", Gtk.IconSize.MENU), False, False, 0)
        self.now_artist_btn.add(artist_box)
        self.now_artist_btn.set_relief(Gtk.ReliefStyle.NORMAL)
        self.now_artist_btn.set_halign(Gtk.Align.START)
        self.now_artist_btn.set_focus_on_click(False)
        self.now_artist_btn.set_sensitive(False)
        self.now_artist_btn.set_opacity(0.0)   # invisível até haver artista (mantém a altura da barra)
        self.now_artist_btn.set_tooltip_text("Abrir a página do artista")
        self.now_artist_btn.connect("clicked", self.on_open_now_artist)
        info_box.pack_start(self.now_playing_label, True, True, 0)
        info_box.pack_start(self.now_artist_btn, False, False, 0)
        info_box.pack_start(self.time_label, False, False, 0)
        now_playing_box.pack_start(info_box, True, True, 0)

        controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        controls_box.set_valign(Gtk.Align.CENTER)

        btn_paste = Gtk.Button.new_from_icon_name("edit-paste-symbolic", Gtk.IconSize.BUTTON)
        btn_paste.set_tooltip_text("Colar link e tocar (Ctrl+V)")
        btn_paste.connect("clicked", self.on_paste_link)

        self.btn_shuffle = Gtk.ToggleButton()
        self.btn_shuffle.set_image(Gtk.Image.new_from_icon_name("media-playlist-shuffle-symbolic", Gtk.IconSize.BUTTON))
        self.btn_shuffle.set_tooltip_text("Embaralhar")
        self.btn_shuffle.connect("toggled", self.on_toggle_shuffle)

        btn_prev = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic", Gtk.IconSize.BUTTON)
        btn_prev.set_tooltip_text("Anterior (Ctrl+←)")
        btn_prev.connect("clicked", self.on_prev)

        self.btn_playpause = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.btn_playpause.get_style_context().add_class("suggested-action")
        self.btn_playpause.get_style_context().add_class("circular")
        self.btn_playpause.set_tooltip_text("Play/Pause (Espaço)")
        self.btn_playpause.connect("clicked", self.on_play_pause)

        btn_next = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic", Gtk.IconSize.BUTTON)
        btn_next.set_tooltip_text("Próxima (Ctrl+→)")
        btn_next.connect("clicked", self.on_next)

        self.btn_repeat = Gtk.ToggleButton()
        self.btn_repeat.set_image(Gtk.Image.new_from_icon_name("media-playlist-repeat-symbolic", Gtk.IconSize.BUTTON))
        self.btn_repeat.set_tooltip_text("Repetir Fila")
        self.btn_repeat.connect("toggled", self.on_toggle_repeat)

        self.btn_radio = Gtk.ToggleButton(label="Mix")
        self.btn_radio.set_tooltip_text("Mix: continua tocando músicas parecidas com a faixa atual")
        self.btn_radio.connect("toggled", self.on_toggle_radio)

        btn_download_ctrl = Gtk.Button.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON)
        btn_download_ctrl.set_tooltip_text("Baixar seleção em MP3")
        btn_download_ctrl.connect("clicked", self.on_download)

        self.btn_like = self._make_heart(None)
        self.btn_like.set_relief(Gtk.ReliefStyle.NORMAL)   # moldura como os demais botões do player (nas listas segue sem moldura)
        self.btn_eq = Gtk.Button(label="EQ")
        self.btn_eq.connect("clicked", self.on_open_audio)
        self.btn_queue_toggle = Gtk.ToggleButton(label="Fila")
        self.btn_queue_toggle.set_tooltip_text("Fila de reprodução")
        self.btn_queue_toggle.connect("toggled", self._on_panel_toggle, "queue")
        self.btn_lyrics_toggle = Gtk.ToggleButton(label="Letra")
        self.btn_lyrics_toggle.set_tooltip_text("Letra da música")
        self.btn_lyrics_toggle.connect("toggled", self._on_panel_toggle, "lyrics")
        for w in (self.btn_like, btn_paste, self.btn_shuffle, btn_prev, self.btn_playpause, btn_next, self.btn_repeat, self.btn_radio, btn_download_ctrl, self.btn_eq, self.btn_queue_toggle, self.btn_lyrics_toggle):
            controls_box.pack_start(w, False, False, 0)

        now_playing_box.pack_start(controls_box, False, False, 0)
        player_panel.pack_start(now_playing_box, False, False, 0)

        self.progress_download = Gtk.ProgressBar()
        self.progress_download.set_show_text(True)
        self.progress_download.set_no_show_all(True)
        player_panel.pack_start(self.progress_download, False, False, 0)

        # Linha de status discreta (substitui a antiga gaveta de notificações):
        # altura fixa, sem moldura, nada se move quando a mensagem aparece.
        self.infobar_label = Gtk.Label()
        self.infobar_label.set_halign(Gtk.Align.FILL)     # ocupa toda a largura: só corta no limite da janela
        self.infobar_label.set_hexpand(True)
        self.infobar_label.set_xalign(0.0)                # texto alinhado à esquerda
        self.infobar_label.set_valign(Gtk.Align.CENTER)
        self.infobar_label.set_size_request(-1, 22)       # altura fixa folgada (acentos, ♥, ⬇ e fontes de fallback)
        self.infobar_label.set_ellipsize(3)
        self.infobar_label.set_markup(self._STATUS_FMT.format(" "))
        player_panel.pack_start(self.infobar_label, False, False, 0)

        vbox.pack_start(player_panel, False, False, 0)

        # Inicialização do MPV
        self.mpv = MPVController(
            callbacks={
                "on_track_end": self.on_track_finished,
                "on_load_error": self.on_load_error,
                "on_time_change": self.on_mpv_time_change,
                "on_duration_change": self.on_mpv_duration_change,
                "on_pause_change": self.on_mpv_pause_change,
                "on_idle_change": self.on_mpv_idle_change,
                "on_died": self.on_mpv_died,
                "on_metadata": self.on_mpv_metadata,
            }
        )
        self.mpv._af = build_eq_filter(self.eq_gains) if self.eq_enabled else ""   # aplicado já no start()
        if not self.mpv.start(initial_volume=self.config.get("volume", 100)):
            self.mostrar_mensagem("Erro ao iniciar o MPV. Verifique se está instalado.")

        self._apply_volume_ui()
        self._refresh_eq_button()

        self._mpris_pos = 0
        self.mpris = MprisService(self)   # controles de mídia do sistema (MPRIS2)

        self.connect("destroy", self.on_destroy)
        self.connect("window-state-event", self._on_window_state)
        self.connect("key-press-event", self.on_key_press)

        self.render_queue()
        self.render_playlists()
        self.render_history()
        self.render_favorites()
        self._select_nav("home")  # destaca "Início" na barra lateral

        GLib.timeout_add_seconds(25, self._start_ytdlp_check)    # depois que a interface assentou
        GLib.timeout_add(700, self._show_welcome)
        GLib.timeout_add(400, self._startup_chart)
        GLib.timeout_add(1500, self._lib_startup)

    def _check_dependencies(self):
        missing = []
        if not shutil.which("mpv"):
            missing.append("mpv")
        if not shutil.which("yt-dlp"):
            missing.append("yt-dlp")

        if missing:
            msg = f"As seguintes dependências necessárias não foram encontradas no sistema:\n\n• " + "\n• ".join(missing)
            msg += "\n\nPor favor, instale-as via gerenciador de pacotes para utilizar o aplicativo."
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Dependências Ausentes",
            )
            dialog.format_secondary_text(msg)
            dialog.run()
            dialog.destroy()

    # ======================================================================
    # Interface: barra lateral | conteúdo navegável | painel direito (Fila/Letra)
    # ======================================================================
    def _clear(self, container):
        for child in list(container.get_children()):
            container.remove(child)

    def _set_shown(self, widget, shown):
        widget.set_no_show_all(False)
        if shown:
            widget.show_all()
        else:
            widget.hide()
        widget.set_no_show_all(True)

    def _placeholder(self, text):
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("dim-label")
        lbl.set_margin_top(12)
        lbl.set_margin_bottom(12)
        lbl.show()
        return lbl

    def _h2(self, text):
        lbl = Gtk.Label(xalign=0)
        lbl.set_ellipsize(3)
        self._set_h2(lbl, text)
        return lbl

    def _set_h2(self, lbl, text):
        lbl.set_markup(f'<span size="x-large" weight="bold">{GLib.markup_escape_text(text)}</span>')

    def _make_flow(self, min_per_line=2, max_per_line=8, stretch=False):
        flow = Gtk.FlowBox()
        if not stretch:
            flow.set_halign(Gtk.Align.START)  # cartões mantêm o tamanho (não esticam)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(min_per_line)
        flow.set_max_children_per_line(max_per_line)
        flow.set_column_spacing(6)
        flow.set_row_spacing(6)
        flow.set_homogeneous(True)
        flow.set_valign(Gtk.Align.START)
        return flow

    def _card(self, title, subtitle="", size=(120, 120), icon="avatar-default-symbolic", url=None, on_click=None,
              local_item=None):
        """Cartão de capa (artista, álbum, faixa) para as vitrines.

        Sem on_click: um clique só SELECIONA o cartão (destaque, só um por vez); as ações vêm do
        duplo clique / botão direito, tratados por quem cria o cartão."""
        btn = Gtk.ToggleButton()          # 'ativo' = cartão selecionado
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_focus_on_click(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_border_width(4)
        img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
        img.set_pixel_size(min(size))
        img.set_size_request(size[0], size[1])
        box.pack_start(img, False, False, 0)
        t = Gtk.Label(label=title)
        t.set_ellipsize(3)
        t.set_width_chars(14)
        t.set_max_width_chars(16)
        t.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        box.pack_start(t, False, False, 0)
        if subtitle:
            s = Gtk.Label(label=subtitle)
            s.set_ellipsize(3)
            s.set_width_chars(14)
            s.set_max_width_chars(16)
            s.get_style_context().add_class("dim-label")
            box.pack_start(s, False, False, 0)
        btn.add(box)
        btn.set_tooltip_text(title)
        if on_click:
            btn.connect("clicked", lambda b: on_click())
        else:
            btn.connect("button-press-event", self._on_card_select_press)   # 1º handler: roda antes dos do chamador
            btn.connect("destroy", self._on_card_destroy)
        if local_item:
            self._submit_art(self._load_local_artwork_into, local_item, img, size[0], size[1])
        elif url:
            self._submit_art(self._load_artwork_into, url, img, size[0], size[1])
        return btn

    def _on_card_select_press(self, card, event):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button not in (1, 3):
            return False                  # duplo clique segue adiante para quem abre/toca
        self._select_card(card)
        return event.button == 1          # consome o clique esquerdo (não alterna sozinho); o direito segue p/ o menu

    def _select_card(self, card):
        prev = getattr(self, "_sel_card", None)
        if prev is not None and prev is not card:
            try:
                prev.set_active(False)
            except Exception:
                pass
        self._sel_card = card
        if not card.get_active():
            card.set_active(True)

    def _on_card_destroy(self, card):
        if getattr(self, "_sel_card", None) is card:
            self._sel_card = None

    def _submit_art(self, fn, *args):
        try:
            self._art_pool.submit(fn, *args)
        except RuntimeError:   # pool já encerrado (app fechando)
            pass

    def _artist_card(self, a):
        fans = a.get("nb_fan") or 0
        sub = f"{fans:,} fãs".replace(",", ".") if fans else "Artista"
        card = self._card(a.get("name", ""), sub, (120, 120),
                          url=a.get("picture_medium") or a.get("picture"))
        card.set_tooltip_text(f"{a.get('name', '')}\nDuplo clique para abrir · botão direito para opções")
        card.connect("button-press-event", self._on_artist_card_press, a)
        return card

    def _album_card(self, alb, artist_name=None):
        year = (alb.get("release_date") or "")[:4]
        art = artist_name or (alb.get("artist") or {}).get("name") or ""
        sub = " · ".join(x for x in (art, year) if x)
        card = self._card(alb.get("title", ""), sub, (120, 120), icon="media-optical",
                          url=alb.get("cover_medium") or alb.get("cover_small"))
        card.set_tooltip_text(f"{alb.get('title', '')}\nDuplo clique para abrir · botão direito para opções")
        card.connect("button-press-event", self._on_album_card_press, alb, art)
        return card

    def _on_album_card_press(self, btn, event, alb, art):
        # clique simples não faz nada; duplo clique abre o álbum; botão direito abre o menu
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            GLib.idle_add(self._open_album_card, alb, art)   # adiado: navegar mexe nos cartões
            return True
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        menu = Gtk.Menu()
        it_open = Gtk.MenuItem(label="Ver faixas")
        it_open.connect("activate", lambda w: self._open_album(alb, art))
        it_pl = Gtk.MenuItem(label="Adicionar faixas na Playlist")

        def _add(w):
            self.album_artist_name = art or self.album_artist_name
            self._add_album_to_playlist(alb)
        it_pl.connect("activate", _add)
        for it in (it_open, it_pl):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _open_album_card(self, alb, art):
        self._open_album(alb, art)
        return False

    def _fill_flow(self, flow, widgets):
        self._clear(flow)
        for w in widgets:
            flow.add(w)
        flow.show_all()

    # ---------- Estrutura geral ----------
    def _build_body(self):
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.main_stack.set_hhomogeneous(False)  # a largura mínima vem só da página visível
        self.main_stack.connect("notify::visible-child-name", self._on_main_page_changed)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.pack_start(self._build_sidebar(), False, False, 0)
        body.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        center.pack_start(self._build_topbar(), False, False, 0)
        center.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self.main_stack.add_named(self._build_page_home(), "home")
        self.main_stack.add_named(self._build_page_search(), "search")
        self.main_stack.add_named(self._build_page_artist(), "artist")
        self.main_stack.add_named(self._build_wiki_album_page(), "album")
        self.main_stack.add_named(self._build_page_playlist(), "playlist")
        self._build_tab_favorites()
        self._build_tab_offline()
        self._build_tab_history()
        self.main_stack.add_named(self._build_page_webradio(), "webradio")
        self.main_stack.add_named(self._build_page_mylib(), "mylib")
        self._build_tab_about()
        # Abre sempre no Início: a aba Offline chama show_all() ao ser criada e, por ser a primeira página
        # visível do Stack, virava a página inicial. O filho precisa estar visível para ser selecionado.
        self.home_scroll.show_all()
        self.main_stack.set_visible_child_name("home")
        center.pack_start(self.main_stack, True, True, 0)
        body.pack_start(center, True, True, 0)

        body.pack_start(self._build_side_panel(), False, False, 0)
        return body

    def _build_sidebar(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side.set_size_request(230, -1)
        side.set_border_width(12)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        logo = Gtk.Image.new_from_icon_name("audio-headphones-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        icon_path = _find_icon_file()
        if icon_path:
            try:
                logo.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_path, 28, 28))
            except Exception:
                pass
        brand.pack_start(logo, False, False, 0)
        name = Gtk.Label(xalign=0)
        name.set_markup(f'<span size="x-large" weight="bold">{GLib.markup_escape_text(APP_NAME)}</span>')
        brand.pack_start(name, True, True, 0)
        side.pack_start(brand, False, False, 4)

        self.nav_list = Gtk.ListBox()
        self.nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav_list.connect("row-activated", lambda lb, row: self.navigate(row.nav_name))
        for key, icon, label in (
            ("home", "⌂", "Início"),
            ("favorites", '<span foreground="#e0245e">♥</span>', "Favoritas"),
            ("offline", '<span foreground="#2e9e5b">⬇</span>', "Músicas Offline"),
            ("webradio", "📻", "Rádios Web"),
            ("mylib", "⌕", "Buscar na Biblioteca"),
            ("history", "◷", "Recentes"),
            ("about", "ⓘ", "Sobre"),
        ):
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hb.set_border_width(6)
            ic = Gtk.Label()
            ic.set_markup(f'<span size="large">{icon}</span>')
            ic.set_width_chars(2)
            hb.pack_start(ic, False, False, 0)
            hb.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
            row.add(hb)
            row.nav_name = key
            self.nav_list.add(row)
        side.pack_start(self.nav_list, False, False, 0)
        side.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        btn_new_pl = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        btn_new_pl.set_relief(Gtk.ReliefStyle.NONE)
        btn_new_pl.set_tooltip_text("Nova playlist")
        btn_new_pl.connect("clicked", self.on_create_playlist_dialog)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.pack_start(self._section_label("Suas playlists"), True, True, 0)
        head.pack_end(btn_new_pl, False, False, 0)
        side.pack_start(head, False, False, 0)

        self.pl_search_entry = Gtk.SearchEntry()
        self.pl_search_entry.set_placeholder_text("Buscar playlist...")
        self.pl_search_entry.connect("search-changed", self.on_playlist_filter_changed)
        self.pl_search_entry.connect("stop-search", lambda e: e.set_text(""))
        side.pack_start(self.pl_search_entry, False, False, 0)

        pl_scroll = Gtk.ScrolledWindow()
        pl_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.playlists_list = Gtk.ListBox()
        self.playlists_list.connect("row-activated", self.on_playlist_selected)
        self.playlists_list.connect("button-press-event", self.on_playlists_button_press)
        self.playlists_list.set_filter_func(self._playlist_filter_func)
        self.playlists_list.set_placeholder(self._placeholder("Nenhuma playlist ainda.\nClique em + para criar."))
        pl_scroll.add(self.playlists_list)
        side.pack_start(pl_scroll, True, True, 0)
        return side

    def _build_topbar(self):
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_border_width(10)

        self.btn_back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.btn_back.set_tooltip_text("Voltar (Alt+←)")
        self.btn_back.set_sensitive(False)
        self.btn_back.connect("clicked", self.go_back)
        top.pack_start(self.btn_back, False, False, 0)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("O que você quer ouvir? Músicas, artistas, álbuns ou link...")
        self.search_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "edit-find-symbolic")
        self.search_entry.connect("activate", self.on_search)
        top.pack_start(self.search_entry, True, True, 0)

        self.search_spinner = Gtk.Spinner()
        top.pack_start(self.search_spinner, False, False, 0)
        btn_search = Gtk.Button(label="Buscar")
        btn_search.get_style_context().add_class("suggested-action")
        btn_search.connect("clicked", self.on_search)
        top.pack_start(btn_search, False, False, 0)
        return top

    def _build_side_panel(self):
        """Painel direito (Fila / Letra) que desliza e pode ser fechado."""
        self.side_revealer = Gtk.Revealer()
        self.side_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.side_revealer.set_transition_duration(150)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        self.side_stack = Gtk.Stack()
        self.side_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.side_stack.set_size_request(340, -1)
        self.side_stack.add_named(self._build_panel_queue(), "queue")
        self.side_stack.add_named(self._build_panel_lyrics(), "lyrics")
        box.pack_start(self.side_stack, True, True, 0)
        self.side_revealer.add(box)
        self.side_revealer.connect("notify::reveal-child", self._on_queue_visibility)
        self.side_stack.connect("notify::visible-child-name", self._on_queue_visibility)
        return self.side_revealer

    def _panel_head(self, title):
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.pack_start(self._h2(title), True, True, 0)
        btn = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text("Fechar painel")
        btn.connect("clicked", lambda b: self.show_side_panel(self.side_stack.get_visible_child_name()))
        head.pack_end(btn, False, False, 0)
        return head

    def _build_panel_queue(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(12)
        page.pack_start(self._panel_head("Fila"), False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_list = Gtk.ListBox()
        self.queue_list.set_activate_on_single_click(False)
        self.queue_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.queue_list.connect("row-activated", self.on_queue_activated)
        self.queue_list.connect("button-press-event", self.on_queue_button_press)
        self.queue_list.add_events(Gdk.EventMask.BUTTON1_MOTION_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.queue_list.connect("motion-notify-event", self._on_queue_motion)
        self.queue_list.connect("button-release-event", self._on_queue_release)
        self.queue_list.connect_after("draw", self._on_queue_draw)
        self._qdrag = None
        self.queue_list.set_placeholder(self._placeholder("A fila está vazia.\nBusque algo para começar."))
        scroll.add(self.queue_list)
        page.pack_start(scroll, True, True, 0)

        r1 = Gtk.Box(spacing=6)
        btn_add_pl_queue = Gtk.Button(label="+ Playlist")
        btn_add_pl_queue.connect("clicked", self.on_add_selection_to_playlist)
        btn_save_as_pl = Gtk.Button(label="Salvar como Playlist")
        btn_save_as_pl.connect("clicked", self.on_save_queue_as_playlist)
        r1.pack_start(btn_add_pl_queue, True, True, 0)
        r1.pack_start(btn_save_as_pl, True, True, 0)
        r2 = Gtk.Box(spacing=6)
        btn_delete_sel = Gtk.Button(label="Excluir Selecionadas")
        btn_delete_sel.connect("clicked", self.on_delete_selected_queue)
        btn_clear = Gtk.Button(label="Limpar Fila")
        btn_clear.connect("clicked", self.on_clear_queue)
        r2.pack_start(btn_delete_sel, True, True, 0)
        r2.pack_start(btn_clear, True, True, 0)
        page.pack_start(r1, False, False, 0)
        page.pack_start(r2, False, False, 0)
        return page

    def _build_panel_lyrics(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(12)
        page.pack_start(self._panel_head("Letra"), False, False, 0)

        info_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.lyrics_art_img = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.lyrics_art_img.set_pixel_size(80)
        info_card.pack_start(self.lyrics_art_img, False, False, 0)
        info_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_texts.set_valign(Gtk.Align.CENTER)
        self.lyrics_title_label = Gtk.Label(xalign=0)
        self.lyrics_title_label.set_line_wrap(True)
        self.lyrics_title_label.set_markup("<b>Nenhuma música tocando</b>")
        self.lyrics_artist_label = Gtk.Label(xalign=0)
        self.lyrics_album_label = Gtk.Label(xalign=0)
        self.lyrics_year_label = Gtk.Label(xalign=0)
        for lbl in (self.lyrics_artist_label, self.lyrics_album_label, self.lyrics_year_label):
            lbl.get_style_context().add_class("dim-label")
            lbl.set_ellipsize(3)
        info_texts.pack_start(self.lyrics_title_label, False, False, 0)
        info_texts.pack_start(self.lyrics_artist_label, False, False, 0)
        info_texts.pack_start(self.lyrics_album_label, False, False, 0)
        info_texts.pack_start(self.lyrics_year_label, False, False, 0)
        info_card.pack_start(info_texts, True, True, 0)
        page.pack_start(info_card, False, False, 0)
        page.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        lyrics_scroll = Gtk.ScrolledWindow()
        lyrics_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.lyrics_text_view = Gtk.TextView()
        self.lyrics_text_view.set_editable(False)
        self.lyrics_text_view.set_cursor_visible(False)
        self.lyrics_text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.lyrics_text_view.set_justification(Gtk.Justification.CENTER)
        self.lyrics_text_view.set_left_margin(8)
        self.lyrics_text_view.set_right_margin(8)
        self.lyrics_text_view.set_top_margin(8)
        self._update_lyrics_ui("Nenhuma música tocando no momento.")
        lyrics_scroll.add(self.lyrics_text_view)
        page.pack_start(lyrics_scroll, True, True, 0)

        btn_reload_lyrics = Gtk.Button(label="Buscar Novamente")
        btn_reload_lyrics.connect("clicked", lambda w: self.search_current_lyrics())
        page.pack_start(btn_reload_lyrics, False, False, 0)
        return page

    # ---------- Início (vitrines estilo Spotify) ----------
    def _build_page_home(self):
        scroll = Gtk.ScrolledWindow()
        self.home_scroll = scroll
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        page.set_border_width(20)

        self.home_greeting = Gtk.Label(xalign=0)
        page.pack_start(self.home_greeting, False, False, 0)

        # Atalhos: Curtidas + playlists + nova
        self.home_tiles = self._make_flow(1, 3, stretch=True)
        page.pack_start(self.home_tiles, False, False, 0)

        # Tocou recentemente
        self.home_recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.home_recent_box.pack_start(self._h2("Tocou recentemente"), False, False, 0)
        self.home_recent_flow = self._make_flow(2, 8)
        self.home_recent_box.pack_start(self.home_recent_flow, False, False, 0)
        page.pack_start(self.home_recent_box, False, False, 0)

        # Em alta (por país): artistas + músicas mais ouvidas
        self.chart_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        chart_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chart_title_lbl = self._h2("Em alta no Brasil")
        chart_head.pack_start(self.chart_title_lbl, True, True, 0)
        self.chart_combo = Gtk.ComboBoxText()
        for _code, _label, _aliases, _phrase in self.CHART_COUNTRIES:
            self.chart_combo.append(_code, _label)
        saved = self.config.get("chart_country")
        self.chart_combo.set_active_id(saved if any(c[0] == saved for c in self.CHART_COUNTRIES) else "BR")
        self.chart_combo.set_tooltip_text("Escolher o país do ranking")
        self.chart_combo.connect("changed", self.on_chart_country_changed)
        self.chart_combo.connect("scroll-event", self._on_combo_scroll_passthrough)
        chart_head.pack_end(self.chart_combo, False, False, 0)
        lbl_pais = Gtk.Label(label="País:")
        lbl_pais.get_style_context().add_class("dim-label")
        chart_head.pack_end(lbl_pais, False, False, 0)
        self.chart_box.pack_start(chart_head, False, False, 0)

        self.chart_artists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.chart_artists_box.pack_start(self._section_label("Artistas"), False, False, 0)
        self.chart_flow = self._make_flow(2, 8)
        self.chart_artists_box.pack_start(self.chart_flow, False, False, 0)
        self.chart_box.pack_start(self.chart_artists_box, False, False, 0)

        tracks_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tracks_head.pack_start(self._section_label("Músicas mais ouvidas"), True, True, 0)
        btn_add_all_pl = Gtk.Button(label="Adicionar todas à Playlist")
        btn_add_all_pl.connect("clicked", self.on_discover_add_all_to_playlist)
        tracks_head.pack_end(btn_add_all_pl, False, False, 0)
        self.chart_box.pack_start(tracks_head, False, False, 0)

        self.discover_tracks_list = Gtk.ListBox()
        self.discover_tracks_list.set_activate_on_single_click(False)
        self.discover_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.discover_tracks_list.connect("row-activated", self.on_discover_track_activated)
        self.discover_tracks_list.connect("button-press-event", self.on_discover_tracks_button_press)
        self.discover_tracks_list.set_placeholder(self._placeholder("Carregando..."))
        frame = Gtk.Frame()
        frame.add(self.discover_tracks_list)
        self.chart_box.pack_start(frame, False, False, 0)

        track_btns = Gtk.Box(spacing=8)
        btn_t_play = Gtk.Button(label="Reproduzir")
        btn_t_play.get_style_context().add_class("suggested-action")
        btn_t_play.connect("clicked", lambda w: self.on_discover_track_button("play"))
        btn_t_add = Gtk.Button(label="+ Fila")
        btn_t_add.connect("clicked", lambda w: self.on_discover_track_button("queue"))
        btn_t_dl = Gtk.Button(label="Baixar")
        btn_t_dl.connect("clicked", lambda w: self.on_discover_track_button("download"))
        for b in (btn_t_play, btn_t_add, btn_t_dl):
            track_btns.pack_start(b, False, False, 0)
        self.chart_box.pack_start(track_btns, False, False, 0)
        page.pack_start(self.chart_box, False, False, 0)

        scroll.add(page)
        for w in (self.home_recent_box, self.chart_artists_box):
            self._set_shown(w, False)
        self._refresh_home_greeting()
        return scroll

    def _refresh_home_greeting(self):
        name = (self.profile.get("name") or "").strip()
        txt = f"{self._time_greeting()}, {name}" if name else self._time_greeting()
        self.home_greeting.set_markup(f'<span size="xx-large" weight="bold">{GLib.markup_escape_text(txt)}</span>')

    def _render_home_tiles(self):
        if not hasattr(self, "home_tiles"):
            return

        def tile(icon_markup, title, sub, cb):
            btn = Gtk.Button()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hb.set_border_width(8)
            ic = Gtk.Label()
            ic.set_markup(f'<span size="xx-large">{icon_markup}</span>')
            hb.pack_start(ic, False, False, 0)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            vb.set_valign(Gtk.Align.CENTER)
            t = Gtk.Label(xalign=0)
            t.set_ellipsize(3)
            t.set_max_width_chars(20)
            t.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
            s = Gtk.Label(label=sub, xalign=0)
            s.get_style_context().add_class("dim-label")
            vb.pack_start(t, False, False, 0)
            vb.pack_start(s, False, False, 0)
            hb.pack_start(vb, True, True, 0)
            btn.add(hb)
            btn.connect("clicked", lambda b: cb())
            return btn

        n = len(self.favorites)
        tiles = [tile('<span foreground="#e0245e">♥</span>', "Músicas curtidas",
                      "1 música" if n == 1 else f"{n} músicas", lambda: self.navigate("favorites"))]
        nl = len(self.lib_index) if self.lib_root else 0
        tiles.append(tile('<span foreground="#2e9e5b">⬇</span>', "Músicas offline",
                          ("1 música" if nl == 1 else f"{nl} músicas") if nl
                          else ("Indexando..." if self._lib_scanning else "Escolha uma pasta" if not self.lib_root
                                else "Nenhuma música"),
                          lambda: self.navigate("offline")))
        self._fill_flow(self.home_tiles, tiles)

    def _render_home_recents(self):
        if not hasattr(self, "home_recent_flow"):
            return
        cards = []
        for item in self.history[:8]:
            # sem on_click: um clique simples não toca; só duplo clique (ou menu do botão direito)
            card = self._card(
                item.get("title", ""), item.get("uploader", ""), (160, 90), icon="audio-x-generic",
                url=f"https://i.ytimg.com/vi/{item['id']}/mqdefault.jpg" if item.get("id") else None,
                local_item=item if item.get("path") else None)
            card.set_tooltip_text(f"{item.get('title', '')}\nDuplo clique para tocar · botão direito para opções")
            card.connect("button-press-event", self._on_recent_card_press, item)
            cards.append(card)
        self._fill_flow(self.home_recent_flow, cards)
        self._set_shown(self.home_recent_box, bool(cards))

    def _on_recent_card_press(self, btn, event, item):
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            GLib.idle_add(self._play_recent_item, item)   # adiado: tocar reconstrói os cartões
            return True
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            self._recent_card_menu(item, event)
            return True
        return False

    def _play_recent_item(self, item):
        self.play_item(item)
        return False

    def _recent_card_menu(self, item, event):
        menu = Gtk.Menu()
        it_play = Gtk.MenuItem(label="Reproduzir")
        it_play.connect("activate", lambda w: self.play_item(item))
        it_album = Gtk.MenuItem(label="Ver álbum")
        it_album.connect("activate", lambda w: self._open_album_of_item(item))
        it_artist = Gtk.MenuItem(label="Ver artista")
        it_artist.connect("activate", lambda w: self._open_artist_of_item(item))
        it_like = self._like_menu_item([item])
        it_dl = Gtk.MenuItem(label="Baixar")
        it_dl.connect("activate", lambda w: self._download_recent_item(item))
        for it in (it_play, it_album, it_artist, it_like, it_dl, self._radio_menu_item(item)):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _open_artist_of_item(self, item):
        name = self._guess_artist(item)
        if not name:
            self.mostrar_mensagem("Não consegui identificar o artista desta faixa.")
            return
        self._open_artist_in_wiki(name)

    def _download_recent_item(self, item):
        if item.get("path"):
            self.mostrar_mensagem("Esta faixa já está offline no seu computador.")
            return
        self._download_single_dialog(item)

    def _open_album_of_item(self, item):
        """Acha o álbum da faixa no Deezer e abre a página do álbum."""
        artist = self._guess_artist(item)
        title = self._clean_title(item.get("title", ""))
        self.show_toast("Buscando álbum...")

        def work():
            alb, art_name = None, artist
            try:
                q = urllib.parse.quote(f"{artist} {title}".strip())
                for r in self._http_json(f"{DEEZER_API}/search?q={q}&limit=5").get("data", []) or []:
                    a = r.get("album") or {}
                    if a.get("id"):
                        alb = a
                        art_name = (r.get("artist") or {}).get("name") or artist
                        break
            except Exception:
                alb = None
            GLib.idle_add(self._after_album_lookup, alb, art_name)

        threading.Thread(target=work, daemon=True).start()

    def _after_album_lookup(self, alb, artist):
        if self._closing:
            return False
        if alb:
            self._open_album(alb, artist)
        else:
            self.mostrar_mensagem("Não encontrei o álbum desta faixa.")
        return False

    # ---------- Em alta (por país) ----------
    # (código, nome, títulos "Top <nome>" usados pelo Deezer Charts, frase do título)
    CHART_COUNTRIES = (
        ("BR", "Brasil", ("Brazil",), "no Brasil"),
        ("WW", "Mundo", ("Worldwide",), "no mundo"),
        ("US", "Estados Unidos", ("USA", "United States", "US"), "nos Estados Unidos"),
        ("GB", "Reino Unido", ("UK", "United Kingdom"), "no Reino Unido"),
        ("PT", "Portugal", ("Portugal",), "em Portugal"),
        ("AR", "Argentina", ("Argentina",), "na Argentina"),
        ("MX", "México", ("Mexico",), "no México"),
        ("CO", "Colômbia", ("Colombia",), "na Colômbia"),
        ("CL", "Chile", ("Chile",), "no Chile"),
        ("ES", "Espanha", ("Spain",), "na Espanha"),
        ("FR", "França", ("France",), "na França"),
        ("DE", "Alemanha", ("Germany",), "na Alemanha"),
        ("IT", "Itália", ("Italy",), "na Itália"),
        ("TR", "Turquia", ("Turkey",), "na Turquia"),
        ("CA", "Canadá", ("Canada",), "no Canadá"),
    )
    CHART_PRESET_IDS = {"BR": 1111141961}     # "Top Brazil" (Deezer Charts); os demais são achados e guardados

    def _chart_info(self, code):
        for c in self.CHART_COUNTRIES:
            if c[0] == code:
                return c
        return self.CHART_COUNTRIES[0]

    def _on_combo_scroll_passthrough(self, combo, event):
        """A roda do mouse sobre o combo NÃO troca o item: o scroll segue para a página.
        (Só o clique ou o teclado mudam a seleção.)"""
        sw = combo.get_ancestor(Gtk.ScrolledWindow)
        if sw is not None:
            sw.event(event)
        return True      # impede o ComboBox de tratar o scroll e trocar de país

    def on_chart_country_changed(self, combo):
        code = combo.get_active_id()
        if not code:
            return
        self.config["chart_country"] = code
        self._schedule_config_save()
        self._chart_load(code)

    def _chart_load(self, code=None):
        code = code or self.config.get("chart_country") or "BR"
        info = self._chart_info(code)
        self._chart_token += 1
        token = self._chart_token
        self._set_h2(self.chart_title_lbl, f"Em alta {info[3]}")
        self._fill_flow(self.chart_flow, [])
        self._set_shown(self.chart_artists_box, False)
        for child in list(self.discover_tracks_list.get_children()):
            self.discover_tracks_list.remove(child)
        self.discover_tracks_list.set_placeholder(self._placeholder("Carregando..."))
        threading.Thread(target=self._chart_thread, args=(info[0], token), daemon=True).start()

    def _chart_thread(self, code, token):
        tracks = []
        try:
            if code == "WW":
                tracks = self._http_json(f"{DEEZER_API}/chart/0/tracks?limit=50").get("data", []) or []
            else:
                tracks = self._chart_country_tracks(code)
        except Exception:
            tracks = []
        tracks = [t for t in tracks if isinstance(t, dict) and t.get("readable", True)]
        if token == self._chart_token:
            GLib.idle_add(self._populate_chart, code, tracks, token)

    def _chart_country_tracks(self, code):
        """Faixas do chart oficial 'Top <país>' (Deezer Charts). O id é guardado após a 1ª busca."""
        cache = self.config.get("chart_ids")
        cache = cache if isinstance(cache, dict) else {}
        pid = cache.get(code) or self.CHART_PRESET_IDS.get(code)

        def fetch(playlist_id):
            return self._http_json(f"{DEEZER_API}/playlist/{playlist_id}/tracks?limit=50", timeout=10).get("data", []) or []

        tracks = []
        if pid:
            try:
                tracks = fetch(pid)
            except Exception:
                tracks = []
        if not tracks:
            found = self._chart_find_playlist(code)
            if found and found != pid:
                tracks = fetch(found)
                if tracks:
                    GLib.idle_add(self._chart_remember, code, found)
        return tracks

    def _chart_remember(self, code, pid):
        ids = self.config.get("chart_ids")
        if not isinstance(ids, dict):
            ids = {}
        ids[code] = pid
        self.config["chart_ids"] = ids
        self._schedule_config_save()
        return False

    def _chart_find_playlist(self, code):
        """Procura a playlist oficial 'Top <país>' do Deezer Charts (só aceita a conta oficial)."""
        aliases = self._chart_info(code)[2]
        wanted = {self._norm_key("Top " + a) for a in aliases}
        best, best_score = None, None
        for alias in aliases:
            q = urllib.parse.quote(f"Top {alias}")
            data = self._http_json(f"{DEEZER_API}/search/playlist?q={q}&limit=25", timeout=10).get("data", []) or []
            for pl in data:
                if self._norm_key(pl.get("title", "")) not in wanted:
                    continue
                owner = ((pl.get("user") or pl.get("creator") or {}).get("name") or "").lower()
                if "deezer" not in owner:
                    continue
                score = ((pl.get("nb_tracks") or 0) >= 40, pl.get("fans") or pl.get("nb_fan") or 0)
                if best_score is None or score > best_score:
                    best, best_score = pl.get("id"), score
            if best:
                break
        return best

    def _populate_chart(self, code, tracks, token):
        if token != self._chart_token:
            return False
        info = self._chart_info(code)
        for child in list(self.discover_tracks_list.get_children()):
            self.discover_tracks_list.remove(child)
        if not tracks:
            self.discover_tracks_list.set_placeholder(
                self._placeholder(f"Não encontrei o ranking {info[3]} agora (sem internet ou sem chart para esse país)."))
            self._fill_flow(self.chart_flow, [])
            self._set_shown(self.chart_artists_box, False)
            return False

        # artistas em ordem de aparição no ranking, com quantas músicas cada um tem nele
        order, counts = [], {}
        for t in tracks:
            a = t.get("artist") or {}
            aid = a.get("id")
            if not aid:
                continue
            if aid not in counts:
                order.append(a)
                counts[aid] = 0
            counts[aid] += 1
        cards = []
        for i, a in enumerate(order[:12], start=1):
            n = counts[a["id"]]
            sub = f"#{i} · {n} no top" if n > 1 else f"#{i}"
            card = self._card(a.get("name", ""), sub, (120, 120),
                              url=a.get("picture_medium") or a.get("picture"))
            card.set_tooltip_text(f"{a.get('name', '')}\nDuplo clique para abrir · botão direito para opções")
            card.connect("button-press-event", self._on_artist_card_press, a)
            cards.append(card)
        self._fill_flow(self.chart_flow, cards)
        self._set_shown(self.chart_artists_box, bool(cards))

        for i, t in enumerate(tracks, start=1):
            self.discover_tracks_list.add(self._dtrack_row(t, i))
        self.discover_tracks_list.show_all()
        return False

    # ---------- Cartões de artista (Em alta, Busca, Fãs também ouvem): duplo clique + botão direito ----------
    def _on_artist_card_press(self, btn, event, artist):
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            GLib.idle_add(self._open_artist_card, artist)   # adiado: navegar mexe nos cartões
            return True
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            self._artist_card_menu(artist, event)
            return True
        return False

    def _open_artist_card(self, artist):
        self._open_artist_in_wiki(artist.get("name", ""), artist)
        return False

    def _artist_top_dtracks(self, artist, limit=10):
        """Populares do artista como faixas do Deezer prontas para tocar/enfileirar (roda em thread)."""
        tracks = self._deezer_top_tracks(artist, limit=limit)
        return [self._normalize_track({
                    "title": t.get("title", ""),
                    "artist": (t.get("artist") or {}).get("name") or artist.get("name", ""),
                    "duration_fmt": self._fmt_duration(t.get("duration")),
                    "verified": True})
                for t in tracks if t.get("title")]

    def _artist_card_menu(self, artist, event):
        menu = Gtk.Menu()
        it_open = Gtk.MenuItem(label="Ver artista")
        it_open.connect("activate", lambda w: self._open_artist_in_wiki(artist.get("name", ""), artist))
        it_play = Gtk.MenuItem(label="Tocar músicas populares")
        it_play.connect("activate", lambda w: self._artist_play_top(artist))
        it_queue = Gtk.MenuItem(label="Adicionar à Fila")
        it_queue.connect("activate", lambda w: self._artist_queue_top(artist))
        it_pl = Gtk.MenuItem(label="Adicionar à Playlist")
        it_pl.connect("activate", lambda w: self._playlist_add_job(lambda: self._artist_top_dtracks(artist)))
        it_radio = Gtk.MenuItem(label="Iniciar Mix do Artista")
        it_radio.connect("activate", lambda w: self._artist_start_radio(artist))
        for it in (it_open, it_play, it_queue, it_radio, it_pl):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _artist_play_top(self, artist):
        """Toca a 1ª popular assim que localizada; as demais entram na fila (mesmo fluxo de 'Reproduzir Agora')."""
        if self._job_guard():
            return
        state = {"n": 0}

        def on_item(track):
            self.add_items_bulk([track], notify=False)
            if state["n"] == 0:
                self.current_index = len(self.queue) - 1
                self.play_current()
            state["n"] += 1

        def finish(resolved, missed):
            if len(resolved) > 1:
                self.show_toast(f"{len(resolved)} faixas adicionadas à fila.")

        self._start_job(f"Populares de {artist.get('name', '')}", lambda: self._artist_top_dtracks(artist),
                        finish, on_item=on_item,
                        cancel_msg="Cancelado. O que já foi localizado permanece na fila.")

    def _artist_queue_top(self, artist):
        self._discover_resolve_and_source(
            lambda: self._artist_top_dtracks(artist), self._queue_resolved_tracks, "Adicionando à fila")

    def _artist_start_radio(self, artist):
        """Rádio do artista: localiza a faixa mais popular dele e inicia a rádio a partir dela."""
        self.show_toast("Buscando faixa no YouTube...")
        self._discover_resolve_and_source(
            lambda: self._artist_top_dtracks(artist, limit=1),
            lambda resolved: self.start_radio(resolved[0]) if resolved else None,
            "Iniciando mix")

    def _discover_resolve_and_source(self, source, callback, title):
        """Como _discover_resolve_and, mas a lista de faixas vem de uma função (buscada em thread)."""
        if self._job_guard():
            return
        self._start_job(title, source, lambda resolved, missed: callback(resolved))

    def _startup_chart(self):
        self._chart_load()
        return False

    # ---------- Busca unificada ----------
    def _build_page_search(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        page.set_border_width(20)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        chips.get_style_context().add_class("linked")
        chips.set_halign(Gtk.Align.START)
        first = None
        for key, label in (("all", "Tudo"), ("tracks", "Músicas"), ("artists", "Artistas"), ("albums", "Álbuns")):
            b = Gtk.RadioButton.new_with_label_from_widget(first, label)
            b.set_mode(False)
            first = first or b
            b.connect("toggled", self._on_filter_toggled, key)
            chips.pack_start(b, False, False, 0)
        page.pack_start(chips, False, False, 0)

        self.search_status = Gtk.Label(xalign=0)
        self.search_status.get_style_context().add_class("dim-label")
        page.pack_start(self.search_status, False, False, 0)

        # Artistas
        self.sec_artists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.sec_artists.pack_start(self._h2("Artistas"), False, False, 0)
        self.search_artists_flow = self._make_flow(2, 8)
        self.sec_artists.pack_start(self.search_artists_flow, False, False, 0)
        page.pack_start(self.sec_artists, False, False, 0)

        # Álbuns
        self.sec_albums = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.sec_albums.pack_start(self._h2("Álbuns"), False, False, 0)
        self.search_albums_flow = self._make_flow(2, 8)
        self.sec_albums.pack_start(self.search_albums_flow, False, False, 0)
        page.pack_start(self.sec_albums, False, False, 0)

        # Músicas
        self.sec_tracks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.sec_tracks.pack_start(self._h2("Músicas"), False, False, 0)
        self.results_list = Gtk.ListBox()
        self.results_list.set_activate_on_single_click(False)
        self.results_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.results_list.connect("row-activated", self.on_result_activated)
        self.results_list.connect("button-press-event", self.on_results_button_press)
        self.results_list.set_placeholder(self._placeholder("Buscando..."))
        frame = Gtk.Frame()
        frame.add(self.results_list)
        self.sec_tracks.pack_start(frame, False, False, 0)

        result_btns = Gtk.Box(spacing=8)
        btn_add_queue = Gtk.Button(label="+ Fila")
        btn_add_queue.connect("clicked", self.on_add_to_queue)
        btn_play_now = Gtk.Button(label="Tocar Agora")
        btn_play_now.get_style_context().add_class("suggested-action")
        btn_play_now.connect("clicked", self.on_play_now)
        btn_add_pl = Gtk.Button(label="+ Playlist")
        btn_add_pl.connect("clicked", self.on_add_selection_to_playlist)
        btn_download_results = Gtk.Button(label="Baixar")
        btn_download_results.connect("clicked", self.on_download)
        for b in (btn_play_now, btn_add_queue, btn_add_pl, btn_download_results):
            result_btns.pack_start(b, False, False, 0)
        self.sec_tracks.pack_start(result_btns, False, False, 0)
        page.pack_start(self.sec_tracks, False, False, 0)

        scroll.add(page)
        for w in (self.sec_artists, self.sec_albums, self.sec_tracks):
            w.show_all()
            w.set_no_show_all(True)
        self._search_filter = "all"
        return scroll

    def _on_filter_toggled(self, btn, key):
        if btn.get_active():
            self._search_filter = key
            self._apply_search_filter()

    def _apply_search_filter(self):
        f = self._search_filter
        self.sec_artists.set_visible(f in ("all", "artists"))
        self.sec_albums.set_visible(f in ("all", "albums"))
        self.sec_tracks.set_visible(f in ("all", "tracks"))

    def _search_catalog_thread(self, query, token):
        q = urllib.parse.quote(query)
        a, b = self._http_json_many([f"{DEEZER_API}/search/artist?q={q}&limit=12",
                                     f"{DEEZER_API}/search/album?q={q}&limit=18"])
        if token == self.search_token:
            GLib.idle_add(self._populate_catalog, (a or {}).get("data") or [], (b or {}).get("data") or [], token)

    def _populate_catalog(self, artists, albums, token):
        if token != self.search_token:
            return False
        self._fill_flow(self.search_artists_flow, [self._artist_card(a) for a in artists])
        self._fill_flow(self.search_albums_flow, [self._album_card(al) for al in albums])
        if not artists:
            self.search_artists_flow.add(self._placeholder("Nenhum artista encontrado."))
        if not albums:
            self.search_albums_flow.add(self._placeholder("Nenhum álbum encontrado."))
        self.search_artists_flow.show_all()
        self.search_albums_flow.show_all()
        return False

    # ---------- Página do artista ----------
    def _build_page_artist(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.artist_scroll = scroll
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_border_width(20)
        self.wiki_entry = Gtk.Entry()  # apenas guarda o termo pesquisado (não é exibido)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.wiki_art_img = Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.DIALOG)
        self.wiki_art_img.set_pixel_size(140)
        self.wiki_art_img.set_size_request(140, 140)
        head.pack_start(self.wiki_art_img, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        kicker = Gtk.Label(xalign=0)
        kicker.set_markup('<span size="small" weight="bold" letter_spacing="2048">ARTISTA</span>')
        kicker.get_style_context().add_class("dim-label")
        self.wiki_name_label = Gtk.Label(xalign=0)
        self.wiki_name_label.set_line_wrap(True)
        self.wiki_name_label.set_markup('<span size="xx-large" weight="bold">Artista</span>')
        self.wiki_fans_label = Gtk.Label(xalign=0)
        self.wiki_fans_label.get_style_context().add_class("dim-label")
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_margin_top(6)
        btn_play = Gtk.Button(label="▶  Tocar populares")
        btn_play.get_style_context().add_class("suggested-action")
        btn_play.connect("clicked", lambda b: self._play_dtracks_progressive(self._wiki_top_items()))
        btn_q = Gtk.Button(label="+ Fila")
        btn_q.connect("clicked", lambda b: self._queue_dtracks(self._wiki_top_items()))
        self.wiki_spinner = Gtk.Spinner()
        for w in (btn_play, btn_q, self.wiki_spinner):
            btns.pack_start(w, False, False, 0)
        for w in (kicker, self.wiki_name_label, self.wiki_fans_label, btns):
            texts.pack_start(w, False, False, 0)
        head.pack_start(texts, True, True, 0)
        page.pack_start(head, False, False, 0)

        self.wiki_bio_label = Gtk.Label(xalign=0)
        self.wiki_bio_label.set_line_wrap(True)
        self.wiki_bio_label.set_selectable(True)
        self.wiki_bio_label.set_text("Pesquise um artista para ver biografia e discografia.")
        page.pack_start(self.wiki_bio_label, False, False, 0)

        page.pack_start(self._h2("Populares"), False, False, 0)
        self.wiki_top_list = Gtk.ListBox()
        self.wiki_top_list.set_activate_on_single_click(False)
        self.wiki_top_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.wiki_top_list.connect("row-activated", self.on_discover_track_activated)
        self.wiki_top_list.connect("button-press-event", self.on_wiki_top_button_press)
        self.wiki_top_list.set_placeholder(self._placeholder("Sem faixas populares."))
        frame = Gtk.Frame()
        frame.add(self.wiki_top_list)
        page.pack_start(frame, False, False, 0)

        page.pack_start(self._h2("Discografia"), False, False, 0)
        hint = Gtk.Label(label="Clique em um álbum para ver as faixas. Botão direito: adicionar à playlist.", xalign=0)
        hint.get_style_context().add_class("dim-label")
        page.pack_start(hint, False, False, 0)
        self.wiki_albums_flow = self._make_flow(2, 8)
        page.pack_start(self.wiki_albums_flow, False, False, 0)

        self.wiki_related_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.wiki_related_box.pack_start(self._h2("Fãs também ouvem"), False, False, 0)
        self.wiki_related_flow = self._make_flow(2, 8)
        self.wiki_related_box.pack_start(self.wiki_related_flow, False, False, 0)
        page.pack_start(self.wiki_related_box, False, False, 0)

        scroll.add(page)
        self._set_shown(self.wiki_related_box, False)
        return scroll

    def _wiki_top_items(self):
        rows = self.wiki_top_list.get_selected_rows() or self.wiki_top_list.get_children()
        return [r.item for r in rows if hasattr(r, "item")]

    def on_wiki_top_button_press(self, widget, event):
        return self._dtracks_context_menu(
            widget, event, lambda: [r.item for r in widget.get_selected_rows() if hasattr(r, "item")])

    # ---------- Página de uma playlist ----------
    def _build_page_playlist(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(20)

        kicker = Gtk.Label(xalign=0)
        kicker.set_markup('<span size="small" weight="bold" letter_spacing="2048">PLAYLIST</span>')
        kicker.get_style_context().add_class("dim-label")
        page.pack_start(kicker, False, False, 0)
        self.pl_title_label = self._section_label("Selecione uma Playlist")
        self.pl_title_label.set_ellipsize(3)
        page.pack_start(self.pl_title_label, False, False, 0)

        pl_actions_box = Gtk.Box(spacing=8)
        btn_play_pl = Gtk.Button(label="▶  Tocar Playlist")
        btn_play_pl.get_style_context().add_class("suggested-action")
        btn_play_pl.connect("clicked", self.on_play_entire_playlist)
        btn_append_pl = Gtk.Button(label="+ À Fila")
        btn_append_pl.connect("clicked", self.on_append_playlist_to_queue)
        btn_rename_pl = Gtk.Button(label="Renomear")
        btn_rename_pl.connect("clicked", self.on_rename_current_playlist)
        btn_del_pl = Gtk.Button(label="Excluir Playlist")
        btn_del_pl.connect("clicked", self.on_delete_current_playlist)
        for b in (btn_play_pl, btn_append_pl, btn_rename_pl, btn_del_pl):
            pl_actions_box.pack_start(b, False, False, 0)
        page.pack_start(pl_actions_box, False, False, 0)

        pl_content_scroll = Gtk.ScrolledWindow()
        pl_content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pl_tracks_list = Gtk.ListBox()
        self.pl_tracks_list.set_activate_on_single_click(False)
        self.pl_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.pl_tracks_list.connect("row-activated", self.on_pl_track_activated)
        self.pl_tracks_list.connect("button-press-event", self.on_pl_tracks_button_press)
        self.pl_tracks_list.set_placeholder(self._placeholder("Esta playlist está vazia."))
        pl_content_scroll.add(self.pl_tracks_list)
        page.pack_start(pl_content_scroll, True, True, 0)
        return page

    # ---------- Favoritas ----------
    def _build_tab_favorites(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_box.set_border_width(20)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="xx-large" weight="bold"><span foreground="#e0245e">♥</span> Favoritas</span>')
        head.pack_start(title, False, False, 0)
        self.fav_count_label = Gtk.Label(xalign=0)
        self.fav_count_label.get_style_context().add_class("dim-label")
        self.fav_count_label.set_valign(Gtk.Align.END)
        head.pack_start(self.fav_count_label, True, True, 0)
        tab_box.pack_start(head, False, False, 0)

        btns = Gtk.Box(spacing=8)
        btn_play_all = Gtk.Button(label="▶  Tocar tudo")
        btn_play_all.get_style_context().add_class("suggested-action")
        btn_play_all.connect("clicked", lambda b: self._play_tracks(self.favorites, "Favoritas"))
        btn_shuffle = Gtk.Button(label="Aleatório")
        btn_shuffle.set_image(Gtk.Image.new_from_icon_name("media-playlist-shuffle-symbolic", Gtk.IconSize.BUTTON))
        btn_shuffle.set_always_show_image(True)
        btn_shuffle.connect("clicked", lambda b: self._play_tracks(self.favorites, "Favoritas", shuffle=True))
        btn_queue = Gtk.Button(label="+ Fila")
        btn_queue.set_tooltip_text("Adiciona as selecionadas (ou todas, se nada estiver selecionado)")
        btn_queue.connect("clicked", self.on_favorites_add_to_queue)
        btn_save = Gtk.Button(label="Salvar como Playlist")
        btn_save.connect("clicked", self.on_save_favorites_as_playlist)
        for b in (btn_play_all, btn_shuffle, btn_queue, btn_save):
            btns.pack_start(b, False, False, 0)
        tab_box.pack_start(btns, False, False, 0)

        fav_scroll = Gtk.ScrolledWindow()
        fav_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.favorites_list = Gtk.ListBox()
        self.favorites_list.set_activate_on_single_click(False)
        self.favorites_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.favorites_list.connect("row-activated", self.on_favorite_activated)
        self.favorites_list.connect("button-press-event", self.on_favorites_button_press)
        self.favorites_list.set_placeholder(self._placeholder("Nada por aqui ainda.\nToque em ♡ numa música para curtir."))
        fav_scroll.add(self.favorites_list)
        tab_box.pack_start(fav_scroll, True, True, 0)

        self.main_stack.add_named(tab_box, "favorites")

    # ======================================================================
    # Músicas Offline (biblioteca local)
    # ======================================================================
    def _build_tab_offline(self):
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab.set_border_width(20)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="xx-large" weight="bold"><span foreground="#2e9e5b">⬇</span> Músicas Offline</span>')
        head.pack_start(title, False, False, 0)
        self.lib_count_label = Gtk.Label(xalign=0)
        self.lib_count_label.get_style_context().add_class("dim-label")
        self.lib_count_label.set_valign(Gtk.Align.END)
        head.pack_start(self.lib_count_label, True, True, 0)
        tab.pack_start(head, False, False, 0)

        # Cabeçalho de ações (fixo): mesmo estilo dos botões das playlists
        btns = Gtk.Box(spacing=8)
        btn_play = Gtk.Button(label="▶  Tocar pasta")
        btn_play.get_style_context().add_class("suggested-action")
        btn_play.set_tooltip_text("Toca a pasta atual, incluindo subpastas")
        btn_play.connect("clicked", lambda b: self._lib_play(shuffle=False))
        btn_shuffle = Gtk.Button(label="Aleatório")
        btn_shuffle.set_image(Gtk.Image.new_from_icon_name("media-playlist-shuffle-symbolic", Gtk.IconSize.BUTTON))
        btn_shuffle.set_always_show_image(True)
        btn_shuffle.connect("clicked", lambda b: self._lib_play(shuffle=True))
        btn_queue = Gtk.Button(label="+ Fila")
        btn_queue.set_tooltip_text("Adiciona as selecionadas (ou a pasta inteira, se nada estiver selecionado)")
        btn_queue.connect("clicked", self.on_lib_add_to_queue)
        btn_pl = Gtk.Button(label="+ Playlist")
        btn_pl.set_tooltip_text("Adiciona as selecionadas (ou a pasta inteira) a uma playlist")
        btn_pl.connect("clicked", self.on_lib_add_to_playlist)
        for b in (btn_play, btn_shuffle, btn_queue, btn_pl):
            btns.pack_start(b, False, False, 0)
        btn_choose = Gtk.Button(label="Escolher pasta local")
        btn_choose.connect("clicked", self.on_lib_choose_folder)
        btn_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        btn_refresh.set_tooltip_text("Reindexar a biblioteca (detecta arquivos novos e tags alteradas)")
        btn_refresh.connect("clicked", lambda b: self._lib_scan_start(force=False))
        btns.pack_end(btn_choose, False, False, 0)
        btns.pack_end(btn_refresh, False, False, 0)
        tab.pack_start(btns, False, False, 0)

        # Caminho (migalhas clicáveis)
        crumb_scroll = Gtk.ScrolledWindow()
        crumb_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        crumb_scroll.set_propagate_natural_height(True)
        self.lib_crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        crumb_scroll.add(self.lib_crumbs)
        tab.pack_start(crumb_scroll, False, False, 0)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.lib_status = Gtk.Label(xalign=0)
        self.lib_status.get_style_context().add_class("dim-label")
        self.lib_status.set_ellipsize(3)
        status_row.pack_start(self.lib_status, True, True, 0)
        self.lib_online_check = Gtk.CheckButton(label="Completar dados online")
        self.lib_online_check.set_tooltip_text(
            "Para músicas sem artista/álbum/ano/capa nas tags, busca no Deezer (envia só título e artista).\n"
            "Nunca altera seus arquivos nem sobrescreve tags existentes.")
        self.lib_online_check.set_active(bool(self.config.get("library_online_meta", True)))
        self.lib_online_check.connect("toggled", self._on_lib_online_toggled)
        status_row.pack_end(self.lib_online_check, False, False, 0)
        tab.pack_start(status_row, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.lib_empty = Gtk.Label()
        self.lib_empty.set_line_wrap(True)
        self.lib_empty.set_justify(Gtk.Justification.CENTER)
        self.lib_empty.get_style_context().add_class("dim-label")
        self.lib_empty.set_margin_top(40)
        inner.pack_start(self.lib_empty, False, False, 0)

        self.lib_folders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.lib_folders_box.pack_start(self._h2("Pastas"), False, False, 0)
        self.lib_folders_list = Gtk.ListBox()
        self.lib_folders_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.lib_folders_list.set_activate_on_single_click(True)       # pastas: um clique navega
        self.lib_folders_list.connect("row-activated", self.on_lib_folder_activated)
        fr = Gtk.Frame()
        fr.add(self.lib_folders_list)
        self.lib_folders_box.pack_start(fr, False, False, 0)
        inner.pack_start(self.lib_folders_box, False, False, 0)

        self.lib_tracks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.lib_tracks_box.pack_start(self._h2("Músicas"), False, False, 0)
        self.offline_tracks_list = Gtk.ListBox()
        self.offline_tracks_list.set_activate_on_single_click(False)   # músicas: duplo clique toca
        self.offline_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.offline_tracks_list.connect("row-activated", self.on_lib_track_activated)
        self.offline_tracks_list.connect("button-press-event", self.on_lib_tracks_button_press)
        fr2 = Gtk.Frame()
        fr2.add(self.offline_tracks_list)
        self.lib_tracks_box.pack_start(fr2, False, False, 0)
        inner.pack_start(self.lib_tracks_box, False, False, 0)

        scroll.add(inner)
        tab.pack_start(scroll, True, True, 0)
        self.main_stack.add_named(tab, "offline")
        tab.show_all()
        for w in (self.lib_empty, self.lib_folders_box, self.lib_tracks_box):
            w.set_no_show_all(True)

    # ---------- dados ----------
    def _lib_valid_root(self):
        return bool(self.lib_root) and os.path.isdir(self.lib_root)

    def _lib_item(self, path):
        """Faixa da biblioteca (do índice; se ainda não indexada, palpite pelo nome do arquivo)."""
        entry = self.lib_index.get(path)
        if entry:
            return self._normalize_track(entry)
        title, artist = _filename_guess(path)
        return self._normalize_track({"title": title, "uploader": artist, "path": path, "offline": True,
                                      "duration": "--:--"})

    def _lib_sort_key(self, item):
        tn = str(item.get("track_no") or "")
        return (int(tn) if tn.isdigit() else 9999, os.path.basename(item["path"]).lower())

    def _lib_tracks_under(self, folder, recursive=True):
        """Faixas de uma pasta. Recursivo usa o índice; se ele estiver vazio, lê a pasta direto."""
        out = []
        if recursive:
            prefix = folder.rstrip(os.sep) + os.sep
            out = [self._normalize_track(e) for pth, e in self.lib_index.items()
                   if pth.startswith(prefix) and os.path.isfile(pth)]
            out.sort(key=lambda it: (os.path.dirname(it["path"]).lower(),) + self._lib_sort_key(it))
        if not out:
            try:
                names = sorted(os.listdir(folder), key=str.lower)
            except OSError:
                names = []
            out = [self._lib_item(os.path.join(folder, n)) for n in names
                   if n.lower().endswith(AUDIO_EXTS) and os.path.isfile(os.path.join(folder, n))]
            out.sort(key=self._lib_sort_key)
        return out

    # ---------- renderização ----------
    def _lib_make_track_row(self, item):
        row = Gtk.ListBoxRow()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hb.set_border_width(4)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        t = Gtk.Label(xalign=0)
        t.set_ellipsize(3)
        t.set_markup(f"<b>{GLib.markup_escape_text(item['title'])}</b>")
        sub_bits = [x for x in (item.get("uploader"), item.get("album"), item.get("year")) if x]
        s_lbl = Gtk.Label(label="  ·  ".join(sub_bits) if sub_bits else "Artista desconhecido", xalign=0)
        s_lbl.set_ellipsize(3)
        s_lbl.get_style_context().add_class("dim-label")
        vb.pack_start(t, False, False, 0)
        vb.pack_start(s_lbl, False, False, 0)
        hb.pack_start(vb, True, True, 6)
        dur = Gtk.Label(label=item.get("duration", ""))
        dur.get_style_context().add_class("dim-label")
        hb.pack_start(dur, False, False, 4)
        hb.pack_start(self._make_heart(item), False, False, 0)
        row.add(hb)
        row.item = item
        online = (self.lib_index.get(item["path"]) or {}).get("online_fields") or []
        if online:
            names = {"artist": "artista", "album": "álbum", "year": "ano"}
            row.set_tooltip_text("Obtido online (Deezer): " + ", ".join(names.get(x, x) for x in online)
                                 + ". As tags do arquivo não foram alteradas.")
        return row

    def _lib_fill(self, gen, items, start):
        if gen != self._lib_gen:
            return False
        end = min(len(items), start + (self.CHUNK_FIRST if start == 0 else self.CHUNK_STEP))
        for item in items[start:end]:
            row = self._lib_make_track_row(item)
            self.offline_tracks_list.add(row)
            row.show_all()
        if end < len(items):
            GLib.idle_add(self._lib_fill, gen, items, end, priority=GLib.PRIORITY_DEFAULT_IDLE)
        return False

    def _lib_render(self):
        if not hasattr(self, "lib_folders_list"):
            return
        self._lib_gen += 1      # cancela lotes de uma renderização anterior
        self._clear(self.lib_folders_list)
        self._clear(self.offline_tracks_list)
        self._clear(self.lib_crumbs)

        if not self._lib_valid_root():
            msg = ("A pasta escolhida não está acessível (disco desconectado ou pasta movida).\n"
                   f"{self.lib_root}\nClique em “Escolher pasta local” para indicar outra."
                   if self.lib_root else
                   "Nenhuma pasta local escolhida ainda.\n"
                   "Clique em “Escolher pasta local” (canto superior direito) e aponte sua pasta de músicas.\n"
                   "Suas faixas tocam sem internet, com artista, álbum, capa e letra.")
            self.lib_empty.set_text(msg)
            self._set_shown(self.lib_empty, True)
            self._set_shown(self.lib_folders_box, False)
            self._set_shown(self.lib_tracks_box, False)
            self.lib_count_label.set_text("")
            self.lib_status.set_text("")
            return
        self._set_shown(self.lib_empty, False)

        root = os.path.abspath(self.lib_root)
        cur = os.path.abspath(self.lib_dir or root)
        if not (cur == root or cur.startswith(root + os.sep)) or not os.path.isdir(cur):
            cur = root
        self.lib_dir = cur

        # migalhas
        rel = os.path.relpath(cur, root)
        parts = [] if rel == "." else rel.split(os.sep)
        trail = [(os.path.basename(root) or root, root)]
        acc = root
        for part in parts:
            acc = os.path.join(acc, part)
            trail.append((part, acc))
        for i, (name, pth) in enumerate(trail):
            if i:
                sep = Gtk.Label(label="›")
                sep.get_style_context().add_class("dim-label")
                self.lib_crumbs.pack_start(sep, False, False, 2)
            b = Gtk.Button()
            b.set_relief(Gtk.ReliefStyle.NONE)
            lbl = Gtk.Label()
            last = i == len(trail) - 1
            esc = GLib.markup_escape_text(name)
            lbl.set_markup(f"<b>{esc}</b>" if last else esc)
            b.add(lbl)
            b.set_tooltip_text(pth)
            b.connect("clicked", lambda w, d=pth: self._lib_go(d))
            self.lib_crumbs.pack_start(b, False, False, 0)
        self.lib_crumbs.show_all()

        # conteúdo da pasta
        dirs, files = [], []
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    if e.is_dir(follow_symlinks=True):
                        dirs.append(e.path)
                    elif e.is_file() and e.name.lower().endswith(AUDIO_EXTS):
                        files.append(e.path)
        except OSError:
            pass
        dirs.sort(key=lambda d: os.path.basename(d).lower())

        for d in dirs:
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.set_border_width(6)
            hb.pack_start(Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
            nm = Gtk.Label(label=os.path.basename(d), xalign=0)
            nm.set_ellipsize(3)
            hb.pack_start(nm, True, True, 0)
            hb.pack_end(Gtk.Label(label="›"), False, False, 0)
            row.add(hb)
            row.folder_path = d
            self.lib_folders_list.add(row)

        items = sorted((self._lib_item(f) for f in files), key=self._lib_sort_key)
        self._lib_fill(self._lib_gen, items, 0)      # primeiras linhas na hora, o resto em lotes

        self.lib_folders_list.show_all()
        self._set_shown(self.lib_folders_box, bool(dirs))
        self._set_shown(self.lib_tracks_box, bool(items))
        if not dirs and not items:
            self.lib_empty.set_text("Nenhuma música ou subpasta aqui.")
            self._set_shown(self.lib_empty, True)

        total = len(self.lib_index)
        self.lib_count_label.set_text(f"{len(items)} nesta pasta · {total} na biblioteca" if total else
                                      f"{len(items)} nesta pasta")
        if not self._lib_scanning:
            hint = ""
            if mutagen is None and not shutil.which("ffprobe"):
                hint = "  ·  Dica: instale python3-mutagen (ou ffmpeg) para ler artista, álbum e capa."
            self.lib_status.set_text(f"Biblioteca: {root}{hint}")

    def _lib_go(self, path):
        self.lib_dir = path
        self._lib_render()

    def on_lib_folder_activated(self, listbox, row):
        if hasattr(row, "folder_path"):
            self._lib_go(row.folder_path)

    def on_lib_track_activated(self, listbox, row):
        if hasattr(row, "item"):
            self.play_item(row.item)

    # ---------- ações do cabeçalho ----------
    def _lib_action_items(self):
        rows = self.offline_tracks_list.get_selected_rows()
        if rows:
            return [r.item for r in rows if hasattr(r, "item")]
        if self._lib_valid_root():
            return self._lib_tracks_under(self.lib_dir, recursive=True)
        return []

    def _lib_play(self, shuffle=False):
        if not self._lib_valid_root():
            self.mostrar_mensagem("Escolha uma pasta local primeiro.")
            return
        items = self._lib_tracks_under(self.lib_dir, recursive=True)
        label = os.path.basename(self.lib_dir.rstrip(os.sep)) or "Músicas Offline"
        self._play_tracks(items, label, shuffle=shuffle)

    def on_lib_add_to_queue(self, button=None):
        items = self._lib_action_items()
        if not items:
            self.mostrar_mensagem("Não há músicas para adicionar.")
            return
        self.add_items_bulk(items)

    def on_lib_add_to_playlist(self, button=None):
        items = self._lib_action_items()
        if not items:
            self.mostrar_mensagem("Não há músicas para adicionar.")
            return
        self.on_add_selection_to_playlist(items_to_add=items)

    def on_lib_tracks_button_press(self, widget, event):
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, "item"):
            return False
        self._ensure_row_selected(widget, row)
        sel = [r.item for r in widget.get_selected_rows()]
        menu = Gtk.Menu()
        item_play = Gtk.MenuItem(label="Reproduzir")
        item_play.connect("activate", lambda w: self.play_item(row.item))
        item_q = Gtk.MenuItem(label="Adicionar à Fila")
        item_q.connect("activate", lambda w: self.add_items_bulk(sel))
        item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
        item_pl.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=sel))
        item_like = self._like_menu_item(sel)
        item_meta = Gtk.MenuItem(label="Buscar dados online")
        item_meta.connect("activate", lambda w: self._lib_enrich_start([i["path"] for i in sel], force=True))
        item_open = Gtk.MenuItem(label="Mostrar no gerenciador de arquivos")

        def _open(w):
            try:
                subprocess.Popen(["xdg-open", os.path.dirname(row.item["path"])],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.mostrar_mensagem("Não foi possível abrir o gerenciador de arquivos.")
        item_open.connect("activate", _open)
        for it in (item_play, item_q, item_pl, item_like, Gtk.SeparatorMenuItem(), item_meta, item_open):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ---------- escolher pasta e indexar ----------
    def on_lib_choose_folder(self, button=None):
        dialog = Gtk.FileChooserDialog(title="Escolher pasta de músicas", parent=self,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        dialog.add_button("Escolher", Gtk.ResponseType.OK)
        start = self.lib_root if self._lib_valid_root() else None
        if not start:
            for cand in ("~/Música", "~/Músicas", "~/Music", "~"):
                c = os.path.expanduser(cand)
                if os.path.isdir(c):
                    start = c
                    break
        if start:
            dialog.set_current_folder(start)
        response = dialog.run()
        folder = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not folder:
            return
        self.lib_root = os.path.abspath(folder)
        self.lib_dir = self.lib_root
        self.config["library_root"] = self.lib_root
        self._schedule_config_save()
        self.lib_index = {}
        self._lib_render()
        self._render_home_tiles()
        self._lib_scan_start(force=True)

    def _lib_startup(self):
        if self._lib_valid_root():
            self._lib_scan_start(force=False)
        return False

    def _lib_scan_start(self, force=False):
        if not self._lib_valid_root():
            self.mostrar_mensagem("Escolha uma pasta local primeiro.")
            return
        if self._lib_scanning and not force:
            self.show_toast("Indexação já em andamento.")
            return
        self._lib_scan_token += 1
        token = self._lib_scan_token
        self._lib_scanning = True
        self.lib_status.set_text("Indexando biblioteca...")
        threading.Thread(target=self._lib_scan_thread, args=(self.lib_root, dict(self.lib_index), token),
                         daemon=True).start()

    def _lib_scan_thread(self, root, old, token):
        """Varre a pasta. Só relê tags de arquivos novos/alterados (mtime+tamanho)."""
        found = []
        for base, dirs, names in os.walk(root, followlinks=True):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                if n.lower().endswith(AUDIO_EXTS) and not n.startswith("."):
                    found.append(os.path.join(base, n))
        new_index, todo = {}, []
        for path in found:
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = old.get(path)
            if prev and prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size:
                new_index[path] = prev
            else:
                todo.append((path, st))
        total, done = len(found), len(new_index)

        def work(pair):
            path, st = pair
            meta = read_local_track(path)
            meta["mtime"], meta["size"] = st.st_mtime, st.st_size
            return path, meta

        if todo:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(work, pair) for pair in todo]
                for fut in as_completed(futures):
                    if token != self._lib_scan_token:
                        for f in futures:
                            f.cancel()
                        return
                    try:
                        path, meta = fut.result()
                        new_index[path] = meta
                    except Exception:
                        pass
                    done += 1
                    if done % 25 == 0:
                        GLib.idle_add(self.lib_status.set_text, f"Indexando... {done}/{total}")
        if token != self._lib_scan_token:
            return
        if todo or set(new_index) != set(old):      # nada novo/removido: não regrava o índice (pode ter MBs)
            self._save_json(LIBRARY_FILE, {"root": root, "tracks": new_index}, compact=True)
        GLib.idle_add(self._lib_scan_done, new_index, token)

    def _lib_scan_done(self, index, token):
        if token != self._lib_scan_token:
            return False
        self.lib_index = index
        self._lib_scanning = False
        if self.db is not None:
            self.db.sync_local(index.values(), self._lib_file_sig())   # busca FTS5: só trabalha se algo mudou
        self._lib_render()
        self._render_home_tiles()
        self.show_toast(f"Biblioteca atualizada: {len(index)} música(s).")
        self._lib_enrich_start()
        return False

    # ---------- completar metadados online (Deezer) ----------
    def _on_lib_online_toggled(self, btn):
        self.config["library_online_meta"] = btn.get_active()
        self._schedule_config_save()
        if btn.get_active():
            self._lib_enrich_start()

    def _lib_needs_online(self, e):
        return not (e.get("uploader") and e.get("album")) and not e.get("online_checked")

    def _lib_enrich_start(self, paths=None, force=False):
        if not self.config.get("library_online_meta", True) and not force:
            return
        if self._lib_scanning or self._lib_enriching or not self.lib_index:
            if force:
                self.show_toast("Aguarde: a biblioteca está sendo processada.")
            return
        if paths is None:
            paths = [pth for pth, e in self.lib_index.items() if self._lib_needs_online(e)]
        paths = [pth for pth in paths if pth in self.lib_index]
        if not paths:
            if force:
                self.show_toast("Nada para buscar.")
            return
        self._lib_enriching = True
        token = self._lib_scan_token
        threading.Thread(target=self._lib_enrich_thread, args=(paths, token, force), daemon=True).start()

    def _lib_enrich_thread(self, paths, token, force):
        found = checked = errors = 0
        total = len(paths)
        try:
            for n, path in enumerate(paths, start=1):
                if token != self._lib_scan_token:
                    return
                entry = self.lib_index.get(path)
                if not entry:
                    continue
                if entry.get("online_checked") and not force:
                    continue
                GLib.idle_add(self.lib_status.set_text, f"Buscando dados online... {n}/{total}")
                try:
                    res = self._lib_lookup_online(entry)
                except Exception:
                    errors += 1
                    if errors >= 3:          # sem internet: para e tenta de novo numa próxima vez
                        GLib.idle_add(self.show_toast, "Sem conexão: dados online ficam para depois.")
                        break
                    continue
                errors = 0
                checked += 1
                if res and self._lib_apply_online(entry, res):
                    found += 1
                entry["online_checked"] = True
                if checked % 20 == 0:
                    self._save_json(LIBRARY_FILE, {"root": self.lib_root, "tracks": dict(self.lib_index)}, compact=True)
                time.sleep(0.25)             # respeita o limite de requisições do Deezer
            if checked and token == self._lib_scan_token:
                self._save_json(LIBRARY_FILE, {"root": self.lib_root, "tracks": dict(self.lib_index)}, compact=True)
                GLib.idle_add(self._lib_enrich_done, found, checked)
        finally:
            GLib.idle_add(self._lib_enrich_finished)

    def _lib_enrich_finished(self):
        self._lib_enriching = False
        if not self._lib_scanning and self._lib_valid_root():
            self.lib_status.set_text(f"Biblioteca: {self.lib_root}")
        return False

    def _lib_enrich_done(self, found, checked):
        if self.db is not None and found:
            self.db.sync_local(self.lib_index.values(), self._lib_file_sig())
        self.show_toast(f"Dados online: {found} de {checked} música(s) completadas.")
        if self.main_stack.get_visible_child_name() == "offline":
            self._lib_render()
        self._render_home_recents()
        return False

    def _lib_lookup_online(self, entry):
        """Melhor faixa do Deezer para um arquivo local, ou None. Conservador: exige título parecido,
        artista compatível e duração próxima, para não trazer a faixa errada."""
        title = self._clean_title(entry.get("title") or "")
        artist = (entry.get("uploader") or "").strip()
        if not title:
            return None
        dur = _clock_to_seconds(entry.get("duration"))
        tkey = self._norm_key(re.sub(r"[\(\[].*?[\)\]]", " ", title))
        akey = self._norm_key(artist)
        if not tkey:
            return None
        queries = ([f'artist:"{artist}" track:"{title}"'] if artist else []) + [f"{artist} {title}".strip()]
        seen, best, best_score = set(), None, 0.0
        for q in queries:
            data = self._http_json(f"{DEEZER_API}/search?q={urllib.parse.quote(q)}&limit=10").get("data") or []
            for t in data:
                if t.get("id") in seen:
                    continue
                seen.add(t.get("id"))
                ck = self._norm_key(re.sub(r"[\(\[].*?[\)\]]", " ", t.get("title", "")))
                if not ck:
                    continue
                ratio = 1.0 if ck == tkey else difflib.SequenceMatcher(None, ck, tkey).ratio()
                if ratio < 0.85:
                    continue
                cak = self._norm_key((t.get("artist") or {}).get("name", ""))
                if akey:
                    if not (cak == akey or (len(cak) >= 3 and cak in akey) or (len(akey) >= 3 and akey in cak)):
                        continue
                    score, max_diff = 10.0, 10
                else:                        # sem artista: só aceita casamento quase exato (título + duração)
                    if ratio < 0.95 or not dur:
                        continue
                    score, max_diff = 5.0, 4
                dz = t.get("duration") or 0
                if dur and dz:
                    diff = abs(dur - dz)
                    if diff > max_diff:
                        continue
                    score += 4 if diff <= 3 else 1
                score += ratio * 3
                if score > best_score:
                    best, best_score = t, score
            if best:
                break
        if not best:
            return None
        album = best.get("album") or {}
        year = ""
        if album.get("id"):
            year = self._lib_album_cache.get(album["id"])
            if year is None:
                try:
                    year = (self._http_json(f"{DEEZER_API}/album/{album['id']}", timeout=6)
                            .get("release_date") or "")[:4]
                except Exception:
                    year = ""
                self._lib_album_cache[album["id"]] = year
        return {
            "artist": (best.get("artist") or {}).get("name", ""),
            "album": album.get("title", ""),
            "year": year or "",
            "cover_url": album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or "",
        }

    def _lib_apply_online(self, entry, res):
        """Preenche só o que falta. Álbum/ano/capa só entram se o álbum não contradisser as tags."""
        filled = []
        if not entry.get("uploader") and res.get("artist"):
            entry["uploader"] = entry["artist"] = res["artist"]
            filled.append("artist")
        cur_album = self._norm_key(entry.get("album") or "")
        new_album = self._norm_key(res.get("album") or "")
        album_ok = not cur_album or (new_album and (cur_album == new_album or cur_album in new_album
                                                     or new_album in cur_album))
        if album_ok:
            if not entry.get("album") and res.get("album"):
                entry["album"] = res["album"]
                filled.append("album")
            if not entry.get("year") and res.get("year"):
                entry["year"] = res["year"]
                filled.append("year")
            if res.get("cover_url"):
                entry["cover_url"] = res["cover_url"]
        if filled:
            entry["online_fields"] = filled
        return bool(filled or entry.get("cover_url"))

    def _build_tab_history(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_box.set_border_width(20)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="xx-large" weight="bold">Recentes</span>')
        tab_box.pack_start(title, False, False, 0)

        hist_scroll = Gtk.ScrolledWindow()
        hist_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_list = Gtk.ListBox()
        self.history_list.set_activate_on_single_click(False)
        self.history_list.connect("row-activated", self.on_history_activated)
        self.history_list.connect("button-press-event", self.on_history_button_press)
        self.history_list.set_placeholder(self._placeholder("Nenhuma faixa tocada ainda."))
        hist_scroll.add(self.history_list)
        tab_box.pack_start(hist_scroll, True, True, 0)

        btn_clear_hist = Gtk.Button(label="Apagar Histórico")
        btn_clear_hist.connect("clicked", self.on_clear_history)
        tab_box.pack_start(btn_clear_hist, False, False, 0)
        self.main_stack.add_named(tab_box, "history")

    def _build_wiki_album_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(20)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_back = Gtk.Button(label="Voltar")
        btn_back.set_image(Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON))
        btn_back.set_always_show_image(True)
        btn_back.set_valign(Gtk.Align.CENTER)
        btn_back.connect("clicked", self.on_wiki_album_back)
        head.pack_start(btn_back, False, False, 0)

        self.album_cover_img = Gtk.Image.new_from_icon_name("media-optical", Gtk.IconSize.DIALOG)
        self.album_cover_img.set_pixel_size(64)
        head.pack_start(self.album_cover_img, False, False, 0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        self.album_title_label = Gtk.Label(xalign=0)
        self.album_title_label.set_line_wrap(True)
        self.album_meta_label = Gtk.Label(xalign=0)
        self.album_meta_label.get_style_context().add_class("dim-label")
        texts.pack_start(self.album_title_label, False, False, 0)
        texts.pack_start(self.album_meta_label, False, False, 0)
        head.pack_start(texts, True, True, 0)

        self.album_spinner = Gtk.Spinner()
        head.pack_start(self.album_spinner, False, False, 0)

        btn_album_pl = Gtk.Button(label="Adicionar faixas na Playlist")
        btn_album_pl.set_valign(Gtk.Align.CENTER)
        btn_album_pl.set_tooltip_text("Usa as faixas selecionadas (ou o álbum inteiro, se nada estiver selecionado)")
        btn_album_pl.connect("clicked", self.on_album_add_to_playlist)
        head.pack_end(btn_album_pl, False, False, 0)
        page.pack_start(head, False, False, 0)
        page.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.album_tracks_list = Gtk.ListBox()
        self.album_tracks_list.set_activate_on_single_click(False)
        self.album_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.album_tracks_list.connect("row-activated", self.on_album_track_activated)
        self.album_tracks_list.connect("button-press-event", self.on_album_tracks_button_press)
        scroll.add(self.album_tracks_list)
        page.pack_start(scroll, True, True, 0)

        btns = Gtk.Box(spacing=8)
        tip = "Usa as faixas selecionadas (ou o álbum inteiro, se nada estiver selecionado)"
        btn_play = Gtk.Button(label="Reproduzir")
        btn_play.connect("clicked", lambda w: self.on_album_track_button("play"))
        btn_add = Gtk.Button(label="+ Fila")
        btn_add.connect("clicked", lambda w: self.on_album_track_button("queue"))
        btn_dl = Gtk.Button(label="Baixar")
        btn_dl.connect("clicked", lambda w: self.on_album_track_button("download"))
        for b in (btn_play, btn_add, btn_dl):
            b.set_tooltip_text(tip)
            btns.pack_start(b, False, False, 0)
        page.pack_start(btns, False, False, 0)
        return page

    # ======================================================================
    # Navegação
    # ======================================================================
    def _current_view(self, page=None):
        return self.main_stack.get_visible_child_name()

    def _select_nav(self, name):
        for row in self.nav_list.get_children():
            if getattr(row, "nav_name", None) == name:
                self.nav_list.select_row(row)
                return
        self.nav_list.unselect_all()

    def _after_nav(self, name):
        self.btn_back.set_sensitive(bool(self._nav_history))
        self._select_nav(name)
        if name == "home":
            self._refresh_home_greeting()
        elif name == "offline":
            self._lib_render()
        elif name == "webradio":
            if not self._webradio_loaded:
                self.on_webradio_search()          # 1ª visita: top rádios do Brasil
        elif name == "mylib":
            self.mylib_entry.grab_focus()
            self._mylib_run()

    def navigate(self, name):
        cur = self.main_stack.get_visible_child_name()
        if cur != name:
            self._nav_history.append(cur)
            del self._nav_history[:-30]
        self.main_stack.set_visible_child_name(name)
        self._after_nav(name)
        if name == "home":
            self._home_scroll_top()
        elif name == "artist":
            self._scroll_top(self.artist_scroll)

    def _scroll_top(self, sw):
        adj = sw.get_vadjustment()
        adj.set_value(adj.get_lower())

    def _home_scroll_top(self):
        self._scroll_top(self.home_scroll)

    def go_back(self, button=None):
        if not self._nav_history:
            return
        if self.main_stack.get_visible_child_name() == "album":
            self.album_token += 1
            self.album_spinner.stop()
        name = self._nav_history.pop()
        self.main_stack.set_visible_child_name(name)
        self._after_nav(name)

    def show_side_panel(self, name, toggle=True):
        revealed = self.side_revealer.get_reveal_child()
        if toggle and revealed and self.side_stack.get_visible_child_name() == name:
            self.side_revealer.set_reveal_child(False)
            revealed = False
        else:
            self.side_stack.set_visible_child_name(name)
            self.side_revealer.set_reveal_child(True)
            revealed = True
        self._panel_lock = True
        self.btn_queue_toggle.set_active(revealed and name == "queue")
        self.btn_lyrics_toggle.set_active(revealed and name == "lyrics")
        self._panel_lock = False

    def _on_panel_toggle(self, btn, name):
        if not self._panel_lock:
            self.show_side_panel(name)

    def on_open_now_artist(self, button=None):
        item = self._current_item()
        if not item:
            self.mostrar_mensagem("Nenhuma faixa tocando no momento.")
            return
        self._open_artist_in_wiki(self._guess_artist(item))

    def _open_playlist(self, name):
        if name not in self.playlists:
            return
        self.selected_playlist = name
        self.render_playlist_tracks()
        for row in self.playlists_list.get_children():
            if getattr(row, "pl_name", None) == name:
                self.playlists_list.select_row(row)
                break
        self.navigate("playlist")

    def on_rename_current_playlist(self, button=None):
        if self.selected_playlist:
            self._rename_playlist(self.selected_playlist)

    def _current_item(self):
        if 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return None

    def _update_now_playing_card(self, item):
        artist = self._guess_artist(item) or item.get("uploader", "")
        self.now_artist_lbl.set_text(artist)
        self.now_artist_btn.set_sensitive(bool(artist))
        self.now_artist_btn.set_opacity(1.0 if artist else 0.0)
        self._refresh_hearts()

    def _current_item(self):
        if 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return None

    # ======================================================================
    # Favoritas (curtir músicas)
    # ======================================================================
    def _fav_keys(self):
        """Conjunto de chaves das favoritas. Memoizado: antes era recalculado para cada coração
        criado (O(linhas x favoritas)); agora só quando a lista muda (id/tamanho ou invalidação explícita)."""
        sig = (id(self.favorites), len(self.favorites))
        cached = getattr(self, "_fav_keys_cache", None)
        if cached is not None and cached[0] == sig:
            return cached[1]
        keys = {self._track_key(t) for t in self.favorites}
        self._fav_keys_cache = (sig, keys)
        return keys

    def _fav_keys_invalidate(self):
        self._fav_keys_cache = None

    def _heart_set(self, btn, liked):
        liked = bool(liked)
        if getattr(btn, "_liked", None) is liked and getattr(btn, "_heart_tip_for", None) == (btn.fav_item is None):
            return      # nada mudou: evita set_markup/tooltip (caro com milhares de linhas)
        btn._liked = liked
        btn._heart_tip_for = (btn.fav_item is None)
        lbl = btn.get_child()
        if liked:
            lbl.set_markup('<span size="large" foreground="#e0245e">♥</span>')
        else:
            lbl.set_markup('<span size="large">♡</span>')
        btn.set_tooltip_text("Remover das Favoritas (L)" if btn.fav_item is None and liked
                             else "Curtir (L)" if btn.fav_item is None
                             else "Remover das Favoritas" if liked else "Curtir")

    def _refresh_heart(self, btn, keys=None):
        if keys is None:
            keys = self._fav_keys()
        item = btn.fav_item or self._current_item()
        liked = bool(item) and self._track_key(item) in keys
        self._heart_set(btn, liked)
        if btn.fav_item is None:
            btn.set_sensitive(item is not None)

    def _refresh_hearts(self):
        keys = self._fav_keys()
        for btn in list(self._hearts):
            try:
                self._refresh_heart(btn, keys)
            except Exception:
                pass

    def _offline_badge(self, item):
        """Ícone ⬇ para faixas disponíveis offline (arquivo local). Vazio para as demais."""
        lbl = Gtk.Label()
        lbl.set_valign(Gtk.Align.CENTER)
        if item and item.get("path"):
            if os.path.isfile(item["path"]):
                lbl.set_markup('<span size="large" foreground="#2e9e5b">⬇</span>')
                lbl.set_tooltip_text("Disponível offline")
            else:
                lbl.set_markup('<span size="large" foreground="#999999">⬇</span>')
                lbl.set_tooltip_text("Arquivo offline não encontrado")
        lbl.set_margin_start(2)
        lbl.set_margin_end(2)
        lbl.show()
        return lbl

    def _make_heart(self, item=None):
        """Botão ♡/♥. Com item=None acompanha a faixa que está tocando."""
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_valign(Gtk.Align.CENTER)
        btn.add(Gtk.Label())
        btn.fav_item = item
        btn.connect("clicked", self._on_heart_clicked)
        self._hearts.add(btn)
        self._refresh_heart(btn)
        btn.show_all()
        return btn

    def _on_heart_clicked(self, btn):
        item = btn.fav_item or self._current_item()
        if not item:
            self.mostrar_mensagem("Nenhuma faixa tocando no momento.")
            return
        self.toggle_favorite(item)

    def on_like_current(self, button=None):
        item = self._current_item()
        if not item:
            self.mostrar_mensagem("Nenhuma faixa tocando no momento.")
            return
        self.toggle_favorite(item)

    def toggle_favorite(self, item):
        item = self._normalize_track(item)
        key = self._track_key(item)
        for i, t in enumerate(self.favorites):
            if self._track_key(t) == key:
                self.favorites.pop(i)
                self.show_toast(f"Removida das Favoritas: {item['title']}")
                break
        else:
            self.favorites.insert(0, item)
            self.show_toast(f"♥ Adicionada às Favoritas: {item['title']}")
        self._fav_keys_invalidate()
        self._save_json(FAVORITES_FILE, self.favorites)
        self._refresh_favorites_ui()

    def set_favorites(self, items, liked):
        """Curte/descurte várias faixas de uma vez (menus de botão direito)."""
        self._fav_keys_invalidate()
        keys = set(self._fav_keys())    # cópia: este laço altera o conjunto
        changed = 0
        for it in items:
            it = self._normalize_track(it)
            k = self._track_key(it)
            if liked and k not in keys:
                self.favorites.insert(0, it)
                keys.add(k)
                changed += 1
            elif not liked and k in keys:
                self.favorites = [t for t in self.favorites if self._track_key(t) != k]
                keys.discard(k)
                changed += 1
        if changed:
            self._fav_keys_invalidate()
            self._save_json(FAVORITES_FILE, self.favorites)
            self._refresh_favorites_ui()
            self.show_toast(f"{changed} faixa(s) {'adicionada(s) às' if liked else 'removida(s) das'} Favoritas.")

    def _like_menu_item(self, items, resolver=None):
        """Item de menu Curtir/Descurtir. `resolver` converte faixas do Deezer em faixas do YouTube."""
        items = [i for i in items if isinstance(i, dict)]
        if resolver is None:
            keys = self._fav_keys()
            all_liked = bool(items) and all(self._track_key(self._normalize_track(i)) in keys for i in items)
            mi = Gtk.MenuItem(label="Remover das Favoritas" if all_liked else "♥ Curtir")
            mi.connect("activate", lambda w: self.set_favorites(items, not all_liked))
        else:
            mi = Gtk.MenuItem(label="♥ Curtir")
            mi.connect("activate", lambda w: resolver(items, lambda resolved: self.set_favorites(resolved, True)))
        return mi

    def _refresh_favorites_ui(self):
        self._refresh_hearts()
        if getattr(self, "_fav_render_pending", False):
            return                      # várias curtidas seguidas viram uma única reconstrução
        self._fav_render_pending = True
        GLib.idle_add(self._render_favorites_once)

    def _render_favorites_once(self):
        self._fav_render_pending = False
        return self.render_favorites()

    def _make_fav_row(self, item):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lbl = Gtk.Label(label=f"{item['title']}  —  {item.get('uploader','')} ({item.get('duration','')})", xalign=0)
        lbl.set_ellipsize(3)
        box.pack_start(lbl, True, True, 6)
        box.pack_start(self._offline_badge(item), False, False, 0)
        box.pack_start(self._make_heart(item), False, False, 0)
        row.add(box)
        row.item = item
        return row

    def render_favorites(self):
        """Contador e vitrine do Início atualizam sempre; a lista de linhas só é montada com a aba
        Favoritas visível (e em lotes), em vez de reconstruir milhares de widgets a cada curtida."""
        self._fav_gen += 1
        n = len(self.favorites)
        self.fav_count_label.set_text("Nenhuma música curtida" if n == 0 else "1 música" if n == 1 else f"{n} músicas")
        self._render_home_tiles()
        if self.main_stack.get_visible_child_name() != "favorites":
            self._fav_dirty = True
            return False
        self._fav_dirty = False
        for child in list(self.favorites_list.get_children()):
            self.favorites_list.remove(child)
        self._fav_fill(self._fav_gen, 0)
        return False

    def _fav_fill(self, gen, start):
        if gen != self._fav_gen:
            return False
        items = self.favorites
        end = min(len(items), start + (self.CHUNK_FIRST if start == 0 else self.CHUNK_STEP))
        for item in items[start:end]:
            row = self._make_fav_row(item)
            self.favorites_list.add(row)
            row.show_all()
        if end < len(items):
            GLib.idle_add(self._fav_fill, gen, end, priority=GLib.PRIORITY_DEFAULT_IDLE)
        return False

    def _on_main_page_changed(self, stack, _pspec):
        if self._fav_dirty and stack.get_visible_child_name() == "favorites":
            self.render_favorites()

    def _favorites_items_for_action(self):
        rows = self.favorites_list.get_selected_rows()
        if rows:
            return [r.item for r in rows if hasattr(r, "item")]
        return list(self.favorites)      # sem seleção = todas (não depende de a lista já estar montada)

    def on_favorite_activated(self, listbox, row):
        if hasattr(row, "item"):
            self.play_item(row.item)

    def on_favorites_add_to_queue(self, button=None):
        items = self._favorites_items_for_action()
        if not items:
            self.mostrar_mensagem("Você ainda não curtiu nenhuma música.")
            return
        self.add_items_bulk(items)

    def _play_tracks(self, tracks, label, shuffle=False):
        tracks = list(tracks)
        if not tracks:
            self.mostrar_mensagem("Você ainda não curtiu nenhuma música." if label == "Favoritas" else "Não há faixas para tocar.")
            return
        if shuffle:
            random.shuffle(tracks)
        self.stop_radio(notify=False)
        self.queue = tracks
        self._save_queue()
        self.render_queue()
        self.current_index = 0
        self.play_current()
        self.show_toast(f"Tocando: {label}" + (" (aleatório)" if shuffle else ""))

    def on_save_favorites_as_playlist(self, button=None):
        if not self.favorites:
            self.mostrar_mensagem("Você ainda não curtiu nenhuma música.")
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL, text="Salvar Favoritas como Playlist",
        )
        dialog.format_secondary_text("Nome da nova playlist (uma cópia das suas favoritas de agora):")
        entry = Gtk.Entry()
        entry.set_text("Minhas Favoritas")
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().pack_start(entry, True, True, 6)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not name:
            return
        if name in self.playlists and not self._confirm_action(f"A playlist '{name}' já existe. Substituir o conteúdo?"):
            return
        self.playlists[name] = list(self.favorites)
        self._save_json(PLAYLISTS_FILE, self.playlists)
        self.render_playlists()
        self.show_toast(f"Favoritas salvas como '{name}'!")

    def on_favorites_button_press(self, widget, event):
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, "item"):
            return False
        self._ensure_row_selected(widget, row)
        sel = [r.item for r in widget.get_selected_rows()]
        menu = Gtk.Menu()
        item_play = Gtk.MenuItem(label="Reproduzir")
        item_play.connect("activate", lambda w: self.play_item(row.item))
        item_q = Gtk.MenuItem(label="Adicionar à Fila")
        item_q.connect("activate", lambda w: self.add_items_bulk(sel))
        item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
        item_pl.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=sel))
        item_dl = Gtk.MenuItem(label="Baixar")
        item_dl.connect("activate", lambda w: self.on_download(None))
        item_rm = Gtk.MenuItem(label="Remover das Favoritas")
        item_rm.connect("activate", lambda w: self.set_favorites(sel, False))
        for it in (item_play, item_q, self._radio_menu_item(row.item), item_pl, item_dl, Gtk.SeparatorMenuItem(), item_rm):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ---------- Manipulação Atômica de JSON e Arquivos ----------
    def _load_json(self, path, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _save_queue(self):
        """Grava a fila com atraso (debounce): adicionar muitas faixas seguidas não regrava o arquivo a cada uma."""
        if self._preload is not None:     # a fila mudou: confere (após a mutação terminar) se o plano ainda vale
            GLib.idle_add(self._check_preload_still_valid)
        if self._queue_save_id:
            GLib.source_remove(self._queue_save_id)
        self._queue_save_id = GLib.timeout_add(400, self._flush_queue_save)

    def _flush_queue_save(self):
        self._queue_save_id = None
        self._save_json(QUEUE_FILE, self.queue, compact=True)
        return False

    def _open_database(self):
        self.db = None
        try:
            self.db = LibraryDB(DB_FILE, {"playlists": PLAYLISTS_FILE, "favorites": FAVORITES_FILE,
                                          "history": HISTORY_FILE})
        except Exception as exc:
            print(f"[{APP_NAME}] banco SQLite indisponível, usando JSON: {exc}", file=sys.stderr)
            self.db = None

    def _db_load(self, what):
        """Playlists/favoritas/histórico: do SQLite; sem banco, dos .json (ou do .bak deixado pela migração)."""
        if self.db is not None:
            try:
                return {"playlists": self.db.load_playlists, "favorites": self.db.load_favorites,
                        "history": self.db.load_history}[what]()
            except Exception as exc:
                print(f"[{APP_NAME}] falha ao ler o banco ({what}): {exc}", file=sys.stderr)
                self.db = None
        path, default = {"playlists": (PLAYLISTS_FILE, {}), "favorites": (FAVORITES_FILE, []),
                         "history": (HISTORY_FILE, [])}[what]
        data = self._load_json(path, None)
        return data if data is not None else self._load_json(path + ".bak", default)

    def _save_json(self, path, data, compact=False):
        """Playlists/favoritas/histórico vão para o SQLite (gravação incremental em segundo plano).
        Os demais arquivos: JSON atômico (arquivo temporário + os.replace) para evitar corrupção.
        compact=True (fila, biblioteca): sem indentação — arquivo menor e gravação mais rápida."""
        if self.db is not None and path in (PLAYLISTS_FILE, FAVORITES_FILE, HISTORY_FILE):
            try:
                if path == PLAYLISTS_FILE:
                    self.db.save_playlists(data)
                elif path == FAVORITES_FILE:
                    self.db.save_favorites(data)
                else:
                    self.db.save_history(data)
                return
            except Exception as exc:
                print(f"[{APP_NAME}] falha ao gravar no banco, usando JSON: {exc}", file=sys.stderr)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                if compact:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                else:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---------- Normalização do Esquema de Dados de Faixas ----------
    def _clean_track_list(self, data):
        """Lista vinda de um JSON (possivelmente corrompido/editado à mão): só dicionários válidos entram."""
        out = []
        if isinstance(data, list):
            for t in data:
                if isinstance(t, dict):
                    try:
                        out.append(self._normalize_track(t))
                    except Exception:
                        pass
        return out

    def _normalize_track(self, item):
        return normalize_track(item)

    # ---------- Mensagens e Utilitários ----------
    # Estilo da linha de status: pequeno, fino e translúcido (segue o tema claro/escuro)
    _STATUS_FMT = '<span size="small" weight="light" letter_spacing="400" alpha="75%">{}</span>'

    def mostrar_mensagem(self, texto):
        self._flash_status(texto, 4.0)

    def _flash_status(self, texto, base_secs):
        """Mostra 'texto' na linha de status e some com um fade suave."""
        for attr in ("_msg_timeout_id", "_msg_fade_id"):
            sid = getattr(self, attr, None)
            if sid:
                try:
                    GLib.source_remove(sid)
                except Exception:
                    pass
                setattr(self, attr, None)
        texto = (texto or "").strip()
        if not texto:
            self._clear_mensagem()
            return
        self.infobar_label.set_markup(self._STATUS_FMT.format(GLib.markup_escape_text(texto)))
        self.infobar_label.set_tooltip_text(texto)
        self.infobar_label.set_opacity(1.0)
        hold = max(base_secs, min(8.0, len(texto) / 14.0))   # mensagens longas ficam um pouco mais
        self._msg_timeout_id = GLib.timeout_add(int(hold * 1000), self._start_fade_mensagem)

    def _start_fade_mensagem(self):
        self._msg_timeout_id = None
        self._msg_fade_id = GLib.timeout_add(30, self._fade_step_mensagem)
        return False

    def _fade_step_mensagem(self):
        op = self.infobar_label.get_opacity() - 0.1
        if op <= 0.0:
            self._clear_mensagem()
            return False
        self.infobar_label.set_opacity(op)
        return True

    def _clear_mensagem(self):
        self._msg_timeout_id = None
        self._msg_fade_id = None
        # texto em branco com o mesmo tamanho de fonte: a altura da linha nunca muda
        self.infobar_label.set_markup(self._STATUS_FMT.format(" "))
        self.infobar_label.set_tooltip_text(None)
        self.infobar_label.set_opacity(1.0)
        return False

    def _confirm_action(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=message,
        )
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def show_toast(self, texto):
        self._flash_status(texto, 3.0)

    def _section_label(self, text):
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        return lbl

    def _fmt_duration(self, seconds):
        if not seconds:
            return "0:00"
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _extract_id(self, uri):
        m = re.search(r"v=([^&]+)", uri)
        if m:
            return m.group(1)
        m = re.search(r"youtu\.be/([a-zA-Z0-9_-]+)", uri)
        if m:
            return m.group(1)
        return None

    # ---------- Normalização de texto e nomes ----------
    @staticmethod
    def _unaccent(text):
        return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))

    def _norm_words(self, text):
        return re.findall(r"[a-z0-9]+", self._unaccent(text).lower())

    def _norm_key(self, text):
        return "".join(self._norm_words(text))

    def _clean_artist_name(self, name):
        n = (name or "").strip()
        n = re.sub(r"(?i)\s*-\s*topic$", "", n)
        if len(n) > 5:
            n = re.sub(r"(?i)\s*vevo$", "", n)
        n = re.sub(r"(?i)\s*(canal\s+oficial|official(\s+channel)?|oficial)$", "", n)
        n = re.split(r"(?i)\s+(?:feat\.?|ft\.?|featuring|part\.?)\s+", n)[0]
        return n.strip(" -–—|")

    def _tidy_yt_metadata(self, title, uploader):
        """Limpa título/canal do YouTube quando não há correspondência no Deezer."""
        title = self._clean_title(title)
        artist = self._clean_artist_name(uploader)
        parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
        if len(parts) == 2 and 0 < len(parts[0]) <= 40 and parts[1].strip():
            left, right = parts
            lk, ak = self._norm_key(left), self._norm_key(artist)
            if not ak or (lk and (lk in ak or ak in lk)):
                artist, title = self._clean_artist_name(left), right.strip()
        return (title or "Sem título"), artist

    def _guess_artist(self, item):
        """Melhor palpite do artista de uma faixa (para Wiki/Descobrir).

        Faixas verificadas (Deezer) e faixas offline (artista vindo das tags do
        arquivo) têm o campo de artista confiável. Só para itens do YouTube sem
        correspondência é que se tenta deduzir o artista pelo título "Artista - Música".
        """
        known = item.get("artist") or item.get("uploader") or ""
        if (item.get("verified") or item.get("path")) and known:
            return self._clean_artist_name(known)
        parts = re.split(r"\s+[-–—]\s+", item.get("title", ""), maxsplit=1)
        if len(parts) == 2 and 0 < len(parts[0]) <= 40:
            name = self._clean_artist_name(parts[0])
            if name:
                return name
        return self._clean_artist_name(known)

    def _notify(self, title, message, icon="audio-x-generic"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", icon or "audio-x-generic", title, message], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _check_ytdlp_update(self):
        """Atualiza o yt-dlp em segundo plano, no máximo 1x por dia (antes: a cada abertura, competindo
        com a montagem da interface em PCs fracos)."""
        try:
            now = time.time()
            if now - float(self.config.get("ytdlp_checked", 0) or 0) < 86400:
                return
            self.config["ytdlp_checked"] = now
            self._schedule_config_save()
            subprocess.run(["yt-dlp", "-U"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            pass

    def _start_ytdlp_check(self):
        threading.Thread(target=self._check_ytdlp_update, daemon=True).start()
        return False

    # ---------- Gerenciamento de Playlists ----------
    def render_playlists(self):
        for child in list(self.playlists_list.get_children()):
            self.playlists_list.remove(child)

        for pl_name in sorted(self.playlists.keys()):
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=f"🎵 {pl_name}", xalign=0)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            row.add(lbl)
            row.pl_name = pl_name
            row.pl_key = self._norm_key(pl_name)
            self.playlists_list.add(row)

        self.playlists_list.show_all()
        self._render_home_tiles()

    def _playlist_filter_func(self, row, *_args):
        key = self._pl_filter_key
        return (not key) or key in getattr(row, "pl_key", "")

    def on_playlist_filter_changed(self, entry):
        self._pl_filter_key = self._norm_key(entry.get_text())
        self.playlists_list.invalidate_filter()

    def on_playlist_selected(self, listbox, row):
        if not row:
            return
        self._open_playlist(row.pl_name)

    def render_playlist_tracks(self):
        for child in list(self.pl_tracks_list.get_children()):
            self.pl_tracks_list.remove(child)

        if not self.selected_playlist or self.selected_playlist not in self.playlists:
            self.pl_title_label.set_markup("<b>Selecione uma Playlist</b>")
            return

        tracks = self.playlists[self.selected_playlist]
        title_txt = f"{self.selected_playlist}  ·  {len(tracks)} faixas"
        self.pl_title_label.set_markup(f'<span size="xx-large" weight="bold">{GLib.markup_escape_text(title_txt)}</span>')

        for i, item in enumerate(tracks):
            row = self._create_playlist_track_row(i, item)
            self.pl_tracks_list.add(row)

        self.pl_tracks_list.show_all()

    def _create_playlist_track_row(self, i, item):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lbl = Gtk.Label(label=f"{i+1}. {item['title']} ({item.get('duration','')})", xalign=0)
        lbl.set_ellipsize(3)
        box.pack_start(lbl, True, True, 6)

        btn_del = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.MENU)
        btn_del.set_relief(Gtk.ReliefStyle.NONE)
        btn_del.connect("clicked", lambda b: self._remove_track_from_playlist(row))
        box.pack_start(self._offline_badge(item), False, False, 0)
        box.pack_start(self._make_heart(item), False, False, 0)
        box.pack_start(btn_del, False, False, 0)

        row.add(box)
        row.item = item
        return row

    def _remove_track_from_playlist(self, row):
        if self.selected_playlist in self.playlists:
            tracks = self.playlists[self.selected_playlist]
            idx = row.get_index()
            if 0 <= idx < len(tracks):
                tracks.pop(idx)
                self._save_json(PLAYLISTS_FILE, self.playlists)
                self.pl_tracks_list.remove(row)
                self._update_playlist_indices()

    def _update_playlist_indices(self):
        tracks = self.playlists.get(self.selected_playlist, [])
        for i, child in enumerate(self.pl_tracks_list.get_children()):
            if i < len(tracks):
                item = tracks[i]
                lbl = child.get_child().get_children()[0]
                lbl.set_text(f"{i+1}. {item['title']} ({item.get('duration','')})")
        title_txt = f"{self.selected_playlist}  ·  {len(tracks)} faixas"
        self.pl_title_label.set_markup(f'<span size="xx-large" weight="bold">{GLib.markup_escape_text(title_txt)}</span>')

    def on_create_playlist_dialog(self, button=None):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Nova Playlist",
        )
        dialog.format_secondary_text("Digite o nome da nova playlist:")
        entry = Gtk.Entry()
        dialog.get_content_area().pack_start(entry, True, True, 6)
        dialog.show_all()

        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and name:
            if name not in self.playlists:
                self.playlists[name] = []
                self._save_json(PLAYLISTS_FILE, self.playlists)
                self.render_playlists()
                self.show_toast(f"Playlist '{name}' criada!")
                return name
            else:
                self.mostrar_mensagem("Uma playlist com esse nome já existe.")
        return None

    def on_save_queue_as_playlist(self, button):
        if not self.queue:
            self.mostrar_mensagem("A fila está vazia.")
            return

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Salvar Fila",
        )
        dialog.format_secondary_text("Digite o nome para salvar a fila atual como playlist:")
        entry = Gtk.Entry()
        dialog.get_content_area().pack_start(entry, True, True, 6)
        dialog.show_all()

        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and name:
            self.playlists[name] = list(self.queue)
            self._save_json(PLAYLISTS_FILE, self.playlists)
            self.render_playlists()
            self.show_toast(f"Fila salva como '{name}'!")

    def _play_playlist_by_name(self, name):
        tracks = self.playlists.get(name)
        if tracks is None:
            return
        if not tracks:
            self.mostrar_mensagem("A playlist selecionada está vazia.")
            return
        self.stop_radio(notify=False)
        self.queue = list(tracks)
        self._save_queue()
        self.render_queue()
        self.current_index = 0
        self.play_current()
        self.show_toast(f"Tocando playlist: {name}")

    def _append_playlist_by_name(self, name):
        tracks = self.playlists.get(name)
        if tracks is None:
            return
        if not tracks:
            self.mostrar_mensagem("A playlist selecionada está vazia.")
            return
        self.add_items_bulk(tracks)

    def _delete_playlist_by_name(self, name):
        if name not in self.playlists:
            return
        if not self._confirm_action(f"Excluir a playlist '{name}'?"):
            return
        del self.playlists[name]
        self._save_json(PLAYLISTS_FILE, self.playlists)
        if self.selected_playlist == name:
            self.selected_playlist = None
        self.render_playlists()
        self.render_playlist_tracks()
        if self._current_view() == "playlist":
            self.go_back() if self._nav_history else self.navigate("home")
        self.show_toast("Playlist excluída.")

    def _rename_playlist(self, name):
        if name not in self.playlists:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Renomear Playlist",
        )
        dialog.format_secondary_text(f"Novo nome para '{name}':")
        entry = Gtk.Entry()
        entry.set_text(name)
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().pack_start(entry, True, True, 6)
        dialog.show_all()
        entry.select_region(0, -1)

        response = dialog.run()
        new_name = entry.get_text().strip()
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not new_name or new_name == name:
            return
        if new_name in self.playlists:
            self.mostrar_mensagem("Uma playlist com esse nome já existe.")
            return

        self.playlists[new_name] = self.playlists.pop(name)
        if self.selected_playlist == name:
            self.selected_playlist = new_name
        self._save_json(PLAYLISTS_FILE, self.playlists)
        self.render_playlists()
        self.render_playlist_tracks()
        for row in self.playlists_list.get_children():
            if getattr(row, "pl_name", None) == self.selected_playlist:
                self.playlists_list.select_row(row)
                break
        self.show_toast(f"Playlist renomeada para '{new_name}'")

    def on_play_entire_playlist(self, button):
        if self.selected_playlist:
            self._play_playlist_by_name(self.selected_playlist)

    def on_append_playlist_to_queue(self, button):
        if self.selected_playlist:
            self._append_playlist_by_name(self.selected_playlist)

    def on_delete_current_playlist(self, button):
        if self.selected_playlist:
            self._delete_playlist_by_name(self.selected_playlist)

    def on_playlists_button_press(self, widget, event):
        """Botão direito em 'Suas Playlists': tocar, adicionar à fila, renomear, excluir."""
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, "pl_name"):
            return False
        widget.select_row(row)
        self.selected_playlist = row.pl_name
        self.render_playlist_tracks()
        name = row.pl_name

        menu = Gtk.Menu()
        item_play = Gtk.MenuItem(label="Tocar playlist")
        item_play.connect("activate", lambda w: self._play_playlist_by_name(name))
        item_queue = Gtk.MenuItem(label="Adicionar à fila")
        item_queue.connect("activate", lambda w: self._append_playlist_by_name(name))
        item_rename = Gtk.MenuItem(label="Renomear")
        item_rename.connect("activate", lambda w: self._rename_playlist(name))
        item_delete = Gtk.MenuItem(label="Excluir")
        item_delete.connect("activate", lambda w: self._delete_playlist_by_name(name))
        for it in (item_play, item_queue, item_rename, Gtk.SeparatorMenuItem(), item_delete):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_pl_track_activated(self, listbox, row):
        if hasattr(row, "item"):
            self.play_item(row.item)

    def on_pl_tracks_button_press(self, widget, event):
        if event.button == 3:
            row = widget.get_row_at_y(int(event.y))
            if row is None or not hasattr(row, "item"):
                return False
            self._ensure_row_selected(widget, row)
            menu = Gtk.Menu()
            item_play = Gtk.MenuItem(label="Reproduzir")
            item_play.connect("activate", lambda w: self.play_item(row.item))
            item_add_q = Gtk.MenuItem(label="Adicionar à Fila")
            item_add_q.connect("activate", lambda w: self.add_to_queue(row.item))
            item_dl = Gtk.MenuItem(label="Baixar")
            item_dl.connect("activate", lambda w: self.on_download(None))
            item_like = self._like_menu_item([r.item for r in widget.get_selected_rows() if hasattr(r, "item")])
            for it in (item_play, item_add_q, self._radio_menu_item(row.item), item_like, item_dl):
                menu.append(it)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    # ---------- Busca e YouTube (Com Cancelamento) ----------
    def on_search(self, widget):
        query = self.search_entry.get_text().strip()
        if not query:
            return

        self.search_token += 1
        current_token = self.search_token

        self.search_spinner.start()
        self.mostrar_mensagem("Buscando...")
        self.navigate("search")
        self._apply_search_filter()
        is_link = query.startswith(("http://", "https://"))
        self.search_status.set_text("Resultados para “%s”" % query if not is_link else "Link colado")
        self._clear(self.search_artists_flow)
        self._clear(self.search_albums_flow)
        if is_link:
            self.search_artists_flow.add(self._placeholder("Não se aplica a links."))
            self.search_albums_flow.add(self._placeholder("Não se aplica a links."))
            self.search_artists_flow.show_all()
            self.search_albums_flow.show_all()
        else:
            threading.Thread(target=self._search_catalog_thread, args=(query, current_token), daemon=True).start()

        for child in list(self.results_list.get_children()):
            self.results_list.remove(child)

        threading.Thread(target=self._search_thread, args=(query, current_token), daemon=True).start()

    def _search_thread(self, query, token):
        is_url = query.startswith("http://") or query.startswith("https://")
        target = query if is_url else f"ytsearch12:{query}"

        # Metadados limpos do Deezer buscados em paralelo ao yt-dlp (sem somar espera)
        dz = {"tracks": []}
        dz_thread = None
        if not is_url:
            def _dz_worker():
                try:
                    url = f"{DEEZER_API}/search?q={urllib.parse.quote(query)}&limit=30"
                    dz["tracks"] = self._http_json(url, timeout=6).get("data", []) or []
                except Exception:
                    pass

            dz_thread = threading.Thread(target=_dz_worker, daemon=True)
            dz_thread.start()

        try:
            out = subprocess.check_output(
                ["yt-dlp", target, "--flat-playlist", "-j", "--no-warnings"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
            )
        except Exception as e:
            if token == self.search_token:
                GLib.idle_add(self._search_failed, str(e))
            return

        if dz_thread:
            dz_thread.join(timeout=3)

        items = []
        for line in out.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            vid_id = data.get("id") or self._extract_id(data.get("url", ""))
            if not vid_id:
                continue

            title = data.get("title") or "Sem título"
            uploader = data.get("uploader") or data.get("channel") or ""
            dur_s = data.get("duration") or 0

            match = self._match_deezer_track(title, uploader, dur_s, dz["tracks"])
            if match:
                title = match.get("title") or title
                uploader = (match.get("artist") or {}).get("name") or uploader
                verified = True
            else:
                title, uploader = self._tidy_yt_metadata(title, uploader)
                verified = False

            items.append(
                self._normalize_track(
                    {
                        "id": vid_id,
                        "title": title,
                        "uploader": uploader,
                        "duration": self._fmt_duration(dur_s),
                        "verified": verified,
                    }
                )
            )

        if token == self.search_token:
            GLib.idle_add(self._populate_results, items)

    def _match_deezer_track(self, yt_title, yt_uploader, yt_duration, dz_tracks):
        """Casa um vídeo do YouTube com uma faixa do Deezer (nome limpo + artista certo)."""
        if not dz_tracks:
            return None
        yt_words = self._norm_words(yt_title)
        yt_set = set(yt_words)
        all_words = " " + " ".join(yt_words + self._norm_words(yt_uploader)) + " "
        up_key = self._norm_key(yt_uploader)

        best, best_score = None, 0.0
        for t in dz_tracks:
            artist = (t.get("artist") or {}).get("name", "")
            full_title = t.get("title", "")
            main_title = re.sub(r"[\(\[].*?[\)\]]", " ", full_title)
            tw = self._norm_words(main_title)
            aw = self._norm_words(artist)
            if not tw or not aw or not set(tw) <= yt_set:
                continue
            if f" {' '.join(aw)} " not in all_words and "".join(aw) not in up_key:
                continue

            score = 10.0 + 0.1 * len(tw)
            dz_dur = t.get("duration") or 0
            if yt_duration and dz_dur:
                diff = abs(yt_duration - dz_dur)
                if diff > 20:
                    continue
                score += 6 if diff <= 3 else (3 if diff <= 10 else 0)
            if set(self._norm_words(full_title)) <= yt_set:
                score += 2
            if score > best_score:
                best, best_score = t, score
        return best

    def _search_failed(self, err_msg):
        self.search_spinner.stop()
        self.mostrar_mensagem(f"Erro na busca: {err_msg}")

    def _populate_results(self, items):
        self.search_spinner.stop()
        self.results_list.set_placeholder(self._placeholder("Nenhuma música encontrada."))
        self.results = items
        for item in items:
            row = Gtk.ListBoxRow()
            rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            label = Gtk.Label(label=f"{item['title']}  —  {item['uploader']} ({item['duration']})", xalign=0)
            label.set_ellipsize(3)
            label.set_margin_start(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            rbox.pack_start(label, True, True, 0)
            rbox.pack_start(self._make_heart(item), False, False, 0)
            row.add(rbox)
            row.item = item
            self.results_list.add(row)
        self.results_list.show_all()
        self.mostrar_mensagem(f"{len(items)} itens encontrados.")

    def on_result_activated(self, listbox, row):
        self.play_item(row.item)

    def _ensure_row_selected(self, listbox, row):
        if row not in listbox.get_selected_rows():
            listbox.unselect_all()
            listbox.select_row(row)

    def on_results_button_press(self, widget, event):
        if event.button == 3:
            row = widget.get_row_at_y(int(event.y))
            if row is None:
                return False
            self._ensure_row_selected(widget, row)
            menu = Gtk.Menu()
            item_add = Gtk.MenuItem(label="Adicionar à Fila")
            item_add.connect("activate", lambda w: self.on_add_to_queue(None))
            item_play = Gtk.MenuItem(label="Reproduzir Agora")
            item_play.connect("activate", lambda w: self.on_play_now(None))
            item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
            item_pl.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=[r.item for r in widget.get_selected_rows()]))
            item_dl = Gtk.MenuItem(label="Baixar")
            item_dl.connect("activate", lambda w: self.on_download(None))
            item_like = self._like_menu_item([r.item for r in widget.get_selected_rows()])
            for it in (item_add, item_play, self._radio_menu_item(row.item), item_pl, item_like, item_dl):
                menu.append(it)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def on_add_to_queue(self, button):
        items = [row.item for row in self.results_list.get_selected_rows()]
        if not items:
            self.mostrar_mensagem("Selecione ao menos uma faixa.")
            return
        self.add_items_bulk(items)

    def on_play_now(self, button):
        rows = self.results_list.get_selected_rows()
        if not rows:
            self.mostrar_mensagem("Selecione ao menos uma faixa.")
            return
        items = [r.item for r in rows]
        self.add_items_bulk(items, notify=False)
        self.current_index = len(self.queue) - len(items)
        self.play_current()

    # ---------- Fila e Histórico (Renderizações Otimizadas) ----------
    def _queue_panel_open(self):
        try:
            return bool(self.side_revealer.get_reveal_child()) and self.side_stack.get_visible_child_name() == "queue"
        except Exception:
            return True

    def _queue_rows_in_sync(self):
        """True se dá para só anexar linhas (painel aberto, sem reconstrução pendente)."""
        if self._queue_dirty or self._queue_building or not self._queue_panel_open():
            self.render_queue()     # fechado: só marca como suja; aberto e em lotes: recomeça com a fila nova
            return False
        return True

    def add_to_queue(self, item, notify=True):
        item = self._normalize_track(item)
        self.queue.append(item)
        self._save_queue()

        if self._queue_rows_in_sync():
            row = self._create_queue_row(len(self.queue) - 1, item)
            self.queue_list.add(row)
            row.show_all()

        if notify:
            self.show_toast(f"Adicionado à fila: {item['title']}")

    def add_items_bulk(self, items, notify=True):
        norm_items = [self._normalize_track(it) for it in items]
        start_idx = len(self.queue)
        self.queue.extend(norm_items)
        self._save_queue()

        if self._queue_rows_in_sync():
            for i, item in enumerate(norm_items, start=start_idx):
                row = self._create_queue_row(i, item)
                self.queue_list.add(row)
                row.show_all()

        if notify:
            if len(norm_items) == 1:
                self.show_toast(f"Adicionado à fila: {norm_items[0]['title']}")
            else:
                self.show_toast(f"{len(norm_items)} faixas adicionadas à fila.")

    def render_queue(self):
        """Reconstrói a lista da fila. Se o painel está fechado, só marca como 'suja' (monta ao abrir);
        aberto, cria as primeiras linhas na hora e o resto em lotes, sem congelar a interface."""
        self._queue_gen += 1
        self._queue_building = False
        if not self._queue_panel_open():
            self._queue_dirty = True
            return
        self._queue_dirty = False
        for child in list(self.queue_list.get_children()):
            self.queue_list.remove(child)
        self._hl_idx = self.current_index
        self._queue_fill(self._queue_gen)

    def _queue_fill(self, gen):
        if gen != self._queue_gen:
            return False
        n = len(self.queue_list.get_children())      # continua de onde parou (aguenta remoções/trocas no meio)
        total = len(self.queue)
        end = min(total, n + (self.CHUNK_FIRST if n == 0 else self.CHUNK_STEP))
        for i in range(n, end):
            row = self._create_queue_row(i, self.queue[i])
            self.queue_list.add(row)
            row.show_all()
        if end < total:
            self._queue_building = True
            GLib.idle_add(self._queue_fill, gen, priority=GLib.PRIORITY_DEFAULT_IDLE)
        else:
            self._queue_building = False
            self._highlight_current_row()
        return False

    def _on_queue_visibility(self, *args):
        if self._queue_dirty and self._queue_panel_open():
            self.render_queue()

    def _create_queue_row(self, i, item):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(self._queue_label_markup(i, item, i == self.current_index))
        lbl.set_ellipsize(3)
        box.pack_start(lbl, True, True, 6)
        row.label = lbl

        btn_up = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.MENU)
        btn_up.set_relief(Gtk.ReliefStyle.NONE)
        btn_up.connect("clicked", lambda b: self._move_queue_row(row, -1))

        btn_down = Gtk.Button.new_from_icon_name("go-down-symbolic", Gtk.IconSize.MENU)
        btn_down.set_relief(Gtk.ReliefStyle.NONE)
        btn_down.connect("clicked", lambda b: self._move_queue_row(row, 1))

        btn_del = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.MENU)
        btn_del.set_relief(Gtk.ReliefStyle.NONE)
        btn_del.connect("clicked", lambda b: self._remove_queue_row(row))

        box.pack_start(self._offline_badge(item), False, False, 0)
        box.pack_start(self._make_heart(item), False, False, 0)
        for b in (btn_up, btn_down, btn_del):
            box.pack_start(b, False, False, 0)

        row.add(box)
        row.item = item
        return row

    def _move_queue_row(self, row, delta):
        idx = row.get_index()
        new_idx = idx + delta
        if 0 <= new_idx < len(self.queue):
            self.queue[idx], self.queue[new_idx] = self.queue[new_idx], self.queue[idx]
            self._save_queue()

            if self.current_index == idx:
                self.current_index = new_idx
            elif self.current_index == new_idx:
                self.current_index = idx

            # Reordena o widget na interface sem destruição
            self.queue_list.remove(row)
            self.queue_list.insert(row, new_idx)
            self._update_queue_indices()

    def _remove_queue_row(self, row):
        idx = row.get_index()
        if 0 <= idx < len(self.queue):
            self.queue.pop(idx)
            self._save_queue()
            self.queue_list.remove(row)

            if self.current_index == idx:
                if idx < len(self.queue):
                    self.play_current()
                else:  # removeu a última faixa (ou a fila ficou vazia): não há "próxima"
                    self.current_index = -1
                    self.mpv.stop()
                    self._set_mpv_idle(True)
                    self.now_playing_label.set_text("Parado")
            elif self.current_index > idx:
                self.current_index -= 1

            self._update_queue_indices()

    def _update_queue_indices(self):
        self._highlight_current_row()

    def on_delete_selected_queue(self, button):
        rows = self.queue_list.get_selected_rows()
        if not rows:
            self.mostrar_mensagem("Selecione faixas para excluir.")
            return
        if not self._confirm_action(f"Remover {len(rows)} faixa(s) selecionada(s) da fila?"):
            return
        
        idxs = sorted((r.get_index() for r in rows), reverse=True)
        playing_item = self.queue[self.current_index] if 0 <= self.current_index < len(self.queue) else None
        
        for row in rows:
            self.queue_list.remove(row)

        for idx in idxs:
            if 0 <= idx < len(self.queue):
                self.queue.pop(idx)

        self._save_queue()

        if playing_item is not None and playing_item in self.queue:
            self.current_index = self.queue.index(playing_item)
        elif playing_item is not None:
            self.current_index = -1
            self.mpv.stop()
            self._set_mpv_idle(True)
            self.now_playing_label.set_text("Parado")

        self._update_queue_indices()
        self.show_toast(f"{len(idxs)} faixa(s) removida(s) da fila.")

    def on_clear_queue(self, button):
        if not self.queue:
            self.mostrar_mensagem("A fila já está vazia.")
            return
        if not self._confirm_action("Limpar toda a fila de reprodução?"):
            return
        self.queue.clear()
        self.stop_radio(notify=False)
        self._save_queue()
        self.current_index = -1
        self.mpv.stop()
        self._set_mpv_idle(True)
        self.render_queue()

    def on_queue_activated(self, listbox, row):
        self.current_index = row.get_index()
        self.play_current()

    # ---------- Fila: clique + segurar o botão esquerdo e arrastar reposiciona faixa(s) ----------
    # Arrasta a linha clicada ou, se ela fizer parte de uma seleção múltipla, todas as selecionadas.
    # Uma linha horizontal mostra o ponto de inserção ENTRE as faixas.
    QDRAG_THRESHOLD = 8      # px de movimento antes de virar arrasto (clique normal continua selecionando)

    def _qdrag_gap_at(self, y):
        """Posição de inserção (0..n): 'antes da linha g'. n = depois da última."""
        n = len(self.queue_list.get_children())
        row = self.queue_list.get_row_at_y(int(y))
        if row is None:
            return 0 if y < 0 else n
        a = row.get_allocation()
        return row.get_index() + (1 if y > a.y + a.height / 2.0 else 0)

    def _qdrag_gap_y(self, gap):
        n = len(self.queue_list.get_children())
        if n == 0:
            return None
        if gap >= n:
            a = self.queue_list.get_row_at_index(n - 1).get_allocation()
            return a.y + a.height
        return self.queue_list.get_row_at_index(max(gap, 0)).get_allocation().y

    def _on_queue_draw(self, widget, cr):
        st = self._qdrag
        if not st or not st.get("on") or st.get("gap") is None:
            return False
        y = self._qdrag_gap_y(st["gap"])
        if y is None:
            return False
        ctx = widget.get_style_context()
        ok, col = ctx.lookup_color("theme_selected_bg_color")
        if not ok:
            col = ctx.get_color(Gtk.StateFlags.NORMAL)
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        y = min(max(y, 2.0), max(h - 2.0, 2.0))
        cr.set_source_rgba(col.red, col.green, col.blue, 1.0)
        cr.set_line_width(2.0)
        cr.move_to(8, y)
        cr.line_to(max(w - 8, 9), y)
        cr.stroke()
        cr.arc(8, y, 3.5, 0, 2 * math.pi)     # bolinha na ponta, como nos indicadores de soltar
        cr.fill()
        return False

    def _qdrag_cursor(self, grabbing):
        win = self.queue_list.get_window()
        if win is None:
            return
        try:
            win.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), "grabbing") if grabbing else None)
        except Exception:
            pass

    def _qdrag_update(self):
        st = self._qdrag
        if not st or not st.get("on"):
            return
        gap = self._qdrag_gap_at(st["y"])
        if gap != st.get("gap"):
            st["gap"] = gap
        self.queue_list.queue_draw()

    def _on_queue_motion(self, widget, event):
        st = self._qdrag
        if not st or not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        st["y"] = event.y
        if not st["on"]:
            if abs(event.y - st["y0"]) < self.QDRAG_THRESHOLD:
                return False
            if self._queue_building or self._queue_dirty:
                return False                  # fila ainda montando: não reordena
            st["on"] = True
            self._qdrag_cursor(True)
            st["tick"] = GLib.timeout_add(40, self._qdrag_tick)
        self._qdrag_update()
        return False

    def _qdrag_tick(self):
        """Rolagem automática quando o ponteiro encosta na borda de cima/baixo durante o arrasto."""
        st = self._qdrag
        if not st or not st.get("on"):
            if st:
                st["tick"] = 0
            return False
        sw = self.queue_list.get_ancestor(Gtk.ScrolledWindow)
        if sw is not None:
            adj = sw.get_vadjustment()
            top, page = adj.get_value(), adj.get_page_size()
            ry, edge, d = st["y"] - top, 28, 0
            if ry < edge:
                d = -max(2, int((edge - ry) / 2))
            elif ry > page - edge:
                d = max(2, int((ry - (page - edge)) / 2))
            if d:
                new = max(adj.get_lower(), min(top + d, adj.get_upper() - page))
                if new != top:
                    adj.set_value(new)
                    st["y"] += new - top      # o conteúdo andou; o ponteiro continua no mesmo lugar da tela
                    self._qdrag_update()
        return True

    def _on_queue_release(self, widget, event):
        st = self._qdrag
        if event.button != 1 or not st:
            return False
        self._qdrag = None
        if st.get("tick"):
            GLib.source_remove(st["tick"])
        if st.get("on"):
            self._qdrag_cursor(False)
            widget.queue_draw()               # apaga a linha de inserção
            GLib.idle_add(self._queue_move_rows, st["rows"], st.get("gap"))   # fora do handler de evento
        elif st.get("multi"):
            # clique simples (sem arrastar) numa linha de seleção múltipla: vira seleção única, como no padrão
            self.queue_list.unselect_all()
            self.queue_list.select_row(st["row"])
        return False

    def _queue_move_rows(self, rows, gap):
        """Move as linhas `rows` (uma ou várias) para a posição de inserção `gap`, mantendo a ordem entre elas."""
        n = len(self.queue)
        children = self.queue_list.get_children()
        if (gap is None or self._queue_building or self._queue_dirty or len(children) != n
                or any(r.get_parent() is not self.queue_list for r in rows)):
            return False
        sel = sorted({r.get_index() for r in rows})
        sel = [i for i in sel if 0 <= i < n]
        if not sel:
            return False
        gap = max(0, min(int(gap), n))
        ins = gap - sum(1 for i in sel if i < gap)          # posição entre as faixas que não se movem
        selset = set(sel)
        rest = [i for i in range(n) if i not in selset]
        order = rest[:ins] + sel + rest[ins:]               # order[j] = índice antigo da nova posição j
        if order == list(range(n)):
            return False                                    # soltou onde já estava

        moved_rows = [children[i] for i in sel]
        self.queue[:] = [self.queue[i] for i in order]
        if 0 <= self.current_index < n:
            self.current_index = order.index(self.current_index)   # a faixa tocando continua a mesma
        self._save_queue()

        for r in reversed(moved_rows):
            self.queue_list.remove(r)
        for k, r in enumerate(moved_rows):
            self.queue_list.insert(r, ins + k)
        self.queue_list.unselect_all()
        for r in moved_rows:
            self.queue_list.select_row(r)
        self._update_queue_indices()
        return False

    def on_queue_button_press(self, widget, event):
        if (event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS
                and not (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))):
            row = widget.get_row_at_y(int(event.y))
            if row is not None and hasattr(row, "item"):
                sel = widget.get_selected_rows()
                multi = len(sel) > 1 and row in sel
                rows = sorted(sel, key=lambda r: r.get_index()) if multi else [row]
                self._qdrag = {"row": row, "rows": rows, "multi": multi, "y0": event.y, "y": event.y,
                               "on": False, "tick": 0, "gap": None}
                if multi:
                    return True      # segura a seleção múltipla enquanto decide entre clique e arrasto
            else:
                self._qdrag = None
        if event.button == 3:
            row = widget.get_row_at_y(int(event.y))
            if row is None:
                return False
            self._ensure_row_selected(widget, row)
            menu = Gtk.Menu()
            item_play = Gtk.MenuItem(label="Reproduzir")
            item_play.connect("activate", lambda w: self._play_queue_row(row))
            item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
            item_pl.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=[r.item for r in widget.get_selected_rows()]))
            item_del = Gtk.MenuItem(label="Excluir Selecionadas")
            item_del.connect("activate", lambda w: self.on_delete_selected_queue(None))
            item_dl = Gtk.MenuItem(label="Baixar Selecionadas")
            item_dl.connect("activate", lambda w: self.on_download(None))
            item_like = self._like_menu_item([r.item for r in widget.get_selected_rows()])
            for it in (item_play, self._radio_menu_item(row.item), item_pl, item_like, item_del, item_dl):
                menu.append(it)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _play_queue_row(self, row):
        self.current_index = row.get_index()
        self.play_current()

    def add_to_history(self, item):
        item = self._normalize_track(item)
        if not self.history or self._track_key(self.history[0]) != self._track_key(item):
            self.history.insert(0, item)
            self.history = self.history[:HISTORY_LIMIT]
            self._save_json(HISTORY_FILE, self.history, compact=True)
            self.render_history()

    def render_history(self):
        for child in list(self.history_list.get_children()):
            self.history_list.remove(child)
        for item in self.history:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            lbl = Gtk.Label(label=f"{item['title']} — {item.get('uploader','')}", xalign=0)
            lbl.set_ellipsize(3)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            hbox.pack_start(lbl, True, True, 0)
            hbox.pack_start(self._offline_badge(item), False, False, 0)
            hbox.pack_start(self._make_heart(item), False, False, 0)
            row.add(hbox)
            row.item = item
            self.history_list.add(row)
        self.history_list.show_all()
        self._render_home_recents()

    def on_history_activated(self, listbox, row):
        self.play_item(row.item)

    def on_history_button_press(self, widget, event):
        if event.button == 3:
            row = widget.get_row_at_y(int(event.y))
            if row is None or not hasattr(row, "item"):
                return False
            self._ensure_row_selected(widget, row)
            menu = Gtk.Menu()
            item_play = Gtk.MenuItem(label="Reproduzir")
            item_play.connect("activate", lambda w: self.play_item(row.item))
            item_add_q = Gtk.MenuItem(label="Adicionar à Fila")
            item_add_q.connect("activate", lambda w: self.add_to_queue(row.item))
            item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
            item_pl.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=[r.item for r in widget.get_selected_rows()]))
            item_like = self._like_menu_item([r.item for r in widget.get_selected_rows()])
            for it in (item_play, item_add_q, self._radio_menu_item(row.item), item_pl, item_like):
                menu.append(it)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def on_clear_history(self, button):
        if not self.history:
            self.mostrar_mensagem("O histórico já está vazio.")
            return
        if not self._confirm_action("Apagar todo o histórico de reprodução?"):
            return
        self.history.clear()
        self._save_json(HISTORY_FILE, self.history, compact=True)
        self.render_history()
        self.show_toast("Histórico apagado.")

    # ---------- Letras / Metadados ----------
    def _clean_title(self, title):
        def _strip_group(m):
            return " " if JUNK_GROUP_RE.fullmatch(m.group(1).strip() or "x") else m.group(0)

        title = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", _strip_group, title or "")
        title = re.sub(r"(?i)\b(hd|4k|mv)\b", " ", title)
        title = re.sub(r"\s{2,}", " ", title)
        return title.strip(" -–—|")

    def search_current_lyrics(self):
        if 0 <= self.current_index < len(self.queue):
            item = self.queue[self.current_index]
            if item.get("webradio"):
                self._show_webradio_lyrics_panel(item)
                return
            self._update_lyrics_ui("Buscando letra...")
            self.lyrics_title_label.set_markup("<b>Carregando...</b>")
            self.lyrics_artist_label.set_text("")
            self.lyrics_album_label.set_text("")
            self.lyrics_year_label.set_text("")
            self.lyrics_art_img.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            if item.get("path"):
                threading.Thread(target=self._local_lyrics_thread, args=(item, self._play_token), daemon=True).start()
                return
            threading.Thread(
                target=self._fetch_lyrics_thread,
                args=(item["title"], item.get("uploader", "")),
                daemon=True,
            ).start()
        else:
            self._update_lyrics_ui("Nenhuma música tocando no momento.")

    def _fetch_lyrics_thread(self, raw_title, uploader):
        clean_t = self._clean_title(raw_title)
        query = f"{uploader} {clean_t}".strip() if uploader and uploader.lower() not in clean_t.lower() else clean_t

        lyrics_found = None
        try:
            url = f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for res in data:
                    if res.get("plainLyrics"):
                        lyrics_found = res["plainLyrics"]
                        break
                    elif res.get("syncedLyrics"):
                        lyrics_found = re.sub(r"\[\d+:\d+\.\d+\]\s*", "", res["syncedLyrics"])
                        break
        except Exception:
            pass

        if not lyrics_found and query != clean_t:
            try:
                url = f"https://lrclib.net/api/search?q={urllib.parse.quote(clean_t)}"
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    for res in data:
                        if res.get("plainLyrics"):
                            lyrics_found = res["plainLyrics"]
                            break
                        elif res.get("syncedLyrics"):
                            lyrics_found = re.sub(r"\[\d+:\d+\.\d+\]\s*", "", res["syncedLyrics"])
                            break
            except Exception:
                pass

        meta = self._fetch_track_metadata(clean_t, uploader)
        GLib.idle_add(self._apply_lyrics_result, lyrics_found, clean_t, meta)

    def _local_lyrics_thread(self, item, token):
        """Faixa offline: tags/.lrc do próprio arquivo primeiro; só vai à internet se faltar algo."""
        lyrics = read_local_lyrics(item["path"])
        entry = self.lib_index.get(item["path"]) or {}
        meta = {"artist": item.get("uploader") or entry.get("uploader", ""),
                "album": item.get("album") or entry.get("album", ""),
                "year": item.get("year") or entry.get("year", ""), "artwork": ""}
        title = item.get("title", "")
        if not lyrics or not meta["album"]:
            clean_t = self._clean_title(title)
            try:
                if not lyrics:
                    q = f"{meta['artist']} {clean_t}".strip()
                    data = self._http_json(f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}")
                    for res in data:
                        if res.get("plainLyrics"):
                            lyrics = res["plainLyrics"]
                            break
                        if res.get("syncedLyrics"):
                            lyrics = re.sub(r"\[\d+:\d+\.\d+\]\s*", "", res["syncedLyrics"])
                            break
                if not meta["album"]:
                    online = self._fetch_track_metadata(clean_t, meta["artist"])
                    for k in ("album", "year"):
                        meta[k] = meta[k] or online.get(k, "")
            except Exception:
                pass
        if token == self._play_token:
            GLib.idle_add(self._apply_lyrics_result, lyrics, title, meta)

    def _fetch_track_metadata(self, title, uploader):
        """Artista, álbum, ano e capa exibidos na aba Letra (via Deezer)."""
        empty = {"artist": uploader, "album": "", "year": "", "artwork": ""}
        query = f"{uploader} {title}".strip()
        try:
            url = f"{DEEZER_API}/search?q={urllib.parse.quote(query)}&limit=1"
            data = self._http_json(url, timeout=8).get("data", []) or []
            if not data:
                return empty
            r = data[0]
            album = r.get("album") or {}
            year = ""
            if album.get("id"):
                try:
                    detail = self._http_json(f"{DEEZER_API}/album/{album['id']}", timeout=6)
                    year = (detail.get("release_date") or "")[:4]
                except Exception:
                    pass
            return {
                "artist": (r.get("artist") or {}).get("name") or uploader,
                "album": album.get("title") or "",
                "year": year,
                "artwork": album.get("cover_medium") or "",
            }
        except Exception:
            return empty

    def _apply_lyrics_result(self, lyrics_found, clean_t, meta):
        self._update_lyrics_ui(lyrics_found or f"Letra não encontrada para:\n'{clean_t}'")
        self.lyrics_title_label.set_markup(f"<b>{GLib.markup_escape_text(clean_t)}</b>")
        self.lyrics_artist_label.set_text(meta.get("artist") or "")
        self.lyrics_album_label.set_text(f"Álbum: {meta['album']}" if meta.get("album") else "")
        self.lyrics_year_label.set_text(f"Ano: {meta['year']}" if meta.get("year") else "")
        artwork = meta.get("artwork")
        if artwork:
            self._submit_art(self._load_artwork_into, artwork, self.lyrics_art_img, 80)   # pool (máx. 4), sem thread nova

    def _update_lyrics_ui(self, text):
        buffer = self.lyrics_text_view.get_buffer()
        buffer.set_text(text)

    @staticmethod
    def _decode_scaled(data, w, h):
        """Decodifica a imagem já no tamanho final (JPEG/PNG reduzem durante a leitura: bem menos
        CPU e memória que carregar inteira e depois chamar scale_simple)."""
        loader = GdkPixbuf.PixbufLoader()
        loader.set_size(w, h)
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()

    def _load_artwork_into(self, url, image_widget, size, height=None):
        try:
            data = self._art_cache.get(url)
            if data is None:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = resp.read()
                while len(self._art_cache) >= 300:          # descarta a mais antiga, não o cache todo
                    try:
                        del self._art_cache[next(iter(self._art_cache))]
                    except (StopIteration, KeyError, RuntimeError):
                        break
                self._art_cache[url] = data
            pixbuf = self._decode_scaled(data, size, height or size)
            GLib.idle_add(image_widget.set_from_pixbuf, pixbuf)
        except Exception:
            pass

    # ---------- Aba Descobrir (Com Cancelamento) ----------
    def _http_json(self, url, timeout=8):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_json_many(self, urls, timeout=8):
        """Baixa várias URLs JSON em paralelo (threads curtas, sem bloquear a UI)."""
        results = [None] * len(urls)

        def work(i, u):
            try:
                results[i] = self._http_json(u, timeout)
            except Exception:
                results[i] = None

        threads = [threading.Thread(target=work, args=(i, u), daemon=True) for i, u in enumerate(urls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout + 2)
        return results

    def _deezer_artist_candidates(self, query):
        url = f"{DEEZER_API}/search/artist?q={urllib.parse.quote(query)}&limit=10"
        return self._http_json(url).get("data", []) or []

    def _pick_artist(self, cands, key):
        """Escolhe o candidato com nome mais parecido; empate decidido por nº de fãs."""
        best, best_score = None, -1.0
        for a in cands:
            nk = self._norm_key(a.get("name", ""))
            if not nk:
                continue
            if nk == key:
                ratio = 1.0
            else:
                ratio = difflib.SequenceMatcher(None, key, nk).ratio()
                if nk.startswith(key) or key.startswith(nk):
                    ratio = max(ratio, 0.85)
            fans = min(a.get("nb_fan") or 0, 5_000_000) / 5_000_000
            score = ratio * 10 + fans
            if score > best_score:
                best, best_score = a, score
        return best, best_score

    def _deezer_search_artist(self, query):
        name = self._clean_artist_name(query) or (query or "").strip()
        key = self._norm_key(name)
        if not key:
            return None

        cands = []
        try:
            cands = self._deezer_artist_candidates(name)
        except Exception:
            pass
        best, score = self._pick_artist(cands, key)

        # Sem correspondência exata (ex.: "Titãs" -> "TITA$"): tenta de novo sem acentos
        plain = self._unaccent(name)
        if score < 10 and plain != name:
            try:
                seen = {a.get("id") for a in cands}
                cands += [a for a in self._deezer_artist_candidates(plain) if a.get("id") not in seen]
                best, score = self._pick_artist(cands, key)
            except Exception:
                pass

        return best if best and score >= 6 else None

    # ---------- Faixas do Deezer em listas (Em alta, Wiki) ----------
    def _dtrack_row(self, t, index=None):
        artist_name = (t.get("artist") or {}).get("name", "")
        dur = self._fmt_duration(t.get("duration"))
        prefix = f"{index}. " if index else ""
        row = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=f"{prefix}{t.get('title','')}  —  {artist_name} ({dur})", xalign=0)
        lbl.set_ellipsize(3)
        lbl.set_margin_start(8)
        lbl.set_margin_top(5)
        lbl.set_margin_bottom(5)
        row.add(lbl)
        row.item = self._normalize_track(
            {"title": t.get("title", ""), "artist": artist_name, "duration_fmt": dur, "verified": True})
        return row

    def _open_artist_in_wiki(self, name, artist=None):
        name = (name or "").strip()
        if not name:
            return
        self.wiki_entry.set_text(name)
        self._wiki_forced = artist
        self.navigate("artist")
        self.on_wiki_search(None)

    def on_discover_add_all_to_playlist(self, button=None):
        """Equivale a selecionar todas as faixas e usar 'Adicionar à Playlist' do botão direito."""
        self.discover_tracks_list.select_all()
        items = self._get_discover_selected_tracks()
        if not items:
            self.mostrar_mensagem("Não há faixas para adicionar.")
            return
        self._add_dtracks_to_playlist(items)

    def on_discover_track_activated(self, listbox, row):
        item = getattr(row, "item", None)
        if item:
            self.on_discover_track_play(item)

    def _get_discover_selected_tracks(self):
        rows = self.discover_tracks_list.get_selected_rows()
        return [r.item for r in rows if hasattr(r, "item")]

    def _dtracks_context_menu(self, widget, event, get_selected):
        """Menu de botão direito compartilhado por Descobrir e álbuns da Wiki."""
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, "item"):
            return False
        self._ensure_row_selected(widget, row)
        sel = get_selected()
        menu = Gtk.Menu()
        item_play = Gtk.MenuItem(label="Reproduzir Agora")
        item_play.connect("activate", lambda w: self._play_dtracks_progressive(sel))
        item_add = Gtk.MenuItem(label="Adicionar à Fila")
        item_add.connect("activate", lambda w: self._queue_dtracks(sel))
        item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
        item_pl.connect("activate", lambda w: self._add_dtracks_to_playlist(sel))
        item_dl = Gtk.MenuItem(label="Baixar")
        item_dl.connect("activate", lambda w: self._download_dtracks(sel))
        item_like = self._like_menu_item(sel, resolver=lambda items, cb: self._discover_resolve_and(items, cb, "Curtindo faixas"))
        for it in (item_play, item_add, self._radio_menu_item_dtrack(row.item), item_pl, item_like, item_dl):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_discover_tracks_button_press(self, widget, event):
        return self._dtracks_context_menu(widget, event, self._get_discover_selected_tracks)

    def _run_dtracks_action(self, action, items):
        if action == "play":
            self._play_dtracks_progressive(items)
        elif action == "queue":
            self._queue_dtracks(items)
        elif action == "download":
            self._download_dtracks(items)

    def on_discover_track_button(self, action):
        self._run_dtracks_action(action, self._get_discover_selected_tracks())

    # ======================================================================
    # Adição a playlists: trava contra cliques repetidos + feedback de espera
    # ======================================================================
    def _build_busy_overlay(self):
        """Camada sobre a janela: bloqueia cliques e mostra o andamento até o fim do processo."""
        self.busy_overlay = Gtk.EventBox()
        self.busy_overlay.set_visible_window(False)
        self.busy_overlay.connect("button-press-event", lambda w, e: True)  # engole cliques fora do cartão

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.set_border_width(22)
        card.set_size_request(380, -1)
        # fundo opaco (cor padrão do tema) para o texto de trás não vazar
        bg = Gtk.EventBox()
        bg.get_style_context().add_class("background")  # classe nativa do GTK: pinta o fundo do tema
        card.get_style_context().add_class("background")
        bg.add(card)
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.OUT)
        frame.add(bg)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.CENTER)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.busy_spinner = Gtk.Spinner()
        head.pack_start(self.busy_spinner, False, False, 0)
        self.busy_title = Gtk.Label(xalign=0)
        self.busy_title.set_ellipsize(3)
        head.pack_start(self.busy_title, True, True, 0)
        card.pack_start(head, False, False, 0)

        self.busy_status = Gtk.Label(xalign=0)
        self.busy_status.set_line_wrap(True)
        card.pack_start(self.busy_status, False, False, 0)

        self.busy_bar = Gtk.ProgressBar()
        self.busy_bar.set_pulse_step(0.12)
        self.busy_bar.set_show_text(True)
        card.pack_start(self.busy_bar, False, False, 0)

        hint = Gtk.Label(label="Aguarde o término. Isso pode levar alguns instantes.", xalign=0)
        hint.set_opacity(0.7)
        card.pack_start(hint, False, False, 0)

        self.busy_cancel = Gtk.Button(label="Cancelar")
        self.busy_cancel.set_halign(Gtk.Align.END)
        self.busy_cancel.connect("clicked", self._busy_cancel_clicked)
        card.pack_start(self.busy_cancel, False, False, 0)

        self.busy_overlay.add(frame)
        self.busy_overlay.show_all()
        self.busy_overlay.set_no_show_all(True)
        self.busy_overlay.hide()
        return self.busy_overlay

    def _busy_pulse(self):
        self.busy_bar.pulse()
        return True

    def _busy_begin(self, title, status):
        self._pl_busy = True
        self.busy_title.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(title)}</span>')
        self.busy_status.set_text(status)
        self.busy_bar.set_fraction(0)
        self.busy_bar.set_text("")
        if self._busy_pulse_id is None:
            self._busy_pulse_id = GLib.timeout_add(120, self._busy_pulse)
        self.busy_spinner.start()
        self.busy_cancel.set_sensitive(True)
        self._body_widget.set_sensitive(False)  # escurece e bloqueia a interface
        self.busy_overlay.set_no_show_all(False)
        self.busy_overlay.show_all()
        self.busy_overlay.set_no_show_all(True)

    def _busy_update(self, done, total, status):
        if self._busy_pulse_id is not None:
            GLib.source_remove(self._busy_pulse_id)
            self._busy_pulse_id = None
        self.busy_status.set_text(status + "...")
        self.busy_bar.set_fraction(done / total if total else 0)
        self.busy_bar.set_text(f"{done}/{total}")

    def _busy_end(self):
        if self._busy_pulse_id is not None:
            GLib.source_remove(self._busy_pulse_id)
            self._busy_pulse_id = None
        self.busy_spinner.stop()
        self.busy_overlay.hide()
        self._body_widget.set_sensitive(True)
        self._busy_job = None
        self._pl_busy = False

    def _busy_cancel_clicked(self, btn=None):
        job = self._busy_job
        if job is None:
            return
        job["cancel"] = True  # a thread de trabalho para de tocar na interface
        self._busy_end()
        self.show_toast(job.get("cancel_msg", "Cancelado."))

    def _job_call(self, job, fn, *args):
        """Executa fn na thread da UI, a menos que o job tenha sido cancelado."""
        GLib.idle_add(self._job_dispatch, job, fn, args)

    def _job_dispatch(self, job, fn, args):
        if not job["cancel"]:
            fn(*args)
        return False

    def _ask_playlist_target(self):
        """Diálogo modal para escolher (ou criar) a playlist de destino. Retorna o nome ou None."""
        dialog = Gtk.Dialog(title="Adicionar à Playlist", parent=self, flags=0)
        dialog.set_modal(True)
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box_combo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box_combo.set_border_width(10)

        combo = Gtk.ComboBoxText()
        for name in sorted(self.playlists.keys()):
            combo.append_text(name)
        if self.playlists:
            combo.set_active(0)

        btn_new_pl = Gtk.Button(label="➕ Nova Playlist")

        def _create_and_select(b):
            new_name = self.on_create_playlist_dialog()
            if new_name:
                combo.append_text(new_name)
                combo.set_active(len(combo.get_model()) - 1)

        btn_new_pl.connect("clicked", _create_and_select)
        box_combo.pack_start(combo, True, True, 0)
        box_combo.pack_start(btn_new_pl, False, False, 0)
        content.pack_start(box_combo, True, True, 0)

        dialog.show_all()
        response = dialog.run()
        target = combo.get_active_text()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        if not target:
            self.mostrar_mensagem("Escolha ou crie uma playlist.")
            return None
        return target

    def _dup_keys(self, t):
        """Chaves de identidade de uma faixa: id do YouTube e/ou título+artista normalizados."""
        keys = set()
        if t.get("path"):
            return {"file:" + t["path"]}
        tid = (t.get("id") or "").strip()
        if tid:
            keys.add("id:" + tid)
        ttl = self._norm_key(t.get("title") or "")
        if ttl:
            keys.add(f"tu:{ttl}|{self._norm_key(t.get('uploader') or '')}")
        return keys

    def _commit_items_to_playlist(self, target, items, missed=0):
        """Grava na playlist ignorando faixas que já estão nela (ou repetidas no próprio lote)."""
        current = self.playlists.get(target, [])
        seen = set()
        for t in current:
            seen |= self._dup_keys(t)
        new, dups = [], 0
        for it in items:
            keys = self._dup_keys(it)
            if keys & seen:
                dups += 1
                continue
            new.append(it)
            seen |= keys

        if new:
            self.playlists[target] = current + new
            self._save_json(PLAYLISTS_FILE, self.playlists)
            msg = f"{len(new)} faixa(s) adicionadas em '{target}'"
        else:
            msg = f"Nenhuma faixa nova: já estavam em '{target}'"
        if new and dups:
            msg += f" · {dups} já estava(m) na playlist"
        elif dups and not new and missed:
            msg += f" · {missed} não encontrada(s)"
        if new and missed:
            msg += f" · {missed} não encontrada(s) no YouTube"
        self.show_toast(msg)
        if new:
            self.render_playlists()
            if self.selected_playlist == target:
                self.render_playlist_tracks()

    def on_add_selection_to_playlist(self, button=None, items_to_add=None):
        if self._job_guard():
            return
        if items_to_add is None:
            items_to_add = self.get_active_selection()
        if not items_to_add:
            self.mostrar_mensagem("Selecione ao menos uma faixa.")
            return
        items_to_add = [self._normalize_track(it) for it in items_to_add]

        self._pl_busy = True  # impede abrir outro diálogo/adição enquanto este está aberto
        try:
            target = self._ask_playlist_target()
        finally:
            self._pl_busy = False
        if target:
            self._commit_items_to_playlist(target, items_to_add)

    # ---------- Motor único: localizar faixas no YouTube com trava, progresso e cancelamento ----------
    def _job_guard(self):
        """True (e avisa) se já existe uma operação longa em andamento."""
        if self._pl_busy:
            self.show_toast("Aguarde: já existe uma operação em andamento.")
            return True
        return False

    def _start_job(self, title, source, finish, on_item=None, cancel_msg="Cancelado.",
                   status="Localizando faixas no YouTube"):
        """source() -> lista de faixas do Deezer (roda em thread); on_item(track) recebe cada faixa
        localizada, em ordem; finish(resolved, missed) roda na UI ao terminar. O overlay só some no fim."""
        job = {"cancel": False, "cancel_msg": cancel_msg}
        self._busy_job = job
        self._busy_begin(title, "Preparando")
        threading.Thread(target=self._job_worker, args=(source, finish, on_item, status, job), daemon=True).start()

    def _job_worker(self, source, finish, on_item, status, job):
        try:
            self._job_worker_impl(source, finish, on_item, status, job)
        except Exception:
            # qualquer falha inesperada nunca pode deixar o overlay preso
            self._job_call(job, self._job_finish, finish, None, 0, "Erro inesperado ao processar as faixas.")

    def _job_worker_impl(self, source, finish, on_item, status, job):
        try:
            dtracks = source()
        except Exception:
            dtracks = None
        if job["cancel"]:
            return
        if dtracks is None:
            self._job_call(job, self._job_finish, finish, None, 0, "Não foi possível carregar as faixas.")
            return
        if not dtracks:
            self._job_call(job, self._job_finish, finish, None, 0, "Não há faixas para processar.")
            return

        total = len(dtracks)
        self._job_call(job, self._busy_update, 0, total, status)
        results = [None] * total
        ready = [False] * total

        def run(idx, dtrack):
            return idx, (None if job["cancel"] else self._resolve_one_dtrack(dtrack))

        ex = ThreadPoolExecutor(max_workers=3)
        try:
            futs = [ex.submit(run, i, d) for i, d in enumerate(dtracks)]
            done = nxt = 0
            for f in as_completed(futs):
                idx, track = f.result()
                results[idx] = track
                ready[idx] = True
                done += 1
                if job["cancel"]:
                    return
                if on_item:  # entrega na ordem original, assim que a próxima da fila estiver pronta
                    while nxt < total and ready[nxt]:
                        if results[nxt]:
                            self._job_call(job, on_item, results[nxt])
                        nxt += 1
                self._job_call(job, self._busy_update, done, total, status)
        finally:
            ex.shutdown(wait=False)
        resolved = [t for t in results if t]
        self._job_call(job, self._job_finish, finish, resolved, total - len(resolved), None)

    def _job_finish(self, finish, resolved, missed, err):
        self._busy_end()  # só aqui o feedback some: o processo terminou
        if err:
            self.mostrar_mensagem(err)
        elif not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
        else:
            finish(resolved, missed)
        return False

    def _discover_resolve_and(self, dtracks, callback, title="Localizando faixas"):
        if self._job_guard():
            return
        if not dtracks:
            self.mostrar_mensagem("Selecione uma ou mais faixas.")
            return
        self._start_job(title, lambda: dtracks, lambda resolved, missed: callback(resolved))

    def _queue_dtracks(self, dtracks):
        self._discover_resolve_and(dtracks, self._queue_resolved_tracks, "Adicionando à fila")

    def _download_dtracks(self, dtracks):
        self._discover_resolve_and(dtracks, self._download_resolved_tracks, "Preparando download")

    def _resolve_deezer_track(self, dtrack, callback):
        self._discover_resolve_and([dtrack], lambda resolved: callback(resolved[0]), "Localizando faixa")

    def _play_dtracks_progressive(self, dtracks):
        """Toca a 1ª faixa assim que ela é localizada; as demais entram na fila em ordem."""
        if self._job_guard():
            return
        if not dtracks:
            self.mostrar_mensagem("Selecione uma ou mais faixas.")
            return
        state = {"n": 0}

        def on_item(track):
            self.add_items_bulk([track], notify=False)
            if state["n"] == 0:
                self.current_index = len(self.queue) - 1
                self.play_current()
            state["n"] += 1

        def finish(resolved, missed):
            if len(resolved) > 1:
                self.show_toast(f"{len(resolved)} faixas adicionadas à fila.")

        self._start_job("Preparando reprodução", lambda: dtracks, finish, on_item=on_item,
                        cancel_msg="Cancelado. O que já foi localizado permanece na fila.")

    def _add_dtracks_to_playlist(self, dtracks):
        """Faixas do Deezer (descobertas/álbuns): localiza no YouTube e adiciona à playlist."""
        if not dtracks:
            self.mostrar_mensagem("Selecione uma ou mais faixas.")
            return
        self._playlist_add_job(lambda: dtracks)

    def _playlist_add_job(self, source):
        """Fluxo completo com trava: escolhe a playlist -> espera (overlay) -> grava."""
        if self._job_guard():
            return
        self._pl_busy = True
        try:
            target = self._ask_playlist_target()
        except Exception:
            target = None
        if not target:
            self._pl_busy = False
            return
        self._start_job(
            f"Adicionando em “{target}”", source,
            lambda resolved, missed: self._commit_items_to_playlist(target, resolved, missed),
            cancel_msg="Cancelado. Nenhuma faixa foi adicionada.")

    def _add_album_to_playlist(self, alb):
        """Adiciona todas as faixas de um álbum (sem precisar abri-lo) a uma playlist."""
        def source():
            cached = self._album_cache.get(alb.get("id"))
            if cached is None:
                cached = self._http_json(f"{DEEZER_API}/album/{alb.get('id')}/tracks?limit=100").get("data", []) or []
                self._album_cache[alb.get("id")] = cached
            return self._album_tracks_to_items(cached)
        self._playlist_add_job(source)

    def _resolve_one_dtrack(self, dtrack):
        query = f"{dtrack.get('artist','')} - {dtrack.get('title','')}".strip(" -")
        try:
            out = subprocess.check_output(
                ["yt-dlp", f"ytsearch1:{query}", "--flat-playlist", "-j", "--no-warnings"],
                stderr=subprocess.DEVNULL, text=True, timeout=20,
            )
            data = json.loads(out.strip().split("\n")[0])
            vid_id = data.get("id") or self._extract_id(data.get("url", ""))
            if vid_id:
                return self._normalize_track({
                    "id": vid_id,
                    "title": dtrack.get("title", ""),
                    "uploader": dtrack.get("artist", ""),
                    "duration": dtrack.get("duration", ""),
                    "verified": True,
                })
        except Exception:
            pass
        return None

    def _queue_resolved_tracks(self, resolved):
        if not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            return
        self.add_items_bulk(resolved)

    def _download_resolved_tracks(self, resolved):
        if not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            return
        if len(resolved) == 1:
            self._download_single_dialog(resolved[0])
        else:
            self._download_bulk_dialog(resolved)

    def on_discover_track_play(self, dtrack):
        self.show_toast("Buscando faixa no YouTube...")
        self._resolve_deezer_track(dtrack, self.play_item)

    def on_discover_track_queue(self, dtrack):
        self.show_toast("Buscando faixa no YouTube...")
        self._resolve_deezer_track(dtrack, lambda item: self.add_to_queue(item))

    def on_discover_track_download(self, dtrack):
        self.show_toast("Buscando faixa no YouTube...")
        self._resolve_deezer_track(dtrack, self._download_single_dialog)

    # ---------- Aba Wiki (Com Cancelamento) ----------
    def on_wiki_use_current(self, button):
        if 0 <= self.current_index < len(self.queue):
            name = self._guess_artist(self.queue[self.current_index])
            if name:
                self.wiki_entry.set_text(name)
                self.on_wiki_search(None)
                return
        self.mostrar_mensagem("Nenhuma faixa tocando no momento.")

    def on_wiki_search(self, widget):
        query = self.wiki_entry.get_text().strip()
        if not query:
            self.mostrar_mensagem("Digite o nome de um artista.")
            return

        self._artist_stale["wiki"] = False
        self._loaded_key["wiki"] = self._norm_key(query)

        self.wiki_token += 1
        current_token = self.wiki_token

        forced, self._wiki_forced = self._wiki_forced, None
        self.wiki_spinner.start()
        self.wiki_name_label.set_markup(f'<span size="xx-large" weight="bold">{GLib.markup_escape_text(query)}</span>')
        self.wiki_fans_label.set_text("")
        self.wiki_bio_label.set_text("Buscando...")
        self.wiki_art_img.set_from_icon_name("avatar-default-symbolic", Gtk.IconSize.DIALOG)
        threading.Thread(target=self._wiki_thread, args=(query, current_token, forced), daemon=True).start()

    def _wiki_thread(self, query, token, forced=None):
        artist = forced
        if not artist:
            try:
                artist = self._deezer_search_artist(query)
            except Exception:
                artist = None

        if token != self.wiki_token:
            return
        if not artist:
            GLib.idle_add(self._wiki_failed, "Artista não encontrado.")
            return

        # Biografia (Wikipédia) em paralelo a discografia, populares e relacionados (Deezer)
        box = {}

        def _bio_worker():
            box["bio"] = self._fetch_artist_bio(artist.get("name", ""))

        t_bio = threading.Thread(target=_bio_worker, daemon=True)
        t_bio.start()
        aid = artist["id"]
        albums_r, top_r, rel_r = self._http_json_many([
            f"{DEEZER_API}/artist/{aid}/albums?limit=100",
            f"{DEEZER_API}/artist/{aid}/top?limit=10",
            f"{DEEZER_API}/artist/{aid}/related?limit=10",
        ], timeout=10)
        t_bio.join(timeout=12)

        # /top falha às vezes (timeout, quota, lista vazia): tenta de novo e cai pra busca
        top_tracks = self._deezer_top_tracks(artist, top_r, limit=10, albums=(albums_r or {}).get("data", []) or [])

        if token == self.wiki_token:
            GLib.idle_add(
                self._populate_wiki, artist,
                (albums_r or {}).get("data", []) or [], box.get("bio", "Biografia não encontrada."),
                top_tracks, (rel_r or {}).get("data", []) or [],
            )

    def _deezer_top_tracks(self, artist, first=None, limit=10, albums=None):
        """Faixas populares de um artista no Deezer, completando por etapas até `limit`.

        O /artist/{id}/top do Deezer pode vir vazio ou truncado (ex.: só 4 faixas), então
        cada etapa ADICIONA faixas (sem repetir títulos) até atingir o limite:
        1) /artist/{id}/top (first, ou nova tentativa se veio vazio)
        2) busca artist:"nome" ordenada por ranking (filtrada pelo id do artista)
        3) /artist/{id}/radio (filtrada pelo id do artista)
        4) faixas dos álbuns mais populares do artista, ordenadas pelo campo rank
        O log em stderr mostra quais etapas contribuíram.
        """
        aid = artist.get("id")
        name = artist.get("name", "")
        out, seen, used = [], set(), []

        def _data(r):
            if isinstance(r, dict):
                d = r.get("data")
                if isinstance(d, list):
                    return d
            return []

        def _own(tracks):
            return [t for t in tracks if (t.get("artist") or {}).get("id") == aid]

        def _add(step, tracks):
            added = 0
            for t in tracks:
                if len(out) >= limit:
                    break
                k = self._norm_key(t.get("title_short") or t.get("title") or "")
                if k and k not in seen:
                    seen.add(k)
                    out.append(t)
                    added += 1
            if added:
                used.append(f"{step}(+{added})")
            return len(out) >= limit

        def _finish():
            if out:
                print(f"[populares] {name}: {len(out)} faixa(s) via {', '.join(used)}", file=sys.stderr)
            else:
                print(f"[populares] {name}: nenhuma etapa devolveu faixas", file=sys.stderr)
            return out[:limit]

        # 1) /top
        tracks = _data(first)
        if not tracks:
            try:
                tracks = _data(self._http_json(f"{DEEZER_API}/artist/{aid}/top?limit={limit}", timeout=12))
            except Exception:
                tracks = []
        if _add("/top", tracks):
            return _finish()

        # 2) busca por ranking
        try:
            q = urllib.parse.quote(f'artist:"{name}"')
            tracks = _own(_data(self._http_json(f"{DEEZER_API}/search?q={q}&order=RANKING&limit=50", timeout=12)))
        except Exception:
            tracks = []
        if _add("busca", tracks):
            return _finish()

        # 3) rádio do artista
        try:
            tracks = _own(_data(self._http_json(f"{DEEZER_API}/artist/{aid}/radio?limit=40", timeout=12)))
        except Exception:
            tracks = []
        if _add("radio", tracks):
            return _finish()

        # 4) faixas dos álbuns mais populares
        try:
            top_albums = sorted(albums or [], key=lambda a: a.get("fans") or 0, reverse=True)[:4]
            tracks = []
            if top_albums:
                results = self._http_json_many(
                    [f"{DEEZER_API}/album/{a.get('id')}/tracks?limit=60" for a in top_albums], timeout=12)
                pool = []
                for r in results:
                    for t in _data(r):
                        t = dict(t)
                        t.setdefault("artist", {"id": aid, "name": name})
                        pool.append(t)
                pool = _own(pool) or pool
                pool.sort(key=lambda t: t.get("rank") or 0, reverse=True)
                tracks = pool
        except Exception:
            tracks = []
        _add("albuns", tracks)
        return _finish()

    def _wiki_failed(self, msg):
        self.wiki_spinner.stop()
        self.wiki_bio_label.set_text(msg)

    # ---------- Biografia em português (Wikipédia pt; fallback en + tradução) ----------
    def _wikipedia_music_extract(self, host, name):
        """Resumo (texto, url) da página de músico/banda com título igual ao nome (evita homônimos)."""
        try:
            q = urllib.parse.quote(name)
            url = (
                f"{host}/w/api.php?action=query&format=json&formatversion=2&generator=search"
                f"&gsrsearch={q}&gsrlimit=6&gsrnamespace=0&prop=description&redirects=1"
            )
            pages = self._http_json(url).get("query", {}).get("pages", []) or []
        except Exception:
            return None

        pages.sort(key=lambda pg: pg.get("index", 99))
        nk = self._norm_key(name)
        exact, prefix = [], []
        for pg in pages:
            tk = self._norm_key(re.sub(r"\s*\(.*?\)\s*", "", pg.get("title", "")))
            if tk == nk:
                exact.append(pg)
            elif tk.startswith(nk):
                prefix.append(pg)

        for pg in exact + prefix:
            desc = pg.get("description") or ""
            try:
                title = urllib.parse.quote(pg.get("title", "").replace(" ", "_"), safe="")
                summary = self._http_json(f"{host}/api/rest_v1/page/summary/{title}")
            except Exception:
                continue
            extract = summary.get("extract", "")
            if not extract or summary.get("type") == "disambiguation":
                continue
            if MUSIC_RE.search(desc or summary.get("description") or extract[:300]):
                page_url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
                return extract, page_url
        return None

    def _translate_to_pt(self, text):
        try:
            if len(text) > 1800:
                text = text[:1800].rsplit(".", 1)[0] + "."
            url = f"{TRANSLATE_API}?client=gtx&sl=en&tl=pt&dt=t&q={urllib.parse.quote(text)}"
            data = self._http_json(url, timeout=8)
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
            return out.strip() or None
        except Exception:
            return None

    def _fetch_artist_bio(self, name):
        """Biografia da Wikipédia (texto sob CC BY-SA 4.0: sempre cita fonte, licença e link)."""
        if not name:
            return "Biografia não encontrada."

        def credit(lang, page, note=""):
            line = f"\n\nFonte: Wikipédia ({lang}), licença CC BY-SA 4.0{note}"
            return line + (f"\n{page}" if page else "")

        found = self._wikipedia_music_extract(WIKIPEDIA_HOSTS[0], name)
        if found:
            extract, page = found
            return extract + credit("pt", page)
        found = self._wikipedia_music_extract(WIKIPEDIA_HOSTS[1], name)
        if found:
            extract, page = found
            translated = self._translate_to_pt(extract) if TRANSLATE_ENABLED else None
            if translated:
                return translated + credit("en", page, ", tradução automática")
            return extract + credit("en", page)
        return "Biografia não encontrada."

    def _populate_wiki(self, artist, albums, bio, top=None, related=None):
        self.wiki_spinner.stop()
        self.wiki_artist_name = artist.get("name", "")
        self.album_artist_name = self.wiki_artist_name
        self._loaded_key["wiki"] = self._norm_key(self.wiki_artist_name)
        self._album_cache.clear()
        self.album_token += 1
        self.album_spinner.stop()

        self.wiki_name_label.set_markup(
            f'<span size="xx-large" weight="bold">{GLib.markup_escape_text(self.wiki_artist_name)}</span>')
        fans = artist.get("nb_fan", 0) or 0
        self.wiki_fans_label.set_text(f"{fans:,} fãs no Deezer".replace(",", ".") if fans else "")
        self.wiki_bio_label.set_text(bio)

        for child in list(self.wiki_top_list.get_children()):
            self.wiki_top_list.remove(child)
        for i, t in enumerate(top or [], start=1):
            self.wiki_top_list.add(self._dtrack_row(t, i))
        self.wiki_top_list.show_all()

        albums = sorted(albums, key=lambda a: a.get("release_date") or "", reverse=True)
        cards = [self._album_card(alb, self.wiki_artist_name) for alb in albums]
        if not cards:
            cards = [self._placeholder("Nenhum álbum encontrado.")]
        self._fill_flow(self.wiki_albums_flow, cards)

        rel = [a for a in (related or []) if a.get("id") != artist.get("id")][:8]
        self._fill_flow(self.wiki_related_flow, [self._artist_card(a) for a in rel])
        self._set_shown(self.wiki_related_box, bool(rel))

        picture = artist.get("picture_medium") or artist.get("picture") or ""
        if picture:
            threading.Thread(target=self._load_artwork_into, args=(picture, self.wiki_art_img, 140, 140), daemon=True).start()

    # ---------- Faixas de um álbum (duplo clique na discografia) ----------
    def on_wiki_album_back(self, button=None):
        self.go_back()

    def _open_album(self, alb, artist_name=None):
        self.album_artist_name = artist_name or self.album_artist_name or self.wiki_artist_name
        self.album_token += 1
        token = self.album_token

        year = (alb.get("release_date") or "")[:4]
        self.album_title_label.set_markup(f"<b>{GLib.markup_escape_text(alb.get('title', ''))}</b>")
        self.album_meta_label.set_text(" · ".join(x for x in (self.album_artist_name, year) if x))
        self.album_cover_img.set_from_icon_name("media-optical", Gtk.IconSize.DIALOG)
        for child in list(self.album_tracks_list.get_children()):
            self.album_tracks_list.remove(child)
        self.navigate("album")

        cover = alb.get("cover_medium") or alb.get("cover_small")
        if cover:
            threading.Thread(target=self._load_artwork_into, args=(cover, self.album_cover_img, 64), daemon=True).start()

        cached = self._album_cache.get(alb.get("id"))
        if cached is not None:
            self._populate_album_tracks(alb, cached, token)
            return

        self.album_spinner.start()
        threading.Thread(target=self._album_thread, args=(alb, token), daemon=True).start()

    def _album_thread(self, alb, token):
        try:
            data = self._http_json(f"{DEEZER_API}/album/{alb.get('id')}/tracks?limit=100").get("data", []) or []
        except Exception:
            data = None
        if token == self.album_token:
            GLib.idle_add(self._populate_album_tracks, alb, data, token)

    def _populate_album_tracks(self, alb, tracks, token):
        if token != self.album_token:
            return
        self.album_spinner.stop()
        if tracks is None:
            self.mostrar_mensagem("Não foi possível carregar as faixas do álbum.")
            return
        self._album_cache[alb.get("id")] = tracks

        artist_default = self.album_artist_name
        for i, t in enumerate(tracks, start=1):
            artist_name = (t.get("artist") or {}).get("name") or artist_default
            dur = self._fmt_duration(t.get("duration"))
            pos = t.get("track_position") or i
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=f"{pos}. {t.get('title', '')}  ({dur})", xalign=0)
            lbl.set_line_wrap(True)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            row.add(lbl)
            row.item = self._normalize_track(
                {"title": t.get("title", ""), "artist": artist_name, "duration_fmt": dur, "verified": True}
            )
            self.album_tracks_list.add(row)
        self.album_tracks_list.show_all()

        year = (alb.get("release_date") or "")[:4]
        parts = [artist_default, year, f"{len(tracks)} faixas"]
        self.album_meta_label.set_text(" · ".join(x for x in parts if x))

    def on_album_track_activated(self, listbox, row):
        item = getattr(row, "item", None)
        if item:
            self.on_discover_track_play(item)

    def _album_items_for_action(self):
        rows = self.album_tracks_list.get_selected_rows() or self.album_tracks_list.get_children()
        return [r.item for r in rows if hasattr(r, "item")]

    def on_album_tracks_button_press(self, widget, event):
        return self._dtracks_context_menu(
            widget, event, lambda: [r.item for r in widget.get_selected_rows() if hasattr(r, "item")]
        )

    def on_album_track_button(self, action):
        self._run_dtracks_action(action, self._album_items_for_action())

    def on_album_add_to_playlist(self, button=None):
        items = self._album_items_for_action()
        if not items:
            self.mostrar_mensagem("As faixas do álbum ainda não foram carregadas.")
            return
        self._add_dtracks_to_playlist(items)

    def _album_tracks_to_items(self, tracks):
        artist_default = self.album_artist_name
        items = []
        for t in tracks:
            artist_name = (t.get("artist") or {}).get("name") or artist_default
            dur = self._fmt_duration(t.get("duration"))
            items.append(self._normalize_track(
                {"title": t.get("title", ""), "artist": artist_name, "duration_fmt": dur, "verified": True}
            ))
        return items

    # ---------- Artista tocando ----------
    def _sync_artist_tabs(self, name):
        name = (name or "").strip()
        if not name:
            return
        self.now_artist = name

    # ---------- Controle de Reprodução ----------
    def _mpris_notify(self, *props):
        m = getattr(self, "mpris", None)
        if m is not None:
            m.notify(*props)

    def _mpris_art(self, src_path, key):
        m = getattr(self, "mpris", None)
        if m is not None and src_path:
            cur = self._current_item()
            if cur is not None and (cur.get("id") or cur.get("path")) == key:
                m.set_art(src_path, key)
        return False

    def play_item(self, item):
        item = self._normalize_track(item)
        self.add_to_queue(item, notify=False)
        self.current_index = len(self.queue) - 1
        self.play_current()

    def _media_source(self, item):
        """Caminho do arquivo (faixa offline) ou URL do YouTube. None se o arquivo sumiu."""
        if item.get("stream_url"):          # rádio web: a URL do stream vai direto ao mpv
            return item["stream_url"]
        if item.get("path"):
            return item["path"] if os.path.isfile(item["path"]) else None
        return f"https://www.youtube.com/watch?v={item['id']}"

    def play_current(self, gapless=False):
        """Toca queue[current_index]. gapless=True: o mpv já está tocando esta faixa (pré-carregada);
        só a interface é sincronizada, sem novo loadfile (é o que elimina a pausa)."""
        if not (0 <= self.current_index < len(self.queue)):
            return
        item = self.queue[self.current_index]
        url = self._media_source(item)
        if url is None:
            self.mostrar_mensagem(f"Arquivo não encontrado: {item['title']} (pasta movida ou disco desconectado?)")
            self.show_toast("Arquivo offline não encontrado. Pulando.")
            self._highlight_current_row()
            self._missing_skips += 1
            if self._missing_skips <= len(self.queue):
                GLib.idle_add(self._skip_missing_file)
            return
        self._missing_skips = 0
        self._radio_pending_next = False

        self._play_token += 1
        self._track_started = False
        self._stall_retry_count = 0
        self._cancel_stall_watchdog()
        self._preload = None             # `loadfile replace` (ou a troca gapless) já descartou o plano anterior
        self._preload_fired = None
        self._preload_gen += 1
        self._gapless_guard = self._play_token if gapless else None

        self._mpv_paused = False
        self._mpv_idle = False
        self._update_playpause_icon()
        if not gapless:
            self.mpv.load(url)
        self.now_playing_label.set_text(f"{item['title']}")
        self._highlight_track_change()
        self.add_to_history(item)
        self._update_now_playing_card(item)
        self._mpris_pos = 0
        self._mpris_notify("Metadata", "PlaybackStatus", "CanGoNext", "CanGoPrevious", "CanSeek")

        self._schedule_stall_watchdog(self._play_token)

        if item.get("webradio"):
            self.stop_radio(notify=False)      # a "Rádio" de músicas parecidas não se aplica a uma transmissão ao vivo
            threading.Thread(target=self._load_webradio_thumbnail, args=(item, self._play_token), daemon=True).start()
            self._show_webradio_lyrics_panel(item)
            threading.Thread(target=webradio_register_click, args=(item["id"].split(":", 1)[-1],), daemon=True).start()
            return
        if item.get("path"):
            threading.Thread(target=self._load_local_thumbnail, args=(item, self._play_token), daemon=True).start()
        else:
            threading.Thread(target=self._fetch_thumbnail, args=(item["id"], item["title"]), daemon=True).start()
        self.search_current_lyrics()
        self._sync_artist_tabs(self._guess_artist(item))
        self._radio_refill()

    def _skip_missing_file(self):
        if self.current_index + 1 < len(self.queue):
            self.on_next(None)
        return False

    # ---------- Watchdog ----------
    def _schedule_stall_watchdog(self, token, delay_seconds=12):
        self._stall_check_id = GLib.timeout_add_seconds(delay_seconds, self._check_playback_stall, token)

    def _cancel_stall_watchdog(self):
        if self._stall_check_id is not None:
            try:
                GLib.source_remove(self._stall_check_id)
            except Exception:
                pass
            self._stall_check_id = None

    def _check_playback_stall(self, token):
        self._stall_check_id = None

        if token != self._play_token:
            return False

        if self._track_started:
            return False

        if not (0 <= self.current_index < len(self.queue)):
            return False

        item = self.queue[self.current_index]
        self._stall_retry_count += 1
        max_retries = 2

        if self._stall_retry_count <= max_retries:
            self.mostrar_mensagem(
                f"'{item['title']}' não iniciou. Tentando novamente ({self._stall_retry_count}/{max_retries})..."
            )
            url = self._media_source(item)
            if url is None:
                self.on_next(None)
                return False
            self.mpv.load(url)
            self._schedule_stall_watchdog(token)
        else:
            self.mostrar_mensagem(f"Não foi possível reproduzir '{item['title']}'. Pulando para a próxima faixa.")
            self.on_next(None)

        return False

    def _fetch_thumbnail(self, video_id, title):
        url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        icon_path = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()

            thumb_path = os.path.join(tempfile.gettempdir(), f"{APP_ID}_now_playing.jpg")
            try:
                with open(thumb_path, "wb") as f:
                    f.write(data)
                icon_path = thumb_path
            except OSError:
                icon_path = None

            pixbuf = self._decode_scaled(data, 64, 48)
            GLib.idle_add(self.thumbnail_img.set_from_pixbuf, pixbuf)
        except Exception:
            GLib.idle_add(self.thumbnail_img.set_from_icon_name, "audio-x-generic", Gtk.IconSize.DIALOG)
        finally:
            GLib.idle_add(self._notify, APP_NAME, f"Tocando: {title}", icon_path or "audio-x-generic")
            GLib.idle_add(self._mpris_art, icon_path, video_id)

    def _local_cover_cached(self, item):
        """(bytes, caminho_do_cache) da capa de uma faixa offline. Cache por álbum/pasta em COVERS_DIR."""
        path = item.get("path", "")
        os.makedirs(COVERS_DIR, exist_ok=True)
        key = re.sub(r"\W+", "_", f"{os.path.dirname(path)}_{item.get('album') or os.path.basename(path)}")[-120:]
        cache = os.path.join(COVERS_DIR, key + ".jpg")
        if os.path.isfile(cache):
            with open(cache, "rb") as fh:
                return fh.read(), cache
        data = local_cover_bytes(path)
        if not data:
            url = item.get("cover_url") or (getattr(self, "lib_index", {}).get(path) or {}).get("cover_url")
            if url:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = resp.read()
                except Exception:
                    data = None
        if data:
            with open(cache, "wb") as fh:
                fh.write(data)
            return data, cache
        return None, None

    def _load_local_artwork_into(self, item, image_widget, width, height):
        """Capa de faixa offline num card. Mantém a proporção (capas são quadradas) e centraliza."""
        try:
            data, _ = self._local_cover_cached(item)
            if not data:
                return
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pb = loader.get_pixbuf()
            ratio = min(width / pb.get_width(), height / pb.get_height())
            w, h = max(1, int(pb.get_width() * ratio)), max(1, int(pb.get_height() * ratio))
            GLib.idle_add(image_widget.set_from_pixbuf, pb.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR))
        except Exception:
            pass

    def _load_local_thumbnail(self, item, token):
        """Capa da faixa offline (embutida / folder.jpg), com cache em disco."""
        icon_path = None
        try:
            data, cache = self._local_cover_cached(item)
            if data:
                icon_path = cache
                if token == self._play_token:
                    GLib.idle_add(self.thumbnail_img.set_from_pixbuf, self._decode_scaled(data, 48, 48))
                    GLib.idle_add(self._set_lyrics_cover, self._decode_scaled(data, 80, 80), token)
            elif token == self._play_token:
                GLib.idle_add(self.thumbnail_img.set_from_icon_name, "audio-x-generic", Gtk.IconSize.DIALOG)
        except Exception:
            if token == self._play_token:
                GLib.idle_add(self.thumbnail_img.set_from_icon_name, "audio-x-generic", Gtk.IconSize.DIALOG)
        finally:
            GLib.idle_add(self._notify, APP_NAME, f"Tocando: {item.get('title', '')}", icon_path or "audio-x-generic")
            GLib.idle_add(self._mpris_art, icon_path, item.get("id") or item.get("path"))

    def _set_lyrics_cover(self, pixbuf, token):
        if token == self._play_token:
            self.lyrics_art_img.set_from_pixbuf(pixbuf)
        return False

    def _queue_label_markup(self, i, item, current=False):
        txt = GLib.markup_escape_text(f"{i+1}. {item['title']} ({item.get('duration','')})")
        return f"<b>▶ {txt}</b>" if current else txt

    def _highlight_current_row(self):
        """Marca a faixa tocando em negrito (e renumera). Não mexe no estado de seleção das linhas
        (usar SELECTED aqui fazia a seleção da fila 'sumir' e confundia o download)."""
        self._hl_idx = self.current_index
        if self._queue_dirty:
            return          # painel fechado: as linhas serão refeitas ao abrir
        for i, row in enumerate(self.queue_list.get_children()):
            lbl = getattr(row, "label", None)
            if lbl is not None and i < len(self.queue):
                lbl.set_markup(self._queue_label_markup(i, self.queue[i], i == self.current_index))

    def _highlight_track_change(self):
        """Troca de faixa: só a linha anterior e a nova mudam (antes eram todas, a cada música)."""
        old, new = self._hl_idx, self.current_index
        self._hl_idx = new
        if self._queue_dirty or self._queue_building:
            return
        for idx in {old, new}:
            if 0 <= idx < len(self.queue):
                row = self.queue_list.get_row_at_index(idx)
                lbl = getattr(row, "label", None) if row is not None else None
                if lbl is not None:
                    lbl.set_markup(self._queue_label_markup(idx, self.queue[idx], idx == new))

    # ---------- Callbacks do MPV ----------
    def _on_window_state(self, widget, event):
        self._iconified = bool(event.new_window_state & Gdk.WindowState.ICONIFIED)
        return False

    def on_mpv_time_change(self, pos):
        self._mpris_pos = pos or 0
        if pos and self.track_duration and self.track_duration - pos <= PRELOAD_MAX_S:
            self.on_mpv_time_remaining(self.track_duration - pos)     # pré-carregamento da próxima faixa (gapless)
        if pos and pos > 0.5 and not self._track_started:
            self._track_started = True
            self._cancel_stall_watchdog()
        if not self.user_is_seeking and not getattr(self, "_iconified", False):
            self.seek_scale.set_value(pos)
            sec = int(pos)
            if sec != getattr(self, "_shown_sec", None):   # texto só muda 1x por segundo
                self._shown_sec = sec
                self.time_label.set_text(f"{self._fmt_duration(pos)} / {self._fmt_duration(self.track_duration)}")
        return False

    def on_mpv_duration_change(self, duration):
        self.track_duration = duration or 0
        self.seek_scale.set_range(0, max(float(duration or 0), 1.0))
        self._shown_sec = None
        self._mpris_notify("Metadata")
        return False

    def on_mpv_pause_change(self, is_paused):
        self._mpv_paused = bool(is_paused)
        self._update_playpause_icon()
        return False

    def on_mpv_died(self):
        """O processo do mpv caiu: tenta reiniciar (no máx. 3 vezes por minuto) em vez de ficar mudo."""
        if getattr(self, "_closing", False):
            return False
        now = time.monotonic()
        self._mpv_restarts = [t for t in getattr(self, "_mpv_restarts", []) if now - t < 60]
        self._set_mpv_idle(True)
        if len(self._mpv_restarts) >= 3:
            self.mostrar_mensagem("O mpv parou de responder repetidamente. Reinicie o aplicativo.")
            return False
        self._mpv_restarts.append(now)
        if self.mpv.restart():
            self._apply_volume_ui()
            self.mostrar_mensagem("O player de áudio foi reiniciado. Aperte play para continuar.")
        else:
            self.mostrar_mensagem("Falha ao reiniciar o mpv. Verifique a instalação.")
        return False

    def on_mpv_idle_change(self, is_idle):
        self._mpv_idle = bool(is_idle)
        self._update_playpause_icon()
        # A faixa pré-carregada falhou ao abrir (mpv ficou ocioso): recupera já pelo caminho normal.
        if is_idle and self._gapless_guard == self._play_token and not self._track_started and not self._closing:
            self._gapless_guard = None
            self._cancel_stall_watchdog()
            self._check_playback_stall(self._play_token)
        return False

    def _set_mpv_idle(self, idle):
        self._mpv_idle = bool(idle)
        self._update_playpause_icon()

    def _update_playpause_icon(self):
        """Mostra 'pause' só quando realmente há áudio tocando; caso contrário, 'play'."""
        self._mpris_notify("PlaybackStatus", "CanSeek")
        playing = not self._mpv_idle and not self._mpv_paused
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        if icon == getattr(self, "_playpause_icon", None):
            return False
        self._playpause_icon = icon
        self.btn_playpause.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR))
        return False

    def toggle_playback(self):
        """Play/Pause: se nada está carregado, começa pela primeira faixa da fila."""
        if self._mpv_idle:
            if not self.queue:
                self.show_toast("Sua fila está vazia. Escolha ou adicione músicas para tocar.")
                self.mostrar_mensagem("Escolha ou adicione músicas à fila para começar.")
                return
            self.current_index = 0
            self.play_current()
            return
        self.mpv.pause_toggle()

    def on_track_finished(self):
        if self.is_repeat:
            self.play_current()
            return False
        # Gapless: o mpv já passou sozinho para a faixa pré-carregada; só sincroniza a interface.
        pre, self._preload = self._preload, None
        if pre and self.gapless_enabled and self._gapless_accept(pre):
            self.current_index = pre["idx"]
            self.play_current(gapless=True)
            return False
        has_next = (self.is_shuffle and len(self.queue) > 1) or self.current_index + 1 < len(self.queue)
        if not has_next and self._radio is None:
            self._set_mpv_idle(True)  # fila acabou: o botão volta a ser "play"
        self._radio_ended = (not has_next and self._radio is not None)   # com rádio, on_next busca mais faixas
        self.on_next(None)
        return False

    # ---------- Rádio (fila infinita com músicas parecidas) ----------
    RADIO_REFILL_AT = 2      # reabastece quando restam até N faixas à frente
    RADIO_BATCH = 8          # faixas adicionadas por vez
    RADIO_POOL = 40          # tamanho do mix baixado por chamada (sobra fica em reserva, sem rede)
    RADIO_MIN_SEC = 45
    RADIO_MAX_SEC = 720
    _RADIO_JUNK = re.compile(r"(?i)full\s+album|[aá]lbum\s+completo|\b\d+\s*(?:hours?|horas?)\b|non-?stop|compilation")
    _YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

    def _radio_key(self, t):
        return self._norm_key(t.get("title", "")) + "|" + self._norm_key(t.get("artist") or t.get("uploader") or "")

    def _radio_menu_item(self, item):
        mi = Gtk.MenuItem(label="Iniciar Mix da Faixa")
        mi.connect("activate", lambda w: self.start_radio(item))
        return mi

    def _radio_menu_item_dtrack(self, dtrack):
        """Faixa do Deezer (Descobrir/Wiki): localiza no YouTube e inicia a rádio a partir dela."""
        def go(w):
            self.show_toast("Buscando faixa no YouTube...")
            self._resolve_deezer_track(dtrack, lambda item: self.start_radio(item))
        mi = Gtk.MenuItem(label="Iniciar Mix da Faixa")
        mi.connect("activate", go)
        return mi

    def _radio_set_button(self, active):
        self._radio_ui_lock = True
        try:
            self.btn_radio.set_active(bool(active))
        finally:
            self._radio_ui_lock = False

    def on_toggle_radio(self, button):
        if self._radio_ui_lock:
            return
        if button.get_active():
            item = self._current_item()
            if item is None:
                self._radio_set_button(False)
                self.mostrar_mensagem("Toque uma música primeiro, ou use o botão direito > Iniciar Mix da Faixa.")
                return
            self.start_radio(item, play_now=False)
        else:
            self.stop_radio()

    def start_radio(self, item, play_now=True):
        """Liga a rádio a partir de uma faixa. play_now=True toca a faixa já; False só continua depois da fila."""
        item = self._normalize_track(item)
        if item.get("stream_url"):          # rádio web ao vivo: não existem "músicas parecidas" de uma transmissão
            self._radio_set_button(self._radio is not None)   # reflete o estado real (pode já haver uma Mix ligado)
            self.show_toast("O Mix automático não funciona com rádios ao vivo.")
            return
        self._radio_token += 1
        self._radio_busy = False
        self._radio_pending_next = False
        self._radio_ended = False
        sid = item.get("id", "")
        sid = sid if self._YT_ID_RE.match(sid or "") else ""
        self._radio = {"seed": item, "seed_id": sid, "last_seed_id": sid, "pool": [], "fails": 0}
        self._radio_set_button(True)
        if self.is_shuffle:
            self.btn_shuffle.set_active(False)   # rádio segue a ordem da fila
        if play_now:
            self.show_toast("Mix: buscando músicas parecidas...")
            self.play_item(item)                 # play_current já dispara o reabastecimento
        else:
            ahead = len(self.queue) - self.current_index - 1
            self.show_toast("Mix ligado." if ahead <= self.RADIO_REFILL_AT
                            else "Mix ligado: entra depois do que já está na fila.")
        self._radio_refill()

    def stop_radio(self, notify=True):
        if self._radio is None:
            return
        self._radio = None
        self._radio_token += 1
        self._radio_busy = False
        self._radio_pending_next = False
        if self._radio_ended:
            self._radio_ended = False
            self._set_mpv_idle(True)
        self._radio_set_button(False)
        if notify:
            self.show_toast("Mix desligado.")

    def _radio_refill(self, force=False):
        """Pede mais faixas em segundo plano quando a fila está acabando (não bloqueia a interface)."""
        rd = self._radio
        if rd is None or self._radio_busy:
            return
        ahead = len(self.queue) - self.current_index - 1
        if not force and ahead > self.RADIO_REFILL_AT:
            return
        self._radio_busy = True
        ex_ids = {t.get("id") for t in self.queue if t.get("id")} | {t.get("id") for t in self.history[:60] if t.get("id")}
        ex_keys = {self._radio_key(t) for t in self.queue[-150:]}
        tail = [self._norm_key(t.get("artist") or t.get("uploader") or "") for t in self.queue[-2:]]
        threading.Thread(target=self._radio_fill_thread,
                         args=(rd, self._radio_token, ex_ids, ex_keys, tail), daemon=True).start()

    def _radio_take(self, pool, want, ex_ids, ex_keys, tail):
        """Tira até `want` faixas do pool: sem repetir faixa/título e sem o mesmo artista 3x seguidas."""
        out, keep = [], []
        for t in pool:
            if len(out) >= want:
                keep.append(t)
                continue
            k = self._radio_key(t)
            if t["id"] in ex_ids or k in ex_keys:
                continue
            ak = self._norm_key(t.get("artist") or "")
            if ak and len(tail) >= 2 and tail[-1] == ak and tail[-2] == ak:
                keep.append(t)          # adia: fica para o próximo lote
                continue
            out.append(t)
            ex_ids.add(t["id"])
            ex_keys.add(k)
            tail.append(ak)
        pool[:] = keep
        return out

    def _radio_fetch_mix(self, seed_id):
        """Mix automático do YouTube para um vídeo (a mesma 'rádio' do site), via yt-dlp. Lista normalizada."""
        urls = (f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}",
                f"https://music.youtube.com/watch?v={seed_id}&list=RDAMVM{seed_id}")
        for url in urls:
            try:
                out = subprocess.check_output(
                    ["yt-dlp", url, "--yes-playlist", "--flat-playlist", "-j", "--no-warnings",
                     "--playlist-end", str(self.RADIO_POOL)],
                    stderr=subprocess.DEVNULL, text=True, timeout=40)
            except Exception:
                continue
            items = []
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vid = d.get("id") or self._extract_id(d.get("url", "") or "")
                if not vid or not self._YT_ID_RE.match(vid):
                    continue
                dur = d.get("duration")
                if dur and not (self.RADIO_MIN_SEC <= dur <= self.RADIO_MAX_SEC):
                    continue
                if d.get("live_status") in ("is_live", "is_upcoming"):
                    continue
                raw_title = d.get("title") or ""
                if self._RADIO_JUNK.search(raw_title):
                    continue
                title, artist = self._tidy_yt_metadata(raw_title, d.get("uploader") or d.get("channel") or "")
                items.append(self._normalize_track({
                    "id": vid, "title": title, "uploader": artist,
                    "duration": self._fmt_duration(dur), "verified": False}))
            if items:
                return items
        return []

    def _radio_deezer_fallback(self, rd, token, ex_ids, ex_keys, limit=5):
        """Reserva sem o mix do YouTube: rádio do artista no Deezer, cada faixa localizada no YouTube."""
        name = self._clean_artist_name(self._guess_artist(rd["seed"]))
        if not name:
            return []
        artist = self._deezer_search_artist(name)
        if not artist:
            return []
        data = self._http_json(f"{DEEZER_API}/artist/{artist['id']}/radio?limit=40", timeout=12).get("data", []) or []
        out = []
        for t in data:
            if token != self._radio_token or len(out) >= limit:
                break
            title = t.get("title_short") or t.get("title") or ""
            art = (t.get("artist") or {}).get("name", "")
            k = self._norm_key(title) + "|" + self._norm_key(art)
            if not title or k in ex_keys:
                continue
            res = self._resolve_one_dtrack({"artist": art, "title": title,
                                            "duration": self._fmt_duration(t.get("duration"))})
            if res and res["id"] not in ex_ids:
                out.append(res)
                ex_ids.add(res["id"])
                ex_keys.add(k)
        return out

    def _radio_fill_thread(self, rd, token, ex_ids, ex_keys, tail):
        picked = []
        try:
            if not rd.get("seed_id"):
                # faixa local/sem ID do YouTube: acha o vídeo por "artista - título"
                seed = rd["seed"]
                res = self._resolve_one_dtrack({"artist": seed.get("artist") or seed.get("uploader") or "",
                                                "title": seed.get("title", "")})
                if res:
                    rd["seed_id"] = rd["last_seed_id"] = res["id"]
            picked = self._radio_take(rd["pool"], self.RADIO_BATCH, ex_ids, ex_keys, tail)
            if not picked:
                tried = set()
                for sid in (rd.get("last_seed_id"), rd.get("seed_id")):
                    if not sid or sid in tried or token != self._radio_token:
                        continue
                    tried.add(sid)
                    mix = self._radio_fetch_mix(sid)
                    print(f"[rádio] mix de {sid}: {len(mix)} faixa(s)", file=sys.stderr)
                    rd["pool"].extend(mix)
                    picked = self._radio_take(rd["pool"], self.RADIO_BATCH, ex_ids, ex_keys, tail)
                    if picked:
                        break
            if not picked and token == self._radio_token:
                picked = self._radio_deezer_fallback(rd, token, ex_ids, ex_keys)
                print(f"[rádio] reserva Deezer: {len(picked)} faixa(s)", file=sys.stderr)
        except Exception as e:
            print(f"[rádio] erro: {e}", file=sys.stderr)
        GLib.idle_add(self._radio_append, picked, token)

    def _radio_append(self, items, token):
        if token != self._radio_token or self._radio is None:
            return False                # rádio desligada/reiniciada durante a busca: descarta
        self._radio_busy = False
        rd = self._radio
        if items:
            rd["fails"] = 0
            rd["last_seed_id"] = items[-1]["id"]      # a rádio "anda": o próximo mix parte da última faixa
            first_new = len(self.queue)
            self.add_items_bulk(items, notify=False)
            if self._radio_pending_next:
                self._radio_pending_next = False
                self._radio_ended = False
                self.current_index = first_new
                self.play_current()
            return False
        rd["fails"] += 1
        if self._radio_pending_next:
            self._radio_pending_next = False
            if self._radio_ended:
                self._radio_ended = False
                self._set_mpv_idle(True)
                self.now_playing_label.set_text("Fila finalizada")
        if rd["fails"] >= 3:
            self.stop_radio(notify=False)
            self.show_toast("Mix desligado: não encontrei mais músicas parecidas.")
        else:
            self.show_toast("Mix: não encontrei músicas parecidas agora.")
        return False

    def on_load_error(self, error_msg):
        item = self.queue[self.current_index] if 0 <= self.current_index < len(self.queue) else None
        titulo = item["title"] if item else "faixa"
        self.mostrar_mensagem(f"Falha ao carregar '{titulo}': {error_msg}")
        if not self._track_started and item is not None:
            self._cancel_stall_watchdog()
            self._check_playback_stall(self._play_token)
        return False

    # ---------- Ações de Botões e Sliders ----------
    def on_play_pause(self, button):
        self.toggle_playback()

    def on_prev(self, button):
        if self.current_index > 0:
            self.current_index -= 1
            self.play_current()

    def on_next(self, button):
        if not self.queue:
            return
        if self.is_shuffle and len(self.queue) > 1:
            next_idx = self.current_index
            while next_idx == self.current_index:
                next_idx = random.randint(0, len(self.queue) - 1)
            self.current_index = next_idx
            self.play_current()
        elif self.current_index + 1 < len(self.queue):
            self.current_index += 1
            self.play_current()
        elif self._radio is not None:
            self._radio_pending_next = True      # toca a primeira faixa nova assim que chegar
            self.show_toast("Mix: buscando mais músicas...")
            self._radio_refill(force=True)
        else:
            self.now_playing_label.set_text("Fila finalizada")

    def on_toggle_shuffle(self, button):
        self.is_shuffle = button.get_active()
        self._invalidate_preload()
        self._mpris_notify("Shuffle", "CanGoNext")

    def on_toggle_repeat(self, button):
        self.is_repeat = button.get_active()
        self._invalidate_preload()
        self._mpris_notify("LoopStatus")

    def on_volume_changed(self, scale):
        vol = int(scale.get_value())
        if vol > 0:
            self._last_volume = vol
            if self.is_muted:  # mexer no slider tira do mudo
                self.is_muted = False
                self.mpv.set_mute(False)
        self.mpv.set_volume(vol)
        self.config["volume"] = vol
        self._apply_volume_ui()
        self._schedule_config_save()

    def on_toggle_mute(self, button=None):
        vol = int(self.vol_scale.get_value())
        if self.is_muted or vol == 0:
            self.is_muted = False
            self.mpv.set_mute(False)
            if vol == 0:
                self.vol_scale.set_value(self._last_volume)  # dispara on_volume_changed
                return
        else:
            self.is_muted = True
            self.mpv.set_mute(True)
        self._apply_volume_ui()

    def on_volume_scroll(self, widget, event):
        if event.direction == Gdk.ScrollDirection.UP:
            delta = 5
        elif event.direction == Gdk.ScrollDirection.DOWN:
            delta = -5
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = event.get_scroll_deltas()
            delta = -5 if dy > 0 else (5 if dy < 0 else 0)
        else:
            return False
        if delta:
            self.vol_scale.set_value(min(100, max(0, self.vol_scale.get_value() + delta)))
        return True

    def _apply_volume_ui(self):
        vol = int(self.vol_scale.get_value())
        silent = self.is_muted or vol == 0
        if silent:
            icon = "audio-volume-muted-symbolic"
        elif vol < 34:
            icon = "audio-volume-low-symbolic"
        elif vol < 67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        if icon != self._mute_icon:
            self._mute_icon = icon
            self.mute_img.set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self.btn_mute.set_tooltip_text("Ativar som (M)" if silent else "Silenciar (M)")
        self.vol_scale.set_tooltip_text("Mudo" if self.is_muted else f"Volume: {vol}%")
        self._mpris_notify("Volume")

    def _schedule_config_save(self):
        # Debounce: evita gravar o config a cada pixel arrastado no slider
        if self._config_save_id:
            GLib.source_remove(self._config_save_id)
        self._config_save_id = GLib.timeout_add(600, self._flush_config_save)

    def _flush_config_save(self):
        self._config_save_id = None
        self._save_json(CONFIG_FILE, self.config)
        return False

    def on_seek_start(self, widget, event):
        self.user_is_seeking = True

    def on_seek_finish(self, widget, event):
        val = self.seek_scale.get_value()
        self.mpv.seek(val)
        self.user_is_seeking = False
        self._mpris_pos = val
        if getattr(self, "mpris", None) is not None:
            self.mpris.seeked(val)

    # ---------- Colar Link Manual ----------
    def on_paste_link(self, button=None):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        link = clipboard.wait_for_text()
        if not link:
            self.mostrar_mensagem("Nenhum link copiado.")
            return

        self.search_entry.set_text(link)
        self.on_search(None)

    # ---------- Download MP3 (Com Progresso em Real-time) ----------
    def _selection_lists(self):
        return [self.results_list, self.queue_list, self.pl_tracks_list, self.history_list,
                self.favorites_list, self.discover_tracks_list, self.offline_tracks_list,
                self.webradio_list, self.mylib_list]

    def _wire_selection_tracking(self):
        """Só UMA lista mantém seleção por vez; a última em que você clicou é a 'ativa'.
        Antes, a lista de resultados/recomendadas guardava a seleção antiga e, por ter
        prioridade fixa, o botão Baixar (e '+ Playlist') agia sempre nela, nunca na Fila."""
        self._sel_guard = False
        self._active_list = None
        for lb in self._selection_lists():
            lb.connect("selected-rows-changed", self._on_any_selection_changed)

    def _on_any_selection_changed(self, lb):
        if self._sel_guard or not lb.get_selected_rows():
            return
        self._sel_guard = True
        try:
            for other in self._selection_lists():
                if other is not lb and other.get_selected_rows():
                    other.unselect_all()
            self._active_list = lb
        finally:
            self._sel_guard = False

    def get_active_selection(self):
        lists = self._selection_lists()
        active = getattr(self, "_active_list", None)
        if active is not None:
            lists = [active] + [lb for lb in lists if lb is not active]
        for lb in lists:
            items = [r.item for r in lb.get_selected_rows() if hasattr(r, "item")]
            if items:
                return items
        return []

    def on_download(self, button):
        items = self.get_active_selection()
        if not items:
            self.mostrar_mensagem("Selecione uma ou mais faixas para baixar.")
            return
        online = [i for i in items if not i.get("path") and not i.get("webradio")]
        if not online:
            self.mostrar_mensagem("As faixas selecionadas já estão offline ou são rádios ao vivo (não baixáveis).")
            return
        if len(online) < len(items):
            self.show_toast(f"{len(items) - len(online)} faixa(s) offline ignorada(s).")
        items = online
        if getattr(self, "_active_list", None) is self.discover_tracks_list:
            self._download_dtracks(items)  # recomendadas ainda não têm id do YouTube
            return
        if len(items) == 1:
            self._download_single_dialog(items[0])
        else:
            self._download_bulk_dialog(items)

    def _download_single_dialog(self, item):
        item = self._normalize_track(item)
        dialog = Gtk.FileChooserDialog(title="Salvar MP3", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        dialog.add_button("Salvar", Gtk.ResponseType.OK)
        safe_title = re.sub(r'[\\/*?:"<>|]', "", item["title"])
        dialog.set_current_name(f"{safe_title}.mp3")
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not path:
            return

        self.progress_download.set_fraction(0.0)
        self.progress_download.show()
        self._download_pulse("Conectando ao YouTube...")
        threading.Thread(target=self._download_thread, args=(item, path), daemon=True).start()

    def _download_thread(self, item, path):
        out_template = path[:-4] if path.lower().endswith(".mp3") else path
        try:
            final = self._download_track_mp3(
                item,
                out_template,
                lambda pct: GLib.idle_add(self._update_download_progress, pct, f"Baixando... {int(pct*100)}%"),
                lambda text, stage: GLib.idle_add(self._download_pulse, text),
            )
            if final:
                GLib.idle_add(self._update_download_progress, 1.0, "Download concluído!")
                GLib.idle_add(self._notify, "Download", f"Salvo em: {final}", "folder-download-symbolic")
            else:
                GLib.idle_add(self._download_fail, "Erro no download.")
        except Exception as e:
            GLib.idle_add(self._download_fail, f"Erro: {e}")
        finally:
            GLib.timeout_add_seconds(3, self._download_hide)

    def _update_download_progress(self, frac, text):
        self._download_stop_pulse()
        self.progress_download.set_fraction(frac)
        self.progress_download.set_text(text)

    def _download_pulse(self, text):
        """Etapa sem porcentagem conhecida (conversão, tags...): barra em vai-e-vem + texto da etapa."""
        self.progress_download.set_text(text)
        if getattr(self, "_dl_pulse_id", None) is None:
            self.progress_download.set_pulse_step(0.08)
            self.progress_download.pulse()
            self._dl_pulse_id = GLib.timeout_add(120, self._download_pulse_tick)

    def _download_pulse_tick(self):
        if getattr(self, "_dl_pulse_id", None) is None:
            return False
        self.progress_download.pulse()
        return True

    def _download_stop_pulse(self):
        pid = getattr(self, "_dl_pulse_id", None)
        if pid is not None:
            self._dl_pulse_id = None
            GLib.source_remove(pid)

    def _download_fail(self, text):
        self._download_stop_pulse()
        self.progress_download.set_fraction(0.0)
        self.progress_download.set_text(text)

    def _download_hide(self):
        self._download_stop_pulse()
        self.progress_download.hide()
        return False

    def _download_bulk_dialog(self, items):
        dialog = Gtk.FileChooserDialog(
            title="Selecionar pasta para downloads", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        dialog.add_button("Selecionar", Gtk.ResponseType.OK)
        response = dialog.run()
        folder = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not folder:
            return

        self.progress_download.set_fraction(0.0)
        self.progress_download.set_text(f"Baixando 0/{len(items)}...")
        self.progress_download.show()
        threading.Thread(target=self._download_bulk_thread, args=(items, folder), daemon=True).start()

    def _download_bulk_thread(self, items, folder):
        total = len(items)
        ok = 0
        for i, item in enumerate(items, start=1):
            item = self._normalize_track(item)
            safe_title = re.sub(r'[\\/*?:"<>|]', "", item["title"])
            out_template = os.path.join(folder, safe_title)
            GLib.idle_add(self._update_download_progress, (i - 1) / total, f"Faixa {i}/{total} · Conectando ao YouTube...")
            try:
                # O download ocupa 85% da "fatia" da faixa; o restante é conversão, capa e tags.
                final = self._download_track_mp3(
                    item,
                    out_template,
                    lambda pct, i=i: GLib.idle_add(
                        self._update_download_progress, ((i - 1) + pct * 0.85) / total,
                        f"Faixa {i}/{total} · Baixando... {int(pct*100)}%"),
                    lambda text, stage, i=i: GLib.idle_add(
                        self._update_download_progress, ((i - 1) + stage) / total,
                        f"Faixa {i}/{total} · {text}"),
                )
                if final:
                    ok += 1
            except Exception:
                pass
            GLib.idle_add(self._update_download_progress, i / total, f"Concluído {i}/{total}")
        GLib.idle_add(self._update_download_progress, 1.0,
                      "Todos os downloads foram concluídos!" if ok == total
                      else f"Concluído: {ok} de {total} faixa(s).")
        GLib.idle_add(self._notify, "Download", f"{ok} faixa(s) salvas em: {folder}", "folder-download-symbolic")
        GLib.timeout_add_seconds(3, self._download_hide)

    # ---------- Download: capa de álbum e metadados ----------
    def _download_track_mp3(self, item, out_template, on_progress, on_phase):
        """Baixa a faixa como MP3 em '<out_template>.mp3' e grava capa + metadados.
        Os dados do Deezer são buscados em paralelo ao yt-dlp. Retorna o caminho do MP3 ou None.
        on_progress(pct 0..1) acompanha o download; on_phase(texto, estágio 0..1) avisa cada etapa
        seguinte (conversão, capa, tags), que não têm porcentagem."""
        url = f"https://www.youtube.com/watch?v={item['id']}"
        result = {}

        def lookup_worker():
            try:
                meta = self._lookup_download_meta(item)
                result["meta"] = meta
                result["cover"] = self._download_cover_bytes(meta.get("cover_url"))
            except Exception:
                pass

        lookup = threading.Thread(target=lookup_worker, daemon=True)
        lookup.start()

        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-playlist",
            "--embed-metadata",                # tags do YouTube (fallback caso o Deezer não case)
            "--embed-thumbnail",               # miniatura do YouTube (fallback caso o Deezer não case)
            "--convert-thumbnails", "jpg",
            "--newline",
            "-o", f"{out_template}.%(ext)s",
            url,
        ]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._dl_procs.add(process)
        phase = None
        for line in process.stdout:
            match = re.search(r"\[download\]\s+(\d+\.\d+)%", line)
            if match:
                on_progress(float(match.group(1)) / 100.0)
                continue
            new_phase = None
            if line.startswith("[ExtractAudio]"):
                new_phase = ("Convertendo para MP3...", 0.88)
            elif line.startswith("[Metadata]"):
                new_phase = ("Gravando metadados...", 0.92)
            elif line.startswith(("[ThumbnailsConvertor]", "[EmbedThumbnail]")):
                new_phase = ("Processando capa...", 0.94)
            if new_phase and new_phase[0] != phase:
                phase = new_phase[0]
                on_phase(*new_phase)
        on_phase("Finalizando arquivo...", 0.95)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
        finally:
            self._dl_procs.discard(process)

        final = out_template + ".mp3"
        if process.returncode != 0 or not os.path.isfile(final):
            return None

        if lookup.is_alive():
            on_phase("Buscando dados do álbum...", 0.96)
            lookup.join(timeout=20)
        on_phase("Gravando capa e metadados do álbum...", 0.98)

        meta = dict(result.get("meta") or {})
        if item.get("title"):
            meta.setdefault("title", item["title"])
        artist = item.get("artist") or item.get("uploader") or ""
        if artist:
            meta.setdefault("artist", self._clean_artist_name(artist) or artist)
        try:
            self._embed_tags_mp3(final, meta, result.get("cover"))
        except Exception:
            pass  # o MP3 já está salvo; tags e capa são um extra
        return final

    def _lookup_download_meta(self, item):
        """Busca no Deezer título, artista, álbum, ano, nº da faixa, gênero e capa.
        Devolve {} se não houver correspondência segura (mesmo título, mesmo artista, duração próxima)."""
        title = self._clean_title(item.get("title") or "")
        artist = self._clean_artist_name(item.get("artist") or item.get("uploader") or "")
        if not title or not artist:
            return {}
        main_title = re.sub(r"[\(\[].*?[\)\]]", " ", title)
        want_main = self._norm_key(main_title)
        want_full = self._norm_key(title)
        want_artist = self._norm_key(artist)
        if not want_main or not want_artist:
            return {}
        want_dur = _clock_to_seconds(item.get("duration") or item.get("duration_fmt"))

        def pick(candidates):
            best, best_score = None, 0.0
            for t in candidates:
                t_artist = self._norm_key((t.get("artist") or {}).get("name", ""))
                t_main = self._norm_key(re.sub(r"[\(\[].*?[\)\]]", " ", t.get("title", "")))
                if not t_artist or t_main != want_main:
                    continue
                if not (want_artist == t_artist or want_artist in t_artist or t_artist in want_artist):
                    continue
                score = 10.0
                if self._norm_key(t.get("title", "")) == want_full:
                    score += 5
                if want_artist == t_artist:
                    score += 4
                dz_dur = t.get("duration") or 0
                if want_dur and dz_dur:
                    diff = abs(want_dur - dz_dur)
                    if diff > 20:
                        continue
                    score += 6 if diff <= 3 else (3 if diff <= 10 else 0)
                if score > best_score:
                    best, best_score = t, score
            return best

        best = None
        for query in (f'artist:"{artist}" track:"{title}"', f"{artist} {title}"):
            try:
                data = self._http_json(
                    f"{DEEZER_API}/search?q={urllib.parse.quote(query)}&limit=15", timeout=6
                ).get("data", []) or []
            except Exception:
                data = []
            best = pick(data)
            if best:
                break
        if not best:
            return {}

        album = best.get("album") or {}
        urls, kinds = [], []
        if best.get("id"):
            urls.append(f"{DEEZER_API}/track/{best['id']}")
            kinds.append("track")
        if album.get("id"):
            urls.append(f"{DEEZER_API}/album/{album['id']}")
            kinds.append("album")
        details = dict(zip(kinds, self._http_json_many(urls, timeout=6))) if urls else {}
        trk = details.get("track") or {}
        alb = details.get("album") or {}

        names = [c.get("name") for c in (trk.get("contributors") or []) if c.get("name")]
        if not names:
            names = [(best.get("artist") or {}).get("name") or artist]
        release = alb.get("release_date") or trk.get("release_date") or ""
        genres = (alb.get("genres") or {}).get("data") or []
        cover_url = (alb.get("cover_xl") or album.get("cover_xl") or alb.get("cover_big")
                     or album.get("cover_big") or album.get("cover_medium") or "")
        if "/cover//" in cover_url:  # álbum sem capa (imagem padrão do Deezer)
            cover_url = ""

        return {
            "title": best.get("title") or title,
            "artist": ", ".join(dict.fromkeys(names)),
            "album": alb.get("title") or album.get("title") or "",
            "album_artist": (alb.get("artist") or {}).get("name") or (best.get("artist") or {}).get("name") or "",
            "year": release[:4],
            "track_no": trk.get("track_position") or 0,
            "track_total": alb.get("nb_tracks") or 0,
            "disc_no": trk.get("disk_number") or 0,
            "genre": genres[0].get("name", "") if genres else "",
            "isrc": trk.get("isrc") or "",
            "cover_url": cover_url,
        }

    def _download_cover_bytes(self, url):
        """Baixa a capa (JPEG/PNG). None se falhar ou se não for uma imagem válida."""
        if not url:
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(8 * 1024 * 1024)
        except Exception:
            return None
        if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
        return None

    def _embed_tags_mp3(self, path, meta, cover):
        """Grava tags ID3 e capa no MP3: mutagen se estiver instalado, senão ffmpeg.
        Sempre remove o campo Comentários (o yt-dlp grava ali só a URL do vídeo)."""
        if mutagen is not None:
            try:
                self._embed_tags_mutagen(path, meta, cover)
                return True
            except Exception:
                pass
        return self._embed_tags_ffmpeg(path, meta, cover)

    def _embed_tags_mutagen(self, path, meta, cover):
        from mutagen.id3 import (ID3, ID3NoHeaderError, APIC, TALB, TCON, TDRC, TIT2,
                                 TPE1, TPE2, TPOS, TRCK, TSRC)
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        def put(frame_id, frame):
            tags.delall(frame_id)
            tags.add(frame)

        tags.delall("COMM")  # Comentários: o yt-dlp coloca só a URL do vídeo
        for key in [k for k in tags.keys() if k.startswith("TXXX:") and k[5:].strip().lower() == "comment"]:
            del tags[key]

        if meta.get("title"):
            put("TIT2", TIT2(encoding=3, text=[meta["title"]]))
        if meta.get("artist"):
            put("TPE1", TPE1(encoding=3, text=[meta["artist"]]))
        if meta.get("album_artist"):
            put("TPE2", TPE2(encoding=3, text=[meta["album_artist"]]))
        if meta.get("album"):
            put("TALB", TALB(encoding=3, text=[meta["album"]]))
        if meta.get("year"):
            put("TDRC", TDRC(encoding=3, text=[meta["year"]]))
        if meta.get("track_no"):
            trck = str(meta["track_no"])
            if meta.get("track_total"):
                trck += f"/{meta['track_total']}"
            put("TRCK", TRCK(encoding=3, text=[trck]))
        if meta.get("disc_no"):
            put("TPOS", TPOS(encoding=3, text=[str(meta["disc_no"])]))
        if meta.get("genre"):
            put("TCON", TCON(encoding=3, text=[meta["genre"]]))
        if meta.get("isrc"):
            put("TSRC", TSRC(encoding=3, text=[meta["isrc"]]))
        if cover:
            mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
            tags.delall("APIC")  # troca a miniatura do YouTube pela capa do álbum
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)  # ID3v2.3: máxima compatibilidade com players e celulares

    def _embed_tags_ffmpeg(self, path, meta, cover):
        """Fallback sem mutagen: remuxa o MP3 com ffmpeg (sem recodificar) gravando tags e capa."""
        if not shutil.which("ffmpeg"):
            return False
        tmp_out = path + ".tagged.mp3"
        cover_path = None
        try:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", path]
            if cover:
                ext = ".png" if cover[:4] == b"\x89PNG" else ".jpg"
                fd, cover_path = tempfile.mkstemp(suffix=ext)
                with os.fdopen(fd, "wb") as f:
                    f.write(cover)
                cmd += ["-i", cover_path, "-map", "0:a", "-map", "1:0",
                        "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
            else:
                cmd += ["-map", "0"]
            cmd += ["-c", "copy", "-id3v2_version", "3", "-write_id3v1", "1", "-metadata", "comment="]
            track = str(meta.get("track_no") or "")
            if track and meta.get("track_total"):
                track += f"/{meta['track_total']}"
            fields = {
                "title": meta.get("title"),
                "artist": meta.get("artist"),
                "album_artist": meta.get("album_artist"),
                "album": meta.get("album"),
                "date": meta.get("year"),
                "track": track,
                "disc": str(meta.get("disc_no") or ""),
                "genre": meta.get("genre"),
            }
            for key, val in fields.items():
                if val:
                    cmd += ["-metadata", f"{key}={val}"]
            cmd.append(tmp_out)
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
            os.replace(tmp_out, path)
            return True
        except Exception:
            return False
        finally:
            for p in (tmp_out, cover_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # ---------- Atalhos de Teclado Ampliados ----------
    def on_key_press(self, widget, event):
        focused = self.get_focus()
        in_entry = isinstance(focused, Gtk.Entry)
        state = event.state & Gdk.ModifierType.CONTROL_MASK

        if self._pl_busy:
            navega = (state and event.keyval in (Gdk.KEY_v, Gdk.KEY_V, Gdk.KEY_f, Gdk.KEY_F)) or (
                event.state & Gdk.ModifierType.MOD1_MASK)
            if navega:
                return True

        # Ctrl + V: Colar link de qualquer lugar da aplicação
        if state and event.keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if not in_entry:
                self.on_paste_link()
                return True

        # Ctrl + E: equalizador e opções de áudio
        if state and event.keyval in (Gdk.KEY_e, Gdk.KEY_E) and not in_entry:
            self.on_open_audio()
            return True

        # Ctrl + F: foca a busca
        if state and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_entry.grab_focus()
            return True

        # Atalhos ativados fora de campos de texto
        if not in_entry:
            if event.keyval == Gdk.KEY_space:
                self.toggle_playback()
                return True

            elif not state and event.keyval in (Gdk.KEY_m, Gdk.KEY_M):
                self.on_toggle_mute()
                return True

            elif not state and event.keyval in (Gdk.KEY_l, Gdk.KEY_L):
                self.on_like_current()
                return True

            elif event.state & Gdk.ModifierType.MOD1_MASK and event.keyval == Gdk.KEY_Left:
                self.go_back()
                return True

            elif state and event.keyval == Gdk.KEY_Left:
                self.on_prev(None)
                return True

            elif state and event.keyval == Gdk.KEY_Right:
                self.on_next(None)
                return True

            elif event.keyval == Gdk.KEY_Left:
                self.mpv.seek_relative(-5)
                return True

            elif event.keyval == Gdk.KEY_Right:
                self.mpv.seek_relative(5)
                return True

        return False


    # ======================================================================
    # Boas-vindas
    # ======================================================================
    def _build_welcome_overlay(self):
        """Cartão flutuante (estilo OSD do tema) que desliza no topo ao abrir o app."""
        self.welcome_revealer = Gtk.Revealer()
        self.welcome_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.welcome_revealer.set_transition_duration(450)
        self.welcome_revealer.set_halign(Gtk.Align.CENTER)
        self.welcome_revealer.set_valign(Gtk.Align.START)
        self.welcome_revealer.set_margin_top(14)
        self.welcome_revealer.set_margin_start(16)
        self.welcome_revealer.set_margin_end(16)

        event_box = Gtk.EventBox()
        event_box.connect("button-press-event", lambda w, e: self._hide_welcome() or True)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        card.get_style_context().add_class("osd")
        card.set_border_width(18)

        icon = Gtk.Image.new_from_icon_name("audio-headphones-symbolic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(44)
        card.pack_start(icon, False, False, 0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.welcome_greeting = Gtk.Label(xalign=0)
        self.welcome_name = Gtk.Label(xalign=0)
        self.welcome_name.set_ellipsize(3)
        self.welcome_name.set_max_width_chars(28)
        self.welcome_sub = Gtk.Label(xalign=0)
        self.welcome_sub.set_line_wrap(True)
        self.welcome_sub.set_max_width_chars(34)
        texts.pack_start(self.welcome_greeting, False, False, 0)
        texts.pack_start(self.welcome_name, False, False, 0)
        texts.pack_start(self.welcome_sub, False, False, 2)
        card.pack_start(texts, True, True, 0)

        event_box.add(card)
        self.welcome_revealer.add(event_box)
        return self.welcome_revealer

    def _time_greeting(self):
        hour = datetime.datetime.now().hour
        if hour < 5:
            return "Boa madrugada"
        if hour < 12:
            return "Bom dia"
        if hour < 18:
            return "Boa tarde"
        return "Boa noite"

    def _show_welcome(self):
        name = (self.profile.get("name") or "").strip()
        n_pl, n_tracks, _unique = self._profile_stats()
        esc = GLib.markup_escape_text

        if name:
            greeting = self._time_greeting().upper()
            title = name
            if n_pl:
                pl_txt = "1 playlist" if n_pl == 1 else f"{n_pl} playlists"
                tr_txt = "1 faixa salva" if n_tracks == 1 else f"{n_tracks} faixas salvas"
                sub = f"Que bom ter você de volta. {pl_txt} · {tr_txt} esperando por você."
            else:
                sub = "Que bom ter você de volta. Vamos encontrar algo bom para ouvir?"
        else:
            greeting = "BEM-VINDO"
            title = APP_NAME
            sub = "Defina seu nome em Sobre › Perfil de usuário para uma recepção personalizada."

        self.welcome_greeting.set_markup(f'<span size="small" weight="bold" letter_spacing="2048">{esc(greeting)}</span>')
        self.welcome_name.set_markup(f'<span size="xx-large" weight="bold">{esc(title)}</span>')
        self.welcome_sub.set_markup(f'<span size="small">{esc(sub)}</span>')

        self.welcome_revealer.set_reveal_child(True)
        if self._welcome_timeout_id:
            GLib.source_remove(self._welcome_timeout_id)
        self._welcome_timeout_id = GLib.timeout_add_seconds(5, self._hide_welcome)
        return False

    def _hide_welcome(self):
        self.welcome_revealer.set_reveal_child(False)
        if self._welcome_timeout_id:
            try:
                GLib.source_remove(self._welcome_timeout_id)
            except Exception:
                pass
            self._welcome_timeout_id = None
        return False

    # ======================================================================
    # Aba "Sobre" (wiki do aplicativo)
    # ======================================================================
    def _about_heading(self, text):
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(text)}</span>')
        lbl.set_margin_top(12)
        return lbl

    def _about_text(self, markup, dim=False):
        lbl = Gtk.Label(xalign=0)
        lbl.set_line_wrap(True)
        lbl.set_markup(markup)
        if dim:
            lbl.get_style_context().add_class("dim-label")
        return lbl

    def _about_card(self, title, body, tag=None):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(10)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label(xalign=0)
        t.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        head.pack_start(t, False, False, 0)
        if tag:
            tg = Gtk.Label(label=tag, xalign=1)
            tg.set_ellipsize(3)
            tg.get_style_context().add_class("dim-label")
            head.pack_end(tg, True, True, 0)
        box.pack_start(head, False, False, 0)
        box.pack_start(self._about_text(GLib.markup_escape_text(body)), False, False, 0)
        frame.add(box)
        return frame

    def _build_tab_about(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(14)

        # --- Cabeçalho ---
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon_path = _find_icon_file() or ""
        logo = Gtk.Image()
        try:
            if os.path.exists(icon_path):
                logo.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(icon_path, 64, 64))
            else:
                raise OSError
        except Exception:
            logo.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            logo.set_pixel_size(64)
        head.pack_start(logo, False, False, 0)

        head_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0)
        title.set_markup(f'<span size="x-large" weight="bold">{APP_NAME}</span>')
        ver = Gtk.Label(label=f"{APP_TAGLINE}\nVersão {APP_VERSION} · Python + GTK3 + mpv\n\nSoftware livre ({APP_LICENSE})\n© {APP_YEAR} {APP_AUTHOR}", xalign=0)
        ver.set_line_wrap(True)
        ver.get_style_context().add_class("dim-label")
        head_txt.pack_start(title, False, False, 0)
        head_txt.pack_start(ver, False, False, 0)
        head.pack_start(head_txt, True, True, 0)
        page.pack_start(head, False, False, 0)

        btn_profile = Gtk.Button(label="Perfil de usuário")
        btn_profile.set_image(Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.BUTTON))
        btn_profile.set_always_show_image(True)
        btn_profile.set_halign(Gtk.Align.START)
        btn_profile.connect("clicked", self.on_open_profile)

        btn_installer = Gtk.Button(label="Atualizar ou desinstalar")
        btn_installer.set_image(Gtk.Image.new_from_icon_name("system-software-update-symbolic", Gtk.IconSize.BUTTON))
        btn_installer.set_always_show_image(True)
        btn_installer.set_tooltip_text("Abre o instalador: atualizar, diagnosticar problemas ou desinstalar")
        btn_installer.connect("clicked", self.on_open_installer)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.START)
        btn_row.pack_start(btn_profile, False, False, 0)
        btn_row.pack_start(btn_installer, False, False, 0)
        page.pack_start(btn_row, False, False, 4)

        # --- Visão geral ---
        page.pack_start(self._about_heading("Visão geral"), False, False, 0)
        page.pack_start(self._about_text(
            f"A {GLib.markup_escape_text(APP_NAME)} é uma biblioteca musical leve para desktop Linux. Busca faixas no YouTube, reproduz apenas o áudio, "
            "organiza sua fila, playlists, favoritas e histórico, mostra letras, descobre artistas parecidos e exibe "
            "biografia e discografia, tudo usando os widgets e o tema nativos do GTK, sem CSS customizado."
        ), False, False, 0)

        # --- Abas ---
        page.pack_start(self._about_heading("Abas do aplicativo"), False, False, 0)
        for name, desc in (
            ("Barra lateral", "Início, Favoritas, Recentes e Sobre, além das suas playlists sempre à mão (botão direito: tocar, renomear, excluir)."),
            ("Início", "Vitrines: atalho para suas curtidas, o que tocou recentemente (duplo clique toca; botão direito abre o menu) e o \"Em alta\" por país (padrão: Brasil) com os artistas (duplo clique abre; botão direito: tocar populares, fila, mix, playlist) e as músicas mais ouvidas do ranking. Os cartões de artista da Busca e de \"Fãs também ouvem\", e todos os cartões de álbum, funcionam do mesmo jeito (um clique seleciona o cartão; duplo clique abre)."),
            ("Busca", "Uma busca só para músicas (YouTube), artistas e álbuns (Deezer), com filtros Tudo / Músicas / Artistas / Álbuns."),
            ("Artista e álbuns", "Biografia, faixas populares, discografia em capas e artistas parecidos. Abra um álbum para ver e tocar as faixas."),
            ("Mix", "Botão Mix no player (ou botão direito > Iniciar Mix da Faixa): a fila se abastece sozinha com músicas parecidas, sem fim. Usa o mix automático do YouTube e, se ele falhar, as músicas do artista no Deezer."),
            ("Fila e Letra", "Painel que desliza à direita, aberto pelos botões do player: fila reordenável (clique e segure para arrastar uma ou várias faixas selecionadas; uma linha mostra onde vão ficar; ou use as setas) e letra da música atual."),
            ("Rádios Web", "Estações de rádio online gratuitas (Radio-Browser): busque por país, gênero ou nome e ouça na hora."),
            ("Buscar na Biblioteca", "Busca instantânea (SQLite FTS5) por título, artista ou álbum em tudo que é seu: playlists, favoritas, recentes e músicas offline."),
            ("Equalizador (EQ)", "Botão EQ no player (ou Ctrl+E): 10 bandas de ±12 dB, presets Flat, Rock, Pop, Bass Boost, Vocal e Jazz, e a opção de reprodução sem pausa."),
            ("Favoritas", "Curta músicas com o ♡ (ou tecla L): tocar tudo, aleatório, adicionar à fila ou salvar como playlist."),
            ("Recentes", f"Histórico das últimas {HISTORY_LIMIT} faixas tocadas."),
        ):
            page.pack_start(self._about_card(name, desc), False, False, 0)

        # --- Como foi feito ---
        page.pack_start(self._about_heading("Como foi feito"), False, False, 0)
        for name, desc, tag in (
            ("Interface", "Python 3 com GTK 3 via PyGObject (Gtk, Gdk, GLib, GdkPixbuf). Apenas widgets nativos, herdando o tema do sistema.", "PyGObject"),
            ("Reprodução", "O mpv roda em segundo plano (modo idle, sem vídeo) e é controlado por IPC: comandos JSON enviados por um socket Unix. O app observa as propriedades time-pos, duration e pause para atualizar a barra de progresso e detectar o fim da faixa.", "mpv IPC"),
            ("Resolução do áudio", "O mpv usa o yt-dlp como hook para transformar o ID do vídeo em um stream de áudio (bestaudio). Um watchdog detecta reproduções travadas e tenta novamente.", "yt-dlp"),
            ("Concorrência", "Buscas, letras, capas e downloads rodam em threads. Os resultados voltam à interface via GLib.idle_add; um token por busca descarta respostas obsoletas.", "threading"),
            ("Metadados limpos", "Cada resultado do YouTube é comparado com faixas do Deezer (palavras do título, artista e duração) para exibir título e artista corretos. Sem correspondência, o título é higienizado por expressões regulares.", "Deezer + regex"),
            ("Persistência", "Playlists, favoritas, histórico e o índice de faixas ficam num banco SQLite (library.db) com busca FTS5, gravado em segundo plano e só no que mudou. Fila e configurações seguem em JSON atômico (arquivo temporário + os.replace). Os .json antigos são importados sozinhos na 1ª abertura.", "SQLite + FTS5"),
            ("Áudio sem pausa", "Faltando ~15 s para o fim da faixa, o yt-dlp resolve a próxima em segundo plano e o mpv a recebe com loadfile append, encadeando sem silêncio. O equalizador usa filtros lavfi enviados por IPC (set_property af).", "gapless + lavfi"),
            ("Downloads", "Áudio extraído e convertido para MP3 na melhor qualidade pelo yt-dlp, com progresso lido da saída do processo. Capa do álbum, artista, álbum, ano, nº da faixa e gênero (Deezer) são gravados como tags ID3 no arquivo.", "yt-dlp + ffmpeg"),
        ):
            page.pack_start(self._about_card(name, desc, tag), False, False, 0)

        # --- APIs ---
        page.pack_start(self._about_heading("APIs e serviços online"), False, False, 0)
        page.pack_start(self._about_text("Nenhuma exige chave de API ou login.", dim=True), False, False, 0)
        for name, desc, tag in (
            ("Deezer API", "Refino de metadados na busca, álbum/ano/capa da aba Letra, busca de artistas, artistas relacionados, faixas mais populares, discografia, faixas de cada álbum e os rankings por país (playlists oficiais \"Top <país>\" do Deezer Charts). Uso não comercial.", "api.deezer.com"),
            ("LRCLIB", "Fonte das letras. Usa a letra simples ou, se não houver, a sincronizada sem os marcadores de tempo.", "lrclib.net"),
            ("Wikipédia (pt/en)", "Biografia do artista (texto sob licença CC BY-SA 4.0). Tenta primeiro a versão em português; se não achar, usa a inglesa. Só aceita páginas de músicos e bandas.", "wikipedia.org"),
            ("Radio-Browser", "Diretório aberto de rádios web: busca por país, gênero e nome, e o endereço do stream. Uso gratuito, sem chave.", "api.radio-browser.info"),
            ("YouTube (via yt-dlp)", "Busca de vídeos e stream de áudio. As miniaturas da faixa atual vêm do servidor de imagens do YouTube.", "youtube.com · i.ytimg.com"),
        ) + TRANSLATE_CARDS:
            page.pack_start(self._about_card(name, desc, tag), False, False, 0)

        # --- Apps externos ---
        page.pack_start(self._about_heading("Aplicativos externos"), False, False, 0)
        for name, desc, tag in (
            ("mpv", "Motor de reprodução. Obrigatório; o app avisa se não estiver instalado.", "obrigatório"),
            ("yt-dlp", "Busca e resolução dos streams e downloads. Obrigatório; o app tenta se atualizar (yt-dlp -U) a cada abertura.", "obrigatório"),
            ("ffmpeg", "Usado pelo yt-dlp para converter o áudio em MP3 nos downloads. Não é verificado na inicialização.", "necessário para MP3"),
            ("notify-send (libnotify)", "Notificações do desktop ao trocar de faixa e concluir downloads. Opcional.", "opcional"),
        ):
            page.pack_start(self._about_card(name, desc, tag), False, False, 0)

        # --- Dados ---
        page.pack_start(self._about_heading("Onde ficam seus dados"), False, False, 0)
        page.pack_start(self._about_text(f"Pasta: <tt>{GLib.markup_escape_text(CONFIG_DIR)}</tt>"), False, False, 0)
        for name, desc in (
            ("library.db", "Banco SQLite: faixas, playlists, favoritas e histórico (com índice de busca FTS5)."),
            ("config.json", "Volume, tamanho da janela, equalizador e opção gapless."),
            ("queue.json", "Fila de reprodução."),
            ("*.json.bak", "Cópias dos antigos playlists/favorites/history.json, deixadas após a migração para o SQLite."),
            ("profile.json", "Nome de usuário."),
        ):
            page.pack_start(self._about_card(name, desc), False, False, 0)
        page.pack_start(self._about_text(
            "Para trocar de PC, use Perfil de usuário › Exportar: nome, playlists, favoritas e histórico vão para um único arquivo, "
            "que pode ser restaurado com Importar.", dim=True), False, False, 0)

        # --- Licença e créditos ---
        page.pack_start(self._about_heading("Licença e créditos"), False, False, 0)
        page.pack_start(self._about_text(
            f"{GLib.markup_escape_text(APP_NAME)} é software livre, distribuído sob a licença "
            f"{GLib.markup_escape_text(APP_LICENSE)}, sem qualquer garantia.\n"
            f"Código-fonte: {GLib.markup_escape_text(APP_URL)}\n\n"
            "mpv e yt-dlp são programas independentes, executados como processos separados, cada um com sua própria licença. "
            "Biografias: Wikipédia (CC BY-SA 4.0). Letras: LRCLIB. Catálogo: Deezer. "
            "Este projeto não é afiliado nem endossado por YouTube, Google, Deezer, Wikimedia ou pelo projeto mpv; "
            "todas as marcas pertencem aos seus donos."
        ), False, False, 0)

        # --- Atalhos ---
        page.pack_start(self._about_heading("Atalhos de teclado"), False, False, 0)
        page.pack_start(self._about_text(
            "<tt>Espaço</tt>  Play/Pause\n"
            "<tt>← / →</tt>  Voltar / avançar 5 s\n"
            "<tt>Ctrl+← / Ctrl+→</tt>  Faixa anterior / próxima\n"
            "<tt>M</tt>  Mudo\n"
            "<tt>L</tt>  Curtir / descurtir a faixa atual\n"
            "<tt>Ctrl+F</tt>  Ir para a busca\n"
            "<tt>Alt+←</tt>  Voltar\n"
            "<tt>Ctrl+V</tt>  Colar link e tocar\n"
            "Rolar o mouse sobre o botão de volume ajusta o volume."
        ), False, False, 0)

        scroll.add(page)
        self.tab_about = scroll
        self.main_stack.add_named(scroll, "about")

    # ======================================================================
    # Perfil de usuário
    # ======================================================================
    @staticmethod
    def _track_key(t):
        if t.get("path"):
            return "file:" + t["path"]
        tid = (t.get("id") or "").strip()
        return tid or f"{(t.get('title') or '').lower()}|{(t.get('uploader') or '').lower()}"

    def _profile_stats(self):
        """(nº de playlists, nº total de faixas em playlists, nº de faixas únicas)."""
        lists = [v for v in self.playlists.values() if isinstance(v, list)]
        total = sum(len(v) for v in lists)
        unique = len({self._track_key(t) for v in lists for t in v if isinstance(t, dict)})
        return len(lists), total, unique

    def _save_profile(self):
        self._save_json(PROFILE_FILE, self.profile)

    def _make_stat(self, caption):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        value = Gtk.Label(xalign=0.5)
        cap = Gtk.Label(label=caption, xalign=0.5)
        cap.get_style_context().add_class("dim-label")
        box.pack_start(value, False, False, 0)
        box.pack_start(cap, False, False, 0)
        return box, value

    def on_open_profile(self, button=None):
        dialog = Gtk.Dialog(title="Perfil de usuário", transient_for=self, modal=True)
        dialog.set_default_size(420, -1)
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        btn_ok = dialog.add_button("Salvar", Gtk.ResponseType.OK)
        btn_ok.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_border_width(16)

        content.pack_start(self._section_label("Nome do usuário"), False, False, 0)
        name_entry = Gtk.Entry()
        name_entry.set_text(self.profile.get("name", ""))
        name_entry.set_placeholder_text("Como devemos te chamar?")
        name_entry.set_max_length(40)
        name_entry.set_activates_default(True)
        content.pack_start(name_entry, False, False, 0)

        content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        stat_pl, lbl_pl = self._make_stat("Playlists")
        stat_tr, lbl_tr = self._make_stat("Faixas salvas")
        stat_hi, lbl_hi = self._make_stat("No histórico")
        for s in (stat_pl, stat_tr, stat_hi):
            stats_box.pack_start(s, True, True, 0)
        content.pack_start(stats_box, False, False, 0)

        def refresh_stats():
            n_pl, n_tr, n_un = self._profile_stats()
            for lbl, val in ((lbl_pl, n_pl), (lbl_tr, n_tr), (lbl_hi, len(self.history))):
                lbl.set_markup(f'<span size="xx-large" weight="bold">{val}</span>')
            stat_tr.set_tooltip_text(f"Total de faixas nas playlists ({n_un} únicas, sem contar repetições)")

        refresh_stats()

        content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        content.pack_start(self._section_label("Backup e transferência"), False, False, 0)
        content.pack_start(self._about_text(
            "Exporte nome, playlists e histórico para um único arquivo e importe-o em outro computador.",
            dim=True), False, False, 0)

        backup_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        btn_export = Gtk.Button(label="Exportar perfil…")
        btn_export.set_image(Gtk.Image.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON))
        btn_export.set_always_show_image(True)
        btn_import = Gtk.Button(label="Importar perfil…")
        btn_import.set_image(Gtk.Image.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON))
        btn_import.set_always_show_image(True)
        backup_row.pack_start(btn_export, True, True, 0)
        backup_row.pack_start(btn_import, True, True, 0)
        content.pack_start(backup_row, False, False, 0)

        btn_export.connect("clicked", lambda b: self._export_profile(dialog, name_entry.get_text().strip()))

        def _do_import(b):
            if self._import_profile(dialog):
                name_entry.set_text(self.profile.get("name", ""))
                refresh_stats()

        btn_import.connect("clicked", _do_import)

        dialog.show_all()
        response = dialog.run()
        new_name = name_entry.get_text().strip()
        dialog.destroy()

        if response == Gtk.ResponseType.OK:
            if new_name != (self.profile.get("name") or ""):
                self.profile["name"] = new_name
                self._save_profile()
                self.show_toast(f"Perfil salvo. Olá, {new_name}!" if new_name else "Nome removido do perfil.")

    def on_open_installer(self, button=None):
        path = _find_installer_file()
        if not path:
            self._message_dialog(
                self, Gtk.MessageType.INFO,
                "Instalador não encontrado",
                "Abra um terminal na pasta da Fonoteca (a que você clonou) e rode:\n\n"
                "python3 fonoteca-installer.py")
            return
        try:
            subprocess.Popen(["python3", path], cwd=os.path.dirname(path), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.show_toast("Instalador aberto. Depois de atualizar, feche e reabra a Fonoteca.")
        except Exception as e:
            self._message_dialog(self, Gtk.MessageType.ERROR, "Não foi possível abrir o instalador", str(e))

    def _message_dialog(self, parent, kind, text, secondary=None):
        d = Gtk.MessageDialog(transient_for=parent, flags=0, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=text)
        if secondary:
            d.format_secondary_text(secondary)
        d.run()
        d.destroy()

    def _json_file_filter(self):
        flt = Gtk.FileFilter()
        flt.set_name(f"Perfil da {APP_NAME} (*.json)")
        flt.add_pattern("*.json")
        return flt

    def _export_profile(self, parent, name):
        chooser = Gtk.FileChooserDialog(title="Exportar perfil", transient_for=parent,
                                        action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Exportar", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        chooser.add_filter(self._json_file_filter())
        chooser.set_current_name(f"{APP_ID}_perfil_{datetime.date.today().isoformat()}.json")
        response = chooser.run()
        path = chooser.get_filename()
        chooser.destroy()
        if response != Gtk.ResponseType.OK or not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".json"

        data = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "app_version": APP_VERSION,
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "profile": {"name": name},
            "playlists": self.playlists,
            "history": self.history,
            "favorites": self.favorites,
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._message_dialog(parent, Gtk.MessageType.ERROR, "Não foi possível exportar", str(e))
            return False

        n_pl, n_tr, _ = self._profile_stats()
        self.show_toast(f"Perfil exportado ({n_pl} playlists, {n_tr} faixas)")
        return True

    def _read_backup(self, path):
        """Lê e valida um backup. Retorna (dados_limpos, None) ou (None, mensagem_de_erro)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            return None, f"Não foi possível ler o arquivo: {e}"

        if not isinstance(raw, dict) or raw.get("format") not in (BACKUP_FORMAT, *LEGACY_BACKUP_FORMATS):
            return None, f"Este arquivo não é um backup da {APP_NAME}."
        ver = raw.get("version")
        if not isinstance(ver, int) or ver > BACKUP_VERSION:
            return None, "O arquivo foi criado por uma versão mais nova do aplicativo."

        def clean_tracks(lst):
            out = []
            for t in lst if isinstance(lst, list) else []:
                if isinstance(t, dict) and (t.get("id") or t.get("title")):
                    out.append(self._normalize_track(t))
            return out

        playlists = {}
        raw_pl = raw.get("playlists")
        if isinstance(raw_pl, dict):
            for pname, tracks in raw_pl.items():
                if isinstance(pname, str) and pname.strip():
                    playlists[pname] = clean_tracks(tracks)

        profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
        name = profile.get("name") if isinstance(profile.get("name"), str) else ""
        return {
            "name": name.strip()[:40],
            "playlists": playlists,
            "history": clean_tracks(raw.get("history")),
            "favorites": clean_tracks(raw.get("favorites")),
            "exported_at": raw.get("exported_at", ""),
        }, None

    def _import_profile(self, parent):
        chooser = Gtk.FileChooserDialog(title="Importar perfil", transient_for=parent,
                                        action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Abrir", Gtk.ResponseType.OK)
        chooser.add_filter(self._json_file_filter())
        response = chooser.run()
        path = chooser.get_filename()
        chooser.destroy()
        if response != Gtk.ResponseType.OK or not path:
            return False

        data, err = self._read_backup(path)
        if err:
            self._message_dialog(parent, Gtk.MessageType.ERROR, "Importação falhou", err)
            return False

        n_pl = len(data["playlists"])
        n_tr = sum(len(v) for v in data["playlists"].values())
        who = f" de {data['name']}" if data["name"] else ""
        when = f"\nExportado em {data['exported_at'].replace('T', ' ')}." if data["exported_at"] else ""

        ask = Gtk.MessageDialog(transient_for=parent, flags=0, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=f"Importar perfil{who}?")
        ask.format_secondary_text(
            f"O arquivo contém {n_pl} playlist(s), {n_tr} faixa(s), {len(data['favorites'])} favorita(s) e {len(data['history'])} item(ns) de histórico.{when}\n\n"
            "Mesclar: mantém o que você já tem e adiciona o que estiver faltando.\n"
            "Substituir: apaga seus dados atuais e usa somente o conteúdo do arquivo."
        )
        ask.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ask.add_button("Mesclar", 1)
        btn_replace = ask.add_button("Substituir tudo", 2)
        btn_replace.get_style_context().add_class("destructive-action")
        choice = ask.run()
        ask.destroy()
        if choice not in (1, 2):
            return False

        if choice == 2:
            self.playlists = data["playlists"]
            self.history = data["history"][:HISTORY_LIMIT]
            self.favorites = data["favorites"]
            if data["name"]:
                self.profile["name"] = data["name"]
        else:
            for pname, tracks in data["playlists"].items():
                current = self.playlists.setdefault(pname, [])
                seen = {self._track_key(t) for t in current}
                for t in tracks:
                    k = self._track_key(t)
                    if k not in seen:
                        current.append(t)
                        seen.add(k)
            merged, seen = [], set()
            for t in list(self.history) + data["history"]:
                k = self._track_key(t)
                if k not in seen:
                    merged.append(t)
                    seen.add(k)
            self.history = merged[:HISTORY_LIMIT]
            fav_seen = {self._track_key(t) for t in self.favorites}
            for t in data["favorites"]:
                k = self._track_key(t)
                if k not in fav_seen:
                    self.favorites.append(t)
                    fav_seen.add(k)
            if not (self.profile.get("name") or "").strip() and data["name"]:
                self.profile["name"] = data["name"]

        self._fav_keys_invalidate()
        self._save_json(PLAYLISTS_FILE, self.playlists)
        self._save_json(HISTORY_FILE, self.history, compact=True)
        self._save_json(FAVORITES_FILE, self.favorites)
        self._save_profile()

        if self.selected_playlist not in self.playlists:
            self.selected_playlist = None
        self.render_playlists()
        self.render_playlist_tracks()
        self.render_history()
        self.render_favorites()
        self._refresh_hearts()
        self.show_toast("Perfil importado com sucesso!")
        return True

    # ======================================================================
    # Equalizador + opções de áudio
    # ======================================================================
    def on_open_audio(self, button=None):
        if getattr(self, "_audio_dialog", None) is None:
            self._audio_dialog = AudioDialog(self)
        self._audio_dialog.show_all()
        self._audio_dialog.present()

    def apply_eq(self):
        """Envia o filtro ao mpv (set_property af ...) e grava o estado no config.json."""
        af = build_eq_filter(self.eq_gains) if self.eq_enabled else ""
        self.mpv.set_af(af)
        self.config["eq"] = {"enabled": bool(self.eq_enabled), "gains": [round(g, 1) for g in self.eq_gains],
                             "preset": self.eq_preset}
        self._schedule_config_save()
        self._refresh_eq_button()

    def _refresh_eq_button(self):
        ctx = self.btn_eq.get_style_context()
        if self.eq_enabled and any(abs(g) >= 0.05 for g in self.eq_gains):
            ctx.add_class("suggested-action")
            self.btn_eq.set_tooltip_text(f"Equalizador ligado ({self.eq_preset}) - Ctrl+E")
        else:
            ctx.remove_class("suggested-action")
            self.btn_eq.set_tooltip_text("Equalizador e opções de áudio (Ctrl+E)")

    def set_gapless(self, on):
        self.gapless_enabled = bool(on)
        self.config["gapless"] = self.gapless_enabled
        self._schedule_config_save()
        if not on:
            self._invalidate_preload()

    @staticmethod
    def _lib_file_sig():
        """Assinatura barata do library.json (o app só o regrava quando o índice muda). '' = sem arquivo."""
        try:
            st = os.stat(LIBRARY_FILE)
            return f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return ""

    # ======================================================================
    # Reprodução sem pausa (gapless) com pré-carregamento
    # ======================================================================
    def _plan_next_index(self):
        """Próxima faixa que o on_next() escolheria (sem efeitos colaterais). None se a fila acaba."""
        if not self.queue or self.current_index < 0:
            return None
        if self.is_shuffle and len(self.queue) > 1:
            nxt = self.current_index
            while nxt == self.current_index:
                nxt = random.randint(0, len(self.queue) - 1)
            return nxt
        if self.current_index + 1 < len(self.queue):
            return self.current_index + 1
        return None

    def on_mpv_time_remaining(self, rem):
        """Chamado pelo on_mpv_time_change com duração - posição. Perto do fim, resolve a próxima em segundo plano."""
        if (not (PRELOAD_LATE_S <= rem <= PRELOAD_MAX_S) or self._closing or not self.gapless_enabled
                or self.is_repeat or self._preload is not None or self._preload_fired == self._play_token):
            return False
        cur = self._current_item()
        if not cur or cur.get("webradio"):
            return False
        nxt = self._plan_next_index()
        if nxt is None:
            return False
        item = self.queue[nxt]
        src = self._media_source(item)
        if src is None:        # arquivo local sumiu: o fluxo normal avisa o usuário e pula
            return False
        self._preload_fired = self._play_token
        token, gen, key = self._play_token, self._preload_gen, track_key(item)
        if item.get("path") or item.get("stream_url"):
            self._preload_ready(token, gen, nxt, key, src)          # sem rede: enfileira direto
        else:
            try:
                self._preload_pool.submit(self._preload_worker, token, gen, nxt, key, src)
            except RuntimeError:
                pass
        return False

    def _preload_worker(self, token, gen, idx, key, watch_url):
        """Thread: resolve a URL direta com o yt-dlp. Se falhar, enfileira o link do vídeo (o mpv resolve)."""
        url = resolve_stream_url(watch_url) or watch_url
        GLib.idle_add(self._preload_ready, token, gen, idx, key, url)

    def _preload_ready(self, token, gen, idx, key, url):
        if (self._closing or token != self._play_token or gen != self._preload_gen or self._preload is not None
                or not self.gapless_enabled or self.is_repeat):
            return False
        if not (0 <= idx < len(self.queue)) or track_key(self.queue[idx]) != key:
            return False
        self.mpv.append(url)                      # ["loadfile", url, "append"]
        self._preload = {"token": token, "idx": idx, "key": key}
        return False

    def _invalidate_preload(self):
        """Descarta a faixa pré-carregada (fila mudou, shuffle/repeat alterado...)."""
        self._preload_gen += 1
        self._preload_fired = None
        if self._preload is not None:
            self._preload = None
            self.mpv.playlist_clear()             # remove só a entrada pendente; a atual continua tocando

    def _check_preload_still_valid(self):
        pre = self._preload
        if pre is not None:
            ok = (pre["token"] == self._play_token and not self.is_repeat
                  and 0 <= pre["idx"] < len(self.queue) and track_key(self.queue[pre["idx"]]) == pre["key"]
                  and (self.is_shuffle or pre["idx"] == self.current_index + 1))
            if not ok:
                self._invalidate_preload()
        return False

    def _gapless_accept(self, pre):
        return (pre["token"] == self._play_token and not self.is_repeat
                and 0 <= pre["idx"] < len(self.queue) and track_key(self.queue[pre["idx"]]) == pre["key"])

    # ======================================================================
    # Rádios web (Radio-Browser)
    # ======================================================================
    def _build_page_webradio(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(20)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="xx-large" weight="bold">Rádios Web</span>')
        page.pack_start(title, False, False, 0)
        sub = Gtk.Label(label="Estações de rádio online gratuitas, do Radio-Browser. Dê dois cliques para ouvir.", xalign=0)
        sub.get_style_context().add_class("dim-label")
        page.pack_start(sub, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.webradio_name = Gtk.SearchEntry()
        self.webradio_name.set_placeholder_text("Nome da rádio...")
        self.webradio_name.connect("activate", self.on_webradio_search)
        row.pack_start(self.webradio_name, True, True, 0)

        self.webradio_country = Gtk.ComboBoxText()
        for label, _code in WEBRADIO_COUNTRIES:
            self.webradio_country.append_text(label)
        self.webradio_country.set_active(0)                       # Brasil
        self.webradio_country.connect("changed", self.on_webradio_search)
        row.pack_start(self.webradio_country, False, False, 0)

        self.webradio_tag = Gtk.ComboBoxText.new_with_entry()
        for t in WEBRADIO_TAGS:
            self.webradio_tag.append_text(t.capitalize() if t else "")
        self.webradio_tag.get_child().set_placeholder_text("Gênero (Rock, MPB, Notícias...)")
        self.webradio_tag.get_child().connect("activate", self.on_webradio_search)
        # "changed" também dispara ao digitar; só busca quando o item vem da lista (active >= 0)
        self.webradio_tag.connect("changed", lambda c: self.on_webradio_search() if c.get_active() >= 0 else None)
        row.pack_start(self.webradio_tag, False, False, 0)

        self.webradio_spinner = Gtk.Spinner()
        row.pack_start(self.webradio_spinner, False, False, 0)
        btn = Gtk.Button(label="Buscar")
        btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", self.on_webradio_search)
        row.pack_start(btn, False, False, 0)
        page.pack_start(row, False, False, 0)

        self.webradio_status = Gtk.Label(xalign=0)
        self.webradio_status.get_style_context().add_class("dim-label")
        page.pack_start(self.webradio_status, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.webradio_list = Gtk.ListBox()
        self.webradio_list.set_activate_on_single_click(False)
        self.webradio_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.webradio_list.connect("row-activated", lambda lb, r: self.play_item(r.item))
        self.webradio_list.connect("button-press-event", self.on_webradio_button_press)
        self.webradio_list.set_placeholder(self._placeholder("Nenhuma rádio carregada."))
        sw.add(self.webradio_list)
        page.pack_start(sw, True, True, 0)
        return page

    def on_webradio_search(self, widget=None):
        self._webradio_loaded = True
        self.webradio_token += 1
        token = self.webradio_token
        name = self.webradio_name.get_text()
        idx = max(self.webradio_country.get_active(), 0)
        code = WEBRADIO_COUNTRIES[idx][1]
        tag = (self.webradio_tag.get_child().get_text() or "").strip()
        self.webradio_spinner.start()
        self.webradio_status.set_text("Buscando rádios...")
        try:
            self._net_pool.submit(self._webradio_thread, name, code, tag, token)
        except RuntimeError:
            pass

    def _webradio_thread(self, name, code, tag, token):
        try:
            stations = webradio_search(name, code, tag)
            tracks = []
            for st in stations:
                t = webradio_to_track(st)
                if t:
                    codec, br = st.get("codec") or "", st.get("bitrate") or 0
                    t["_info"] = " · ".join(x for x in (
                        st.get("country") or "", ", ".join([g for g in (st.get("tags") or "").split(",") if g][:3]),
                        f"{codec} {br} kbps" if codec and br else codec) if x)
                    tracks.append(t)
            GLib.idle_add(self._webradio_populate, tracks, token, None)
        except Exception as exc:
            GLib.idle_add(self._webradio_populate, [], token, str(exc))

    def _webradio_populate(self, tracks, token, error):
        if token != self.webradio_token:
            return False
        self.webradio_spinner.stop()
        for child in list(self.webradio_list.get_children()):
            self.webradio_list.remove(child)
        if error:
            self.webradio_status.set_text("Não foi possível carregar as rádios. Verifique a conexão.")
            return False
        for t in tracks:
            info = t.pop("_info", "")
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.set_border_width(4)
            ic = Gtk.Label(label="📻")
            hb.pack_start(ic, False, False, 4)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            n = Gtk.Label(xalign=0)
            n.set_ellipsize(3)
            n.set_markup(f"<b>{GLib.markup_escape_text(t['title'])}</b>")
            s = Gtk.Label(label=info or "Rádio web", xalign=0)
            s.set_ellipsize(3)
            s.get_style_context().add_class("dim-label")
            vb.pack_start(n, False, False, 0)
            vb.pack_start(s, False, False, 0)
            hb.pack_start(vb, True, True, 0)
            play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
            play.set_relief(Gtk.ReliefStyle.NONE)
            play.set_tooltip_text("Ouvir agora")
            play.connect("clicked", lambda b, it=t: self.play_item(it))
            hb.pack_start(play, False, False, 0)
            hb.pack_start(self._make_heart(t), False, False, 0)
            row.add(hb)
            row.item = t
            self.webradio_list.add(row)
        self.webradio_list.show_all()
        self.webradio_status.set_text(f"{len(tracks)} estação(ões) encontrada(s)." if tracks else "Nenhuma estação encontrada.")
        return False

    def _simple_track_menu(self, widget, event, extra=None):
        """Menu de contexto genérico (Tocar / Fila / Playlist / Curtir) para listas de faixas."""
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        if row is None or not hasattr(row, "item"):
            return False
        self._ensure_row_selected(widget, row)
        sel = [r.item for r in widget.get_selected_rows()] or [row.item]
        menu = Gtk.Menu()
        mi = Gtk.MenuItem(label="Reproduzir")
        mi.connect("activate", lambda w: self.play_item(row.item))
        menu.append(mi)
        if not row.item.get("webradio"):
            menu.append(self._radio_menu_item(row.item))      # "Iniciar Mix da Faixa" (fila infinita)
        mi = Gtk.MenuItem(label="Adicionar à Fila")
        mi.connect("activate", lambda w: self.add_items_bulk(sel))
        menu.append(mi)
        mi = Gtk.MenuItem(label="Adicionar à Playlist")
        mi.connect("activate", lambda w: self.on_add_selection_to_playlist(items_to_add=sel))
        menu.append(mi)
        menu.append(self._like_menu_item(sel))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_webradio_button_press(self, widget, event):
        return self._simple_track_menu(widget, event)

    def on_mpv_metadata(self, md):
        """Rádios enviam o título da música (ICY). Mostra 'Estação — Música' no player."""
        item = self._current_item()
        if not item or not item.get("webradio") or not isinstance(md, dict):
            return False
        icy = next((str(v) for k, v in md.items() if str(k).lower() == "icy-title" and v), "")
        if icy:
            self.now_playing_label.set_text(f"{item['title']} — {icy}")
            self.lyrics_album_label.set_text(f"Tocando agora: {icy}")
        return False

    def _show_webradio_lyrics_panel(self, item):
        self._update_lyrics_ui("Rádio ao vivo.\n\nTransmissões ao vivo não têm letra.")
        self.lyrics_title_label.set_markup(f"<b>{GLib.markup_escape_text(item['title'])}</b>")
        self.lyrics_artist_label.set_text(item.get("uploader", ""))
        self.lyrics_album_label.set_text(item.get("tags", ""))
        self.lyrics_year_label.set_text("")
        self.lyrics_art_img.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)

    def _load_webradio_thumbnail(self, item, token):
        """Logotipo da estação (favicon) no player e na aba Letra."""
        data = None
        fav = item.get("favicon") or ""
        if fav.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(fav, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = resp.read(2_000_000)
            except Exception:
                data = None
        shown = False
        if data and token == self._play_token:
            try:
                GLib.idle_add(self.thumbnail_img.set_from_pixbuf, self._decode_scaled(data, 48, 48))
                GLib.idle_add(self._set_lyrics_cover, self._decode_scaled(data, 80, 80), token)
                shown = True
            except Exception:
                pass
        if token == self._play_token:
            if not shown:
                GLib.idle_add(self.thumbnail_img.set_from_icon_name, "audio-x-generic", Gtk.IconSize.DIALOG)
        GLib.idle_add(self._notify, APP_NAME, f"Rádio: {item.get('title', '')}", "audio-x-generic")

    # ======================================================================
    # Buscar na Biblioteca (SQLite FTS5)
    # ======================================================================
    def _build_page_mylib(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(20)
        title = Gtk.Label(xalign=0)
        title.set_markup('<span size="xx-large" weight="bold">Buscar na Biblioteca</span>')
        page.pack_start(title, False, False, 0)
        self.mylib_entry = Gtk.SearchEntry()
        self.mylib_entry.set_placeholder_text("Título, artista ou álbum de qualquer música sua (playlists, favoritas, recentes, offline)...")
        self.mylib_entry.connect("search-changed", self._on_mylib_changed)
        self.mylib_entry.connect("stop-search", lambda e: e.set_text(""))
        page.pack_start(self.mylib_entry, False, False, 0)
        self.mylib_status = Gtk.Label(xalign=0)
        self.mylib_status.get_style_context().add_class("dim-label")
        page.pack_start(self.mylib_status, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.mylib_list = Gtk.ListBox()
        self.mylib_list.set_activate_on_single_click(False)
        self.mylib_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.mylib_list.connect("row-activated", lambda lb, r: self.play_item(r.item))
        self.mylib_list.connect("button-press-event", lambda w, e: self._simple_track_menu(w, e))
        self.mylib_list.set_placeholder(self._placeholder("Digite para buscar."))
        sw.add(self.mylib_list)
        page.pack_start(sw, True, True, 0)
        return page

    def _on_mylib_changed(self, entry):
        if self._mylib_timer:
            GLib.source_remove(self._mylib_timer)
        self._mylib_timer = GLib.timeout_add(120, self._mylib_run)      # debounce curto

    def _mylib_run(self):
        self._mylib_timer = None
        self.mylib_token += 1
        token = self.mylib_token
        text = self.mylib_entry.get_text().strip()
        if self.db is None:
            self.mylib_status.set_text("Banco de dados indisponível nesta sessão.")
            return False
        try:
            self._net_pool.submit(self._mylib_thread, text, token)
        except RuntimeError:
            pass
        return False

    def _mylib_thread(self, text, token):
        t0 = time.perf_counter()
        try:
            items = self.db.search(text, 200) if text else []
            total = self.db.count_tracks()
        except Exception:
            items, total = [], 0
        GLib.idle_add(self._mylib_populate, items, total, text, (time.perf_counter() - t0) * 1000, token)

    def _mylib_populate(self, items, total, text, ms, token):
        if token != self.mylib_token:
            return False
        for child in list(self.mylib_list.get_children()):
            self.mylib_list.remove(child)
        for it in items:
            it = self._normalize_track(it)
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hb.set_border_width(4)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            t = Gtk.Label(xalign=0)
            t.set_ellipsize(3)
            t.set_markup(f"<b>{GLib.markup_escape_text(it['title'])}</b>")
            sub = "  ·  ".join(x for x in (it.get("uploader"), it.get("album"), it.get("year")) if x) or "Artista desconhecido"
            s = Gtk.Label(label=sub, xalign=0)
            s.set_ellipsize(3)
            s.get_style_context().add_class("dim-label")
            vb.pack_start(t, False, False, 0)
            vb.pack_start(s, False, False, 0)
            hb.pack_start(vb, True, True, 6)
            d = Gtk.Label(label=it.get("duration", ""))
            d.get_style_context().add_class("dim-label")
            hb.pack_start(d, False, False, 4)
            hb.pack_start(self._offline_badge(it), False, False, 0)
            hb.pack_start(self._make_heart(it), False, False, 0)
            row.add(hb)
            row.item = it
            self.mylib_list.add(row)
        self.mylib_list.show_all()
        engine = "FTS5" if self.db is not None and self.db.fts else "LIKE"
        if text:
            self.mylib_status.set_text(f"{len(items)} resultado(s) em {ms:.1f} ms · {total} faixas indexadas ({engine})")
        else:
            self.mylib_status.set_text(f"{total} faixas indexadas ({engine}).")
        self.mylib_list.set_placeholder(self._placeholder("Nenhum resultado." if text else "Digite para buscar."))
        return False

    # ---------- Encerramento ----------
    def on_destroy(self, widget):
        self._closing = True
        try:
            self.mpv.quit()      # música para na hora, antes de qualquer outra coisa
        except Exception:
            pass
        self._cancel_stall_watchdog()
        for pid_name in ("_queue_save_id", "_msg_timeout_id", "_msg_fade_id", "_dl_pulse_id", "_busy_pulse_id"):
            sid = getattr(self, pid_name, None)
            if sid:
                try:
                    GLib.source_remove(sid)
                except Exception:
                    pass
                setattr(self, pid_name, None)
        for proc in list(getattr(self, "_dl_procs", ())):   # não deixa yt-dlp órfão rodando depois de fechar
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        self._art_pool.shutdown(wait=False)
        self._preload_pool.shutdown(wait=False)
        self._net_pool.shutdown(wait=False)
        if getattr(self, "_mylib_timer", None):
            GLib.source_remove(self._mylib_timer)
            self._mylib_timer = None
        if self._config_save_id:
            GLib.source_remove(self._config_save_id)
            self._config_save_id = None
        if not self.is_maximized():  # maximizada, get_size() devolveria a tela toda e estragaria o "restaurar"
            self.config["win_w"], self.config["win_h"] = self.get_size()
        self._save_json(CONFIG_FILE, self.config)
        self._save_json(QUEUE_FILE, self.queue, compact=True)
        self._save_json(PLAYLISTS_FILE, self.playlists)
        self._save_json(FAVORITES_FILE, self.favorites)
        self._save_json(PROFILE_FILE, self.profile)
        self._save_json(HISTORY_FILE, self.history, compact=True)
        if self.db is not None:
            self.db.close()          # espera as gravações pendentes e fecha o SQLite
        if getattr(self, "mpris", None) is not None:
            self.mpris.close()
        Gtk.main_quit()


if __name__ == "__main__":
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    Gtk.Window.set_default_icon_name(APP_ID)
    _reap_orphan_mpv()
    app = MusicPlayerApp()
    _install_exit_handlers(app)
    app.show_all()
    Gtk.main()
