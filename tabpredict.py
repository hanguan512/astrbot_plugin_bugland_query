"""Tab 玩家列表识别 + 起床战争队伍胜率估算。

流程：
1. 把截图丢给 VL 模型（默认 opencode-go/gemini-4-argon），让它按「队伍|ID」吐名单；
2. 逐个玩家查起床战绩，套幻梦评分公式算分；
3. 按分数换成功率，算出每队胜率。

胜率算法的思路：
- 评分是百分制的实力值，115 分左右是普通号（1 万场以下大多在 50~180）。
- 先把评分换成「战力」：power = exp((score - 115) / 70)。
  这样 227 分的战力约 4.9，115 分是 1.0，50 分是 0.4，差距不会像直接相减那么离谱。
- 队伍战力就是把队员战力加起来（人多的队自然会高一点，符合实际）。
- 四队对抗再放大差距：P = 战力^2.5 / Σ战力^2.5。指数取 2.5 是试出来的：
  3 会让强队飘到 80% 以上，2 又太保守，2.5 大概是 75%~80% 这个档。
- 查不到战绩的按 100 分算（当新人），并在返回里标出来。
"""

from __future__ import annotations

import base64
import json
import math
import re

import aiohttp

DEFAULT_MODEL = "gemini-4-argon"
DEFAULT_BASE = "https://opencode.ai/inference/go/v1"

OCR_PROMPT = (
    "这是 Minecraft 服务器（布吉岛）起床战争的 Tab 玩家列表截图。"
    "请把每一行识别出来，按队伍分组。"
    "要求：玩家ID逐字照抄，注意 0/O、1/l/I、_ 这种形近字符，不要纠正、不要翻译、不要改写。"
    "只输出 JSON，格式：{\"teams\":[{\"team\":\"红队\",\"players\":[\"ID1\",\"ID2\"]}]}，不要任何解释。"
)

_TEAM_ALIAS = {
    "红": "红队", "红队": "红队", "red": "红队",
    "蓝": "蓝队", "蓝队": "蓝队", "blue": "蓝队",
    "黄": "黄队", "黄队": "黄队", "yellow": "黄队",
    "绿": "绿队", "绿队": "绿队", "green": "绿队",
    "紫": "紫队", "紫队": "紫队", "purple": "紫队",
    "白": "白队", "白队": "白队", "white": "白队",
}


def norm_team(raw: str) -> str:
    key = str(raw or "").strip().replace("队|", "").replace("队", "")
    low = key.lower()
    if low in _TEAM_ALIAS:
        return _TEAM_ALIAS[low]
    if key in _TEAM_ALIAS:
        return _TEAM_ALIAS[key]
    for k, v in _TEAM_ALIAS.items():
        if k and k in key:
            return v
    return str(raw or "未知").strip() or "未知"


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def parse_roster(text: str) -> dict[str, list[str]]:
    """把模型输出解析成 {队伍: [ID...]}，JSON 和「队伍|ID」两种都吃。"""
    text = _strip_fence(text)
    teams: dict[str, list[str]] = {}
    try:
        data = json.loads(text)
        for item in (data.get("teams") or []) if isinstance(data, dict) else []:
            team = norm_team(item.get("team") or item.get("name") or "")
            names = [str(n).strip() for n in (item.get("players") or []) if str(n).strip()]
            if names:
                teams.setdefault(team, []).extend(names)
        if teams:
            return _dedup(teams)
    except Exception:
        pass

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.split(r"[|｜:：\t]", line, maxsplit=1)
        if len(m) == 2:
            team = norm_team(m[0])
            name = m[1].strip().strip("、,，")
            if name:
                teams.setdefault(team, []).append(name)
    return _dedup(teams)


def _dedup(teams: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for team, names in teams.items():
        seen, keep = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                keep.append(n)
        out[team] = keep
    return out


async def ocr_tab(
    image_bytes: bytes,
    api_base: str = DEFAULT_BASE,
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    timeout: int = 120,
    extra_headers: dict | None = None,
) -> dict[str, list[str]]:
    """调用 VL 模型识别 Tab 列表，返回 {队伍: [ID...]}。"""
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ],
        }],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.5.0",
    }
    headers.update(extra_headers or {})
    url = api_base.rstrip("/") + "/chat/completions"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            body = json.loads(await resp.text())
    content = body["choices"][0]["message"].get("content") or ""
    return parse_roster(content)


# ---------------------------------------------------------------- 胜率


def player_power(score: float | None) -> float:
    s = 100.0 if score is None else float(score)
    return math.exp((s - 115.0) / 70.0)


def team_probabilities(
    teams: dict[str, list[dict]], exponent: float = 2.5, floor: float = 0.03
) -> tuple[dict[str, float], dict[str, float]]:
    """teams: {队伍: [{'score': 数值或None}, ...]}，返回 (胜率, 队伍战力)。"""
    strength = {
        t: sum(player_power(p.get("score")) for p in players) if players else 0.0
        for t, players in teams.items()
    }
    live = {t: s for t, s in strength.items() if s > 0}
    if not live:
        return {}, strength
    powered = {t: s ** exponent for t, s in live.items()}
    total = sum(powered.values()) or 1.0
    probs = {t: v / total for t, v in powered.items()}
    # 兜个底，免得弱队被算成 0%
    probs = {t: max(p, floor) for t, p in probs.items()}
    s = sum(probs.values()) or 1.0
    return {t: p / s for t, p in probs.items()}, strength


def format_report(teams: dict[str, list[dict]], model: str, probs: dict[str, float], strength: dict[str, float]) -> str:
    lines = [f"Tab 列表识别（{model}）＋起床评分预测，共 {sum(len(v) for v in teams.values())} 人："]
    order = sorted(teams, key=lambda t: probs.get(t, 0), reverse=True)
    for t in order:
        players = teams[t]
        scored = [p["score"] for p in players if p.get("score") is not None]
        avg = sum(scored) / len(scored) if scored else 0
        top = max(scored) if scored else None
        top_name = ""
        if top is not None:
            top_name = next(p["name"] for p in players if p.get("score") == top)
        lines.append(
            f"{t}：{len(players)} 人，有数据 {len(scored)} 人，人均 {avg:.1f}，"
            f"最高 {top:.1f}({top_name})" if top is not None else f"{t}：{len(players)} 人（都没战绩）"
        )
        lines.append(f"  胜率 {probs.get(t, 0) * 100:.0f}%")
    lines.append("")
    for t in order:
        items = []
        for p in teams[t]:
            if p.get("score") is None:
                items.append(f"{p['name']} 无数据(按100算)")
            else:
                items.append(f"{p['name']} {p['score']:.1f}({p.get('level', '')})")
        lines.append(f"{t} 明细：" + "、".join(items))
    return "\n".join(lines)
