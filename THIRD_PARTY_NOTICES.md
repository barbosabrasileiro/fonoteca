# Avisos de terceiros

A Fonoteca (código sob GPL-3.0-or-later, veja `LICENSE`) **não inclui nem redistribui** nenhum dos programas ou bibliotecas abaixo: eles são instalados separadamente pelo usuário e executados como processos independentes, ou fornecidos pelo sistema.
Se você criar um pacote que os inclua (AppImage, Flatpak, .deb com dependências embutidas, etc.), passa a ter de cumprir a licença de cada um e incluir os textos correspondentes.

## Programas e bibliotecas

| Componente | Como é usado | Licença | Incluído neste repositório? |
|---|---|---|---|
| [mpv](https://mpv.io) | Motor de reprodução, controlado por IPC (socket Unix) em processo separado | GPL-2.0-or-later (LGPL-2.1-or-later se compilado sem GPL) | Não |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Chamado como comando (`subprocess`) para busca, streams e downloads | The Unlicense (domínio público) | Não |
| [FFmpeg](https://ffmpeg.org) | Usado pelo yt-dlp para converter para MP3 | LGPL-2.1+ ou GPL-2+, conforme a compilação | Não |
| [GTK 3](https://www.gtk.org) e [PyGObject](https://pygobject.gnome.org) | Interface gráfica (importados como bibliotecas do sistema) | LGPL-2.1-or-later | Não |
| [Python](https://www.python.org) | Interpretador | PSF License | Não |
| libnotify (`notify-send`) | Notificações de desktop (opcional) | LGPL-2.1-or-later | Não |

Ícones da interface (`audio-volume-high-symbolic`, etc.) vêm do **tema de ícones instalado no sistema do usuário** e não são distribuídos com a Fonoteca. Nenhuma fonte tipográfica é distribuída.

## Serviços online

| Serviço | Termos / licença dos dados | O que a Fonoteca faz para cumprir |
|---|---|---|
| **Deezer API** | [Termos para desenvolvedores](https://developers.deezer.com/termsofuse): uso não comercial; sem receita direta ou indireta ligada ao serviço; conteúdo (capas, imagens, textos) pertence ao Deezer ou a terceiros | Usa só endpoints públicos de catálogo, exibe as informações no app sem redistribuí-las e declara a restrição nas telas e no README |
| **LRCLIB** ([lrclib.net](https://lrclib.net)) | API aberta e gratuita, sem chave. As letras pertencem aos seus titulares de direitos | User-Agent identificando o app e o repositório; a letra é exibida ao usuário e não é armazenada nem redistribuída |
| **Wikipédia / Wikimedia** | Texto sob [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); a política de API exige User-Agent identificável | Cada biografia exibida cita a Wikipédia, a licença CC BY-SA 4.0 e o link da página; User-Agent identificável |
| **YouTube** (via yt-dlp, `i.ytimg.com`) | [Termos de Serviço do YouTube](https://www.youtube.com/t/terms) | Aviso de uso responsável no README; o app não hospeda nem redistribui mídia |

## Texto de atribuição da Wikipédia

> Biografias de artistas: © colaboradores da Wikipédia, disponíveis sob a licença Creative Commons Atribuição-CompartilhaIgual 4.0 (CC BY-SA 4.0). O link da página de origem é exibido junto de cada texto.
