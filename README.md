<p align="center">
  <img src="fonoteca.png" alt="Ícone da Fonoteca" width="128">
</p>

<h1 align="center">Fonoteca</h1>

<p align="center"><em>Uma biblioteca musical para descobrir, organizar e ouvir música.</em></p>

<p align="center">
  Software livre · GPL-3.0-or-later · Linux · Python + GTK3 + mpv
</p>

---

## O que é

A **Fonoteca** é um player de música leve para o desktop Linux. Ela busca faixas, toca só o áudio, organiza sua fila, playlists e histórico, mostra letras, ajuda a descobrir artistas parecidos e exibe biografia e discografia, tudo usando os widgets e o tema nativos do GTK (sem CSS customizado) e pensada para rodar bem em PCs modestos.

## Instalação rápida (copiar e colar)

Funciona em **Debian, Ubuntu, Mint, Fedora, Arch, Manjaro, openSUSE, Void e Alpine**. Abra um terminal (`Ctrl+Alt+T` na maioria das distros) e siga os passos na ordem. Cada bloco pode ser copiado inteiro e colado no terminal.

### Passo 1: instale o `git`

Escolha **um** bloco, o da sua distro:

```bash
# Debian / Ubuntu / Mint / Pop!_OS
sudo apt update && sudo apt install -y git
```

```bash
# Fedora
sudo dnf install -y git
```

```bash
# Arch / Manjaro
sudo pacman -S --needed --noconfirm git
```

```bash
# openSUSE
sudo zypper --non-interactive install git
```

> Já tem o `git`? Confira com `git --version` e pule para o passo 2.

### Passo 2: baixe a Fonoteca

```bash
git clone https://github.com/barbosabrasileiro/fonoteca.git
cd fonoteca
```

> **Sem `git`?** Na página do projeto no GitHub, clique em **Code › Download ZIP**, extraia o arquivo e abra um terminal dentro da pasta extraída.

### Passo 3: rode o instalador

Este comando instala tudo o que a Fonoteca precisa (mpv, ffmpeg, GTK, `yt-dlp` oficial), copia os arquivos e cria **dois atalhos** no menu de aplicativos: **Fonoteca** e **Instalador da Fonoteca** (este serve para atualizar, diagnosticar ou desinstalar depois). **Ele vai pedir a sua senha** (é a mesma senha do seu usuário; o terminal não mostra nada enquanto você digita, é normal).

```bash
python3 fonoteca-installer.py --install
```

Ao final, o instalador mostra um resumo. O que você quer ver é **"Tudo certo."**

<details>
<summary><b>Prefere um assistente gráfico (avançar, avançar, concluir)?</b></summary>

<br>

Se o seu sistema já tem GTK e PyGObject, este comando abre a janela do assistente. Se não tiver, ele cai sozinho para um menu no terminal.

```bash
python3 fonoteca-installer.py
```

</details>

### Passo 4: abra a Fonoteca

Procure por **Fonoteca** no menu de aplicativos. Se o ícone ainda não apareceu, saia e entre na sessão de novo, ou abra direto pelo terminal:

```bash
~/.local/share/fonoteca/fonoteca.sh
```

Pronto. Vá para [Como usar](#como-usar-em-1-minuto).

---

## Manutenção (atualizar, diagnosticar, desinstalar)

Você pode fazer tudo isso **sem digitar nada**, por qualquer um destes caminhos:

- **Menu de aplicativos:** abra **Instalador da Fonoteca**.
- **Dentro do app:** aba **Sobre › Atualizar ou desinstalar**.
- **Terminal:** comandos abaixo.

O assistente gráfico pergunta o que você quer fazer (Instalar, Atualizar, Diagnosticar ou Desinstalar) e mostra um resumo antes de aplicar.

### Atualizar

O `yt-dlp` é o que mais quebra, porque o YouTube muda sempre. Se a busca parar de funcionar, comece por aqui. A atualização **baixa as novidades do repositório sozinha** (`git pull` na pasta que você clonou, ou um clone novo se ela tiver sumido) e reaplica os arquivos.

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --update
```

Para atualizar também o `mpv` e o `ffmpeg` pelo gerenciador de pacotes da distro (pede a senha):

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --update --system
```

Quer só o `yt-dlp`, sem mexer nos arquivos da Fonoteca?

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --update --no-app
```

Depois de atualizar, **feche e abra a Fonoteca de novo** para carregar a versão nova. A versão anterior fica guardada em `fonoteca.py.bak`.

### Diagnosticar problemas

Verifica Python, GTK, mpv, yt-dlp, ffmpeg, atalhos, seus dados, a conexão com os serviços e faz uma busca de teste de verdade. Se achar algo errado, oferece corrigir na hora.

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --diagnose
```

### Desinstalar

Remove o app, os atalhos e o ícone. **Seus dados (playlists, histórico, perfil) ficam guardados** em `~/.config/fonoteca/`, e o `yt-dlp` também. O comando pede confirmação:

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --uninstall
```

Para apagar **também seus dados** e o `yt-dlp` instalado pelo instalador (não dá para desfazer):

```bash
python3 ~/.local/share/fonoteca/fonoteca-installer.py --uninstall --remove-data --remove-ytdlp
```

> Antes de apagar seus dados, você pode salvar um backup em **Perfil › Exportar** dentro do app.
>
> `mpv`, `ffmpeg`, Python e GTK **não são removidos**, porque outros programas podem usá-los. O instalador mostra o comando para removê-los, se você quiser.
>
> A pasta que você clonou (`fonoteca/`) também não é apagada. Depois de desinstalar, remova-a com `rm -rf ~/fonoteca` (ajuste o caminho se clonou em outro lugar).

---

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| A busca não retorna nada ou dá erro | Atualize: abra **Instalador da Fonoteca** no menu (ou **Sobre › Atualizar ou desinstalar**) e escolha *Atualizar* |
| O ícone não aparece no menu | Saia e entre na sessão de novo. Enquanto isso, abra com `~/.local/share/fonoteca/fonoteca.sh` |
| `yt-dlp: command not found` ao abrir com `python3 fonoteca.py` | O `yt-dlp` fica em `~/.local/bin`, que pode não estar no PATH do terminal. Abra pelo atalho do menu ou por `~/.local/share/fonoteca/fonoteca.sh`, que já corrige isso |
| A janela não abre | Rode `python3 ~/.local/share/fonoteca/fonoteca-installer.py --diagnose` e aceite a correção (`s` no terminal) |
| Downloads não viram MP3 | Falta o `ffmpeg`. Rode de novo, dentro da pasta clonada: `python3 fonoteca-installer.py --install` |
| Sumiu o atalho do instalador | Rode o `--update` (ele recria os atalhos) |
| "Não achei o código-fonte para atualizar" | A pasta clonada foi apagada ou movida. Clone de novo (`git clone ...`) e rode `python3 fonoteca-installer.py --update` dentro dela |
| "Fonoteca parece estar aberta" mesmo fechada | O `--diagnose` remove o socket abandonado do mpv quando você aceita a correção |
| Sistema imutável (Silverblue, Kinoite etc.) | O instalador não mexe em pacotes do sistema. Instale `mpv` e `ffmpeg` numa caixa (toolbox/distrobox) ou com `rpm-ostree install` e rode o instalador de novo |

Se nada resolver, rode o `--diagnose`, copie o relatório final e abra uma *issue* no GitHub.

---

## Instalação manual (sem o instalador)

<details>
<summary>Para quem prefere controlar cada passo</summary>

<br>

**1. Dependências do sistema** (escolha o bloco da sua distro):

```bash
# Debian / Ubuntu / Mint
sudo apt install python3-gi gir1.2-gtk-3.0 mpv ffmpeg libnotify-bin
```

```bash
# Arch / Manjaro
sudo pacman -S python-gobject gtk3 mpv ffmpeg libnotify
```

```bash
# Fedora
sudo dnf install python3-gobject gtk3 mpv ffmpeg libnotify
```

**2. yt-dlp** (a versão oficial se atualiza sozinha com `yt-dlp -U`):

```bash
mkdir -p ~/.local/bin
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o ~/.local/bin/yt-dlp
chmod +x ~/.local/bin/yt-dlp
```

**3. Rodar direto da pasta:**

```bash
PATH="$HOME/.local/bin:$PATH" python3 fonoteca.py
```

**4. (Opcional) Ícone e atalho no menu:**

```bash
mkdir -p ~/.local/share/icons/hicolor/256x256/apps ~/.local/share/applications
cp fonoteca.png ~/.local/share/icons/hicolor/256x256/apps/
sed "s|/CAMINHO/PARA/fonoteca.py|$PWD/fonoteca.py|" fonoteca.desktop > ~/.local/share/applications/fonoteca.desktop
```

</details>

### Requisitos

- Linux com GTK 3 e PyGObject
- Python 3.8 ou superior
- [`mpv`](https://mpv.io) (reprodução)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (busca, streams e downloads)
- `ffmpeg` (converter downloads para MP3)
- `notify-send` / libnotify (notificações; opcional)

O instalador cuida de tudo isso para você.

---

## Como o projeto funciona

| | |
|---|---|
| Código | **Open source** (GPL-3.0-or-later) |
| Player | **Gratuito** |
| Busca cultural (letras, biografia, discografia) | **Gratuita** |
| Biblioteca local (fila, playlists, histórico) | **Gratuita** |

<!--
  Só descomente a seção abaixo depois de ler docs/AUDITORIA-LICENCAS.md (itens 5 e 6):
  os termos da API do Deezer proíbem qualquer receita, direta ou indireta, ligada ao uso do serviço.

## Apoie o projeto

Doações são opcionais e ajudam a manter o desenvolvimento: LINK-AQUI
-->

## Seus dados

Tudo fica em `~/.config/fonoteca/` (`config.json`, `queue.json`, `playlists.json`, `history.json`, `profile.json`). Nada é enviado para servidores da Fonoteca: **não há conta, login, telemetria nem anúncios**.

O perfil de usuário pode ser salvo, exportado e importado (aba **Perfil**) para garantir portabilidade de seus dados. Para trocar de PC, exporte o perfil no antigo e importe no novo.

## Serviços online usados

A Fonoteca não tem servidor próprio. Para funcionar, ela consulta serviços de terceiros diretamente do seu computador, sem chave de API. Ao usar o app, termos de busca e nomes de artistas e faixas são enviados a eles, e o seu IP fica visível para eles, como em qualquer navegador.

| Serviço | Para quê | Observação |
|---|---|---|
| YouTube (via `yt-dlp`) | Busca e áudio das faixas | Sujeito aos termos do YouTube |
| Deezer (API pública) | Nomes limpos, artistas similares, discografia, capas | Uso não comercial |
| LRCLIB | Letras | Serviço comunitário e aberto |
| Wikipédia | Biografias | Texto sob CC BY-SA 4.0 |

Detalhes e avisos de licença em [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Uso responsável

A Fonoteca é uma ferramenta para uso pessoal. **Você é responsável pelo que toca e baixa com ela.** Baixe apenas conteúdo que você tem direito de baixar (músicas de sua autoria, conteúdo livre/Creative Commons, ou o que a lei do seu país permitir para cópia privada) e respeite os termos de uso dos serviços acessados. Os autores não incentivam nem apoiam a violação de direitos autorais.

## Contribuindo

Contribuições são bem-vindas: abra uma *issue* ou um *pull request*. Ao contribuir, você declara que tem direito de enviar o código e concorda em licenciá-lo sob a mesma licença do projeto. Use `git commit -s` (Developer Certificate of Origin).

## Licença

Copyright © 2026 Josuel Barbosa.

A Fonoteca é software livre, distribuída sob os termos da **GNU General Public License v3.0 ou posterior** (veja [`LICENSE`](LICENSE)). Ela é fornecida **sem nenhuma garantia**.

O ícone (`fonoteca.svg` / `fonoteca.png`) é uma arte original do projeto, distribuída sob a mesma licença.

## Marcas e afiliação

Fonoteca é um projeto independente. **Não é afiliada, patrocinada nem endossada** por YouTube, Google, Deezer, Wikimedia Foundation, LRCLIB ou pelo projeto mpv. Todos os nomes e marcas citados pertencem aos seus respectivos donos e são usados apenas para identificar os serviços com os quais o programa se comunica.
