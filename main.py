"""布吉岛查询插件

通过 BuGLand API (https://api.mcbjd.net/v2) 查询布吉岛服务器数据。
所有功能以 LLM 工具形式提供，由模型根据用户语义自行选择调用。
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
from collections import deque
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At, Image

try:
    from .render import (
        normalize_blocks,
        parse_stat_lines,
        render_blocks,
        stats_blocks,
    )
except ImportError:  # 单独调试时按顶层模块导入
    from render import (  # type: ignore[no-redef]
        normalize_blocks,
        parse_stat_lines,
        render_blocks,
        stats_blocks,
    )

PLUGIN_NAME = "astrbot_plugin_bugland_query"

# 字段名 -> 图表里显示的中文
KEY_NAMES = {
    "game": "场次",
    "total_game": "总场次",
    "win": "胜场",
    "total_win": "总胜场",
    "lose": "败场",
    "kills": "击杀",
    "deaths": "死亡",
    "final_kills": "终结击杀",
    "final_deaths": "被终结",
    "bed_lose": "床被破坏",
    "ttl": "时长(秒)",
    "rating": "评分",
    "score": "分数",
    "level": "等级",
    "exp": "经验",
}

GAMES_HINT = (
    "bedwars(起床战争), skywars(空岛战争), vdefense(空岛防御), arenapvp(竞技场), "
    "anqu(安全区), kitbattle(职业战争), anni( anni 模式), achievement(成就), "
    "survivalgame(生存游戏), parkour(跑酷)"
)

# 对局记录里的模式代码 -> 中文名。可以在插件配置 behavior.mode_names 里覆盖/追加。
DEFAULT_MODE_NAMES = {
    "BWXP32": "经验起床四队八人（无限火力48）",
    "BWXP": "经验起床",
    "BW": "起床战争",
    "SW": "空岛战争",
    "SWXP": "经验空岛",
    "VD": "空岛防御",
    "AP": "竞技场",
    "KB": "职业战争",
    "ANNI": "anni",
    "SG": "生存游戏",
    "PK": "跑酷",
}

# 战绩接口里 gametype 下的细分模式键 -> 中文小类名
SUBMODE_NAMES = {
    "bwxp32": "经验4队8人",
    "bw999": "无限火力",
    "bw16": "4队4人",
    "bw8": "双人",
    "bwxp8x4": "经验8队4人",
    "bwxp64": "经验64人",
    "bwxp": "经验起床",
    "bw": "常规",
}

# 游戏大类中文名
GAMETYPE_NAMES = {
    "bedwars": "起床战争",
    "skywars": "空岛战争",
    "vdefense": "空岛防御",
    "arenapvp": "竞技场",
    "anqu": "安全区",
    "kitbattle": "职业战争",
    "anni": "anni",
    "achievement": "成就",
    "survivalgame": "生存游戏",
    "parkour": "跑酷",
}

# 道具 / 升级项中文名
ITEM_NAMES = {
    "FIRE_CHARGE": "火焰弹",
    "GOLDEN_APPLE": "金苹果",
    "EGG": "鸡蛋",
    "SNOWBALL": "雪球",
    "ENDER_PEARL": "末影珍珠",
    "TNT": "TNT",
    "IRON_GOLEM": "铁傀儡",
    "BOW": "弓箭",
}
UPGRADE_NAMES = {
    "PROTECTION": "保护",
    "SHARPNESS": "锋利",
    "ALARM_TRAP": "警戒陷阱",
    "HEAL": "治疗",
    "DEFENSE": "防御",
    "HASTE": "急迫",
    "MANIAC_MINER": "疯狂矿工",
    "IRON_ARMOR": "铁护甲",
}

# 一个细分模式至少要有这些字段之一才算有效数据段
STAT_LEAVES = (
    "game",
    "win",
    "lose",
    "kills",
    "deaths",
    "final_kills",
    "final_deaths",
    "bed_lose",
)


# ------------------------------------------------------------------ 幻梦评分
# 幻梦（第三方起床数据卡）没公开算法。这套是拿 5 张真实卡（344.7 ACE / 217.4 S- /
# 212.8 S- / 184.5 A+ / 178.9 A）用最小二乘拟合出来的：
#     评分 ≈ 49.7 + 1.99*sqrt(场次) + 70.6*ln(K/D + 0.1)
# 样本内最大误差 1.1，留一交叉验证最大误差 1.4，只有三个参数。
# 值得强调的是胜率在数据里权重是 0：幻梦基本只看场次和 K/D（场次开方 ≈ 每翻四倍多 2 分）。
# 早期版本用「场次饱和 + 胜率² + 场均终结 − 二次惩罚」那套，在同一张 344.7 卡上只有 241.3，
# 而且 K/D 过 2 会倒扣、deaths=0 会算出百万级负数，已废弃。
_DREAM_BASE = 49.66
_DREAM_SQRT_W = 1.991
_DREAM_KD_W = 70.62
_DREAM_KD_EPS = 0.1
_DREAM_KD_CAP = 20.0
_DREAM_MIN_GAME = 10

# (分数下限, 等级)，从高到低匹配。
# 130 分以上是官方公示的档位，以下按档宽推算。
_DREAM_LEVELS = (
    (350.0, "ACE"),
    (310.0, "ACE"),
    (275.0, "S+"),
    (230.0, "S"),
    (205.0, "S-"),
    (180.0, "A+"),
    (145.0, "A"),
    (130.0, "A-"),
    (105.0, "B+"),
    (70.0, "B"),
    (60.0, "C+"),
    (40.0, "C"),
    (0.0, "D"),
    (-1e9, "F"),
)


def _dream_level(score: float) -> str:
    for low, name in _DREAM_LEVELS:
        if score >= low:
            return name
    return "F"


def _dream_rating(stats: dict) -> float | None:
    """按幻梦口径估算评分，场次不足或数据异常时返回 None。"""
    try:
        game = float(stats.get("game") or 0)
        kills = float(stats.get("kills") or 0)
        deaths = float(stats.get("deaths") or 0)
    except (TypeError, ValueError):
        return None
    if game < _DREAM_MIN_GAME:
        return None
    kd = kills / deaths if deaths > 0 else kills
    kd = min(max(kd, 0.0), _DREAM_KD_CAP)
    return (
        _DREAM_BASE
        + _DREAM_SQRT_W * math.sqrt(game)
        + _DREAM_KD_W * math.log(kd + _DREAM_KD_EPS)
    )


def _dream_tag(stats: dict) -> str:
    """返回形如 178.9(A) 的标签；样本不足返回空串。"""
    score = _dream_rating(stats)
    if score is None:
        return ""
    return f"{score:.1f}({_dream_level(score)})"


def _dream_aggregate(split: list[tuple[str, dict]]) -> str:
    """整体评分：把各细分模式的场次和击杀死亡汇总后再套同一个公式。"""
    total: dict[str, float] = {}
    for _, stats in split:
        for key in ("game", "kills", "deaths"):
            try:
                total[key] = total.get(key, 0.0) + float(stats.get(key) or 0)
            except (TypeError, ValueError):
                continue
    tag = _dream_tag(total)
    return tag


def _submode_label(gametype: str, key: str) -> str:
    """大类-小类，例如 起床战争-经验4队8人。"""
    cn = GAMETYPE_NAMES.get(gametype, gametype)
    sub = SUBMODE_NAMES.get(str(key).lower())
    return f"{cn}-{sub}" if sub else f"{cn}-{key}"


def _split_submodes(data: Any, gametype: str) -> list[tuple[str, dict]]:
    """把 gametype 下面的细分模式拆开，返回 [(标签, 该模式统计), ...]。"""
    node = data.get(gametype) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return []
    out: list[tuple[str, dict]] = []
    for key, val in node.items():
        if not isinstance(val, dict):
            continue
        if not any(k in val for k in STAT_LEAVES):
            continue
        out.append((_submode_label(gametype, key), val))
    out.sort(key=lambda x: -int(x[1].get("game") or 0))
    return out


def _sorted_items(d: dict, names: dict) -> list[dict]:
    rows = sorted(d.items(), key=lambda x: -float(x[1] or 0))
    return [{"label": names.get(str(k).upper(), str(k)), "value": int(v)} for k, v in rows]


def _submode_blocks(label: str, stats: dict) -> list[dict]:
    """单个细分模式的卡片块：结论数据 + 道具使用 + 升级。"""
    blocks: list[dict] = [{"type": "section", "text": label}]
    game = int(stats.get("game") or 0)
    win = int(stats.get("win") or 0)
    kills = int(stats.get("kills") or 0)
    deaths = int(stats.get("deaths") or 0)

    if game:
        blocks.append({"type": "hero", "label": "总场次", "value": str(game), "unit": "场"})
    kpi: list[dict] = []
    if win or game:
        kpi.append({"label": "胜场", "value": str(win)})
    if game:
        kpi.append({"label": "胜率", "value": f"{win / game * 100:.1f}%"})
    if deaths:
        kpi.append({"label": "K/D", "value": f"{kills / deaths:.2f}"})
    if kpi:
        blocks.append({"type": "kpi", "items": kpi[:3]})
    if game:
        blocks.append(
            {"type": "bars", "items": [{"label": "胜率", "value": round(win / game * 100, 1)}]}
        )

    rows: list[dict] = []
    tag = _dream_tag(stats)
    if tag:
        rows.append({"label": "幻梦评分", "value": tag})
    elif game and int(game) < _DREAM_MIN_GAME:
        rows.append({"label": "幻梦评分", "value": "样本不足，不评"})
    for key, lab in (
        ("kills", "击杀"),
        ("deaths", "死亡"),
        ("final_kills", "终结击杀"),
        ("final_deaths", "被终结"),
        ("bed_lose", "床被破坏"),
        ("lose", "败场"),
    ):
        if key in stats:
            rows.append({"label": lab, "value": str(int(stats.get(key) or 0))})
    if rows:
        blocks.append({"type": "rows", "items": rows})

    use = stats.get("use_item")
    if isinstance(use, dict) and use:
        blocks.append({"type": "section", "text": "道具使用"})
        blocks.append({"type": "bars", "items": _sorted_items(use, ITEM_NAMES)})

    up = stats.get("upgrade")
    if isinstance(up, dict) and up:
        blocks.append({"type": "section", "text": "升级"})
        blocks.append({"type": "bars", "items": _sorted_items(up, UPGRADE_NAMES)})
    return blocks


def _submode_text(split: list[tuple[str, dict]]) -> list[str]:
    """细分模式的文字版，给 LLM 点评用。"""
    lines: list[str] = []
    for label, stats in split:
        game = int(stats.get("game") or 0)
        win = int(stats.get("win") or 0)
        kills = int(stats.get("kills") or 0)
        deaths = int(stats.get("deaths") or 0)
        rate = f"{win / game * 100:.1f}%" if game else "无"
        kd = f"{kills / deaths:.2f}" if deaths else "无"
        tag = _dream_tag(stats)
        seg = [
            f"【{label}】场次{game} 胜{win} 胜率{rate}",
            f"K/D {kills}/{deaths}≈{kd}" + (f" 幻梦评分{tag}" if tag else f" 幻梦评分样本不足（{game}场）"),
        ]
        if "final_kills" in stats or "final_deaths" in stats:
            seg.append(
                f"终结击杀{int(stats.get('final_kills') or 0)}/被终结{int(stats.get('final_deaths') or 0)}"
            )
        if "bed_lose" in stats:
            seg.append(f"床被破坏{int(stats.get('bed_lose') or 0)}")
        use = stats.get("use_item")
        if isinstance(use, dict) and use:
            seg.append(
                "道具："
                + "、".join(
                    f"{ITEM_NAMES.get(str(k).upper(), k)}{int(v)}"
                    for k, v in sorted(use.items(), key=lambda x: -float(x[1] or 0))
                )
            )
        up = stats.get("upgrade")
        if isinstance(up, dict) and up:
            seg.append(
                "升级："
                + "、".join(
                    f"{UPGRADE_NAMES.get(str(k).upper(), k)}{int(v)}"
                    for k, v in sorted(up.items(), key=lambda x: -float(x[1] or 0))
                )
            )
        lines.append("\n".join(seg))
    return lines


def _flatten_numbers(obj: Any, prefix: str = "") -> dict[str, float]:
    """把嵌套结构里所有数字摊平成 路径 -> 数值。"""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[p] = float(v)
            elif isinstance(v, (dict, list)):
                out.update(_flatten_numbers(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[p] = float(v)
            elif isinstance(v, (dict, list)):
                out.update(_flatten_numbers(v, p))
    return out


def _stat_lines(data: Any, max_lines: int = 8) -> list[str]:
    """从数据里推断胜率之类的比率，返回可读文本行。"""
    nums = _flatten_numbers(data)
    lines: list[str] = []
    seen: set[str] = set()

    total_words = ("game", "games", "match", "matches", "play", "plays",
                   "played", "round", "rounds", "battle", "battles", "total")

    for path, val in nums.items():
        parent, _, leaf = path.rpartition(".")
        low = leaf.lower()
        if "win" not in low:
            continue
        if any(w in low for w in ("rate", "pct", "percent", "ratio", "avg")):
            continue
        if val <= 0:
            continue
        for tpath, tval in nums.items():
            tparent, _, tleaf = tpath.rpartition(".")
            if tparent != parent:
                continue
            tlow = tleaf.lower()
            if "win" in tlow:
                continue
            if not any(w in tlow for w in total_words):
                continue
            if tval >= val:
                rate = val / tval * 100
                line = f"{leaf}/{tleaf}: {int(val)}/{int(tval)} ≈ {rate:.1f}%"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
                break

    # 击杀/死亡 K/D
    for path, val in nums.items():
        parent, _, leaf = path.rpartition(".")
        low = leaf.lower()
        if low not in ("kills", "kill", "kill_count"):
            continue
        for tpath, tval in nums.items():
            tparent, _, tleaf = tpath.rpartition(".")
            if tparent != parent:
                continue
            if tleaf.lower() in ("deaths", "death", "death_count") and tval > 0:
                line = f"K/D: {int(val)}/{int(tval)} ≈ {val / tval:.2f}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
                break

    return lines[:max_lines]


class _TokenSlot:
    """单个 Token 的额度状态。"""

    __slots__ = ("token", "calls", "cooldown_until", "dead")

    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: deque = deque()
        self.cooldown_until = 0.0
        self.dead = False


class TokenPool:
    """多 Token 轮询池：按每分钟限额自动切换，都满了就等最早恢复的那个。"""

    def __init__(self, tokens: list[str], limit_per_minute: int = 30) -> None:
        self.limit = max(1, int(limit_per_minute or 30))
        self._slots = [_TokenSlot(t) for t in tokens if t]
        self._cursor = 0
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._slots)

    def _prune(self, slot: _TokenSlot, now: float) -> None:
        while slot.calls and now - slot.calls[0] >= 60.0:
            slot.calls.popleft()

    async def acquire(self) -> tuple[str | None, float]:
        """返回 (可用的 token, 需要等待的秒数)。token 为 None 表示全部限额。"""
        async with self._lock:
            now = time.time()
            n = len(self._slots)
            if n == 0:
                return None, 0.0
            best_wait = float("inf")
            for i in range(n):
                slot = self._slots[(self._cursor + i) % n]
                if slot.dead:
                    continue
                if slot.cooldown_until > now:
                    best_wait = min(best_wait, slot.cooldown_until - now)
                    continue
                self._prune(slot, now)
                if len(slot.calls) < self.limit:
                    slot.calls.append(now)
                    self._cursor = (self._cursor + i + 1) % n
                    return slot.token, 0.0
                if slot.calls:
                    best_wait = min(best_wait, 60.0 - (now - slot.calls[0]))
            if best_wait == float("inf"):
                best_wait = 60.0
            return None, max(0.5, best_wait)

    def mark_rate_limited(self, token: str, seconds: float = 60.0) -> None:
        for slot in self._slots:
            if slot.token == token:
                slot.cooldown_until = time.time() + seconds

    def mark_dead(self, token: str) -> None:
        for slot in self._slots:
            if slot.token == token:
                slot.dead = True

    def stats(self) -> list[dict]:
        now = time.time()
        out = []
        for i, slot in enumerate(self._slots):
            self._prune(slot, now)
            out.append({
                "index": i + 1,
                "tail": slot.token[-6:],
                "used": len(slot.calls),
                "limit": self.limit,
                "dead": slot.dead,
                "cooldown": max(0, round(slot.cooldown_until - now)),
            })
        return out


class BuglandQueryPlugin(Star):
    '''布吉岛查询：绑定布吉岛玩家昵称，然后按语义查询玩家信息、战绩、排行榜、公会、对局记录，自动计算胜率等。'''

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        api = self.config.get("api") or {}
        behavior = self.config.get("behavior") or {}

        raw_pool = api.get("token_pool") or ""
        if isinstance(raw_pool, list):
            raw_list = [str(x) for x in raw_pool]
        else:
            raw_list = str(raw_pool).splitlines()
        raw_list.append(str(api.get("token") or ""))
        tokens: list[str] = []
        seen: set[str] = set()
        for item in raw_list:
            tok = str(item or "").strip()
            if tok.lower().startswith("bearer "):
                tok = tok[7:].strip()
            if tok and tok not in seen:
                seen.add(tok)
                tokens.append(tok)
        self.tokens = tokens
        self.token = tokens[0] if tokens else ""
        self._pool = TokenPool(tokens, int(api.get("token_limit_per_minute") or 30))
        self.api_base = str(api.get("api_base") or "https://api.mcbjd.net/v2").rstrip("/")
        self.timeout = int(api.get("timeout") or 20)
        self.max_chars = int(api.get("max_result_chars") or 3000)

        self.allow_query_others = bool(behavior.get("allow_query_others", True))
        self.cache_ttl = int(behavior.get("cache_ttl", 30) or 0)
        self.auto_bind_on_query = bool(behavior.get("auto_bind_on_query", True))
        self.render_image = bool(behavior.get("render_image", True))

        mode_names = dict(DEFAULT_MODE_NAMES)
        extra = behavior.get("mode_names") or ""
        if isinstance(extra, dict):
            items = extra.items()
        elif isinstance(extra, str):
            items = []
            for line in extra.splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                items.append((k.strip(), v.strip()))
        else:
            items = []
        for k, v in items:
            if k and v:
                mode_names[str(k).strip().upper()] = str(v)
        self.mode_names = mode_names

        self._cache: dict[str, tuple[float, Any]] = {}
        if self.token:
            self.logger.info(
                f"布吉岛查询已加载，接口: {self.api_base}，Token 池 {len(self._pool)} 个"
                f"（每个 {self._pool.limit} 次/分）"
            )
        else:
            self.logger.warning("布吉岛查询未配置 API Token，工具会提示用户联系管理员。")

    # ------------------------------------------------------------------ 内部

    async def _request(self, path: str, payload: dict) -> tuple[bool, Any]:
        """POST 到 BuGLand API，返回 (是否成功, 数据或错误信息)。"""
        if len(self._pool) == 0:
            return False, (
                "还没配置 BuGLand API Token。让管理员在插件配置里填一下"
                "（布吉岛大厅输入 /openapi 申领）。"
            )

        url = f"{self.api_base}{path}"
        cache_key = url + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        now = time.time()
        if self.cache_ttl > 0:
            hit = self._cache.get(cache_key)
            if hit and now - hit[0] < self.cache_ttl:
                return True, hit[1]

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        last_err = "所有 Token 都不可用。"
        max_tries = len(self._pool) + 1

        for _ in range(max_tries):
            token, wait = await self._pool.acquire()
            if not token:
                return False, f"Token 额度已经用光了，{wait:.0f} 秒后再试。"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AstrBot-BuglandQuery/1.0",
            }
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        text = await resp.text()
                        try:
                            body = json.loads(text)
                        except Exception:
                            body = None

                        if resp.status == 429:
                            self._pool.mark_rate_limited(token, 60.0)
                            last_err = "接口限速（HTTP 429），已切到下一个 Token。"
                            continue
                        if resp.status in (401, 403):
                            self._pool.mark_dead(token)
                            last_err = f"Token 失效（HTTP {resp.status}），已切到下一个。"
                            continue

                        if resp.status != 200:
                            msg = ""
                            if isinstance(body, dict):
                                msg = str(body.get("message") or body.get("msg") or "")
                            if not msg:
                                msg = text[:180]
                            return False, f"接口返回 HTTP {resp.status}: {msg}"

                        if isinstance(body, dict):
                            code = body.get("code")
                            if code not in (None, 0, 200):
                                if str(code) in ("429", "401", "403"):
                                    self._pool.mark_rate_limited(token, 60.0)
                                    last_err = f"接口返回 code={code}，已切到下一个 Token。"
                                    continue
                                return False, (
                                    f"接口返回 code={code}: "
                                    f"{body.get('message') or body.get('msg') or '未知错误'}"
                                )
                            data = body.get("data", body)
                        else:
                            data = body if body is not None else text
            except asyncio.TimeoutError:
                return False, f"请求超时（{self.timeout} 秒）"
            except aiohttp.ClientError as e:
                return False, f"网络请求失败: {e}"
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"请求 {url} 出错: {e}")
                return False, f"请求出错: {e}"

            if self.cache_ttl > 0:
                self._cache[cache_key] = (now, data)
            return True, data

        return False, last_err

    def _dump(self, data: Any, limit: int | None = None) -> str:
        limit = limit or self.max_chars
        if isinstance(data, str):
            text = data
        else:
            try:
                text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                text = str(data)
        if len(text) > limit:
            text = text[:limit] + f"...(已截断，共 {len(text)} 字符)"
        return text

    def _mode_name(self, code: Any) -> str:
        code = str(code or "").strip()
        if not code:
            return "未知模式"
        up = code.upper()
        if up in self.mode_names:
            return self.mode_names[up]
        for k in sorted(self.mode_names, key=len, reverse=True):
            if up.startswith(k):
                return self.mode_names[k]
        return code

    def _format_game_log(self, data: Any, target: str) -> str:
        recs = []
        if isinstance(data, dict):
            recs = data.get("data") or data.get("list") or []
        if not isinstance(recs, list):
            return f"「{target}」的对局记录：\n{self._dump(data)}"
        if not recs:
            return f"「{target}」最近没有对局记录。"

        lines = []
        wins = 0
        for i, r in enumerate(recs, 1):
            if not isinstance(r, dict):
                continue
            date = str(r.get("date") or "")[5:16]
            mode = self._mode_name(r.get("type") or r.get("mode"))
            win = r.get("win")
            if win is True:
                wins += 1
                res = "胜"
            elif win is False:
                res = "负"
            else:
                res = "?"
            lines.append(f"{i}. {date}｜{mode}｜{res}｜{r.get('matchId') or ''}")

        total = len(lines)
        tail = f"\n\n共 {total} 场，{wins} 胜 {total - wins} 负"
        if total:
            tail += f"，胜率 {wins / total * 100:.1f}%"
        return f"「{target}」最近的对局：\n" + "\n".join(lines) + tail

    @staticmethod
    def _prune_cards() -> None:
        """清掉一小时前的临时卡片图，避免堆积。"""
        import glob
        import os

        out_dir = os.path.join(tempfile.gettempdir(), "bugland_cards")
        now = time.time()
        for p in glob.glob(os.path.join(out_dir, "*.png")):
            try:
                if now - os.path.getmtime(p) > 3600:
                    os.remove(p)
            except OSError:
                pass

    async def _send_card(
        self,
        event: AstrMessageEvent,
        title: str,
        subtitle: str = "",
        blocks: list[dict] | None = None,
        footer: str = "数据来源：BuGLand API",
    ) -> bool:
        """把 block 列表画成图片发给用户，失败返回 False 好回退到文字。"""
        if not self.render_image:
            return False
        try:
            path = render_blocks(title, blocks or [], subtitle, footer)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 渲染图片失败: {e}")
            return False
        try:
            await event.send(MessageChain(chain=[Image(file=path)]))
            self._prune_cards()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 发送图片失败: {e}")
            return False

    @staticmethod
    def _pretty_leaf(leaf: str) -> str:
        return KEY_NAMES.get(leaf, leaf)

    def _auto_kpi(self, data: Any, limit: int = 9) -> list[dict]:
        """从数据里挑数字字段拼 KPI 卡片，优先展示常用统计。"""
        nums = _flatten_numbers(data)
        by_leaf: dict[str, float] = {}
        for path, val in nums.items():
            leaf = path.rsplit(".", 1)[-1]
            low = path.lower()
            if leaf in ("deaths2", "_id"):
                continue
            if "use_item" in low or "upgrade" in low:
                continue
            by_leaf.setdefault(leaf, float(val))

        order = [
            "win",
            "lose",
            "game",
            "kills",
            "deaths",
            "final_kills",
            "final_deaths",
            "bed_lose",
            "total_win",
            "total_game",
            "ttl",
        ]
        items: list[dict] = []
        for leaf in order:
            if leaf in by_leaf:
                val = by_leaf.pop(leaf)
                value = str(int(val)) if val.is_integer() else f"{val:.2f}"
                items.append({"label": self._pretty_leaf(leaf), "value": value})
            if len(items) >= limit:
                break
        for leaf, val in by_leaf.items():
            if len(items) >= limit:
                break
            value = str(int(val)) if val.is_integer() else f"{val:.2f}"
            items.append({"label": self._pretty_leaf(leaf), "value": value})
        return items

    def _stat_card_blocks(self, data: Any) -> list[dict]:
        """通用战绩卡片：大数字 + KPI + 进度条 + 明细行。"""
        nums = _flatten_numbers(data)
        lines = _stat_lines(data)
        rows, bars = parse_stat_lines(lines)
        blocks: list[dict] = []

        game = nums.get("total_game") or nums.get("game")
        if game:
            blocks.append(
                {"type": "hero", "label": "总场次", "value": str(int(game)), "unit": "场"}
            )
        kpi = self._auto_kpi(data)
        if game:
            # 大数字块已经占了场次，卡片里就别重复
            kpi = [i for i in kpi if i["label"] not in ("场次", "总场次", "总胜场")]
        if kpi:
            blocks.append({"type": "kpi", "items": kpi})
        if bars:
            blocks.append(
                {
                    "type": "bars",
                    "items": [{"label": lab, "value": r, "text": txt} for lab, r, txt in bars],
                }
            )
        if rows:
            blocks.append(
                {"type": "rows", "items": [{"label": lab, "value": val} for lab, val in rows]}
            )
        return blocks

    def _render(self, data: Any) -> str:
        """原始数据 + 自动统计。"""
        parts = []
        stats = _stat_lines(data)
        if stats:
            parts.append("【统计】\n" + "\n".join(stats))
        parts.append("【原始数据】\n" + self._dump(data))
        return "\n\n".join(parts)

    @staticmethod
    def _bind_key(event: AstrMessageEvent) -> str:
        return f"bind:{event.get_sender_id()}"

    @staticmethod
    def _bind_key_by_uid(uid: str) -> str:
        return f"bind:{uid}"

    async def _save_binding_by_key(self, key: str, username: str) -> None:
        await self.put_kv_data(
            key,
            {"username": username, "bound_at": int(time.time())},
        )

    async def _get_binding(self, event: AstrMessageEvent) -> str:
        rec = await self.get_kv_data(self._bind_key(event), None)
        if isinstance(rec, dict):
            return str(rec.get("username") or "")
        if isinstance(rec, str):
            return rec
        return ""

    async def _get_binding_by_uid(self, uid: str) -> str:
        rec = await self.get_kv_data(self._bind_key_by_uid(uid), None)
        if isinstance(rec, dict):
            return str(rec.get("username") or "")
        if isinstance(rec, str):
            return rec
        return ""

    async def _save_binding(self, event: AstrMessageEvent, username: str) -> None:
        await self.put_kv_data(
            self._bind_key(event),
            {"username": username, "bound_at": int(time.time())},
        )

    def _raw_mentions(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        """从原始消息对象里取被@的人（QQ 官方适配器不会把别人的@转成 At 组件）。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return []
        mentions = getattr(raw, "mentions", None) or []
        out: list[tuple[str, str]] = []
        for m in mentions:
            if getattr(m, "is_you", False):
                continue
            mid = getattr(m, "id", None) or getattr(m, "member_openid", None)
            if not mid:
                continue
            name = str(getattr(m, "username", "") or getattr(m, "nickname", "") or "")
            out.append((str(mid), name))
        return out

    def _extract_at_user(self, event: AstrMessageEvent) -> tuple[str, str, str] | None:
        """从消息里取第一个"不是机器人自己"的被@者，返回 (用户ID, QQ号, 昵称)。"""
        try:
            self_ids = {str(event.get_self_id() or "")}
        except Exception:
            self_ids = set()
        try:
            messages = event.get_messages() or []
        except Exception:
            messages = []

        for comp in messages:
            if not isinstance(comp, At):
                continue
            qq = str(getattr(comp, "qq", "") or "").strip()
            if not qq or qq == "all" or qq in self_ids:
                continue
            name = str(getattr(comp, "name", "") or "").strip()
            return qq, qq, name

        # 兜底：QQ 官方适配器只把"@机器人"变成 At，别人的 @ 在 raw_message.mentions 里
        for mid, name in self._raw_mentions(event):
            if mid in self_ids:
                continue
            return mid, mid, name

        return None

    async def _get_bound_name(self, user_id: str) -> str:
        rec = await self.get_kv_data(f"bind:{user_id}", None)
        if isinstance(rec, dict):
            return str(rec.get("username") or "")
        if isinstance(rec, str):
            return rec
        return ""

    async def _resolve_target(self, event: AstrMessageEvent, username: str) -> tuple[str, str | None]:
        """确定要查谁：
        - 空 -> 当前用户绑定的账号
        - 以 "@" 开头 -> 消息里被@的那个人绑定的账号（没绑定就报错）
        - 其他 -> 直接当成布吉岛昵称
        """
        username = (username or "").strip()

        # 没指定名字时，如果消息里@了别人，默认就查被@的那个人
        if not username and self._extract_at_user(event):
            username = "@"

        if username.startswith("@"):
            at = self._extract_at_user(event)
            if not at:
                return "", "没拿到被@的人（当前平台可能不传递@信息），直接把游戏昵称告诉我吧。"
            user_id, _qq, name = at
            if not self.allow_query_others and user_id != str(event.get_sender_id()):
                return "", "管理员关掉了查询他人数据的功能。"
            bound = await self._get_bound_name(user_id)
            if not bound:
                who = name or "被@的那个人"
                return "", f"{who}还没绑定布吉岛账号，查不了。"
            return bound, None

        bound = await self._get_binding(event)
        if username:
            if not self.allow_query_others and bound and bound.lower() != username.lower():
                return "", "管理员关掉了查询他人数据的功能，你只能查自己绑定的账号。"
            return username, None

        if bound:
            return bound, None
        return "", "你还没绑定布吉岛账号。跟我说「绑定布吉岛 你的游戏昵称」就行。"

    async def _verify_player(self, username: str) -> tuple[bool, str, Any]:
        ok, data = await self._request("/player", {"username": username})
        if not ok:
            return False, str(data), None
        if isinstance(data, dict):
            name = data.get("playername") or data.get("username")
            if name:
                return True, str(name), data
            return False, f"没查到玩家「{username}」，昵称要写游戏里的名字，注意大小写", None
        return False, f"没查到玩家「{username}」", None

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="bugland_bind")
    async def bugland_bind(self, event: AstrMessageEvent, username: str, qq_number: str = "") -> str:
        '''把当前用户绑定到指定的布吉岛玩家昵称。用户可以重复绑定，新的绑定会覆盖旧的。

        当用户表达"绑定/绑一下/我是xxx/帮我绑定布吉岛账号/绑错了重新绑"等意图时调用。
        需要用户说出他在布吉岛游戏里的昵称，不是QQ昵称。

        Args:
            username(string): 布吉岛游戏内的玩家昵称
            qq_number(string): 可选。用户如果主动说了自己的 QQ 号，把 QQ 号填这里，这样在 QQ 官方机器人和 OneBot 两边都能查到同一份绑定。
        '''
        username = (username or "").strip()
        if not username:
            return "要绑定的昵称是空的，让用户说一下游戏里的名字。"

        ok, name, _ = await self._verify_player(username)
        if not ok:
            return f"验证玩家「{username}」失败：{name}。昵称要写游戏里的名字，注意大小写。"

        await self._save_binding(event, name)

        qq_number = "".join(ch for ch in str(qq_number or "") if ch.isdigit())
        extra = ""
        if qq_number and len(qq_number) >= 5:
            await self._save_binding_by_key(self._bind_key_by_uid(qq_number), name)
            extra = f"，同时关联到 QQ 号 {qq_number}"

        return f"绑定成功，当前用户已绑定到布吉岛玩家「{name}」{extra}。以后可以直接问数据，也可以随时重新绑定。"

    @filter.llm_tool(name="bugland_link_qq")
    async def bugland_link_qq(self, event: AstrMessageEvent, qq_number: str) -> str:
        '''把当前平台的绑定和某个 QQ 号打通，让两边共用同一份布吉岛绑定。

        当用户在 QQ 官方机器人这边说"我的QQ号是xxx，帮我把数据同步过去/打通"等意图时调用。

        Args:
            qq_number(string): 用户的 QQ 号
        '''
        qq_number = "".join(ch for ch in str(qq_number or "") if ch.isdigit())
        if len(qq_number) < 5:
            return "QQ 号看起来不对，让用户再确认一下。"

        cur = await self._get_binding(event)
        other = await self._get_binding_by_uid(qq_number)

        if cur and not other:
            await self._save_binding_by_key(self._bind_key_by_uid(qq_number), cur)
            return f"已把当前绑定「{cur}」同步到 QQ 号 {qq_number}，之后两边都能查到。"
        if other and not cur:
            await self._save_binding(event, other)
            return f"已从 QQ 号 {qq_number} 同步绑定过来：你现在绑定的是「{other}」。"
        if cur and other:
            if cur.lower() == other.lower():
                return f"两边已经一致了，都是「{cur}」。"
            await self._save_binding_by_key(self._bind_key_by_uid(qq_number), cur)
            return f"两边不一致（这里是「{cur}」，QQ 号那边是「{other}」），已统一成「{cur}」。"
        return "你还没绑定布吉岛账号，先说「绑定布吉岛 你的游戏昵称」。"

    @filter.llm_tool(name="bugland_my_binding")
    async def bugland_my_binding(self, event: AstrMessageEvent) -> str:
        '''查看当前用户绑定的是哪个布吉岛玩家昵称。

        当用户问"我绑的是谁/我绑定了吗/查看我的绑定"时调用。
        '''
        bound = await self._get_binding(event)
        if not bound:
            return "当前用户还没绑定布吉岛账号。"
        rec = await self.get_kv_data(self._bind_key(event), None)
        ts = rec.get("bound_at") if isinstance(rec, dict) else None
        if ts:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
            return f"当前用户绑定的布吉岛玩家是「{bound}」（绑定于 {when}）。"
        return f"当前用户绑定的布吉岛玩家是「{bound}」。"

    @filter.llm_tool(name="bugland_unbind")
    async def bugland_unbind(self, event: AstrMessageEvent) -> str:
        '''解除当前用户的布吉岛账号绑定。

        当用户明确说"解绑/取消绑定/删掉绑定"时调用。
        '''
        bound = await self._get_binding(event)
        if not bound:
            return "当前用户本来就没绑定。"
        await self.delete_kv_data(self._bind_key(event))
        return f"已解除对「{bound}」的绑定。"

    @filter.llm_tool(name="bugland_at_binding")
    async def bugland_at_binding(self, event: AstrMessageEvent) -> str:
        '''查看消息里被@的那个人的布吉岛绑定状态。

        当用户@了某人并问"他绑没绑/他绑的是哪个号"时调用。只会说明绑定情况，不查询游戏数据。
        '''
        at = self._extract_at_user(event)
        if not at:
            return "消息里没找到被@的人。"
        user_id, _qq, name = at
        bound = await self._get_bound_name(user_id)
        who = name or "被@的那个人"
        if not bound:
            return f"{who}还没绑定布吉岛账号。"
        return f"{who}绑定的布吉岛玩家是「{bound}」。"

    @filter.llm_tool(name="bugland_player_info")
    async def bugland_player_info(self, event: AstrMessageEvent, username: str = "") -> str:
        '''查询布吉岛玩家的基础信息：空岛等阶、起床等阶、公会、VIP等级、布吉岛等级。

        当用户问"查一下XXX的信息/资料/等级/啥公会/首页数据/我什么段位"时调用。

        Args:
            username(string): 要查询的布吉岛玩家昵称。留空表示查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"（会自动用消息里被@的人的绑定）。
        '''
        target, err = await self._resolve_target(event, username)
        if err:
            return err
        ok, data = await self._request("/player", {"username": target})
        if not ok:
            return f"查询「{target}」失败：{data}"
        if not isinstance(data, dict) or not (data.get("playername") or data.get("username")):
            return f"没查到玩家「{target}」，确认一下昵称。"

        import re

        def clean(v: Any) -> str:
            s = re.sub(r"§.", "", str(v or "")).strip()
            return s or "-"

        label_map = [
            ("空岛等阶", "swxp_show"),
            ("起床等阶", "bwxp_show"),
            ("防御等阶", "vdxp_show"),
            ("布吉岛等级", "bjdxp_level"),
            ("公会", "guild_name"),
            ("VIP等级", "vip_level"),
        ]
        rows = [(label, clean(data.get(key))) for label, key in label_map]
        sent = await self._send_card(
            event,
            f"{clean(data.get('playername') or target)}",
            "布吉岛玩家资料",
            [{"type": "rows", "items": [{"label": k, "value": v} for k, v in rows]}],
        )
        text = "、".join(f"{k}: {v}" for k, v in rows)
        if sent:
            return (
                f"已经把「{target}」的玩家资料画成图片发给用户了。"
                "用一两句话概括即可，不要重复罗列字段。\n" + text
            )
        return f"玩家「{target}」的信息：{text}"

    @filter.llm_tool(name="bugland_game_stats")
    async def bugland_game_stats(
        self,
        event: AstrMessageEvent,
        gametype: str,
        username: str = "",
        subtype: str = "all",
    ) -> str:
        '''查询玩家在某个具体游戏模式下的详细战绩，会自动按细分模式拆开，并算出胜率、K/D、道具使用和升级项。

        当用户问"起床战争战绩/胜率/空岛打了多少把/我的战绩怎么样/某个模式的统计"时调用。

        点评流程（必须遵守）：
        1. 每个细分模式分开讲，标题写全"大类-小类"，例如"起床战争-经验4队8人""起床战争-4队4人"，禁止合并成一个"起床战争"。
        2. 每个模式先给明确结论（强/一般/新手/刷分嫌疑），再带胜率、K/D、场次、终结击杀、床被破坏这些数据支撑。
        3. 必须评价道具使用（火焰弹/鸡蛋/金苹果等）和升级项（保护/锋利/陷阱等），说明打法风格偏向。
        4. 场次太少的模式要说明样本不足，不要硬评。
        5. 图里和文字里都带了「幻梦评分」，形如 178.9(A)，要在点评里念出来，
           并说明这是按幻梦口径估算的（第三方拟合公式，非官方）。

        Args:
            gametype(string): 游戏模式，可选：bedwars, skywars, vdefense, arenapvp, anqu, kitbattle, anni, achievement, survivalgame, parkour
            username(string): 要查询的布吉岛玩家昵称。留空表示查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"（会自动用消息里被@的人的绑定）。
            subtype(string): 榜单/赛季类型，默认 all，也可以填 2025、2026 这种年份
        '''
        gametype = (gametype or "").strip().lower()
        if not gametype:
            return f"需要指定游戏模式，可用：{GAMES_HINT}"
        target, err = await self._resolve_target(event, username)
        if err:
            return err

        payload = {"username": target, "gametype": gametype}
        if subtype:
            payload["subtype"] = str(subtype).strip()

        ok, data = await self._request("/gamestats", payload)
        if not ok:
            return f"查询「{target}」的 {gametype} 战绩失败：{data}"
        if not data:
            return f"没查到「{target}」在 {gametype} 的数据，可能没打过这个模式。"

        cn_game = GAMETYPE_NAMES.get(gametype, gametype)
        split = _split_submodes(data, gametype)
        if split:
            total_tag = _dream_aggregate(split)
            blocks: list[dict] = []
            if total_tag:
                blocks.append(
                    {
                        "type": "rows",
                        "items": [{"label": "幻梦评分（整体估算）", "value": total_tag}],
                    }
                )
            for i, (label, stats) in enumerate(split):
                if i:
                    blocks.append({"type": "divider"})
                blocks.extend(_submode_blocks(label, stats))
            blocks.append(
                {
                    "type": "note",
                    "text": "点评要求：按细分模式分开下结论，并覆盖道具使用与升级情况",
                }
            )
            text = "\n\n".join(_submode_text(split))
            if total_tag:
                text = f"整体幻梦评分：{total_tag}\n\n" + text
        else:
            stats = _stat_lines(data)
            blocks = self._stat_card_blocks(data) or [
                {"type": "note", "text": "暂无可用的统计数据"}
            ]
            text = "【统计】\n" + ("\n".join(stats) if stats else "无")

        sent = await self._send_card(
            event,
            f"{target} · {cn_game}",
            f"布吉岛战绩（subtype={subtype or 'all'}）",
            blocks,
        )
        guide = (
            f"已经把「{target}」的{cn_game}战绩按细分模式画成图片发给用户了。"
            "点评必须按这个流程来：\n"
            "1) 每个细分模式分开说，标题写全「大类-小类」（如 起床战争-经验4队8人、起床战争-4队4人），"
            "禁止合并成一个大类说「那个模式」；\n"
            "2) 每个模式先给明确结论（强/一般/新手/有刷分嫌疑），再带胜率、K/D、场次、终结击杀、床被破坏等数据；\n"
            "3) 必须评价道具使用（火焰弹/鸡蛋/金苹果等）和升级项（保护/锋利/陷阱等），说清打法偏向；\n"
            "4) 场次太少的模式直接说样本不足，不要硬评；\n"
            "5) 每个模式都有「幻梦评分」（形如 178.9(A)），要点出来，"
            "并说明这是按幻梦口径拟合估算的，不是官方数据；\n"
            "6) 最后给一段总结：这个号属于什么类型（如「起床4v4火球手」）+ 两三条可执行的改进建议。\n"
            "7) 输出纯文本，禁止 markdown：不要 ** 加粗、不要 # 标题、不要表格和列表符号，"
            "QQ 不渲染这些东西，强调就用「」。\n"
            "不要罗列原始 JSON 或表格。\n"
        )
        if sent:
            return guide + text
        return f"{guide}\n{text}"

    @filter.llm_tool(name="bugland_leaderboard")
    async def bugland_leaderboard(
        self,
        event: AstrMessageEvent,
        username: str = "",
        lb_type: str = "",
    ) -> str:
        '''查询排行榜数据。可以查某个玩家在哪些榜上有名，也可以查一个具体排行榜的前几名。

        当用户问"排行榜/榜一/XXX在榜上吗/这个榜前十"时调用。

        Args:
            username(string): 玩家昵称，用于查询该玩家的在榜情况。留空表示查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"。
            lb_type(string): 排行榜标识，例如 solo_win_all_java。留空则只查玩家在榜情况，不查具体榜单。
        '''
        lb_type = (lb_type or "").strip()

        if lb_type:
            ok, data = await self._request("/leaderboard", {"lb_type": lb_type})
            if not ok:
                return f"查询排行榜「{lb_type}」失败：{data}"
            blocks = self._stat_card_blocks(data) or [
                {"type": "note", "text": "这个榜单没有可展示的数据"}
            ]
            sent = await self._send_card(event, f"排行榜 · {lb_type}", "榜单数据", blocks)
            if sent:
                return "已经把排行榜画成图片发给用户了。用一两句话点出亮点，不要逐条念。"
            return f"排行榜「{lb_type}」：\n{self._dump(data)}"

        target, err = await self._resolve_target(event, username)
        if err:
            return err
        ok, data = await self._request("/leaderboard", {"username": target})
        if not ok:
            return f"查询「{target}」的在榜情况失败：{data}"

        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list) or not result:
            return f"「{target}」目前没有上榜记录。"

        rows = [
            (f"{str(item.get('cn_type') or item.get('type') or '')}", f"第{int(item.get('top') or 0)}名")
            for item in result
            if isinstance(item, dict)
        ]
        sent = await self._send_card(
            event,
            f"{target} · 排行榜",
            f"共 {len(rows)} 个榜单在榜",
            [
                {
                    "type": "table",
                    "columns": ["榜单", "名次"],
                    "rows": [[k, v] for k, v in rows],
                }
            ],
        )
        if sent:
            return (
                f"已经把「{target}」的在榜情况画成图片发给用户了。"
                "用一两句话点出最好的名次即可。"
            )
        return f"「{target}」的在榜情况：\n" + "\n".join(f"{k} {v}" for k, v in rows)

    @filter.llm_tool(name="bugland_guild")
    async def bugland_guild(
        self,
        event: AstrMessageEvent,
        username: str = "",
        guild: str = "",
    ) -> str:
        '''查询公会信息：成员、等级、战绩之类。

        当用户问"我的公会/查一下某个公会/公会信息"时调用。

        Args:
            username(string): 通过玩家昵称反查他所在的公会。留空则查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"。
            guild(string): 直接指定公会名称（名字里的空格用 "-" 代替）
        '''
        guild = (guild or "").strip()
        if guild:
            payload = {"guild": guild}
            label = guild
        else:
            target, err = await self._resolve_target(event, username)
            if err:
                return err
            payload = {"username": target}
            label = target

        ok, data = await self._request("/guild", payload)
        if not ok:
            return f"查询公会「{label}」失败：{data}"
        if not data:
            return f"没查到「{label}」的公会信息。"
        blocks = self._stat_card_blocks(data) or [
            {"type": "note", "text": "这个公会没有可展示的数据"}
        ]
        sent = await self._send_card(event, f"{label} · 公会", "公会数据", blocks)
        if sent:
            return f"已经把「{label}」的公会信息画成图片发给用户了。用一两句话概括即可。"
        return f"「{label}」的公会信息：\n{self._dump(data)}"

    @filter.llm_tool(name="bugland_game_log")
    async def bugland_game_log(
        self,
        event: AstrMessageEvent,
        username: str = "",
        page: str = "1",
    ) -> str:
        '''查询玩家最近的对局记录列表，包含时间、模式、输赢、对局ID。

        当用户问"最近打了什么/上一把赢了没/对局记录/战绩列表"时调用。

        Args:
            username(string): 要查询的布吉岛玩家昵称。留空表示查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"（会自动用消息里被@的人的绑定）。
            page(string): 页码，默认 "1"
        '''
        target, err = await self._resolve_target(event, username)
        if err:
            return err
        ok, data = await self._request(
            "/gamelog/user", {"username": target, "page": str(page or "1")}
        )
        if not ok:
            return f"查询「{target}」的对局记录失败：{data}"
        if not data:
            return f"「{target}」最近没有对局记录。"

        recs = data.get("data") if isinstance(data, dict) else None
        if not isinstance(recs, list) or not recs:
            return self._format_game_log(data, target)

        rows = []
        wins = 0
        for r in recs:
            if not isinstance(r, dict):
                continue
            date = str(r.get("date") or "")[5:16]
            mode = self._mode_name(r.get("type") or r.get("mode"))
            win = r.get("win")
            if win is True:
                wins += 1
                res = "胜"
            elif win is False:
                res = "负"
            else:
                res = "?"
            rows.append((f"{date}  {mode}", res))

        total = len(rows)
        rate = wins / total if total else 0.0
        blocks = [
            {"type": "bars", "items": [{"label": "本次列表胜率", "value": rate * 100}]},
            {
                "type": "table",
                "columns": ["时间 模式", "结果"],
                "rows": [[a, b] for a, b in rows],
            },
        ]
        sent = await self._send_card(
            event,
            f"{target} · 最近对局",
            f"第 {page} 页，共 {total} 场，{wins} 胜 {total - wins} 负",
            blocks,
        )
        summary = f"「{target}」最近 {total} 场，{wins} 胜 {total - wins} 负，胜率 {rate * 100:.1f}%"
        if sent:
            return (
                f"已经把「{target}」最近的对局画成图片发给用户了。"
                "用一两句话说说输赢走势就行，不要逐条念记录。\n" + summary
            )
        return self._format_game_log(data, target)

    @filter.llm_tool(name="bugland_match_detail")
    async def bugland_match_detail(
        self,
        event: AstrMessageEvent,
        match: str,
        date: str,
    ) -> str:
        '''查询单场对局的详细数据（伤害、击杀、建筑等）。

        当用户问"这把详情/对局数据/刚才那场打了多少"时调用。
        对局 ID 和日期可以从 bugland_game_log 的结果里拿到。

        Args:
            match(string): 对局 ID
            date(string): 对局时间，格式 yyyy-MM-dd HH:mm:ss
        '''
        match = (match or "").strip()
        date = (date or "").strip()
        if not match or not date:
            return "需要同时提供对局 ID 和日期（yyyy-MM-dd HH:mm:ss）。"
        ok, data = await self._request("/gamelog/match", {"match": match, "date": date})
        if not ok:
            return f"查询对局 {match} 失败：{data}"
        if not data:
            return f"没查到对局 {match} 的数据。"
        blocks = self._stat_card_blocks(data) or [
            {"type": "note", "text": "这场对局没有可展示的数据"}
        ]
        sent = await self._send_card(event, f"对局详情", match, blocks)
        if sent:
            return "已经把这场对局的详情画成图片发给用户了。用一两句话点评即可。"
        return f"对局 {match} 的详情：\n{self._render(data)}"

    @filter.llm_tool(name="bugland_yearly_summary")
    async def bugland_yearly_summary(self, event: AstrMessageEvent, username: str = "") -> str:
        '''查询玩家 2025 年度总结（聚合统计数据）。

        当用户问"年度总结/21年打了多少/去年统计"时调用。

        Args:
            username(string): 要查询的布吉岛玩家昵称。留空表示查当前用户自己绑定的账号；如果用户@了某个人想查那个人，直接传 "@"（会自动用消息里被@的人的绑定）。
        '''
        target, err = await self._resolve_target(event, username)
        if err:
            return err
        ok, data = await self._request("/player/2025total", {"username": target})
        if not ok:
            return f"查询「{target}」的年度总结失败：{data}"
        if not data:
            return f"没查到「{target}」的年度总结。"
        blocks = self._stat_card_blocks(data) or [
            {"type": "note", "text": "这份年度总结没有可展示的数据"}
        ]
        sent = await self._send_card(event, f"{target} · 2025 年度总结", "年度数据", blocks)
        if sent:
            return "已经把年度总结画成图片发给用户了。用一两句话说说亮点即可。"
        return f"「{target}」的 2025 年度总结：\n{self._render(data)}"

    @filter.llm_tool(name="bugland_card")
    async def bugland_card(self, event: AstrMessageEvent, spec: str) -> str:
        '''按需要自由组合模块，画成一张图片发给用户。

        先用其它查询工具拿到数据，再把用户关心的内容拼成 spec 发图。
        用户说"给我画个图/做成图/整理成表格/对比一下"时就该用这个。

        画布吉岛战绩卡时统一走这套格式：
        - 用 section 按细分模式分组，标题写全「大类-小类」，例如"起床战争-经验4队8人""起床战争-4队4人"，不要合并成一个大类；
        - 每个模式给 场次 / 胜场 / 胜率 / K/D，再用 rows 列击杀、死亡、终结击杀、被终结、床被破坏；
        - 每个模式后面必须带"道具使用"和"升级"两个 section（bars 展示火焰弹、鸡蛋、金苹果、保护、锋利、陷阱等）；
        - 场次太少的模式用 note 标注样本不足。

        spec 是一个 JSON 字符串，结构如下（blocks 按顺序从上往下排）：
        {
          "title": "卡片标题",
          "subtitle": "副标题，可省略",
          "footer": "底部小字，可省略",
          "blocks": [
            {"type": "hero", "label": "总场次", "value": "57", "unit": "场"},
            {"type": "kpi", "items": [{"label": "胜场", "value": "48"}, {"label": "K/D", "value": "0.72"}]},
            {"type": "bars", "items": [{"label": "胜率", "value": 85.7}]},
            {"type": "rows", "items": [{"label": "公会", "value": "-"}]},
            {"type": "section", "text": "最近对局"},
            {"type": "table", "columns": ["时间", "模式", "结果"], "rows": [["11-20 00:22", "起床战争", "胜"]]},
            {"type": "note", "text": "补充说明"},
            {"type": "divider"}
          ]
        }

        Args:
            spec(string): 上面这种结构的 JSON 字符串。bars 的 value 可以是 0~1 的小数或百分数，文字会自动算；kpi 一行三个；table 最多 5 列 12 行。
        '''
        try:
            payload = json.loads(spec) if isinstance(spec, str) else spec
        except Exception as e:  # noqa: BLE001
            return f"spec 不是合法 JSON：{e}"

        if not isinstance(payload, dict):
            return "spec 需要是一个 JSON 对象。"

        title = str(payload.get("title") or "布吉岛数据")[:40]
        subtitle = str(payload.get("subtitle") or "")[:80]
        footer = str(payload.get("footer") or "数据来源：BuGLand API")[:80]
        blocks = normalize_blocks(payload.get("blocks"))
        if not blocks:
            return (
                "blocks 是空的或者类型都不认识。可用类型：hero、kpi、bars、rows、"
                "table、section、note、divider。"
            )

        sent = await self._send_card(event, title, subtitle, blocks, footer)
        if not sent:
            return "图片没发出去，可能是渲染开关关掉了。改用文字把关键数据告诉用户。"
        return "已经按 spec 生成并发送图片了。回复时用一两句话说明这是什么东西，不要重复图里的数字。"

    async def terminate(self) -> None:
        self._cache.clear()
        logger.info("布吉岛查询插件已卸载。")
