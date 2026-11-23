# 布吉岛查询

基于 [BuGLand API](https://mcbjd.net/docs/api/bugland-api) 的 AstrBot 插件，用 LLM 工具实现"说人话就能查"。

## 配置

在 WebUI → 插件管理 → 布吉岛查询 里填：

| 配置项 | 说明 |
| --- | --- |
| `api.token` | 布吉岛 API Token。游戏大厅输入 `/openapi` 申领（等级 > 20），30 天有效 |
| `api.api_base` | 默认 `https://api.mcbjd.net/v2` |
| `behavior.allow_query_others` | 是否允许查别人，默认开 |
| `behavior.cache_ttl` | 相同查询缓存秒数，默认 30，省配额 |

## 用法

直接 @机器人说人话，模型会自己选工具：

- 「绑定布吉岛 你的游戏昵称」/「绑错了，重绑 XXX」
- 「我绑的是谁来着」「解绑」
- 「查一下 XXX 的信息」「我什么段位」
- 「我起床战争胜率多少」「查 XXX 的空岛战绩」
- 「排行榜」「XXX 在榜上吗」
- 「我的公会」「查一下公会 XX」
- 「最近打了什么」「上一把赢了没」
- 「这把对局详情」（配合对局记录里的 matchId + date）

> 注意：QQ 官方机器人平台的群消息不会把「@的是哪个成员」传给机器人（`content` 里 @ 会被替换成空格，`mentions`/`msg_elements` 都是空的），
> 所以在 QQ 官方平台上只能直接写游戏昵称来查别人。代码里保留了 @ 解析（At 组件 / raw_message.mentions），
> 换到 aiocqhttp 之类的平台会自动生效。

## 提供的工具

- `bugland_bind` 绑定 / 重新绑定（可带 QQ 号，两个平台共用一份）
- `bugland_link_qq` QQ官方机器人这边的绑定和 QQ 号打通
- `bugland_my_binding` 查看绑定
- `bugland_at_binding` 查看被@的人的绑定状态（需要平台支持传递 @ 信息）
- `bugland_unbind` 解绑
- `bugland_player_info` 玩家基础信息
- `bugland_game_stats` 某模式战绩（自动算胜率、K/D）
- `bugland_leaderboard` 排行榜
- `bugland_guild` 公会
- `bugland_game_log` 对局记录
- `bugland_match_detail` 单局详情
- `bugland_yearly_summary` 2025 年度总结

## 队伍胜率预测

发一张起床战争的 Tab 玩家列表截图，问「哪队会赢」，插件会：

1. 用视觉模型把队伍和玩家ID读出来（模型可在插件设置里选，默认 gemini-4-argon）
2. 逐个查起床战绩，套幻梦评分算出每人分数
3. 按战力 `exp((评分-115)/70)` 累加成队伍战力，再用 `战力^2.5 / Σ战力^2.5` 归一化成胜率
4. 查不到战绩的按 100 分（新人）处理，并在结果里标出来

识别出来的 ID 偶尔会有错字，查不到数据的名字可以手动核对一下。

## 注意

- 接口有频率限制（普通玩家 30 次/分），插件内做了缓存
- 严禁用该接口做 Overlay 等不公平优势功能
