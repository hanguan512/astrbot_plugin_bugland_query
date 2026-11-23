"""幻梦评分拟合脚本（开发用，插件运行不依赖它）。

用法：
    python fit_dream.py            # 读 cards.json，打印拟合权重和留一误差
    python fit_dream.py --apply    # 顺便把权重写回 main.py

cards.json 每条就是一张真实幻梦卡的原始数字：
    {"name": "喜欢上了宇", "game": 10193, "win": 7757, "kills": 37953,
     "deaths": 10296, "score": 344.7}

模型结构（和 main._dream_rating 一致）：
    评分 = base + lng_w*ln(场次) + wr_w*胜率 + lnkd_w*ln(K/D+eps) + kdlin*K/D

只用三个特征，样本内和留一误差都在 2 分内。换结构前先看这里，
之前的「ln(K/D)」和「场次饱和 + 二次惩罚」版本都被真实卡推翻过。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EPS = 0.1        # ln(K/D + eps) 的平滑项，网格搜过 0.05~0.5，0.1 最优
KD_CAP = 20.0    # K/D 封顶，防止 0 死号把分数拉爆


def features(card: dict) -> list[float]:
    game = float(card["game"])
    win = float(card.get("win") or 0)
    kills = float(card.get("kills") or 0)
    deaths = float(card.get("deaths") or 0)
    winrate = min(max(win / game, 0.0), 1.0) if game else 0.0
    kd = min(kills / deaths if deaths > 0 else kills, KD_CAP)
    return [1.0, math.log(game), winrate, math.log(kd + EPS), kd]


def fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """三个特征权重都该是正的，截距可以为负。"""
    return np.linalg.lstsq(X, y, rcond=None)[0]


def main() -> None:
    path = os.path.join(HERE, "cards.json")
    cards = json.load(open(path, encoding="utf-8"))
    X = np.array([features(c) for c in cards])
    y = np.array([float(c["score"]) for c in cards])
    w = fit(X, y)
    pred = X @ w

    print("拟合权重:")
    for name, v in zip(("base", "ln(game)", "winrate", "ln(kd+eps)", "kd"), w):
        print(f"  {name:12s} {v:9.2f}")
    print("\n样本内:")
    for c, p in zip(cards, pred):
        print(f"  {c['name']:12s} 预测 {p:6.1f}  真实 {c['score']:6.1f}  误差 {p - c['score']:+6.1f}")
    print(f"\n样本内最大误差 {np.max(np.abs(pred - y)):.1f}")

    if len(cards) > 3:
        errs = []
        for i in range(len(cards)):
            mask = [j for j in range(len(cards)) if j != i]
            wi = fit(X[mask], y[mask])
            errs.append(float(X[i] @ wi - y[i]))
        print(f"留一误差: 最大 {max(abs(e) for e in errs):.1f} / 平均 {np.mean(np.abs(errs)):.1f}")

    if "--apply" in sys.argv:
        mp = os.path.join(HERE, "main.py")
        src = open(mp, encoding="utf-8-sig").read()
        names = ("_DREAM_BASE", "_DREAM_LNG_W", "_DREAM_WR_W", "_DREAM_LNKD_W", "_DREAM_KD_LIN")
        for name, v in zip(names, w):
            src = re.sub(rf"{name} = [-\d.]+", f"{name} = {v:.2f}", src)
        open(mp, "w", encoding="utf-8-sig").write(src)
        print("\n已写回 main.py")


if __name__ == "__main__":
    main()
