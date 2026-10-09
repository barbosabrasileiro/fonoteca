<div align="center">

<img src="fonoteca.png" alt="Ícone da Fonoteca" width="140">

# Fonoteca

### Uma biblioteca musical para descobrir, organizar e ouvir música.

Player **leve e nativo para Linux** · Python + GTK3 + mpv · sem Electron, sem conta, sem chave de API.

<br>

[![Licença GPLv3](https://img.shields.io/badge/licença-GPL--3.0--or--later-blue?style=for-the-badge)](LICENSE)
[![Linux](https://img.shields.io/badge/plataforma-Linux-success?style=for-the-badge&logo=linux&logoColor=white)](#-instalação)
[![Python](https://img.shields.io/badge/Python-3.8+-FFD43B?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![GTK3](https://img.shields.io/badge/UI-GTK3-7F52FF?style=for-the-badge&logo=gtk&logoColor=white)](https://www.gtk.org/)
[![mpv](https://img.shields.io/badge/áudio-mpv-8E44AD?style=for-the-badge)](https://mpv.io/)

<br>

[**⚡ Instalar**](#-instalação) &nbsp;·&nbsp;
[**🎮 Como usar**](#-como-usar) &nbsp;·&nbsp;
[**⌨️ Atalhos**](#️-atalhos) &nbsp;·&nbsp;
[**🩺 Problemas?**](#-resolução-de-problemas) &nbsp;·&nbsp;
[**🤝 Contribuir**](#-contribuindo)

<br><br>

<img src="docs/screenshot.png" alt="Captura de tela da Fonoteca" width="860">

</div>

---

## ✨ Por que a Fonoteca?

Ela junta o melhor de dois mundos: a **sua biblioteca offline** e a **vastidão do streaming**, em um app que abre rápido e respeita o tema do seu desktop.

|   |   |
|---|---|
| 🎵 **Biblioteca híbrida** | Arquivos locais (tags e capas) + milhões de faixas online. Banco **SQLite com FTS5**: a busca na sua biblioteca é instantânea. |
| ⏭️ **Gapless de verdade** | A próxima faixa é resolvida em segundo plano e encadeada no mpv, sem silêncio entre as músicas. |
| 🎛️ **Equalizador de 10 bandas** | ±12 dB e presets prontos: *Flat, Rock, Pop, Bass Boost, Vocal e Jazz*. Abre com `Ctrl+E`. |
| ♾️ **Mix infinito** | Um clique e a fila se abastece sozinha com faixas parecidas com a que está tocando. |
| 📻 **Rádios web** | Milhares de estações ao vivo via Radio-Browser, por nome, país ou gênero. |
| 🎤 **Letras** | Painel lateral com a letra da faixa (LRCLIB ou a embutida no arquivo). |
| 🧠 **Descoberta** | Biografia (Wikipédia), discografia, faixas populares, artistas parecidos e ranking **Em alta** por país (Deezer). |
| ⬇️ **Downloads em MP3** | Com capa e tags ID3 gravadas, direto na sua coleção. |
| 📊 **Perfil e estatísticas** | Nível, mais ouvidas, horários e últimos 14 dias. Tudo calculado **só no seu computador**. |
| 🖥️ **Integração com o sistema** | **MPRIS2**: teclas de mídia, widget do painel e notificações. |

<details>
<summary><b>🔧 Como ela funciona por dentro</b></summary>

<br>

```mermaid
flowchart LR
    UI["🖼️ GTK3<br/>widgets nativos"] -- "JSON via socket Unix" --> MPV["🔊 mpv<br/>modo idle, sem vídeo"]
    MPV -- "hook de stream" --> YT["⬇️ yt-dlp<br/>bestaudio"]
    UI <--> DB[("🗄️ SQLite + FTS5<br/>library.db")]
    UI -- "threads + GLib.idle_add" --> NET["🌐 Deezer · LRCLIB<br/>Wikipédia · Radio-Browser"]
    UI <-- "MPRIS2" --> OS["🖥️ Desktop<br/>teclas de mídia"]
```

- **Sem pausa:** faltando ~15 s para o fim, a próxima faixa entra na fila do mpv com `loadfile append`.
- **Metadados limpos:** cada resultado do YouTube é cruzado com o Deezer (título, artista, duração).
- **Gravação segura:** arquivos JSON atômicos (temporário + `os.replace`) e socket em pasta privada `0700`.
- **Privacidade:** nenhuma conta, nenhuma telemetria, nenhuma chave de API.

</details>

---

## ⚡ Instalação

### 🧙 Opção 1 · Instalador gráfico (recomendada)

Três passos, copiando e colando no terminal. O instalador cuida do resto: dependências, `yt-dlp` oficial, ícone e atalho no menu.

**1️⃣ Instale `git` e `python3`** (o mínimo para baixar e rodar o instalador; escolha a linha da sua distro):

```bash
sudo apt install git python3     # Ubuntu / Debian / Mint
sudo dnf install git python3     # Fedora
sudo pacman -S git python        # Arch / Manjaro
```

> Já tem os dois? Pule para o passo 2. Para conferir: `git --version && python3 --version`

**2️⃣ Baixe a Fonoteca:**

```bash
git clone https://github.com/barbosabrasileiro/fonoteca.git
cd fonoteca
```

**3️⃣ Abra o instalador** e siga o "avançar, avançar, concluir":

```bash
python3 fonoteca-installer.py
```

Pronto! Abra **Fonoteca** no menu de aplicativos. 🎉

<details>
<summary><b>Prefere não usar o git?</b></summary>

<br>

Baixe o [ZIP do projeto](https://github.com/barbosabrasileiro/fonoteca/archive/refs/heads/main.zip), extraia e rode o instalador dentro da pasta:

```bash
unzip main.zip && cd fonoteca-main
python3 fonoteca-installer.py
```

</details>

<details>
<summary><b>Sem ambiente gráfico? O instalador também roda no terminal</b></summary>

<br>

Funciona em **apt, dnf, pacman, zypper, xbps e apk**:

```bash
python3 fonoteca-installer.py --install     # instalar
python3 fonoteca-installer.py --update      # atualizar
python3 fonoteca-installer.py --diagnose    # diagnosticar
python3 fonoteca-installer.py --uninstall   # desinstalar
python3 fonoteca-installer.py --cli         # menu interativo
```

</details>

> [!TIP]
> O instalador fica no menu como *Instalador da Fonoteca* e na barra lateral do app, em **Corrigir e Atualizar a Fonoteca**.

### 🛠️ Opção 2 · Manual

<details>
<summary><b>Ubuntu / Debian / Mint</b></summary>

```bash
sudo apt update
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 mpv ffmpeg python3-mutagen
pipx install yt-dlp        # ou: python3 -m pip install -U yt-dlp
```

</details>

<details>
<summary><b>Fedora</b></summary>

```bash
sudo dnf install python3 python3-gobject gtk3 mpv ffmpeg-free python3-mutagen
pipx install yt-dlp
```

</details>

<details>
<summary><b>Arch / Manjaro</b></summary>

```bash
sudo pacman -S python python-gobject gtk3 mpv ffmpeg python-mutagen yt-dlp
```

</details>

Depois, rode direto do clone:

```bash
python3 fonoteca.py
```

<details>
<summary><b>📋 Dependências em resumo</b></summary>

<br>

| Pacote | Papel | Status |
|---|---|---|
| `python3` ≥ 3.8 + `python3-gi` (GTK3) | Interface | **Obrigatório** |
| `mpv` | Motor de áudio | **Obrigatório** |
| `yt-dlp` | Busca e stream online | **Obrigatório** |
| `ffmpeg` | Conversão para MP3 e capas | Recomendado |
| `python3-mutagen` | Leitura rápida de tags e capas | Recomendado |
| `python3-gi-cairo` | Arrastar faixas na fila | Recomendado |
| `libnotify` (`notify-send`) | Notificações do desktop | Opcional |

</details>

---

## 🎮 Como usar

<table>
<tr>
<td width="50%" valign="top">

### 🔍 Buscar e tocar
- Digite na busca e aperte `Enter`.
- **Cole um link** (`Ctrl+V` em qualquer lugar do app) para tocar na hora.
- Filtros: *Tudo · Biblioteca · Músicas · Artistas · Álbuns*.
- **Botão direito** em qualquer faixa, álbum ou artista abre o menu completo: fila, mix, playlist e mais.

### ♾️ Mix automático
Toque uma música e clique em **Mix** no player. A fila vira um fluxo sem fim de faixas parecidas.

</td>
<td width="50%" valign="top">

### 📁 Músicas offline
Abra **Músicas Offline**, escolha sua pasta de `.mp3`, `.flac`, `.m4a` e pronto: a Fonoteca indexa as tags e capas. Use **Completar dados online** para preencher o que faltar.

### 🎼 Playlists e favoritas
Curta com `♡` (ou tecla `L`), crie playlists com **+ Playlist** ou salve a fila atual com um clique.

### 💾 Trocando de PC
**Perfil › Exportar perfil** gera um único arquivo com playlists, favoritas e histórico. No outro computador, use **Importar**.

</td>
</tr>
</table>

---

## ⌨️ Atalhos

| Tecla | Ação | | Tecla | Ação |
|:---:|---|---|:---:|---|
| <kbd>Espaço</kbd> | Play / Pause | | <kbd>M</kbd> | Mudo |
| <kbd>Ctrl</kbd> + <kbd>→</kbd> | Próxima faixa | | <kbd>L</kbd> | Curtir faixa |
| <kbd>Ctrl</kbd> + <kbd>←</kbd> | Faixa anterior | | <kbd>Ctrl</kbd> + <kbd>F</kbd> | Ir para a busca |
| <kbd>→</kbd> / <kbd>←</kbd> | Avançar / voltar 5 s | | <kbd>Ctrl</kbd> + <kbd>E</kbd> | Equalizador |
| <kbd>Alt</kbd> + <kbd>←</kbd> | Voltar na navegação | | <kbd>Ctrl</kbd> + <kbd>V</kbd> | Colar link e tocar |

Teclas de mídia do teclado e do painel também funcionam, via **MPRIS2**.

---

## 🩺 Resolução de problemas

> [!TIP]
> **Antes de tudo:** abra o instalador e escolha **Diagnosticar** (ou `python3 fonoteca-installer.py --diagnose`). Ele verifica dependências, `yt-dlp` e conexão com os serviços, explica o que está errado e corrige com um clique.

<details>
<summary>🔇 <b>Não toca / "não foi possível reproduzir" / busca vazia</b></summary>

<br>

Quase sempre é o `yt-dlp` desatualizado, porque o YouTube muda com frequência. Atualize:

```bash
yt-dlp -U
# se instalou via pip:
python3 -m pip install -U yt-dlp
# ou deixe o instalador cuidar de tudo:
python3 fonoteca-installer.py --update
```

</details>

<details>
<summary>⚙️ <b>O app não abre ou fecha sozinho</b></summary>

<br>

Rode pelo terminal com registro detalhado e veja a mensagem de erro:

```bash
FONOTECA_DEBUG=1 python3 fonoteca.py
```

Confira também se o `python3-gi` e o `mpv` estão instalados (`mpv --version`).

</details>

<details>
<summary>🔄 <b>Atualizei, mas nada mudou</b></summary>

<br>

Uma Fonoteca que já estava aberta continua rodando o código antigo. **Feche e abra de novo** (o instalador também oferece reiniciar para você).

</details>

<details>
<summary>⬇️ <b>Download não gera MP3</b></summary>

<br>

Falta o `ffmpeg`. Instale com o gerenciador da sua distro (`sudo apt install ffmpeg`, `sudo dnf install ffmpeg-free`, `sudo pacman -S ffmpeg`).

</details>

<details>
<summary>🗂️ <b>Onde ficam meus dados?</b></summary>

<br>

Tudo em `~/.config/fonoteca/`:

| Arquivo | Conteúdo |
|---|---|
| `library.db` | Faixas, playlists, favoritas e histórico (SQLite + FTS5) |
| `config.json` | Volume, janela, equalizador e gapless |
| `queue.json` | Fila de reprodução |
| `stats.json` | Estatísticas do Perfil (só neste computador) |
| `covers/` | Capas extraídas dos arquivos locais |

Se o app vier de uma versão antiga (*YT Music Player*), seus dados são copiados sozinhos na primeira abertura.

</details>

> [!NOTE]
> Não achou a solução? [Abra uma issue](https://github.com/barbosabrasileiro/fonoteca/issues) e rode antes com `FONOTECA_DEBUG=1` para anexar o log.

---

## 🌐 Serviços usados

Nenhum exige chave de API ou login.

[![Deezer](https://img.shields.io/badge/Deezer-metadados%20e%20rankings-A238FF?style=flat-square)](https://developers.deezer.com/)
[![LRCLIB](https://img.shields.io/badge/LRCLIB-letras-orange?style=flat-square)](https://lrclib.net/)
[![Wikipédia](https://img.shields.io/badge/Wikipédia-biografias-000000?style=flat-square&logo=wikipedia&logoColor=white)](https://www.wikipedia.org/)
[![Radio-Browser](https://img.shields.io/badge/Radio--Browser-rádios%20web-2E86C1?style=flat-square)](https://www.radio-browser.info/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-streams-FF0000?style=flat-square&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)

---

## 🤝 Contribuindo

Bugs, ideias e *pull requests* são muito bem-vindos!

```bash
git checkout -b feature/minha-ideia
git commit -m "Adiciona minha ideia"
git push origin feature/minha-ideia
```

Depois, abra um **Pull Request**. ⭐ Se a Fonoteca toca no seu coração, deixe uma estrela no repositório.

## 📝 Licença

Software livre sob a **[GNU GPL v3.0 ou posterior](LICENSE)**, sem qualquer garantia.
`mpv` e `yt-dlp` são programas independentes, com licenças próprias. Biografias da Wikipédia sob CC BY-SA 4.0. Este projeto não é afiliado a YouTube, Google, Deezer, Wikimedia ou ao projeto mpv; todas as marcas pertencem aos seus donos.

<div align="center">

<br>

Feito com ♥ e muita música por [**Josuel Barbosa**](https://github.com/barbosabrasileiro)

</div>
