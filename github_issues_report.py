#!/usr/bin/env python3
"""
POC: lê as issues de um repositório GitHub e gera um relatório (Markdown e/ou HTML).

Uso:
    export GITHUB_TOKEN=ghp_...        # opcional p/ repos públicos (limite 60 req/h sem token)
    python github_issues_report.py owner/repo
    python github_issues_report.py owner/repo --state all --since 2026-01-01 --format both
    python github_issues_report.py owner/repo --save-raw issues.json     # guarda os dados brutos
    python github_issues_report.py owner/repo --from-json issues.json    # gera offline a partir do cache
    python github_issues_report.py owner/repo --config report.config.json -o site/index   # HTML customizado

Dependência: requests  (pip install requests)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #
def fetch_issues(repo: str, state: str, since: str | None, token: str | None, max_issues: int) -> list[dict]:
    """Busca issues paginando a API REST. Pull requests são descartados."""
    s = requests.Session()
    s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"

    params = {"state": state, "per_page": 100, "sort": "created", "direction": "desc"}
    if since:
        params["since"] = since  # atenção: a API filtra por updated_at, não created_at

    url, issues = f"{API}/repos/{repo}/issues", []
    while url and len(issues) < max_issues:
        r = s.get(url, params=params, timeout=30)
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            reset = datetime.fromtimestamp(int(r.headers.get("X-RateLimit-Reset", 0)))
            sys.exit(f"Rate limit atingido. Libera em {reset:%H:%M}. Use GITHUB_TOKEN para limite maior.")
        if r.status_code == 404:
            sys.exit(f"Repositório '{repo}' não encontrado (ou privado sem token).")
        r.raise_for_status()
        issues.extend(i for i in r.json() if "pull_request" not in i)
        url, params = r.links.get("next", {}).get("url"), None  # a URL 'next' já traz os params
    return issues[:max_issues]


# --------------------------------------------------------------------------- #
# Métricas
# --------------------------------------------------------------------------- #
def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def compute_metrics(issues: list[dict], stale_days: int) -> dict:
    now = datetime.now(timezone.utc)
    open_ = [i for i in issues if i["state"] == "open"]
    closed = [i for i in issues if i["state"] == "closed"]

    close_days = [(_dt(i["closed_at"]) - _dt(i["created_at"])).total_seconds() / 86400 for i in closed]
    open_age = [(now - _dt(i["created_at"])).total_seconds() / 86400 for i in open_]

    labels = Counter(l["name"] for i in issues for l in i["labels"])
    open_labels = Counter(l["name"] for i in open_ for l in i["labels"])
    assignees = Counter()
    for i in open_:
        names = [a["login"] for a in i.get("assignees") or []]
        assignees.update(names or ["(sem responsável)"])
    authors = Counter(i["user"]["login"] for i in issues)

    # série mensal (últimos 12 meses)
    months = []
    for k in range(11, -1, -1):
        y, mo = divmod(now.year * 12 + now.month - 1 - k, 12)
        months.append(f"{y:04d}-{mo + 1:02d}")
    created_m = Counter(_dt(i["created_at"]).strftime("%Y-%m") for i in issues)
    closed_m = Counter(_dt(i["closed_at"]).strftime("%Y-%m") for i in closed)
    monthly = [(m, created_m.get(m, 0), closed_m.get(m, 0)) for m in months]

    stale_cut = now - timedelta(days=stale_days)
    stale = sorted((i for i in open_ if _dt(i["updated_at"]) < stale_cut), key=lambda i: i["updated_at"])

    return {
        "generated_at": now,
        "total": len(issues),
        "open": len(open_),
        "closed": len(closed),
        "closed_completed": sum(1 for i in closed if i.get("state_reason") == "completed"),
        "closed_not_planned": sum(1 for i in closed if i.get("state_reason") == "not_planned"),
        "close_median_d": statistics.median(close_days) if close_days else None,
        "close_mean_d": statistics.mean(close_days) if close_days else None,
        "open_age_median_d": statistics.median(open_age) if open_age else None,
        "open_no_label": sum(1 for i in open_ if not i["labels"]),
        "open_no_assignee": sum(1 for i in open_ if not i.get("assignees")),
        "labels": labels.most_common(10),
        "open_labels": open_labels.most_common(10),
        "assignees": assignees.most_common(10),
        "authors": authors.most_common(10),
        "monthly": monthly,
        "oldest_open": sorted(open_, key=lambda i: i["created_at"])[:10],
        "most_commented": sorted(issues, key=lambda i: i["comments"], reverse=True)[:10],
        "stale": stale[:15],
        "stale_count": len(stale),
        "stale_days": stale_days,
    }


# --------------------------------------------------------------------------- #
# Renderização
# --------------------------------------------------------------------------- #
def _fmt_d(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f} dias"


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def _age(i: dict, now: datetime, field: str = "created_at") -> str:
    return f"{(now - _dt(i[field])).days} d"


def render_markdown(repo: str, m: dict, filters: str) -> str:
    now = m["generated_at"]
    esc = lambda t: t.replace("|", "\\|")
    L = [
        f"# Relatório de Issues — `{repo}`",
        f"_Gerado em {now:%d/%m/%Y %H:%M} UTC · {filters}_",
        "",
        "## Resumo",
        "| Métrica | Valor |", "|---|---|",
        f"| Total de issues | {m['total']} |",
        f"| Abertas | {m['open']} ({_pct(m['open'], m['total'])}) |",
        f"| Fechadas | {m['closed']} (concluídas: {m['closed_completed']}, não planejadas: {m['closed_not_planned']}) |",
        f"| Tempo até fechar (mediana / média) | {_fmt_d(m['close_median_d'])} / {_fmt_d(m['close_mean_d'])} |",
        f"| Idade mediana das abertas | {_fmt_d(m['open_age_median_d'])} |",
        f"| Abertas sem label | {m['open_no_label']} |",
        f"| Abertas sem responsável | {m['open_no_assignee']} |",
        f"| Abertas sem atividade há > {m['stale_days']} dias | {m['stale_count']} |",
        "",
        "## Evolução mensal (últimos 12 meses)",
        "| Mês | Criadas | Fechadas | Saldo |", "|---|---:|---:|---:|",
        *[f"| {mo} | {c} | {f} | {c - f:+d} |" for mo, c, f in m["monthly"]],
        "",
        "## Labels mais frequentes",
        "| Label | Total | Abertas |", "|---|---:|---:|",
        *[f"| {esc(n)} | {c} | {dict(m['open_labels']).get(n, 0)} |" for n, c in m["labels"]],
        "",
        "## Issues abertas por responsável",
        "| Responsável | Abertas |", "|---|---:|",
        *[f"| {n} | {c} |" for n, c in m["assignees"]],
        "",
        "## Issues abertas mais antigas",
        "| # | Título | Idade | Labels |", "|---|---|---:|---|",
        *[f"| [#{i['number']}]({i['html_url']}) | {esc(i['title'])} | {_age(i, now)} | "
          f"{', '.join(l['name'] for l in i['labels'])} |" for i in m["oldest_open"]],
        "",
        f"## Abertas sem atividade há mais de {m['stale_days']} dias",
        "| # | Título | Última atualização |", "|---|---|---:|",
        *[f"| [#{i['number']}]({i['html_url']}) | {esc(i['title'])} | há {_age(i, now, 'updated_at')} |"
          for i in m["stale"]],
        "",
        "## Mais comentadas",
        "| # | Título | Estado | Comentários |", "|---|---|---|---:|",
        *[f"| [#{i['number']}]({i['html_url']}) | {esc(i['title'])} | {i['state']} | {i['comments']} |"
          for i in m["most_commented"]],
        "",
    ]
    return "\n".join(L)


ALL_SECTIONS = ["kpis", "mensal", "labels", "responsaveis", "antigas", "paradas", "comentadas"]


def load_config(path: str | None) -> dict:
    """Lê o arquivo de customização (JSON). Todos os campos são opcionais."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("css"):  # CSS extra: caminho relativo ao arquivo de config
        css_path = os.path.join(os.path.dirname(os.path.abspath(path)), cfg["css"])
        with open(css_path, encoding="utf-8") as f:
            cfg["_css"] = f.read()
    unknown = set(cfg.get("sections", [])) - set(ALL_SECTIONS)
    if unknown:
        sys.exit(f"Seções desconhecidas no config: {sorted(unknown)}. Válidas: {ALL_SECTIONS}")
    return cfg


def render_html(repo: str, m: dict, filters: str, cfg: dict | None = None) -> str:
    now, e, cfg = m["generated_at"], html.escape, cfg or {}
    colors = cfg.get("colors", {})
    title = cfg.get("title", f"Issues — {repo}")
    sections = cfg.get("sections", ALL_SECTIONS)

    def bars(pairs):
        if not pairs:
            return "<p class='muted'>Sem dados.</p>"
        top = max(c for _, c in pairs) or 1
        return "".join(
            f"<div class='bar'><span class='lbl'>{e(n)}</span>"
            f"<span class='track'><span class='fill' style='width:{100 * c / top:.0f}%'></span></span>"
            f"<span class='val'>{c}</span></div>" for n, c in pairs)

    def issue_table(rows, cols):
        head = "".join(f"<th>{c}</th>" for c, _ in cols)
        body = "".join("<tr>" + "".join(f"<td>{f(i)}</td>" for _, f in cols) + "</tr>" for i in rows)
        return f"<table><tr>{head}</tr>{body}</table>" if rows else "<p class='muted'>Nenhuma.</p>"

    link = lambda i: f"<a href='{e(i['html_url'])}'>#{i['number']}</a>"
    peak = max([max(c, f) for _, c, f in m["monthly"]] + [1])
    monthly = "".join(
        f"<div class='col'><div class='pair'>"
        f"<span class='c' style='height:{100 * c / peak:.0f}%' title='Criadas: {c}'></span>"
        f"<span class='f' style='height:{100 * f / peak:.0f}%' title='Fechadas: {f}'></span>"
        f"</div><small>{mo[2:]}</small></div>" for mo, c, f in m["monthly"])

    kpis = [
        ("Total", m["total"]), ("Abertas", m["open"]), ("Fechadas", m["closed"]),
        ("Mediana p/ fechar", _fmt_d(m["close_median_d"])),
        ("Sem responsável", m["open_no_assignee"]),
        (f"Paradas > {m['stale_days']}d", m["stale_count"]),
    ]

    title_cell = lambda i: e(i["title"])
    blocks = {
        "kpis": f"<div class='kpis'>{''.join(f'<div class=kpi><b>{v}</b><span>{k}</span></div>' for k, v in kpis)}</div>",
        "mensal": "<h2>Evolução mensal <span class='legend muted'><span class='c'></span>criadas"
                  f"<span class='f'></span>fechadas</span></h2><div class='chart'>{monthly}</div>",
        "labels": f"<div><h2>Labels mais frequentes</h2>{bars(m['labels'])}</div>",
        "responsaveis": f"<div><h2>Abertas por responsável</h2>{bars(m['assignees'])}</div>",
        "antigas": "<h2>Abertas mais antigas</h2>" + issue_table(
            m["oldest_open"], [("#", link), ("Título", title_cell), ("Idade", lambda i: _age(i, now))]),
        "paradas": f"<h2>Sem atividade há mais de {m['stale_days']} dias ({m['stale_count']})</h2>" + issue_table(
            m["stale"], [("#", link), ("Título", title_cell),
                         ("Última atualização", lambda i: "há " + _age(i, now, "updated_at"))]),
        "comentadas": "<h2>Mais comentadas</h2>" + issue_table(
            m["most_commented"], [("#", link), ("Título", title_cell), ("Estado", lambda i: i["state"]),
                                  ("Comentários", lambda i: i["comments"])]),
    }
    # labels e responsáveis lado a lado quando aparecem em sequência
    body, k = [], 0
    while k < len(sections):
        s = sections[k]
        nxt = sections[k + 1] if k + 1 < len(sections) else None
        if {s, nxt} == {"labels", "responsaveis"}:
            body.append(f"<div class='grid2'>{blocks[s]}{blocks[nxt]}</div>")
            k += 2
        else:
            body.append(blocks[s])
            k += 1

    logo = f"<img class='logo' src='{e(cfg['logo'])}' alt=''>" if cfg.get("logo") else ""
    subtitle = f"<p>{e(cfg['subtitle'])}</p>" if cfg.get("subtitle") else ""
    footer = f"<footer class='muted'>{e(cfg['footer'])}</footer>" if cfg.get("footer") else ""
    accent = "".join(f"--{var}:{colors[key]};" for key, var in (("primary", "a"), ("secondary", "b")) if key in colors)

    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--card:#fff;--line:#e7e5e4;--a:#2563eb;--b:#16a34a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--card:#292524;--line:#44403c}}}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{margin:0}} h2{{margin-top:32px;font-size:1.1rem}} .muted{{color:var(--muted)}} a{{color:var(--a)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:20px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.kpi b{{display:block;font-size:1.5rem}} .kpi span{{color:var(--muted);font-size:.85rem}}
.bar{{display:grid;grid-template-columns:180px 1fr 40px;gap:8px;align-items:center;margin:4px 0;font-size:.9rem}}
.lbl{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .val{{text-align:right}}
.track{{background:var(--line);border-radius:4px;height:12px}} .fill{{display:block;height:100%;background:var(--a);border-radius:4px}}
.chart{{display:flex;gap:6px;height:160px;align-items:flex-end;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.col{{flex:1;display:flex;flex-direction:column;align-items:center;height:100%}}
.pair{{flex:1;display:flex;gap:2px;align-items:flex-end;width:100%}}
.pair span{{flex:1;border-radius:3px 3px 0 0}} .c{{background:var(--a)}} .f{{background:var(--b)}}
.legend span{{display:inline-block;width:10px;height:10px;margin:0 4px 0 12px;border-radius:2px}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}} td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}}
header{{display:flex;gap:16px;align-items:center}} .logo{{height:48px}}
footer{{margin-top:40px;font-size:.85rem;border-top:1px solid var(--line);padding-top:12px}}
:root{{{accent}}}
</style>
<style>{cfg.get('_css', '')}</style></head><body>
<header>{logo}<div><h1>{e(title)}</h1>{subtitle}</div></header>
<p class="muted">Gerado em {now:%d/%m/%Y %H:%M} UTC · {e(repo)} · {e(filters)}</p>
{chr(10).join(body)}
{footer}
</body></html>"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="Gera relatório das issues de um repositório GitHub.")
    p.add_argument("repo", help="owner/repo, ex.: psf/requests")
    p.add_argument("--state", default="all", choices=["open", "closed", "all"])
    p.add_argument("--since", help="ISO 8601 (YYYY-MM-DD); filtra por última atualização")
    p.add_argument("--max", type=int, default=1000, help="máximo de issues a buscar (padrão 1000)")
    p.add_argument("--stale-days", type=int, default=30, help="dias sem atividade p/ considerar parada")
    p.add_argument("--format", default="both", choices=["md", "html", "both"])
    p.add_argument("-o", "--output", default="relatorio_issues", help="nome base do arquivo de saída")
    p.add_argument("--save-raw", help="salva as issues brutas em JSON")
    p.add_argument("--from-json", help="lê issues de um JSON salvo em vez de chamar a API")
    p.add_argument("--config", help="JSON de customização do HTML (título, logo, cores, seções, CSS)")
    a = p.parse_args()
    cfg = load_config(a.config)

    if a.from_json:
        with open(a.from_json, encoding="utf-8") as f:
            issues = json.load(f)
    else:
        print(f"Buscando issues de {a.repo}...", file=sys.stderr)
        issues = fetch_issues(a.repo, a.state, a.since, os.getenv("GITHUB_TOKEN"), a.max)
    print(f"{len(issues)} issues carregadas.", file=sys.stderr)

    if a.save_raw:
        with open(a.save_raw, "w", encoding="utf-8") as f:
            json.dump(issues, f, ensure_ascii=False)

    filters = f"estado: {a.state}" + (f" · desde {a.since}" if a.since else "") + f" · máx. {a.max}"
    m = compute_metrics(issues, a.stale_days)
    base = os.path.splitext(a.output)[0]
    if os.path.dirname(base):
        os.makedirs(os.path.dirname(base), exist_ok=True)
    if a.format in ("md", "both"):
        with open(f"{base}.md", "w", encoding="utf-8") as f:
            f.write(render_markdown(a.repo, m, filters))
        print(f"-> {base}.md", file=sys.stderr)
    if a.format in ("html", "both"):
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(render_html(a.repo, m, filters, cfg))
        print(f"-> {base}.html", file=sys.stderr)


if __name__ == "__main__":
    main()
