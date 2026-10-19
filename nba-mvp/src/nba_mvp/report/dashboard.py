from __future__ import annotations

import html


FICTIONAL_EXAMPLE = "虚构示例：模型 22%，YES bid 16%、ask 18%、mid 17% → +5.0 pp。此行不是真实预测。"
DISCLAIMER = "这是对未来媒体投票结果的估计，不是最佳球员评分，不保证准确，也不构成投注建议。"


def chart_segments(values: list[float | None]) -> list[list[tuple[int, float]]]:
    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((index, float(value)))
    if current:
        segments.append(current)
    return segments


def _fmt_prob(value, status: str) -> str:
    if value is None:
        if status == "pending_training":
            return "待训练"
        if status == "pending_collection":
            return "待采集"
        if status == "ineligible":
            return "无资格"
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def _fmt_market(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def _fmt_pp(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.1f} pp"


def _svg(values: list[float | None], color: str) -> str:
    segments = chart_segments(values)
    if not segments:
        return "<p>暂无曲线</p>"
    width, height, pad = 320, 90, 8
    known = [value for value in values if value is not None]
    low = min(known)
    high = max(known)
    if high == low:
        high = low + 1
    span = max(len(values) - 1, 1)

    def point(index: int, value: float) -> str:
        x = pad + (width - 2 * pad) * (index / span)
        y = pad + (height - 2 * pad) * (1 - (value - low) / (high - low))
        return f"{x:.1f},{y:.1f}"

    parts = []
    for segment in segments:
        if len(segment) == 1:
            x, y = point(*segment[0]).split(",")
            parts.append(f'<circle cx="{x}" cy="{y}" r="2.5" fill="{color}" />')
        else:
            points = " ".join(point(index, value) for index, value in segment)
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" />')
    return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">{"".join(parts)}</svg>'


def _table(rows: list[dict]) -> str:
    if not rows:
        return "<p>待采集 / 待训练。当前没有可展示的球员概率。</p>"
    body = []
    for row in rows:
        status = row.get("row_status") or ""
        cells = [
            row.get("player_name") or "",
            row.get("team_abbr") or "",
            _fmt_prob(row.get("p_model"), status if row.get("p_model") is None else "ok"),
            _fmt_market(row.get("p_market_mid")),
            _fmt_market(row.get("bid")),
            _fmt_market(row.get("ask")),
            _fmt_pp(row.get("delta_pp")),
            _fmt_pp(row.get("delta_vs_prev_hour_pp")),
            _fmt_prob(row.get("p_eligibility"), "ok" if row.get("p_eligibility") is not None else status),
            row.get("market_as_of") or "N/A",
            row.get("model_as_of") or "N/A",
            status or "N/A",
        ]
        body.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    header = (
        "<tr><th>球员</th><th>球队</th><th>模型概率</th><th>YES mid</th><th>bid</th><th>ask</th>"
        "<th>差值</th><th>较上小时</th><th>资格概率</th><th>报价时间</th><th>模型截至</th><th>状态</th></tr>"
    )
    return "<table>" + header + "<tbody>" + "".join(body) + "</tbody></table>"


def _sort_abs(rows: list[dict]) -> list[dict]:
    def key(row):
        delta = row.get("delta_pp")
        if delta is None:
            return (1, 0, row.get("player_name") or "")
        return (0, -abs(delta), row.get("player_name") or "")

    return sorted(rows, key=key)


def _sort_signed(rows: list[dict]) -> list[dict]:
    def key(row):
        delta = row.get("delta_pp")
        if delta is None:
            return (1, 0, row.get("player_name") or "")
        return (0, -delta, row.get("player_name") or "")

    return sorted(rows, key=key)


def _charts(rows: list[dict], history: list[dict]) -> str:
    if not history:
        return "<p>暂无历史曲线。空值不会连成 0。</p>"
    hours = sorted({item["as_of_hour"] for item in history})
    by_player: dict[str, dict[str, dict]] = {}
    for item in history:
        by_player.setdefault(item["player_id"], {})[item["as_of_hour"]] = item
    blocks = []
    for row in rows:
        series = by_player.get(row["player_id"], {})
        model = [None if series.get(hour) is None else series[hour].get("p_model") for hour in hours]
        market = [None if series.get(hour) is None else series[hour].get("p_market_mid") for hour in hours]
        delta = [None if series.get(hour) is None else series[hour].get("delta_pp") for hour in hours]
        blocks.append(
            "<section class='chart'>"
            f"<h3>{html.escape(str(row.get('player_name') or row['player_id']))}</h3>"
            "<p>模型</p>" + _svg(model, "#1d4e89")
            + "<p>市场中间价</p>" + _svg(market, "#b45309")
            + "<p>差值（百分点）</p>" + _svg(delta, "#3f6212")
            + "</section>"
        )
    return "".join(blocks) or "<p>暂无历史曲线。空值不会连成 0。</p>"


def render_dashboard(*, rows: list[dict], queue: list[dict], job: dict | None, history: list[dict], coverage, coverage_status: str, data_status: str, model_version: str, model_as_of: str | None, market_as_of: str | None, comparison_as_of: str | None, last_success: str | None) -> str:
    unmapped = [item for item in queue if item.get("status") == "unmapped"]
    rejected = [item for item in queue if item.get("status") == "rejected"]
    valid = [row for row in rows if row.get("row_status") == "ok"]
    job_status = (job or {}).get("status") or "从未成功"
    success_text = last_success or "从未成功"
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>2026-27 NBA 常规赛 MVP 概率对照</title>",
        "<style>body{font-family:sans-serif;margin:24px;line-height:1.45;color:#1c1917}"
        "table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #d6d3d1;padding:6px;text-align:left}"
        "aside{background:#fff7ed;border:1px solid #fdba74;padding:12px;margin:16px 0}"
        ".warn{background:#fef2f2;border:1px solid #fecaca;padding:12px}"
        ".chart{display:inline-block;margin:8px 16px 8px 0;vertical-align:top}</style></head><body>",
        "<h1>2026—27 NBA 常规赛 MVP</h1>",
        "<p class='warn'>" + html.escape(DISCLAIMER) + "</p>",
        "<section><h2>状态</h2><ul>",
        f"<li>模型版本：{html.escape(str(model_version))}</li>",
        f"<li>model_as_of：{html.escape(model_as_of or 'N/A')}</li>",
        f"<li>market_as_of：{html.escape(market_as_of or 'N/A')}</li>",
        f"<li>comparison_as_of：{html.escape(comparison_as_of or 'N/A')}</li>",
        f"<li>上次成功：{html.escape(success_text)}</li>",
        f"<li>任务状态：{html.escape(str(job_status))}</li>",
        f"<li>数据状态：{html.escape(data_status)}</li>",
        f"<li>覆盖率：{html.escape('N/A' if coverage is None else f'{coverage * 100:.1f}%')}（{html.escape(coverage_status)}）</li>",
        "</ul></section>",
        "<aside data-fictional='true'><strong>虚构示例</strong><p>" + html.escape(FICTIONAL_EXAMPLE) + "</p></aside>",
        "<h2>按绝对差</h2>",
        _table(_sort_abs(rows)),
        "<h2>按差值</h2>",
        _table(_sort_signed(rows)),
        "<h2>仅看有效报价</h2>",
        _table(valid),
        "<h2>未映射</h2>",
        "<ul>" + "".join(f"<li>{html.escape(str(item.get('question') or item.get('market_id')))}（unmapped）</li>" for item in unmapped) + "</ul>",
        "<h2>已拒绝</h2>",
        "<ul>" + "".join(
            f"<li>{html.escape(str(item.get('question') or item.get('market_id')))}（{html.escape(str(item.get('reason') or 'rejected'))}）</li>"
            for item in rejected
        ) + "</ul>",
        "<h2>曲线</h2>",
        _charts(rows, history),
        "<section><h2>情景分析</h2><p>未启用。假设情景与真实预测分开；蒙特卡洛只有在回测证明优于基线后才会打开。</p></section>",
        "</body></html>",
    ]
    return "".join(parts)
