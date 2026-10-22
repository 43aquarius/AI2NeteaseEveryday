# AI2NeteaseEveryday

网易云音乐「每日一歌」自动发布机器人。

每天 08:00（北京时间）从**每日推荐歌曲**中挑一首，由 LLM 生成走心文案，渲染
**专属卡片配图**发布图文动态，自动挂载**歌手乐迷团 + 热门话题**扩大曝光；
08:00~23:00 每 2 小时巡检评论区，**自动回复乐迷评论**。

## 栏目形态

```
[自定义卡片配图: 黑胶+专辑封面+歌名+文案]  (note 模式)
或 [歌曲卡片: 歌名 - 歌手 / 专辑封面]     (song 模式, 可点击播放)

夏夜的蝉鸣里，这首歌像一阵凉风拂过心间。
那些说不出口的话，在钢琴声里找到了归宿。

#每日一歌# #ヨルシカ# #今日份听歌打卡#
```

- **发布模式**（`config.post_mode`）：
  - `note`（默认）：图文笔记 + Playwright 渲染的 1080×1440 卡片配图
    （网易云红黑胶风格：专辑封面 + 歌名歌手 + 文案 + 话题胶囊），失败自动降级；
  - `song`：歌曲卡片动态（可点击播放，自动带专辑封面）。
- **话题挂载**（`activityInfoList`，2026-10 实测验证）：
  - 歌手乐迷团：自动匹配已加入乐迷团中本曲歌手的 `boardId`（type=3/subType=11）；
  - 文本话题：`今日份听歌打卡` 等已验证话题（type=2/subType=0）；
  - 正文格式铁律：文案与话题行之间**单个 `\n`**，话题行**末尾带空格**。
- **选歌**：每日推荐按日期做种随机（同日可复现）；不可用降级热歌榜。
- **评论回复**：他人评论 → LLM 生成 10~45 字走心回复 → xeapi 加密回复，
  失败自动降级 `@昵称` 提及式评论；`replied_comments` 防重复，每轮限 5 条。

## 技术

纯 Python 实现，无 Node.js 依赖：

| 模块 | 说明 |
|---|---|
| `netease_client.py` | weapi/eapi/**xeapi** 三协议加密、日推/榜单/歌曲详情、图文/歌曲发布、NOS 图片上传三步链路、评论读取/回复/删除、动态回读 |
| `image_maker.py` | HTML+Playwright 卡片配图渲染（黑胶+封面+文案） |
| `daily_post.py` | 内容管线：选歌 → LLM 文案 → 卡片渲染上传 → 发布+挂载 → 回读验证 |
| `comment_reply.py` | 评论巡检：扫描近 7 天动态 → LLM 回复 → xeapi/降级发送 |
| `scheduler.py` / `run_daemon.py` | 08:00 发帖 + 评论巡检(2h) 守护进程，凭据缺失优雅等待 |
| `main.py` | CLI（status / post / verify / **reply** / scheduler） |
| `config.py` | 三层凭据持久化（config.json → ~/.secrets → base64 快照位） |
| `rebuild_config.py` | cookie 一键重建（参数/交互/环境变量） |

### 关键接口笔记（2026-10 实测）

- **图文发布**：`/eapi/note/share/friends/resource`，`pics` 为图片ID JSON串
  （`upload_image()` 产物），`activityInfoList` 挂载乐迷团/话题；
  ⚠ `uuid` 是幂等键，**每次必须新生成**，复用会返回旧帖 ID 造成"假成功"。
- **歌曲分享**：`/eapi/share/friends/resource`（eapi + 伪装 android：
  `os=android`/`appver=9.1.65`/Android UA）；**歌曲卡片模式不支持自定义配图**
  （服务端剥离 pics）。
- **图片上传三步**：`/eapi/nos/token/alloc` 拿凭证 → NOS `PUT` 原始字节
  （节点来自 `wanproxy.127.net/lbs`）→ `/eapi/upload/event/img/v1` 换多尺寸 ID。
- **评论回复已升级 xeapi**（2026-10 实测 eapi/weapi 全部 404）：
  - 公钥：明文 `POST /api/bsr/sk/get`（HMAC-SHA256 签名，key 为签名字符串
    本身的 UTF-8 字节，不做 base64 解码）；
  - 请求：AES-ECB 双层 + mid 变换 + X25519 ECDH + AES-GCM 动态密钥，
    POST `/xeapi/v1/resource/comments/reply`（C/S/R 三段密文）；
  - 评论 add 仍可用 eapi；读取评论明文 `/api/v1/resource/comments/{threadId}`。
- **乐迷团**：`/eapi/social/fansgroup/bff/user/groups/get` 列已加入乐迷团，
  `detail/get?groupId=` 取 `boardId`；threadId 格式 `A_EV_2_{eventId}_{uid}`。

## 快速开始

```bash
pip install -r requirements.txt
python3 rebuild_config.py "MUSIC_U=...; __csrf=..."   # 首选: 一键三层写入
python3 main.py status               # 验证登录态
python3 main.py post --dry-run       # 干跑看效果(含卡片渲染)
python3 main.py post                 # 真实发布
python3 main.py reply --dry-run      # 干跑评论回复
python3 run_daemon.py                # 拉起守护进程(发帖+评论巡检)
```

获取 cookie：浏览器登录 music.163.com → F12 → Network 任意请求 → 复制完整
Cookie（关键字段：`MUSIC_U`、`__csrf`）。

⚠ 经验（2026-10-22）：短时间高频发帖+删帖测试会触发风控导致 cookie 失效
（MUSIC_U 被服务端作废）。日常频率（1 帖/天 + 少量回复）安全。

## 凭据三层持久化（沙箱重启生存设计）

运行环境（沙箱）会不定期重启，实测规律：**快照点之后新建/修改的文件会被
清除**，而 `config.json` 在 `.gitignore` 中、从不进快照 → 单纯放在项目里
必然丢失（2026-10-22 已实际发生）。因此凭据做三层存储，加载时逐层回退、
缺失自动回写自愈（见 `config.py`）：

| 层 | 路径 | 存活机制 |
|---|---|---|
| 1 运行位 | `{项目}/config.json` | 随时可用，重启可能丢 |
| 2 项目外 | `/home/z/.secrets/netease_config.json` | 不属快照管辖，重启免疫 |
| 3 快照追踪位 | `{项目}/secrets_backup.json` | base64 包装，随平台快照可靠存活 |

⚠ 第 3 层含凭据（虽 base64 包装），**严禁推送 GitHub**——推送脚本永远使用
显式文件清单，本文件不在任何清单中。

## 配置

`config.json`：

```json
{
  "cookie": "MUSIC_U=...; __csrf=...",
  "post_time": {"hour": 8, "minute": 0},
  "post_mode": "note",
  "topic_tag": "每日一歌",
  "llm": {"style": "warm", "min_len": 30, "max_len": 90},
  "comment_reply": {"enabled": true, "max_per_run": 5, "scan_days": 7}
}
```

## 声明

- 仅供个人学习与自动化记录，请遵守网易云音乐服务条款，控制发布频率。
- `config.json` / `state.json` / `secrets_backup.json` 均不推送 GitHub，
  **cookie 永不入公开仓库**。
