#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Josuel Barbosa
"""
Fonoteca (GTK3 + mpv IPC)
-------------------------
Uma biblioteca musical para descobrir, organizar e ouvir música.

Player leve para Linux, usando os widgets e o tema nativo do GTK do sistema
(sem CSS customizado), com playlists, downloads, letras, descoberta de
artistas e navegação rica.

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
import json
import socket
import time
import tempfile
import random
import urllib.request
import urllib.parse
import shutil
import unicodedata
import difflib
import datetime

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

# ----------------------------------------------------------------------
# Identidade do aplicativo
# ----------------------------------------------------------------------
APP_NAME = "Fonoteca"
APP_ID = "fonoteca"
APP_VERSION = "1.0.0"
APP_TAGLINE = "Uma biblioteca musical para descobrir, organizar e ouvir música."
APP_AUTHOR = "Josuel Barbosa"
APP_YEAR = "2026"
APP_LICENSE = "GPL-3.0-or-later"
# TROQUE pelo endereço real do repositório antes de publicar. Ele também vai no
# User-Agent: a Wikimedia e o MusicBrainz exigem um contato/URL identificável.
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

# Formato do arquivo de backup do perfil (exportar/importar).
# Backups feitos pelo nome antigo continuam sendo aceitos na importação.
BACKUP_FORMAT = "fonoteca_backup"
LEGACY_BACKUP_FORMATS = ("yt_music_player_backup",)
BACKUP_VERSION = 1
HISTORY_LIMIT = 50

MPV_SOCKET = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpv.sock")
MPV_LOG = os.path.join(tempfile.gettempdir(), f"{APP_ID}_mpv.log")

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

    def start(self, initial_volume=100):
        if os.path.exists(MPV_SOCKET):
            try:
                os.remove(MPV_SOCKET)
            except OSError:
                pass

        try:
            log_file = open(MPV_LOG, "w")
            self.proc = subprocess.Popen(
                [
                    "mpv",
                    "--idle=yes",
                    "--no-video",
                    "--no-terminal",
                    "--ytdl=yes",
                    "--ytdl-format=bestaudio/best",
                    "--script-opts=ytdl_hook-ytdl_path=yt-dlp",
                    f"--input-ipc-server={MPV_SOCKET}",
                    f"--volume={initial_volume}",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError:
            return False

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

        self._send(["observe_property", 1, "time-pos"])
        self._send(["observe_property", 2, "duration"])
        self._send(["observe_property", 3, "pause"])
        return True

    def _listen(self):
        buf = b""
        while self._running and self.sock:
            try:
                data = self.sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
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
                        GLib.idle_add(self.callbacks["on_time_change"], val or 0)
                    elif name == "duration" and "on_duration_change" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_duration_change"], val or 0)
                    elif name == "pause" and "on_pause_change" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_pause_change"], val)

                if "request_id" in msg and "error" in msg:
                    if msg["error"] != "success" and "on_load_error" in self.callbacks:
                        GLib.idle_add(self.callbacks["on_load_error"], msg["error"])

    def _send(self, command, request_id=None):
        if not self.sock:
            return
        payload = {"command": command}
        if request_id is not None:
            payload["request_id"] = request_id
        try:
            self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            pass

    def load(self, url):
        with self._lock:
            self._request_id += 1
            rid = self._request_id
        self._send(["loadfile", url, "replace"], request_id=rid)

    def pause_toggle(self):
        self._send(["cycle", "pause"])

    def stop(self):
        self._send(["stop"])

    def seek(self, position):
        self._send(["seek", position, "absolute"])

    def seek_relative(self, seconds):
        self._send(["seek", seconds, "relative"])

    def set_volume(self, vol):
        self._send(["set_property", "volume", vol])

    def set_mute(self, muted):
        self._send(["set_property", "mute", bool(muted)])

    def quit(self):
        self._running = False
        self._send(["quit"])
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


class MusicPlayerApp(Gtk.Window):
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

        self.set_default_size(self.config.get("width", 540), self.config.get("height", 740))
        self.set_position(Gtk.WindowPosition.CENTER)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        self.set_titlebar(header)

        # Estado da aplicação
        self.queue = self._load_json(QUEUE_FILE, [])
        self.history = self._load_json(HISTORY_FILE, [])
        self.playlists = self._load_json(PLAYLISTS_FILE, {})
        self.profile = self._load_json(PROFILE_FILE, {"name": ""})
        if not isinstance(self.profile, dict):
            self.profile = {"name": ""}
        self._welcome_timeout_id = None
        self.selected_playlist = None
        self.results = []
        self.current_index = -1
        self.is_repeat = False
        self.is_shuffle = False
        self.track_duration = 0
        self.user_is_seeking = False
        
        # Gerenciamento de timeouts e cancelamento de buscas
        self._toast_timeout_id = None
        self._msg_timeout_id = None
        self.search_token = 0
        self.discover_token = 0
        self.wiki_token = 0

        self.discover_current_artist = None
        self.album_token = 0
        self._album_cache = {}
        self.wiki_artist_name = ""

        # Sincronização automática do artista tocando com as abas Wiki/Descobrir
        self.now_artist = ""
        self._artist_stale = {"wiki": False, "discover": False}
        self._loaded_key = {"wiki": "", "discover": ""}

        # Volume / mute
        self.is_muted = False
        self._last_volume = max(int(self.config.get("volume", 100) or 100), 1)
        self._config_save_id = None
        self._mute_icon = ""

        # Filtro da lista de playlists
        self._pl_filter_key = ""

        # Watchdog de reprodução
        self._play_token = 0
        self._track_started = False
        self._stall_retry_count = 0
        self._stall_check_id = None

        # Container Principal
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        # Toast de notificação
        self.notify_revealer = Gtk.Revealer()
        self.notify_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.notify_label = Gtk.Label()
        self.notify_label.set_margin_start(12)
        self.notify_label.set_margin_end(12)
        self.notify_label.set_margin_top(6)
        self.notify_label.set_margin_bottom(6)
        self.notify_label.get_style_context().add_class("app-notification")
        self.notify_revealer.add(self.notify_label)

        toast_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toast_box.set_halign(Gtk.Align.CENTER)
        toast_box.pack_start(self.notify_revealer, False, False, 6)
        vbox.pack_start(toast_box, False, False, 0)

        # Notebook (Abas)
        self.notebook = Gtk.Notebook()
        overlay = Gtk.Overlay()
        overlay.add(self.notebook)
        overlay.add_overlay(self._build_welcome_overlay())
        vbox.pack_start(overlay, True, True, 0)

        self._build_tab_search()
        self._build_tab_queue()
        self._build_tab_playlists()
        self._build_tab_history()
        self._build_tab_lyrics()
        self._build_tab_discover()
        self._build_tab_wiki()
        self._build_tab_about()

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
        info_box.pack_start(self.now_playing_label, True, True, 0)
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

        btn_download_ctrl = Gtk.Button.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON)
        btn_download_ctrl.set_tooltip_text("Baixar seleção em MP3")
        btn_download_ctrl.connect("clicked", self.on_download)

        for w in (btn_paste, self.btn_shuffle, btn_prev, self.btn_playpause, btn_next, self.btn_repeat, btn_download_ctrl):
            controls_box.pack_start(w, False, False, 0)

        now_playing_box.pack_start(controls_box, False, False, 0)
        player_panel.pack_start(now_playing_box, False, False, 0)

        self.progress_download = Gtk.ProgressBar()
        self.progress_download.set_show_text(True)
        self.progress_download.set_no_show_all(True)
        player_panel.pack_start(self.progress_download, False, False, 0)

        self.infobar_label = Gtk.Label()
        self.infobar_label.get_style_context().add_class("dim-label")
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
            }
        )
        if not self.mpv.start(initial_volume=self.config.get("volume", 100)):
            self.mostrar_mensagem("Erro ao iniciar o MPV. Verifique se está instalado.")

        self._apply_volume_ui()
        self.notebook.connect("switch-page", self.on_notebook_switch_page)

        self.connect("destroy", self.on_destroy)
        self.connect("key-press-event", self.on_key_press)

        self.render_queue()
        self.render_playlists()
        self.render_history()

        threading.Thread(target=self._check_ytdlp_update, daemon=True).start()
        GLib.timeout_add(700, self._show_welcome)

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
    # Construção das Abas
    # ======================================================================
    def _build_tab_search(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_box.set_border_width(12)

        search_input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Pesquisar músicas, artistas ou colar URL...")
        self.search_entry.connect("activate", self.on_search)
        search_input_box.pack_start(self.search_entry, True, True, 0)

        self.search_spinner = Gtk.Spinner()
        search_input_box.pack_start(self.search_spinner, False, False, 0)

        btn_search = Gtk.Button(label="Buscar")
        btn_search.get_style_context().add_class("suggested-action")
        btn_search.connect("clicked", self.on_search)
        search_input_box.pack_start(btn_search, False, False, 0)
        tab_box.pack_start(search_input_box, False, False, 0)

        results_scroll = Gtk.ScrolledWindow()
        results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.results_list = Gtk.ListBox()
        self.results_list.set_activate_on_single_click(False)
        self.results_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.results_list.connect("row-activated", self.on_result_activated)
        self.results_list.connect("button-press-event", self.on_results_button_press)

        results_scroll.add(self.results_list)
        tab_box.pack_start(results_scroll, True, True, 0)

        result_btns = Gtk.Box(spacing=8)
        btn_add_queue = Gtk.Button(label="+ Fila")
        btn_add_queue.connect("clicked", self.on_add_to_queue)

        btn_play_now = Gtk.Button(label="Tocar Agora")
        btn_play_now.connect("clicked", self.on_play_now)

        btn_add_pl = Gtk.Button(label="+ Playlist")
        btn_add_pl.connect("clicked", self.on_add_selection_to_playlist)

        btn_download_results = Gtk.Button(label="Baixar")
        btn_download_results.connect("clicked", self.on_download)

        for b in (btn_add_queue, btn_play_now, btn_add_pl, btn_download_results):
            result_btns.pack_start(b, False, False, 0)
        tab_box.pack_start(result_btns, False, False, 0)

        self.notebook.append_page(tab_box, Gtk.Label(label="Buscar"))

    def _build_tab_queue(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_box.set_border_width(12)

        tab_box.pack_start(self._section_label("Fila Atual de Reprodução"), False, False, 0)

        queue_scroll = Gtk.ScrolledWindow()
        queue_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.queue_list = Gtk.ListBox()
        self.queue_list.set_activate_on_single_click(False)
        self.queue_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.queue_list.connect("row-activated", self.on_queue_activated)
        self.queue_list.connect("button-press-event", self.on_queue_button_press)
        queue_scroll.add(self.queue_list)
        tab_box.pack_start(queue_scroll, True, True, 0)

        queue_btns = Gtk.Box(spacing=8)
        btn_add_pl_queue = Gtk.Button(label="+ Playlist")
        btn_add_pl_queue.connect("clicked", self.on_add_selection_to_playlist)

        btn_save_as_pl = Gtk.Button(label="Salvar Fila como Playlist")
        btn_save_as_pl.get_style_context().add_class("suggested-action")
        btn_save_as_pl.connect("clicked", self.on_save_queue_as_playlist)

        btn_delete_sel = Gtk.Button(label="Excluir Selecionadas")
        btn_delete_sel.connect("clicked", self.on_delete_selected_queue)

        btn_clear = Gtk.Button(label="Limpar Fila")
        btn_clear.connect("clicked", self.on_clear_queue)

        for b in (btn_add_pl_queue, btn_save_as_pl, btn_delete_sel, btn_clear):
            queue_btns.pack_start(b, False, False, 0)
        tab_box.pack_start(queue_btns, False, False, 0)

        self.notebook.append_page(tab_box, Gtk.Label(label="Fila"))

    def _build_tab_playlists(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tab_box.set_border_width(12)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_size_request(200, -1)

        left_box.pack_start(self._section_label("Suas Playlists"), False, False, 0)

        self.pl_search_entry = Gtk.SearchEntry()
        self.pl_search_entry.set_placeholder_text("Buscar playlist...")
        self.pl_search_entry.connect("search-changed", self.on_playlist_filter_changed)
        self.pl_search_entry.connect("stop-search", lambda e: e.set_text(""))
        left_box.pack_start(self.pl_search_entry, False, False, 0)

        pl_scroll = Gtk.ScrolledWindow()
        pl_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.playlists_list = Gtk.ListBox()
        self.playlists_list.connect("row-activated", self.on_playlist_selected)
        self.playlists_list.connect("button-press-event", self.on_playlists_button_press)
        self.playlists_list.set_filter_func(self._playlist_filter_func)
        pl_placeholder = Gtk.Label(label="Nenhuma playlist encontrada")
        pl_placeholder.get_style_context().add_class("dim-label")
        pl_placeholder.set_margin_top(12)
        pl_placeholder.show()
        self.playlists_list.set_placeholder(pl_placeholder)
        pl_scroll.add(self.playlists_list)
        left_box.pack_start(pl_scroll, True, True, 0)

        btn_new_pl = Gtk.Button(label="+ Nova Playlist")
        btn_new_pl.connect("clicked", self.on_create_playlist_dialog)
        left_box.pack_start(btn_new_pl, False, False, 0)

        tab_box.pack_start(left_box, False, False, 0)
        tab_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self.pl_title_label = self._section_label("Selecione uma Playlist")
        right_box.pack_start(self.pl_title_label, False, False, 0)

        pl_content_scroll = Gtk.ScrolledWindow()
        pl_content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pl_tracks_list = Gtk.ListBox()
        self.pl_tracks_list.set_activate_on_single_click(False)
        self.pl_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.pl_tracks_list.connect("row-activated", self.on_pl_track_activated)
        self.pl_tracks_list.connect("button-press-event", self.on_pl_tracks_button_press)
        pl_content_scroll.add(self.pl_tracks_list)
        right_box.pack_start(pl_content_scroll, True, True, 0)

        pl_actions_box = Gtk.Box(spacing=8)
        btn_play_pl = Gtk.Button(label="Tocar Playlist")
        btn_play_pl.get_style_context().add_class("suggested-action")
        btn_play_pl.connect("clicked", self.on_play_entire_playlist)

        btn_append_pl = Gtk.Button(label="+ À Fila")
        btn_append_pl.connect("clicked", self.on_append_playlist_to_queue)

        btn_del_pl = Gtk.Button(label="Excluir Playlist")
        btn_del_pl.connect("clicked", self.on_delete_current_playlist)

        for b in (btn_play_pl, btn_append_pl, btn_del_pl):
            pl_actions_box.pack_start(b, False, False, 0)
        right_box.pack_start(pl_actions_box, False, False, 0)

        tab_box.pack_start(right_box, True, True, 0)

        self.notebook.append_page(tab_box, Gtk.Label(label="Playlists"))

    def _build_tab_history(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_box.set_border_width(12)

        hist_scroll = Gtk.ScrolledWindow()
        hist_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_list = Gtk.ListBox()
        self.history_list.set_activate_on_single_click(False)
        self.history_list.connect("row-activated", self.on_history_activated)
        self.history_list.connect("button-press-event", self.on_history_button_press)
        hist_scroll.add(self.history_list)
        tab_box.pack_start(hist_scroll, True, True, 0)

        btn_clear_hist = Gtk.Button(label="Apagar Histórico")
        btn_clear_hist.connect("clicked", self.on_clear_history)
        tab_box.pack_start(btn_clear_hist, False, False, 0)

        self.notebook.append_page(tab_box, Gtk.Label(label="Recentes"))

    def _build_tab_lyrics(self):
        tab_lyrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        tab_lyrics_box.set_border_width(12)

        info_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.lyrics_art_img = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        self.lyrics_art_img.set_pixel_size(80)
        info_card.pack_start(self.lyrics_art_img, False, False, 0)

        info_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.lyrics_title_label = Gtk.Label(xalign=0)
        self.lyrics_title_label.set_line_wrap(True)
        self.lyrics_title_label.set_markup("<b>Nenhuma música tocando</b>")
        self.lyrics_artist_label = Gtk.Label(xalign=0)
        self.lyrics_album_label = Gtk.Label(xalign=0)
        self.lyrics_year_label = Gtk.Label(xalign=0)
        for lbl in (self.lyrics_artist_label, self.lyrics_album_label, self.lyrics_year_label):
            lbl.get_style_context().add_class("dim-label")
        info_texts.pack_start(self.lyrics_title_label, False, False, 0)
        info_texts.pack_start(self.lyrics_artist_label, False, False, 0)
        info_texts.pack_start(self.lyrics_album_label, False, False, 0)
        info_texts.pack_start(self.lyrics_year_label, False, False, 0)
        info_card.pack_start(info_texts, True, True, 0)

        tab_lyrics_box.pack_start(info_card, False, False, 0)
        tab_lyrics_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        lyrics_scroll = Gtk.ScrolledWindow()
        lyrics_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.lyrics_text_view = Gtk.TextView()
        self.lyrics_text_view.set_editable(False)
        self.lyrics_text_view.set_cursor_visible(False)
        self.lyrics_text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.lyrics_text_view.set_justification(Gtk.Justification.CENTER)
        self.lyrics_text_view.set_left_margin(12)
        self.lyrics_text_view.set_right_margin(12)
        self.lyrics_text_view.set_top_margin(8)
        self._update_lyrics_ui("Nenhuma música tocando no momento.")

        lyrics_scroll.add(self.lyrics_text_view)
        tab_lyrics_box.pack_start(lyrics_scroll, True, True, 0)

        lyrics_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_reload_lyrics = Gtk.Button(label="Buscar Novamente")
        btn_reload_lyrics.connect("clicked", lambda w: self.search_current_lyrics())
        lyrics_controls.pack_start(btn_reload_lyrics, False, False, 0)
        tab_lyrics_box.pack_start(lyrics_controls, False, False, 0)

        self.notebook.append_page(tab_lyrics_box, Gtk.Label(label="Letra"))

    def _build_tab_discover(self):
        tab_discover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab_discover_box.set_border_width(12)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.discover_entry = Gtk.Entry()
        self.discover_entry.set_placeholder_text("Nome de um artista ou banda...")
        self.discover_entry.connect("activate", self.on_discover_search)
        search_row.pack_start(self.discover_entry, True, True, 0)
        
        self.discover_spinner = Gtk.Spinner()
        search_row.pack_start(self.discover_spinner, False, False, 0)

        btn_discover_current = Gtk.Button(label="Tocando Agora")
        btn_discover_current.connect("clicked", self.on_discover_use_current)
        search_row.pack_start(btn_discover_current, False, False, 0)

        btn_discover = Gtk.Button(label="Descobrir")
        btn_discover.connect("clicked", self.on_discover_search)
        search_row.pack_start(btn_discover, False, False, 0)
        tab_discover_box.pack_start(search_row, False, False, 0)

        outer_scroll = Gtk.ScrolledWindow()
        outer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        content.pack_start(self._section_label("Artistas Similares"), False, False, 0)

        self.discover_artists_list = Gtk.ListBox()
        self.discover_artists_list.set_activate_on_single_click(False)
        self.discover_artists_list.connect("row-activated", self.on_discover_artist_activated)
        self.discover_artists_list.connect("button-press-event", self.on_discover_artists_button_press)
        self.discover_artists_list.set_size_request(-1, 130)
        artists_scroll = Gtk.ScrolledWindow()
        artists_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        artists_scroll.set_size_request(-1, 130)
        artists_scroll.add(self.discover_artists_list)
        content.pack_start(artists_scroll, False, False, 0)

        content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        tracks_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tracks_header.pack_start(self._section_label("Faixas Recomendadas"), True, True, 0)
        btn_add_all_pl = Gtk.Button(label="Adicionar faixas na Playlist")
        btn_add_all_pl.set_tooltip_text("Seleciona todas as faixas recomendadas e adiciona à playlist")
        btn_add_all_pl.connect("clicked", self.on_discover_add_all_to_playlist)
        tracks_header.pack_end(btn_add_all_pl, False, False, 0)
        content.pack_start(tracks_header, False, False, 0)

        self.discover_tracks_list = Gtk.ListBox()
        self.discover_tracks_list.set_activate_on_single_click(False)
        self.discover_tracks_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.discover_tracks_list.connect("row-activated", self.on_discover_track_activated)
        self.discover_tracks_list.connect("button-press-event", self.on_discover_tracks_button_press)
        content.pack_start(self.discover_tracks_list, False, False, 0)

        outer_scroll.add(content)
        tab_discover_box.pack_start(outer_scroll, True, True, 0)

        track_btns = Gtk.Box(spacing=8)
        btn_t_play = Gtk.Button(label="Reproduzir")
        btn_t_play.connect("clicked", lambda w: self.on_discover_track_button("play"))
        btn_t_add = Gtk.Button(label="+ Fila")
        btn_t_add.connect("clicked", lambda w: self.on_discover_track_button("queue"))
        btn_t_dl = Gtk.Button(label="Baixar")
        btn_t_dl.connect("clicked", lambda w: self.on_discover_track_button("download"))
        for b in (btn_t_play, btn_t_add, btn_t_dl):
            track_btns.pack_start(b, False, False, 0)
        tab_discover_box.pack_start(track_btns, False, False, 0)

        self.tab_discover = tab_discover_box
        self.notebook.append_page(tab_discover_box, Gtk.Label(label="Descobrir"))

    def _build_tab_wiki(self):
        tab_wiki_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tab_wiki_box.set_border_width(12)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.wiki_entry = Gtk.Entry()
        self.wiki_entry.set_placeholder_text("Nome do artista...")
        self.wiki_entry.connect("activate", self.on_wiki_search)
        search_row.pack_start(self.wiki_entry, True, True, 0)

        self.wiki_spinner = Gtk.Spinner()
        search_row.pack_start(self.wiki_spinner, False, False, 0)

        btn_wiki_current = Gtk.Button(label="Tocando Agora")
        btn_wiki_current.connect("clicked", self.on_wiki_use_current)
        search_row.pack_start(btn_wiki_current, False, False, 0)

        btn_wiki = Gtk.Button(label="Buscar")
        btn_wiki.connect("clicked", self.on_wiki_search)
        search_row.pack_start(btn_wiki, False, False, 0)
        tab_wiki_box.pack_start(search_row, False, False, 0)

        wiki_scroll = Gtk.ScrolledWindow()
        wiki_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.wiki_art_img = Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.DIALOG)
        self.wiki_art_img.set_pixel_size(96)
        header_box.pack_start(self.wiki_art_img, False, False, 0)

        header_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.wiki_name_label = Gtk.Label(xalign=0)
        self.wiki_name_label.set_markup("<b><big>Busque um artista</big></b>")
        self.wiki_fans_label = Gtk.Label(xalign=0)
        self.wiki_fans_label.get_style_context().add_class("dim-label")
        header_texts.pack_start(self.wiki_name_label, False, False, 0)
        header_texts.pack_start(self.wiki_fans_label, False, False, 0)
        header_box.pack_start(header_texts, True, True, 0)

        content.pack_start(header_box, False, False, 0)
        content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self.wiki_bio_label = Gtk.Label(xalign=0)
        self.wiki_bio_label.set_line_wrap(True)
        self.wiki_bio_label.set_text("Pesquise um artista para ver biografia e discografia.")
        content.pack_start(self.wiki_bio_label, False, False, 0)

        content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        content.pack_start(self._section_label("Discografia"), False, False, 0)
        hint = Gtk.Label(label="Dê dois cliques em um álbum para ver as faixas.", xalign=0)
        hint.get_style_context().add_class("dim-label")
        content.pack_start(hint, False, False, 0)

        self.wiki_albums_list = Gtk.ListBox()
        self.wiki_albums_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.wiki_albums_list.set_activate_on_single_click(False)
        self.wiki_albums_list.connect("row-activated", self.on_wiki_album_activated)
        self.wiki_albums_list.connect("button-press-event", self.on_wiki_albums_button_press)
        content.pack_start(self.wiki_albums_list, False, False, 0)

        wiki_scroll.add(content)

        # Stack sem animação (leve): página "artist" = bio/discografia, "album" = faixas
        self.wiki_stack = Gtk.Stack()
        self.wiki_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.wiki_stack.add_named(wiki_scroll, "artist")
        self.wiki_stack.add_named(self._build_wiki_album_page(), "album")
        tab_wiki_box.pack_start(self.wiki_stack, True, True, 0)

        self.tab_wiki = tab_wiki_box
        self.notebook.append_page(tab_wiki_box, Gtk.Label(label="Wiki"))

    def _build_wiki_album_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

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

    # ---------- Manipulação Atômica de JSON e Arquivos ----------
    def _load_json(self, path, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _save_json(self, path, data):
        """Salva arquivo JSON de forma atômica para evitar corrupção de dados."""
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---------- Normalização do Esquema de Dados de Faixas ----------
    def _normalize_track(self, item):
        uploader = item.get("uploader") or item.get("artist") or ""
        duration = item.get("duration") or item.get("duration_fmt") or "0:00"
        return {
            "id": item.get("id", ""),
            "title": item.get("title", "Sem título"),
            "uploader": uploader,
            "artist": uploader,
            "duration": duration,
            "duration_fmt": duration,
            "verified": bool(item.get("verified")),
        }

    # ---------- Mensagens e Utilitários ----------
    def mostrar_mensagem(self, texto):
        self.infobar_label.set_text(texto)
        if self._msg_timeout_id:
            GLib.source_remove(self._msg_timeout_id)
        self._msg_timeout_id = GLib.timeout_add_seconds(4, self._clear_mensagem)

    def _clear_mensagem(self):
        self.infobar_label.set_text("")
        self._msg_timeout_id = None
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
        self.notify_label.set_text(texto)
        self.notify_revealer.set_reveal_child(True)
        if self._toast_timeout_id:
            GLib.source_remove(self._toast_timeout_id)
        self._toast_timeout_id = GLib.timeout_add_seconds(3, self._hide_toast)

    def _hide_toast(self):
        self.notify_revealer.set_reveal_child(False)
        self._toast_timeout_id = None
        return False

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
        """Melhor palpite do artista de uma faixa (para Wiki/Descobrir)."""
        if item.get("verified") and item.get("artist"):
            return self._clean_artist_name(item["artist"])
        parts = re.split(r"\s+[-–—]\s+", item.get("title", ""), maxsplit=1)
        if len(parts) == 2 and 0 < len(parts[0]) <= 40:
            name = self._clean_artist_name(parts[0])
            if name:
                return name
        return self._clean_artist_name(item.get("uploader") or item.get("artist") or "")

    def _notify(self, title, message, icon="audio-x-generic"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", icon or "audio-x-generic", title, message], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _check_ytdlp_update(self):
        try:
            subprocess.run(["yt-dlp", "-U"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            pass

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

    def _playlist_filter_func(self, row, *_args):
        key = self._pl_filter_key
        return (not key) or key in getattr(row, "pl_key", "")

    def on_playlist_filter_changed(self, entry):
        self._pl_filter_key = self._norm_key(entry.get_text())
        self.playlists_list.invalidate_filter()

    def on_playlist_selected(self, listbox, row):
        if not row:
            return
        self.selected_playlist = row.pl_name
        self.render_playlist_tracks()

    def render_playlist_tracks(self):
        for child in list(self.pl_tracks_list.get_children()):
            self.pl_tracks_list.remove(child)

        if not self.selected_playlist or self.selected_playlist not in self.playlists:
            self.pl_title_label.set_markup("<b>Selecione uma Playlist</b>")
            return

        tracks = self.playlists[self.selected_playlist]
        title_txt = f"Playlist: {self.selected_playlist} ({len(tracks)} faixas)"
        self.pl_title_label.set_markup(f"<b>{GLib.markup_escape_text(title_txt)}</b>")

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
        title_txt = f"Playlist: {self.selected_playlist} ({len(tracks)} faixas)"
        self.pl_title_label.set_markup(f"<b>{GLib.markup_escape_text(title_txt)}</b>")

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

    def on_add_selection_to_playlist(self, button=None, items_to_add=None):
        if items_to_add is None:
            items_to_add = self.get_active_selection()

        if not items_to_add:
            self.mostrar_mensagem("Selecione ao menos uma faixa.")
            return

        items_to_add = [self._normalize_track(it) for it in items_to_add]

        dialog = Gtk.Dialog(title="Adicionar à Playlist", parent=self, flags=0)
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
        target_pl = combo.get_active_text()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and target_pl:
            if target_pl not in self.playlists:
                self.playlists[target_pl] = []
            self.playlists[target_pl].extend(items_to_add)
            self._save_json(PLAYLISTS_FILE, self.playlists)
            self.show_toast(f"{len(items_to_add)} faixa(s) adicionadas em '{target_pl}'")
            if self.selected_playlist == target_pl:
                self.render_playlist_tracks()

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
        self.queue = list(tracks)
        self._save_json(QUEUE_FILE, self.queue)
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
        self.on_playlist_selected(widget, row)
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
            for it in (item_play, item_add_q, item_dl):
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
        self.results = items
        for item in items:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{item['title']}  —  {item['uploader']} ({item['duration']})", xalign=0)
            label.set_line_wrap(True)
            label.set_margin_start(6)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            row.add(label)
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
            for it in (item_add, item_play, item_pl, item_dl):
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
    def add_to_queue(self, item, notify=True):
        item = self._normalize_track(item)
        self.queue.append(item)
        self._save_json(QUEUE_FILE, self.queue)

        row = self._create_queue_row(len(self.queue) - 1, item)
        self.queue_list.add(row)
        self.queue_list.show_all()

        if notify:
            self.show_toast(f"Adicionado à fila: {item['title']}")

    def add_items_bulk(self, items, notify=True):
        norm_items = [self._normalize_track(it) for it in items]
        start_idx = len(self.queue)
        self.queue.extend(norm_items)
        self._save_json(QUEUE_FILE, self.queue)

        for i, item in enumerate(norm_items, start=start_idx):
            row = self._create_queue_row(i, item)
            self.queue_list.add(row)

        self.queue_list.show_all()

        if notify:
            if len(norm_items) == 1:
                self.show_toast(f"Adicionado à fila: {norm_items[0]['title']}")
            else:
                self.show_toast(f"{len(norm_items)} faixas adicionadas à fila.")

    def render_queue(self):
        for child in list(self.queue_list.get_children()):
            self.queue_list.remove(child)

        for i, item in enumerate(self.queue):
            row = self._create_queue_row(i, item)
            self.queue_list.add(row)

        self.queue_list.show_all()
        self._highlight_current_row()

    def _create_queue_row(self, i, item):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        lbl = Gtk.Label(label=f"{i+1}. {item['title']} ({item.get('duration','')})", xalign=0)
        lbl.set_ellipsize(3)
        box.pack_start(lbl, True, True, 6)

        btn_up = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.MENU)
        btn_up.set_relief(Gtk.ReliefStyle.NONE)
        btn_up.connect("clicked", lambda b: self._move_queue_row(row, -1))

        btn_down = Gtk.Button.new_from_icon_name("go-down-symbolic", Gtk.IconSize.MENU)
        btn_down.set_relief(Gtk.ReliefStyle.NONE)
        btn_down.connect("clicked", lambda b: self._move_queue_row(row, 1))

        btn_del = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.MENU)
        btn_del.set_relief(Gtk.ReliefStyle.NONE)
        btn_del.connect("clicked", lambda b: self._remove_queue_row(row))

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
            self._save_json(QUEUE_FILE, self.queue)

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
            self._save_json(QUEUE_FILE, self.queue)
            self.queue_list.remove(row)

            if self.current_index == idx:
                if self.queue:
                    self.play_current()
                else:
                    self.current_index = -1
                    self.mpv.stop()
            elif self.current_index > idx:
                self.current_index -= 1

            self._update_queue_indices()

    def _update_queue_indices(self):
        for i, child in enumerate(self.queue_list.get_children()):
            if i < len(self.queue):
                item = self.queue[i]
                box = child.get_child()
                lbl = box.get_children()[0]
                lbl.set_text(f"{i+1}. {item['title']} ({item.get('duration','')})")
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

        self._save_json(QUEUE_FILE, self.queue)

        if playing_item is not None and playing_item in self.queue:
            self.current_index = self.queue.index(playing_item)
        elif playing_item is not None:
            self.current_index = -1
            self.mpv.stop()
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
        self._save_json(QUEUE_FILE, self.queue)
        self.current_index = -1
        self.mpv.stop()
        self.render_queue()

    def on_queue_activated(self, listbox, row):
        self.current_index = row.get_index()
        self.play_current()

    def on_queue_button_press(self, widget, event):
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
            for it in (item_play, item_pl, item_del, item_dl):
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
        if not self.history or self.history[0].get("id") != item["id"]:
            self.history.insert(0, item)
            self.history = self.history[:HISTORY_LIMIT]
            self._save_json(HISTORY_FILE, self.history)
            self.render_history()

    def render_history(self):
        for child in list(self.history_list.get_children()):
            self.history_list.remove(child)
        for item in self.history:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=f"{item['title']} — {item.get('uploader','')}", xalign=0)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            row.add(lbl)
            row.item = item
            self.history_list.add(row)
        self.history_list.show_all()

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
            for it in (item_play, item_add_q, item_pl):
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
        self._save_json(HISTORY_FILE, self.history)
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
            self._update_lyrics_ui("Buscando letra...")
            self.lyrics_title_label.set_markup("<b>Carregando...</b>")
            self.lyrics_artist_label.set_text("")
            self.lyrics_album_label.set_text("")
            self.lyrics_year_label.set_text("")
            self.lyrics_art_img.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
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
            threading.Thread(target=self._load_artwork_into, args=(artwork, self.lyrics_art_img, 80), daemon=True).start()

    def _update_lyrics_ui(self, text):
        buffer = self.lyrics_text_view.get_buffer()
        buffer.set_text(text)

    def _load_artwork_into(self, url, image_widget, size):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = resp.read()
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf().scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
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

    def on_discover_use_current(self, button):
        if 0 <= self.current_index < len(self.queue):
            name = self._guess_artist(self.queue[self.current_index])
            if name:
                self.discover_entry.set_text(name)
                self.on_discover_search(None)
                return
        self.mostrar_mensagem("Nenhuma faixa tocando no momento.")

    def on_discover_search(self, widget):
        query = self.discover_entry.get_text().strip()
        if not query:
            return

        self._artist_stale["discover"] = False
        self._loaded_key["discover"] = self._norm_key(query)

        self.discover_token += 1
        current_token = self.discover_token

        self.discover_spinner.start()
        self.show_toast("Buscando artista...")
        threading.Thread(target=self._discover_thread, args=(query, current_token), daemon=True).start()

    def _discover_thread(self, query, token):
        try:
            artist = self._deezer_search_artist(query)
        except Exception:
            artist = None

        if token != self.discover_token:
            return

        if not artist:
            GLib.idle_add(self._discover_failed, "Artista não encontrado.")
            return

        aid = artist["id"]
        related_r, top_r = self._http_json_many(
            [
                f"{DEEZER_API}/artist/{aid}/related?limit=15",
                f"{DEEZER_API}/artist/{aid}/top?limit=15",
            ]
        )
        related = (related_r or {}).get("data", []) or []
        top_tracks = (top_r or {}).get("data", []) or []

        if not top_tracks:
            # Alguns perfis vêm sem "top": cai para a busca de faixas pelo nome do artista
            try:
                q = urllib.parse.quote(f'artist:"{artist.get("name", "")}"')
                data = self._http_json(f"{DEEZER_API}/search?q={q}&limit=15").get("data", []) or []
                top_tracks = [t for t in data if (t.get("artist") or {}).get("id") == aid] or data
            except Exception:
                pass

        if token == self.discover_token:
            GLib.idle_add(self._populate_discover, artist, related, top_tracks)

    def _discover_failed(self, msg):
        self.discover_spinner.stop()
        self.mostrar_mensagem(msg)

    def _populate_discover(self, artist, related, top_tracks):
        self.discover_spinner.stop()
        self.discover_current_artist = artist
        self._loaded_key["discover"] = self._norm_key(artist.get("name", ""))
        self.discover_entry.set_text(artist.get("name", ""))

        for child in list(self.discover_artists_list.get_children()):
            self.discover_artists_list.remove(child)
        if not related:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="Nenhum artista similar encontrado.", xalign=0))
            self.discover_artists_list.add(row)
        for a in related:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=a.get("name", ""), xalign=0)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            row.add(lbl)
            row.artist_name = a.get("name", "")
            self.discover_artists_list.add(row)
        self.discover_artists_list.show_all()

        for child in list(self.discover_tracks_list.get_children()):
            self.discover_tracks_list.remove(child)
        if not top_tracks:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="Nenhuma faixa encontrada.", xalign=0))
            self.discover_tracks_list.add(row)
        for t in top_tracks:
            row = Gtk.ListBoxRow()
            artist_name = (t.get("artist") or {}).get("name", "")
            dur = self._fmt_duration(t.get("duration"))
            lbl = Gtk.Label(label=f"{t.get('title','')}  —  {artist_name} ({dur})", xalign=0)
            lbl.set_line_wrap(True)
            lbl.set_margin_start(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            row.add(lbl)
            raw_item = {"title": t.get("title", ""), "artist": artist_name, "duration_fmt": dur, "verified": True}
            row.item = self._normalize_track(raw_item)
            self.discover_tracks_list.add(row)
        self.discover_tracks_list.show_all()
        self.show_toast(f"Descobertas para: {artist.get('name','')}")

    def on_discover_artist_activated(self, listbox, row):
        name = getattr(row, "artist_name", None)
        if name:
            self.discover_entry.set_text(name)
            self.on_discover_search(None)

    def _open_artist_in_wiki(self, name):
        name = (name or "").strip()
        if not name:
            return
        self.wiki_entry.set_text(name)
        self._artist_stale["wiki"] = True
        target = self.notebook.page_num(self.tab_wiki)
        if self.notebook.get_current_page() == target:
            self.on_wiki_search(None)
        else:
            # o handler de troca de aba dispara a busca (aba marcada como pendente)
            self.notebook.set_current_page(target)

    def on_discover_artists_button_press(self, widget, event):
        """Botão direito em 'Artistas Similares': Buscar na Wiki."""
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        name = getattr(row, "artist_name", None) if row is not None else None
        if not name:
            return False
        widget.select_row(row)
        menu = Gtk.Menu()
        item_wiki = Gtk.MenuItem(label="Buscar na Wiki")
        item_wiki.connect("activate", lambda w: self._open_artist_in_wiki(name))
        menu.append(item_wiki)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _add_resolved_to_playlist(self, resolved):
        if not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            return
        self.on_add_selection_to_playlist(items_to_add=resolved)

    def on_discover_add_all_to_playlist(self, button=None):
        """Equivale a selecionar todas as faixas e usar 'Adicionar à Playlist' do botão direito."""
        self.discover_tracks_list.select_all()
        items = self._get_discover_selected_tracks()
        if not items:
            self.mostrar_mensagem("Não há faixas recomendadas para adicionar.")
            return
        self._discover_resolve_and(items, self._add_resolved_to_playlist)

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
        item_add.connect("activate", lambda w: self._discover_resolve_and(sel, self._queue_resolved_tracks))
        item_pl = Gtk.MenuItem(label="Adicionar à Playlist")
        item_pl.connect("activate", lambda w: self._discover_resolve_and(sel, lambda resolved: self.on_add_selection_to_playlist(items_to_add=resolved)))
        item_dl = Gtk.MenuItem(label="Baixar")
        item_dl.connect("activate", lambda w: self._discover_resolve_and(sel, self._download_resolved_tracks))
        for it in (item_play, item_add, item_pl, item_dl):
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
            self._discover_resolve_and(items, self._queue_resolved_tracks)
        elif action == "download":
            self._discover_resolve_and(items, self._download_resolved_tracks)

    def on_discover_track_button(self, action):
        self._run_dtracks_action(action, self._get_discover_selected_tracks())

    def _discover_resolve_and(self, dtracks, callback):
        if not dtracks:
            self.mostrar_mensagem("Selecione uma ou mais faixas.")
            return
        self.show_toast("Buscando faixa(s) no YouTube...")
        self._resolve_deezer_tracks_bulk(dtracks, callback)

    def _queue_resolved_tracks(self, resolved):
        if not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            return
        self.add_items_bulk(resolved)

    def _play_dtracks_progressive(self, dtracks):
        """Toca a 1ª faixa assim que ela é localizada; as demais entram na fila conforme resolvem."""
        if not dtracks:
            self.mostrar_mensagem("Selecione uma ou mais faixas.")
            return
        self.show_toast("Buscando faixa(s) no YouTube...")
        state = {"n": 0}

        def on_item(track):
            self.add_items_bulk([track], notify=False)
            if state["n"] == 0:
                self.current_index = len(self.queue) - 1
                self.play_current()
            state["n"] += 1

        def on_done(resolved):
            if not resolved:
                self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            elif len(resolved) > 1:
                self.show_toast(f"{len(resolved)} faixas adicionadas à fila.")

        self._resolve_deezer_tracks_bulk(dtracks, on_done, on_item=on_item)

    def _download_resolved_tracks(self, resolved):
        if not resolved:
            self.mostrar_mensagem("Não foi possível localizar as faixas no YouTube.")
            return
        if len(resolved) == 1:
            self._download_single_dialog(resolved[0])
        else:
            self._download_bulk_dialog(resolved)

    def _resolve_deezer_tracks_bulk(self, dtracks, callback, on_item=None):
        def worker():
            resolved = []
            for dtrack in dtracks:
                query = f"{dtrack.get('artist','')} - {dtrack.get('title','')}".strip(" -")
                try:
                    out = subprocess.check_output(
                        ["yt-dlp", f"ytsearch1:{query}", "--flat-playlist", "-j", "--no-warnings"],
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=20,
                    )
                    line = out.strip().split("\n")[0]
                    data = json.loads(line)
                    vid_id = data.get("id") or self._extract_id(data.get("url", ""))
                    if vid_id:
                        track = self._normalize_track(
                            {
                                "id": vid_id,
                                "title": dtrack.get("title", ""),
                                "uploader": dtrack.get("artist", ""),
                                "duration": dtrack.get("duration", ""),
                                "verified": True,
                            }
                        )
                        resolved.append(track)
                        if on_item:
                            GLib.idle_add(on_item, track)
                except Exception:
                    pass
            GLib.idle_add(callback, resolved)

        threading.Thread(target=worker, daemon=True).start()

    def _resolve_deezer_track(self, dtrack, callback):
        self._resolve_deezer_tracks_bulk([dtrack], lambda resolved: callback(resolved[0]) if resolved else self.mostrar_mensagem(
            f"Não foi possível localizar '{dtrack.get('title','')}' no YouTube."
        ))

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

        self.wiki_spinner.start()
        self.wiki_bio_label.set_text("Buscando...")
        threading.Thread(target=self._wiki_thread, args=(query, current_token), daemon=True).start()

    def _wiki_thread(self, query, token):
        try:
            artist = self._deezer_search_artist(query)
        except Exception:
            artist = None

        if token != self.wiki_token:
            return

        if not artist:
            GLib.idle_add(self._wiki_failed, "Artista não encontrado.")
            return

        # Discografia (Deezer) e biografia (Wikipédia) em paralelo
        box = {"albums": []}

        def _albums_worker():
            try:
                url = f"{DEEZER_API}/artist/{artist['id']}/albums?limit=100"
                box["albums"] = self._http_json(url).get("data", []) or []
            except Exception:
                pass

        t_albums = threading.Thread(target=_albums_worker, daemon=True)
        t_albums.start()
        bio = self._fetch_artist_bio(artist.get("name", ""))
        t_albums.join(timeout=10)

        if token == self.wiki_token:
            GLib.idle_add(self._populate_wiki, artist, box["albums"], bio)

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

    def _populate_wiki(self, artist, albums, bio):
        self.wiki_spinner.stop()
        self.wiki_artist_name = artist.get("name", "")
        self._loaded_key["wiki"] = self._norm_key(self.wiki_artist_name)
        self._album_cache.clear()
        self.album_token += 1
        self.album_spinner.stop()
        self.wiki_stack.set_visible_child_name("artist")

        self.wiki_name_label.set_markup(f"<b><big>{GLib.markup_escape_text(self.wiki_artist_name)}</big></b>")
        fans = artist.get("nb_fan", 0) or 0
        self.wiki_fans_label.set_text(f"{fans:,} fãs no Deezer".replace(",", "."))
        self.wiki_bio_label.set_text(bio)

        for child in list(self.wiki_albums_list.get_children()):
            self.wiki_albums_list.remove(child)

        if not albums:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="Nenhum álbum encontrado.", xalign=0))
            row.set_activatable(False)
            self.wiki_albums_list.add(row)
        for alb in albums:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            img = Gtk.Image.new_from_icon_name("media-optical", Gtk.IconSize.DND)
            box.pack_start(img, False, False, 0)
            year = (alb.get("release_date") or "")[:4]
            title = alb.get("title", "")
            lbl = Gtk.Label(label=f"{title} ({year})" if year else title, xalign=0)
            lbl.set_line_wrap(True)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            box.pack_start(lbl, True, True, 0)
            row.add(box)
            row.album = alb
            self.wiki_albums_list.add(row)
            cover = alb.get("cover_small")
            if cover:
                threading.Thread(target=self._load_artwork_into, args=(cover, img, 40), daemon=True).start()

        self.wiki_albums_list.show_all()
        picture = artist.get("picture_medium") or artist.get("picture") or ""
        if picture:
            threading.Thread(target=self._load_artwork_into, args=(picture, self.wiki_art_img, 96), daemon=True).start()

    # ---------- Faixas de um álbum (duplo clique na discografia) ----------
    def on_wiki_album_back(self, button):
        self.album_token += 1
        self.album_spinner.stop()
        self.wiki_stack.set_visible_child_name("artist")

    def on_wiki_album_activated(self, listbox, row):
        alb = getattr(row, "album", None)
        if not alb:
            return
        self.album_token += 1
        token = self.album_token

        year = (alb.get("release_date") or "")[:4]
        self.album_title_label.set_markup(f"<b>{GLib.markup_escape_text(alb.get('title', ''))}</b>")
        self.album_meta_label.set_text(" · ".join(x for x in (getattr(self, "wiki_artist_name", ""), year) if x))
        self.album_cover_img.set_from_icon_name("media-optical", Gtk.IconSize.DIALOG)
        for child in list(self.album_tracks_list.get_children()):
            self.album_tracks_list.remove(child)
        self.wiki_stack.set_visible_child_name("album")

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

        artist_default = getattr(self, "wiki_artist_name", "")
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
        self._discover_resolve_and(items, self._add_resolved_to_playlist)

    def _album_tracks_to_items(self, tracks):
        artist_default = getattr(self, "wiki_artist_name", "")
        items = []
        for t in tracks:
            artist_name = (t.get("artist") or {}).get("name") or artist_default
            dur = self._fmt_duration(t.get("duration"))
            items.append(self._normalize_track(
                {"title": t.get("title", ""), "artist": artist_name, "duration_fmt": dur, "verified": True}
            ))
        return items

    def _add_album_to_playlist(self, alb):
        """Adiciona todas as faixas de um álbum da discografia (sem precisar abri-lo) a uma playlist."""
        cached = self._album_cache.get(alb.get("id"))
        if cached is not None:
            items = self._album_tracks_to_items(cached)
            if items:
                self._discover_resolve_and(items, self._add_resolved_to_playlist)
            else:
                self.mostrar_mensagem("Este álbum não tem faixas disponíveis.")
            return

        self.show_toast("Carregando faixas do álbum...")

        def worker():
            try:
                data = self._http_json(f"{DEEZER_API}/album/{alb.get('id')}/tracks?limit=100").get("data", []) or []
            except Exception:
                data = None
            GLib.idle_add(done, data)

        def done(data):
            if data is None:
                self.mostrar_mensagem("Não foi possível carregar as faixas do álbum.")
                return False
            self._album_cache[alb.get("id")] = data
            items = self._album_tracks_to_items(data)
            if items:
                self._discover_resolve_and(items, self._add_resolved_to_playlist)
            else:
                self.mostrar_mensagem("Este álbum não tem faixas disponíveis.")
            return False

        threading.Thread(target=worker, daemon=True).start()

    def on_wiki_albums_button_press(self, widget, event):
        """Botão direito na discografia: abrir álbum ou adicionar as faixas a uma playlist."""
        if event.button != 3:
            return False
        row = widget.get_row_at_y(int(event.y))
        alb = getattr(row, "album", None) if row is not None else None
        if not alb:
            return False
        widget.select_row(row)
        menu = Gtk.Menu()
        item_open = Gtk.MenuItem(label="Ver faixas")
        item_open.connect("activate", lambda w: self.on_wiki_album_activated(widget, row))
        item_pl = Gtk.MenuItem(label="Adicionar faixas na Playlist")
        item_pl.connect("activate", lambda w: self._add_album_to_playlist(alb))
        for it in (item_open, item_pl):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ---------- Sincronia do artista tocando com Wiki / Descobrir ----------
    def _sync_artist_tabs(self, name):
        name = (name or "").strip()
        if not name:
            return
        self.now_artist = name
        key = self._norm_key(name)
        for tab, entry in (("wiki", self.wiki_entry), ("discover", self.discover_entry)):
            if entry.has_focus():
                continue  # não sobrescreve o que a pessoa está digitando
            entry.set_text(name)
            if self._loaded_key[tab] != key:
                self._artist_stale[tab] = True
        self._autoload_artist_tab(self.notebook.get_nth_page(self.notebook.get_current_page()))

    def _autoload_artist_tab(self, page):
        # Só busca na rede quando a aba está visível; nas outras, carrega ao abrir a aba
        if page is self.tab_wiki and self._artist_stale["wiki"]:
            self.on_wiki_search(None)
        elif page is self.tab_discover and self._artist_stale["discover"]:
            self.on_discover_search(None)

    def on_notebook_switch_page(self, notebook, page, page_num):
        self._autoload_artist_tab(page)

    # ---------- Controle de Reprodução ----------
    def play_item(self, item):
        item = self._normalize_track(item)
        self.add_to_queue(item, notify=False)
        self.current_index = len(self.queue) - 1
        self.play_current()

    def play_current(self):
        if not (0 <= self.current_index < len(self.queue)):
            return
        item = self.queue[self.current_index]
        url = f"https://www.youtube.com/watch?v={item['id']}"

        self._play_token += 1
        self._track_started = False
        self._stall_retry_count = 0
        self._cancel_stall_watchdog()

        self.mpv.load(url)
        self.now_playing_label.set_text(f"{item['title']}")
        self._highlight_current_row()
        self.add_to_history(item)

        self._schedule_stall_watchdog(self._play_token)

        threading.Thread(target=self._fetch_thumbnail, args=(item["id"], item["title"]), daemon=True).start()
        self.search_current_lyrics()
        self._sync_artist_tabs(self._guess_artist(item))

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
            url = f"https://www.youtube.com/watch?v={item['id']}"
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

            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
            pixbuf = pixbuf.scale_simple(64, 48, GdkPixbuf.InterpType.BILINEAR)
            GLib.idle_add(self.thumbnail_img.set_from_pixbuf, pixbuf)
        except Exception:
            GLib.idle_add(self.thumbnail_img.set_from_icon_name, "audio-x-generic", Gtk.IconSize.DIALOG)
        finally:
            GLib.idle_add(self._notify, APP_NAME, f"Tocando: {title}", icon_path or "audio-x-generic")

    def _highlight_current_row(self):
        for i, row in enumerate(self.queue_list.get_children()):
            if i == self.current_index:
                row.set_state_flags(Gtk.StateFlags.SELECTED, True)
            else:
                row.unset_state_flags(Gtk.StateFlags.SELECTED)

    # ---------- Callbacks do MPV ----------
    def on_mpv_time_change(self, pos):
        if pos and pos > 0.5 and not self._track_started:
            self._track_started = True
            self._cancel_stall_watchdog()
        if not self.user_is_seeking:
            self.seek_scale.set_value(pos)
            self.time_label.set_text(f"{self._fmt_duration(pos)} / {self._fmt_duration(self.track_duration)}")

    def on_mpv_duration_change(self, duration):
        self.track_duration = duration
        self.seek_scale.set_range(0, duration)

    def on_mpv_pause_change(self, is_paused):
        icon = "media-playback-start-symbolic" if is_paused else "media-playback-pause-symbolic"
        self.btn_playpause.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))

    def on_track_finished(self):
        if self.is_repeat:
            self.play_current()
        else:
            self.on_next(None)

    def on_load_error(self, error_msg):
        item = self.queue[self.current_index] if 0 <= self.current_index < len(self.queue) else None
        titulo = item["title"] if item else "faixa"
        self.mostrar_mensagem(f"Falha ao carregar '{titulo}': {error_msg}")
        if not self._track_started:
            self._cancel_stall_watchdog()
            self._check_playback_stall(self._play_token)

    # ---------- Ações de Botões e Sliders ----------
    def on_play_pause(self, button):
        self.mpv.pause_toggle()

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
        else:
            self.now_playing_label.set_text("Fila finalizada")

    def on_toggle_shuffle(self, button):
        self.is_shuffle = button.get_active()

    def on_toggle_repeat(self, button):
        self.is_repeat = button.get_active()

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

    # ---------- Colar Link Manual ----------
    def on_paste_link(self, button=None):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        link = clipboard.wait_for_text()
        if not link:
            self.mostrar_mensagem("Nenhum link copiado.")
            return

        self.search_entry.set_text(link)
        self.on_search(None)
        self.notebook.set_current_page(0)

    # ---------- Download MP3 (Com Progresso em Real-time) ----------
    def get_active_selection(self):
        for lb in (self.results_list, self.queue_list, self.pl_tracks_list, self.history_list):
            rows = lb.get_selected_rows()
            items = [r.item for r in rows if hasattr(r, "item")]
            if items:
                return items
        return []

    def on_download(self, button):
        items = self.get_active_selection()
        if not items:
            self.mostrar_mensagem("Selecione uma ou mais faixas para baixar.")
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

        url = f"https://www.youtube.com/watch?v={item['id']}"
        self.progress_download.set_fraction(0.0)
        self.progress_download.set_text("Iniciando download...")
        self.progress_download.show()
        threading.Thread(target=self._download_thread, args=(url, path), daemon=True).start()

    def _download_thread(self, url, path):
        out_template = path[:-4] if path.lower().endswith(".mp3") else path
        try:
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "0",
                "--newline",
                "-o",
                f"{out_template}.%(ext)s",
                url,
            ]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                match = re.search(r"\[download\]\s+(\d+\.\d+)%", line)
                if match:
                    pct = float(match.group(1)) / 100.0
                    GLib.idle_add(self._update_download_progress, pct, f"Baixando... {int(pct*100)}%")
            process.wait()
            if process.returncode == 0:
                GLib.idle_add(self._update_download_progress, 1.0, "Download concluído!")
                GLib.idle_add(self._notify, "Download", f"Salvo em: {path}", "folder-download-symbolic")
            else:
                GLib.idle_add(self.progress_download.set_text, "Erro no download.")
        except Exception as e:
            GLib.idle_add(self.progress_download.set_text, f"Erro: {e}")
        finally:
            GLib.timeout_add_seconds(3, lambda: (self.progress_download.hide(), False)[1])

    def _update_download_progress(self, frac, text):
        self.progress_download.set_fraction(frac)
        self.progress_download.set_text(text)

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
        for i, item in enumerate(items, start=1):
            item = self._normalize_track(item)
            url = f"https://www.youtube.com/watch?v={item['id']}"
            safe_title = re.sub(r'[\\/*?:"<>|]', "", item["title"])
            out_template = os.path.join(folder, safe_title)
            try:
                cmd = [
                    "yt-dlp",
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                    "--newline",
                    "-o",
                    f"{out_template}.%(ext)s",
                    url,
                ]
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in process.stdout:
                    match = re.search(r"\[download\]\s+(\d+\.\d+)%", line)
                    if match:
                        pct = float(match.group(1)) / 100.0
                        overall = ((i - 1) + pct) / total
                        GLib.idle_add(self._update_download_progress, overall, f"Faixa {i}/{total} ({int(pct*100)}%)")
                process.wait()
            except Exception:
                pass
            GLib.idle_add(self.progress_download.set_fraction, i / total)
            GLib.idle_add(self.progress_download.set_text, f"Concluído {i}/{total}")
        GLib.idle_add(self.progress_download.set_text, "Todos os downloads foram concluídos!")
        GLib.idle_add(self._notify, "Download", f"{total} faixa(s) salvas em: {folder}", "folder-download-symbolic")
        GLib.timeout_add_seconds(3, lambda: (self.progress_download.hide(), False)[1])

    # ---------- Atalhos de Teclado Ampliados ----------
    def on_key_press(self, widget, event):
        focused = self.get_focus()
        in_entry = isinstance(focused, Gtk.Entry)
        state = event.state & Gdk.ModifierType.CONTROL_MASK

        # Ctrl + V: Colar link de qualquer lugar da aplicação
        if state and event.keyval in (Gdk.KEY_v, Gdk.KEY_V):
            if not in_entry:
                self.on_paste_link()
                return True

        # Atalhos ativados fora de campos de texto
        if not in_entry:
            if event.keyval == Gdk.KEY_space:
                self.mpv.pause_toggle()
                return True

            elif not state and event.keyval in (Gdk.KEY_m, Gdk.KEY_M):
                self.on_toggle_mute()
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
            "organiza sua fila, playlists e histórico, mostra letras, descobre artistas parecidos e exibe "
            "biografia e discografia, tudo usando os widgets e o tema nativos do GTK, sem CSS customizado."
        ), False, False, 0)

        # --- Abas ---
        page.pack_start(self._about_heading("Abas do aplicativo"), False, False, 0)
        for name, desc in (
            ("Buscar", "Pesquisa no YouTube por texto ou link. Os resultados são refinados com metadados do Deezer."),
            ("Fila", "Fila de reprodução atual: reordenar, remover, salvar como playlist. É restaurada ao reabrir o app."),
            ("Playlists", "Criar, renomear, tocar, anexar à fila e excluir playlists, com filtro por nome."),
            ("Recentes", f"Histórico das últimas {HISTORY_LIMIT} faixas tocadas."),
            ("Letra", "Letra da música atual, com álbum, ano e capa."),
            ("Descobrir", "Artistas similares e faixas mais populares do artista que está tocando."),
            ("Wiki", "Biografia e discografia do artista; abra um álbum para ver as faixas e adicioná-las."),
            ("Sobre", "Esta página, além do seu perfil de usuário, backup dos dados e acesso ao instalador para atualizar ou desinstalar."),
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
            ("Persistência", "Dados em arquivos JSON, gravados de forma atômica (arquivo temporário + os.replace) para evitar corrupção. O volume usa debounce para não gravar a cada movimento do slider.", "JSON"),
            ("Downloads", "Áudio extraído e convertido para MP3 na melhor qualidade pelo yt-dlp, com progresso lido da saída do processo.", "yt-dlp + ffmpeg"),
        ):
            page.pack_start(self._about_card(name, desc, tag), False, False, 0)

        # --- APIs ---
        page.pack_start(self._about_heading("APIs e serviços online"), False, False, 0)
        page.pack_start(self._about_text("Nenhuma exige chave de API ou login.", dim=True), False, False, 0)
        for name, desc, tag in (
            ("Deezer API", "Refino de metadados na busca, álbum/ano/capa da aba Letra, busca de artistas, artistas relacionados, faixas mais populares, discografia e faixas de cada álbum. Uso não comercial.", "api.deezer.com"),
            ("LRCLIB", "Fonte das letras. Usa a letra simples ou, se não houver, a sincronizada sem os marcadores de tempo.", "lrclib.net"),
            ("Wikipédia (pt/en)", "Biografia do artista (texto sob licença CC BY-SA 4.0). Tenta primeiro a versão em português; se não achar, usa a inglesa. Só aceita páginas de músicos e bandas.", "wikipedia.org"),
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
            ("config.json", "Volume e tamanho da janela."),
            ("queue.json", "Fila de reprodução."),
            ("playlists.json", "Suas playlists e faixas."),
            ("history.json", f"Últimas {HISTORY_LIMIT} faixas tocadas."),
            ("profile.json", "Nome de usuário."),
        ):
            page.pack_start(self._about_card(name, desc), False, False, 0)
        page.pack_start(self._about_text(
            "Para trocar de PC, use Perfil de usuário › Exportar: nome, playlists e histórico vão para um único arquivo, "
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
            "<tt>Ctrl+V</tt>  Colar link e tocar\n"
            "Rolar o mouse sobre o botão de volume ajusta o volume."
        ), False, False, 0)

        scroll.add(page)
        self.tab_about = scroll
        self.notebook.append_page(scroll, Gtk.Label(label="Sobre"))

    # ======================================================================
    # Perfil de usuário
    # ======================================================================
    @staticmethod
    def _track_key(t):
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
            f"O arquivo contém {n_pl} playlist(s), {n_tr} faixa(s) e {len(data['history'])} item(ns) de histórico.{when}\n\n"
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
            if not (self.profile.get("name") or "").strip() and data["name"]:
                self.profile["name"] = data["name"]

        self._save_json(PLAYLISTS_FILE, self.playlists)
        self._save_json(HISTORY_FILE, self.history)
        self._save_profile()

        if self.selected_playlist not in self.playlists:
            self.selected_playlist = None
        self.render_playlists()
        self.render_playlist_tracks()
        self.render_history()
        self.show_toast("Perfil importado com sucesso!")
        return True

    # ---------- Encerramento ----------
    def on_destroy(self, widget):
        self._cancel_stall_watchdog()
        if self._config_save_id:
            GLib.source_remove(self._config_save_id)
            self._config_save_id = None
        self.config["width"], self.config["height"] = self.get_size()
        self._save_json(CONFIG_FILE, self.config)
        self._save_json(QUEUE_FILE, self.queue)
        self._save_json(PLAYLISTS_FILE, self.playlists)
        self._save_json(PROFILE_FILE, self.profile)
        self.mpv.quit()
        Gtk.main_quit()


if __name__ == "__main__":
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    Gtk.Window.set_default_icon_name(APP_ID)
    app = MusicPlayerApp()
    app.show_all()
    Gtk.main()
