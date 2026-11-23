"""幻梦评分拟合脚本（开发用，插件运行不依赖它）。

用法：
    python fit_dream.py            # 读 cards.json，打印拟合权重和留一误差
    python fit_dream.py --apply    # 顺便把权重写回 main.py

cards.json 每条就是一张真实幻梦卡的原始数字：
    {"name": "喜欢上了宇", "game": 10193, "win": 7757, "kills": 37953,
     "deaths": 10296, "final_kills": 23129, "bed_lose": 5191, "score": 344.7}

特征和 main._dream_rating 完全一致（先按先验做贝叶斯收缩，再线性加权），
拟合用非负最小二乘，保证每个特征单调。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# 和 main.py 里的先验保持一致
PN = 24.0
PWR = 0.35
PKD = 1.20
PAF = 0.12
PBD = 0.15


def features(card: dict) -> list[float]:
    g = float(card["game"])
    win = float(card["win"])
    k = float(card["kills"])
    d = float(card["deaths"])
    fk = float(card["final_kills"])
    bed = float(card.get("bed_lose") or 0)
    wr = (win + PN * PWR) / (g + PN)
    kd = min((k + PN * PKD) / (d + PN), 60.0)
    af = (fk + PN * PAF) / (g + PN)
    bd = (bed + PN * PBD) / (g + PN)
    return [
        1.0,
        math.log1p(g / 40.0),
        wr - PWR,
        math.log(kd / PKD),
        af - PAF,
        bd - PBD,
    ]


def fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """非负最小二乘，权重为正是单调性的前提。"""
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
    for name, v in zip(
        ("base", "vol", "winrate", "kd", "avg_final", "bed_ratio"), w
    ):
        print(f"  {name:10s} {v:8.2f}")
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
        names = (
            "_DREAM_BASE",
            "_DREAM_VOL_W",
            "_DREAM_WR_W",
            "_DREAM_KD_W",
            "_DREAM_AF_W",
            "_DREAM_BD_W",
        )
        for name, v in zip(names, w):
            src = re.sub(rf"{name} = [-\d.]+", f"{name} = {v:.1f}", src)
        open(mp, "w", encoding="utf-8-sig").write(src)
        print("\n已写回 main.py")


if __name__ == "__main__":
    main()
