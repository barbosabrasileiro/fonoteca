# Auditoria de licenças e termos: Fonoteca (antigo `music.py`)

> Isto é uma análise técnica feita a partir do código e dos termos públicos consultados em 28/09/2026. **Não é aconselhamento jurídico.** Termos mudam sem aviso; antes de monetizar, vale uma consulta rápida com advogado (ou, no mínimo, reler os links abaixo).

## Veredito

| Pergunta | Resposta |
|---|---|
| Posso publicar **de graça** no GitHub (código aberto)? | **Sim**, com as mudanças já aplicadas no `fonoteca.py` e os arquivos deste pacote. |
| Posso pedir **doações**? | **Zona cinzenta.** Os termos do Deezer proíbem receita "direta ou indireta" ligada ao uso da API. Uma leitura literal inclui doações. Ver itens 5 e 6. |
| Posso **vender** o app ou serviços extras? | **Não enquanto o app depender do Deezer** (e o download via YouTube é um segundo risco). Caminho seguro: trocar o Deezer por fontes abertas (plano abaixo) e manter os "serviços extras" totalmente separados. |
| Posso usar o nome **Fonoteca**? | Provavelmente sim (ver item 15), mas faça a checagem de 15 minutos indicada. |

## Tabela de auditoria

| # | Item | O que encontrei | Risco | Ação |
|---|---|---|---|---|
| 1 | **mpv** | Chamado por `subprocess` + socket IPC; **não é embutido nem linkado**. Licença GPL-2.0+ (ou LGPL-2.1+ conforme a compilação). | Baixo | Só listar em `THIRD_PARTY_NOTICES.md` (feito). Se um dia empacotar (AppImage/Flatpak), inclua o texto da licença e a oferta do código-fonte. |
| 2 | **yt-dlp** | Chamado como comando. Licença Unlicense (domínio público). O risco não é a licença, é o **uso**: baixar do YouTube contraria os termos do YouTube, e a ferramenta já foi alvo de notificação DMCA em 2020 (repositório restaurado depois). | Médio (legal/uso) | Aviso de uso responsável no README (feito). Considere manter o download como recurso "para conteúdo que você pode baixar" e **nunca** promovê-lo como forma de piratear. |
| 3 | **ffmpeg** | Só chamado pelo yt-dlp; não incluído. | Baixo | Listado nos avisos (feito). |
| 4 | **GTK3 / PyGObject / Python / libnotify** | Bibliotecas do sistema (LGPL / PSF). Nada embutido. | Baixo | Nenhuma. |
| 5 | **Deezer API** | [Termos](https://developers.deezer.com/termsofuse): (a) acesso gratuito para "webpages e aplicações **pessoais**"; (b) §IV: uso **estritamente não comercial**, sem receita ou benefício "direta ou indiretamente" ligado ao serviço nem ao conteúdo, e sem associação a marca; (c) §III.6: proíbe usar o serviço em qualquer app que **promova uso, download ou compartilhamento ilegal/não autorizado de música**; (d) §VII: capas, imagens e textos pertencem ao Deezer ou a terceiros; (e) podem revogar o acesso a qualquer momento. | **Alto para monetizar / Médio para publicar grátis** | Deezer é usado em busca (nomes limpos), Descobrir, Wiki (discografia) e aba Letra. Para publicar grátis: mantido, com aviso "uso não comercial" (feito). Para monetizar: substituir (plano abaixo) ou pedir autorização por escrito ao Deezer. O ponto (c) é o motivo de o README **não** promover downloads. |
| 6 | **Doações / serviços pagos** | Consequência direta do item 5. Doação no README também é "benefício" ligado ao projeto. | Médio a alto | Seção "Apoie o projeto" deixada **comentada** no README até o item 5 ser resolvido. |
| 7 | **iTunes Search API** | A documentação da Apple permite conteúdo promocional (prévias, capas) **apenas para promover conteúdo da loja**, não para entretenimento, perto de um selo da loja, com limite de ~20 chamadas/min. A aba Letra usava a API só para álbum/ano/capa. | Alto | **Removida do código** (feito). Álbum/ano/capa agora vêm do Deezer (`_fetch_track_metadata`). |
| 8 | **Wikipédia** | Texto sob **CC BY-SA 4.0**: exige atribuição (autor/fonte, licença e link). A política da Wikimedia exige User-Agent identificável com contato. Antes o app só escrevia "Fonte: Wikipédia". | Médio | Agora cada bio mostra fonte, licença e link, e o User-Agent é `Fonoteca/1.0.0 (+URL)` (feito). `APP_URL` aponta para github.com/barbosabrasileiro/fonoteca. |
| 9 | **Google Tradutor** | Usava o endpoint não documentado `translate.googleapis.com/translate_a/single?client=gtx`. Não é a API oficial (a oficial é a Cloud Translation, paga e com chave). | Alto | **Desligado por padrão** (`TRANSLATE_ENABLED = False`). Sem tradução, se só existir bio em inglês, ela aparece em inglês com a atribuição. Alternativas: LibreTranslate auto-hospedado ou a API oficial com chave do próprio usuário. |
| 10 | **LRCLIB** | API aberta e sem chave, mas as **letras pertencem aos titulares**; o LRCLIB não concede licença sobre elas. | Baixo a médio | O app só exibe a letra ao usuário, sem armazenar nem redistribuir (mantido). Não inclua letras no repositório. |
| 11 | **Ícone `youtube.png`** | O app carregava `youtube.png`: provavelmente o **logo do YouTube**, marca registrada e arte protegida. | Alto | Substituído por ícone original (`fonoteca.svg/png`, criado do zero). **Não suba `youtube.png` ao GitHub**; se já foi commitado, apague do histórico. |
| 12 | **Ícones da interface** | Nomes freedesktop (`audio-volume-high-symbolic` etc.) resolvidos pelo tema do usuário; não distribuídos. Emojis (🎵) são renderizados pela fonte do sistema. | Baixo | Nenhuma. |
| 13 | **Fontes** | Nenhuma fonte embutida. | Nenhum | Nenhuma. |
| 14 | **Código próprio** | Não havia licença nem cabeçalho. Sem licença, o código é "todos os direitos reservados" mesmo no GitHub. | Alto (lacuna) | `LICENSE` (GPL-3.0-or-later) e cabeçalho SPDX/Copyright no `fonoteca.py` (feito). Ver "Escolha da licença". |
| 15 | **Nome "Fonoteca"** | "Fonoteca" é uma palavra comum do português (acervo de gravações sonoras), então é **fraca como marca** (difícil registrar exclusividade) e provavelmente usada por acervos e instituições. Uma busca rápida não achou um software com esse nome, mas isso **não é uma checagem de anterioridade**. O nome antigo "YT Music Player" era o mais problemático, por lembrar "YouTube Music". | Baixo a médio | Faça: busca no INPI (classes 9, 41 e 42), Google, GitHub, PyPI e Flathub. Se pretende vender no futuro, considere um nome mais distintivo (ex.: "Fonoteca Aurora"), mas para um projeto livre e gratuito o nome atual é aceitável. |
| 16 | **Pastas/identificadores** | Diretório `~/.config/yt_music_player`, socket, formato de backup e User-Agent usavam o nome antigo. | Baixo | Todos renomeados; dados antigos migram sozinhos e backups antigos ainda importam (feito). |

## Escolha da licença do código

Recomendo **GPL-3.0-or-later**, já aplicada:

- Compatível com tudo que o app usa (mpv, GTK, Python), que são apenas chamados, não incorporados.
- **Impede que alguém faça uma versão fechada** da Fonoteca e a venda sem devolver o código, o que combina com "open source + serviços extras pagos".
- Permite que **você**, sendo o único autor, ofereça também uma licença comercial diferente no futuro. Por isso o README pede `git commit -s` (DCO): se aceitar contribuições externas sem essa garantia, você perde a liberdade de relicenciar.
- Você e qualquer pessoa **podem vender** software GPL; o que não podem é fechar o código.

Se preferir máxima adoção e não se importa com forks fechados, troque por **MIT**: substitua o `LICENSE`, o cabeçalho SPDX (`MIT`) e `APP_LICENSE` no código.

## Plano para poder monetizar com segurança

1. **Trocar o Deezer** por fontes abertas: MusicBrainz (dados centrais CC0; limite de 1 requisição por segundo e User-Agent obrigatório) para artistas/álbuns/faixas, Cover Art Archive para capas, Wikipedia/Wikidata para biografias e ListenBrainz para "artistas similares". Atenção: parte dos dados suplementares do MusicBrainz é CC BY-NC-SA, então use só os dados centrais CC0 se for cobrar por algo.
2. Manter os **serviços pagos separados** do app gratuito (por exemplo sincronização na nuvem, backup ou temas premium), sem depender de nenhum serviço de terceiros restrito.
3. Decidir o destino do **download via YouTube**: manter só no app gratuito, com aviso, ou remover.
4. Só então habilitar a seção de doações no README.

## Checklist antes do `git push`

- [x] Trocar `SEU-USUARIO` em `APP_URL` (`fonoteca.py`), em `REPO_URL` (instalador) e no README (feito: github.com/barbosabrasileiro/fonoteca)
- [ ] Confirmar que `youtube.png` **não** está na pasta nem no histórico do Git
- [ ] Buscar o nome no INPI / GitHub / PyPI / Flathub
- [ ] Manter `LICENSE`, `README.md`, `THIRD_PARTY_NOTICES.md` e `docs/` no repositório
- [ ] Abrir o app, ir em **Sobre** e conferir a seção "Licença e créditos"
- [ ] Rodar o app uma vez e verificar que `~/.config/fonoteca/` foi criada com seus dados migrados

## Fontes consultadas

- Termos do Deezer para desenvolvedores: https://developers.deezer.com/termsofuse
- Documentação da iTunes Search API (Apple): https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/
