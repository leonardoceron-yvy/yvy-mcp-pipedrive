# mcp-pipedrive (YvY Capital)

MCP server para gerenciar o pipeline de originação YvY no Pipedrive direto do Claude Code, com **sistema de backup automático** para desfazer qualquer alteração.

## Tools (28 total)

### Deals (7)
- `list_deals` — listar deals (default: pipeline Renda Fixa id=6, status open)
- `get_deal` — detalhe completo de um deal
- `search_deals` — busca por título / campos
- `update_deal` — valor, currency, título, stage, pipeline, status, person_id, org_id, expected_close_date, custom_fields *(auto-backup)*
- `move_deal_to_stage` — conveniência: mover deal entre stages *(auto-backup)*
- `clear_deal_custom_field` — limpar valor de um custom field no deal *(auto-backup)*
- `add_note_to_deal` — anexar nota/descrição rica (HTML) *(auto-backup)*

### Atividades (4)
- `link_meeting_to_deal` — criar reunião vinculada *(auto-backup)*
- `create_follow_up` — task/call/email/meeting de follow-up *(auto-backup)*
- `list_activities_for_deal` — listar pendentes/concluídas
- `mark_activity_done` — concluir ou reabrir *(auto-backup)*

### Pessoas / Contatos (5)
- `search_persons` — buscar por nome/email/telefone
- `get_person` — detalhe completo
- `create_person` — criar com email/telefone/org *(auto-backup)*
- `update_person` — atualizar campos *(auto-backup)*
- `link_person_to_deal` — vincular contato a deal *(auto-backup)*

### Organizações / Empresas (4)
- `search_organizations` — buscar por nome
- `get_organization` — detalhe completo
- `create_organization` — criar com nome + endereço opcional *(auto-backup)*
- `link_organization_to_deal` — vincular empresa a deal *(auto-backup)*

### Combinada (1)
- `attach_contact_to_deal` — find-or-create person (por email) + org (por nome) + vincular tudo ao deal numa só chamada. Para `"vincula a empresa X com contato fulano@y.com no deal Z"` *(auto-backup cada subetapa)*

### Custom Fields — schema (3) + valor (1)
- `list_deal_fields` — lista todos os 88 fields da conta com `key` (usar em `update_deal(custom_fields={key: val})`)
- `add_deal_field` — criar novo custom field (varchar, text, monetary, date, enum, set, address, phone, etc.) *(auto-backup)*
- `remove_deal_field` — ⚠️ deletar custom field (valores nos deals **permanentemente perdidos**; backup só recria o schema) *(auto-backup do schema)*
- `clear_deal_custom_field` — limpar valor sem deletar o field (preferir esse pra "remover" no dia-a-dia)

### Backups (2)
- `list_backups` — lista snapshots recentes (mais novo primeiro), retorna nome do arquivo
- `restore_backup` — desfaz: undo de update → PATCH com estado anterior; undo de create → DELETE; undo de delete → recreate

### Pipeline metadata (2)
- `list_pipelines` — descobrir pipeline_id
- `list_stages` — etapas de um pipeline (default: Renda Fixa)

## Campos essenciais (protegidos contra remoção)

Chaves bloqueadas em `remove_deal_field`, `clear_deal_custom_field` e via `custom_fields` no `update_deal`:

```
value, currency, title, org_id, person_id, stage_id,
pipeline_id, status, id, add_time, update_time, creator_user_id
```

Tentativas (ex: `clear_deal_custom_field(deal_id, "title")` ou `update_deal(title="")`) retornam erro **antes** de chamar o Pipedrive. Pra mudar esses campos, use `update_deal` com o novo valor diretamente — só limpar não rola.

Customize a lista editando `PROTECTED_FIELD_KEYS` no topo do `server.py`.

## Backup automático

Toda operação que muda dados grava um snapshot em `backups/YYYYMMDD-HHMMSS_<operação>_<entidade>_<id>.json` contendo:

```jsonc
{
  "timestamp": "2026-05-13T20:45:12Z",
  "operation": "update_deal",
  "entity_type": "deal",
  "entity_id": 222,
  "before": { /* estado completo antes */ },
  "params": { /* o que foi enviado no PATCH */ },
  "after": { /* estado depois (Pipedrive response) */ }
}
```

**Para desfazer:** `restore_backup(backup_file="20260513-204512_update_deal_deal_222.json")`. A restauração:
- Update → reverte ao `before`
- Create → deleta a entidade criada
- Delete → recria do `before` (⚠️ valores per-deal de custom fields removidos **não são recuperáveis** — só a definição do field é recriada)

A própria restauração grava um novo backup, então dá pra "re-do" também.

## Setup (primeira vez)

```bash
cd ~/yvy/mcp-pipedrive
cp .env.example .env
# edita .env com PIPEDRIVE_API_TOKEN e PIPEDRIVE_DOMAIN

uv venv --python 3.12 && uv pip install -e .
```

## Registrar no Claude Code

```bash
claude mcp add --scope user pipedrive-yvy \
  /Users/leoceron/yvy/mcp-pipedrive/.venv/bin/python \
  /Users/leoceron/yvy/mcp-pipedrive/server.py
```

Validar: `claude mcp get pipedrive-yvy` → `Status: ✓ Connected`.

⚠️ MCPs só carregam no startup do `claude` — reinicie a sessão depois de adicionar/atualizar o servidor pra ver as tools novas.

## Defaults

- **Pipeline default = Renda Fixa (id=6).** Tools que aceitam `pipeline_id` (list_deals, list_stages) usam esse default. Pra outros pipelines, passar `pipeline_id=N` explicitamente.
- Auth via `?api_token=` query param.
- Notas (`add_note_to_deal`) e custom fields (`list_deal_fields`, `add_deal_field`, `remove_deal_field`) usam API v1 — Pipedrive não migrou ainda. Resto v2.

## Pipelines da conta YvY (snapshot 2026-05-13)

| ID | Nome |
|---|---|
| 1 | Venture Studio |
| 4 | Equity |
| 5 | Carbon Platform |
| **6** | **Renda Fixa ← default** |
| 12 | Investor Relations |
| 13 | PDI |
| 15 | Originação Geral |
| 16 | Relacionamento |
| 17 | Fundraising |
| 18 | Venture Studio - Pipeline |

## Deploy HTTP (para Cowork / claude.ai web)

Cowork e claude.ai web não acessam MCPs locais (stdio). Pra usar de lá, sobe o servidor em HTTP num host (Railway recomendado).

### Arquivos já prontos no repo
- `server.py` — suporta `MCP_TRANSPORT=stdio` (default) ou `MCP_TRANSPORT=http`
- `railway.toml` — config do deploy (healthcheck em `/health`)
- `nixpacks.toml` — força Python 3.12 + instala via pip
- Bearer-token auth obrigatório em modo HTTP

### Passos (GitHub → Railway, ~20min)

1. **Inicializa git e cria repo privado no GitHub:**
   ```bash
   cd ~/yvy/mcp-pipedrive
   git init && git add . && git commit -m "initial MCP pipedrive yvy"
   gh repo create yvy-mcp-pipedrive --private --source=. --push
   ```
   (Confere que `.env` está no `.gitignore` — não pode subir token pro GitHub.)

2. **Cria conta na Railway** (railway.app), conecta com GitHub.

3. **New Project → Deploy from GitHub repo → seleciona `yvy-mcp-pipedrive`.**

4. **Settings → Variables, adiciona:**
   ```
   PIPEDRIVE_API_TOKEN = a6bddacf02621acb8bb9ab85575231b93df05d55
   PIPEDRIVE_DOMAIN = yvy
   MCP_TRANSPORT = http
   MCP_AUTH_TOKEN = WlnCIMO1-O96Dp1lIK5ajI1cXPdoAnZZJVDzlivCxaQ
   ```
   (Railway injeta `PORT` automaticamente — não precisa setar.)

5. **Settings → Networking → Generate Domain.** Pega a URL pública (ex: `mcp-pipedrive-yvy.up.railway.app`).

6. **Testa o health check no browser:** `https://<url>/health` → "ok".

7. **Registra no Claude Code local (opcional, pra usar HTTP em vez de stdio):**
   ```bash
   claude mcp add --transport http --scope user pipedrive-yvy-http \
     https://<url>/mcp \
     --header "Authorization: Bearer WlnCIMO1-O96Dp1lIK5ajI1cXPdoAnZZJVDzlivCxaQ"
   ```

8. **No Cowork:** entra na config de MCP/Connectors → "Add Custom" → cola URL `https://<url>/mcp` + header `Authorization: Bearer <token>`.

### Validação rápida

```bash
# Sem auth → 401
curl https://<url>/mcp -X POST -H "Content-Type: application/json"

# Com auth → 200 + JSON-RPC
curl https://<url>/mcp -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

### Segurança

- Token `MCP_AUTH_TOKEN` é o único guard entre a internet e suas 28 tools. **Não vaze.** Se vazar, gera novo e atualiza tanto na Railway quanto no header de cada cliente registrado.
- O `.env` local está no `.gitignore` — confere antes de cada push.
- Railway tem env vars criptografadas at-rest; só você (owner) vê.

## Stages do Renda Fixa

Leads (68) → Prospecção (43) → Engajamento (44) → Pré Análise (77) → Análise Estruturação (45) → NBO (46) → DD (69)
