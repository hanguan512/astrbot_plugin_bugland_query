"""幻梦评分拟合脚本（开发用，插件运行不依赖它）。

用法：
    python fit_dream.py            # 读 cards.json，打印拟合权重和留一误差
    python fit_dream.py --apply    # 顺便把权重写回 main.py

cards.json 每条就是一张真实幻梦卡的原始数字：
    {"name": "喜欢上了宇", "game": 10193, "kills": 37953, "deaths": 10296, "score": 344.7}

模型结构（和 main._dream_rating 一致）：
    评分 = base + sqrt_w * sqrt(场次) + kd_w * ln(K/D + eps)

拟合用非负最小二乘，保证权重为正，评分对场次和 K/D 单调。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EPS = 0.1         # ln(K/D + eps) 里的 eps，网格搜过 0.02~1.0，0.1 最优
KD_CAP = 20.0


def features(card: dict) -> list[float]:
    game = float(card["game"])
    kills = float(card.get("kills") or 0)
    deaths = float(card.get("deaths") or 0)
    kd = kills / deaths if deaths > 0 else kills
    kd = min(max(kd, 0.0), KD_CAP)
    return [1.0, math.sqrt(game), math.log(kd + EPS)]


def fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    try:
        from scipy.optimize import nnls

        return nnls(X, y)[0]
    except Exception:
        w = np.linalg.lstsq(X, y, rcond=None)[0]
        return np.clip(w, 0.0, None)


def main() -> None:
    path = os.path.join(HERE, "cards.json")
    cards = json.load(open(path, encoding="utf-8"))
    X = np.array([features(c) for c in cards])
    y = np.array([float(c["score"]) for c in cards])
    w = fit(X, y)
    pred = X @ w

    print("拟合权重:")
    for name, v in zip(("base", "sqrt_game", "ln_kd"), w):
        print(f"  {name:10s} {v:8.3f}")
    print("\n样本内:")
    for c, p in zip(cards, pred):
        print(f"  {c['name']:12s} 预测 {p:6.1f}  真实 {c['score']:6.1f}  误差 {p - c['score']:+6.1f}")
    if len(cards) > 3:
        errs = []
        for i in range(len(cards)):
            mask = [j for j in range(len(cards)) if j != i]
            wi = fit(X[mask], y[mask])
            errs.append(float(X[i] @ wi - y[i]))
        print(f"\n留一误差: 最大 {max(abs(e) for e in errs):.1f} / 平均 {np.mean(np.abs(errs)):.1f}")

    if "--apply" in sys.argv:
        mp = os.path.join(HERE, "main.py")
        src = open(mp, encoding="utf-8-sig").read()
        names = ("_DREAM_BASE", "_DREAM_SQRT_W", "_DREAM_KD_W")
        for name, v in zip(names, w):
            src = re.sub(rf"{name} = [-\d.]+", f"{name} = {v:.2f}", src)
        open(mp, "w", encoding="utf-8-sig").write(src)
        print("\n已写回 main.py")


if __name__ == "__main__":
    main()
