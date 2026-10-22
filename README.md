# AI2NeteaseEveryday

网易云音乐「每日一歌」自动发布机器人。

每天 08:00（北京时间）从**每日推荐歌曲**中挑一首，由 LLM 生成一段走心文案，以
**歌曲卡片动态**的形式发布到网易云音乐个人动态（自动带封面、可点击播放），
并携带 `#每日一歌#` 话题标签。

## 栏目形态

```
[歌曲卡片: 歌名 - 歌手 / 专辑封面]
夏夜的蝉鸣里，这首歌像一阵凉风拂过心间。
那些说不出口的话，在钢琴声里找到了归宿。
适合一个人戴上耳机，让情绪在星空下轻轻流淌。

#每日一歌#
```

- **选歌策略**：每日推荐 30 首中按日期做种随机选 1 首（同日可复现、每日轮换）；
  日推不可用时自动降级为热歌榜。
- **文案**：LLM 生成 30~90 字走心短文案，纯文本输出（无 JSON 解析风险），
  自动清洗 emoji/引号包裹/多余前后缀；3 次失败降级固定模板。
- **防重**：`state.json` 记录 `last_post_date`，同日不重发；发布后回读动态验证落地。

## 技术

纯 Python 实现，无 Node.js 依赖：

| 模块 | 说明 |
|---|---|
| `netease_client.py` | 网易云 API 客户端：weapi/eapi 加密（AES+RSA+MD5 摘要）、账号/日推/榜单/歌曲详情、分享歌曲动态、文字动态、删除动态、动态回读 |
| `daily_post.py` | 内容管线：选歌 → LLM 文案 → 发布 → 回读验证 |
| `scheduler.py` / `run_daemon.py` | 每日 08:00 定时 + 双 fork 守护进程 + 失败重试 |
| `main.py` | CLI（status / post / verify / scheduler） |
| `config.py` | 配置与 state 管理（北京时间） |

### 关键接口笔记（2026-10 实测）

- 分享歌曲到动态：`POST /eapi/share/friends/resource`，eapi 加密，**必须伪装
  android 客户端**（header.os=android、appver=9.1.65、Android UA），否则报
  「当前版本不支持分享歌曲」；不需要 xeapi/x25519。
- 删除动态：`POST /weapi/event/delete`，参数名是 `id`（不是 evId），路径是
  delete（不是 del）。
- 每日推荐：`/weapi/v1/discovery/recommend/songs`（需登录 cookie）；
  热歌榜：`/weapi/v6/playlist/detail?id=3779629`（降级路径）。
- eapi 加密：`AES-ECB(key=e82ckenh8dichen8)`，明文结构
  `{uri}-36cd479b6b5-{json}-36cd479b6b5-{md5}`。

加密协议参考 [NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)（MIT）
及其社区维护分支，全部用 Python 复刻。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入你的 cookie
python3 main.py status               # 验证登录态
python3 main.py post --dry-run       # 干跑看效果
python3 main.py post                 # 真实发布
python3 run_daemon.py                # 拉起每日调度守护进程
```

获取 cookie：浏览器登录 music.163.com → F12 → Network 任意请求 → 复制完整
Cookie（关键字段：`MUSIC_U`、`__csrf`）。

## 配置

`config.json`：

```json
{
  "cookie": "MUSIC_U=...; __csrf=...",
  "post_time": {"hour": 8, "minute": 0},
  "topic_tag": "每日一歌",
  "llm": {"style": "warm", "min_len": 30, "max_len": 90}
}
```

## 声明

- 仅供个人学习与自动化记录，请遵守网易云音乐服务条款，控制发布频率。
- `config.json` / `state.json` 已在 `.gitignore` 中，**cookie 永不入库**。
