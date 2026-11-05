# 魔搭社区每日签到（AstrBot 插件）

[![GitHub](https://img.shields.io/badge/GitHub-yierlingyi%2Fastrbot__plugin__modelscope__daily-181717?logo=github)](https://github.com/yierlingyi/astrbot_plugin_modelscope_daily)

每天北京时间 **06:00** 依次为配置里的**每个账号**自动签到魔搭社区（ModelScope），
并把汇总结果主动推送到配置指定的会话。支持多账号（多个 Cookie / ms- 令牌）。

整体结果只有三种：

| 结果 | 什么时候出现 | 推送文案开头 |
| --- | --- | --- |
| 未配置账号 | 账号列表和旧版单项 Cookie 都为空 | ⚠️ 魔搭每日签到未执行 |
| 签到失败 | 至少一个账号 Cookie 失效、被风控、网络异常、账单里查不到今日到账 | ❌ 魔搭每日签到失败（1/2 个账号失败） |
| 签到成功 | 所有账号都签到成功 | ✅ 魔搭每日签到成功（2 个账号） |

推送里每个账号占一行，例如：

```
✅ 魔搭每日签到成功（2 个账号）
时间：2026-11-05 06:00:03（北京时间）
✅ 某个昵称：本次到账 +250 魔粒（每日登录 + 绑定阿里云）；当前余额 1200 魔粒（总计 1300）
✅ 另一个昵称：今天已签到过，本次无新增到账；今日合计 +250 魔粒；当前余额 250 魔粒
```

部分账号失败时标题变成「❌ 魔搭每日签到失败（1/2 个账号失败）」，成功的账号仍会列出。

单账号成功时还会区分本次是否真的新到账（重复签到不会重复发魔粒）：

| 情况 | 成功文案里的明细 |
| --- | --- |
| 今天第一次签到 | `本次到账 +250 魔粒（每日登录 + 绑定阿里云）` |
| 今天已经签到过（例如再发一次 `/ms签到`） | `今天已签到过，本次无新增到账；今日合计 +250 魔粒` |
| 签到前账单没读到 | 只报 `今日到账 +250 魔粒`，不做重复判断 |

无论成功还是失败，都会推送到指定会话（未配置推送会话时只写入日志）。

## 安装

方式一（从 GitHub 克隆，方便后续更新）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/yierlingyi/astrbot_plugin_modelscope_daily.git
```

方式二（手动）：把 `astrbot_plugin_modelscope_daily` 整个文件夹放到 AstrBot 的 `data/plugins/` 目录下。

然后在 AstrBot WebUI 的「插件」页重载插件，打开「插件 → 魔搭社区每日签到 → 配置」。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `cookies` | **魔搭账号列表（支持多账号）**。每个账号一条，填浏览器复制的整段 Cookie，或 `ms-` 开头的访问令牌。每日 06:00 按顺序依次签到。 |
| `cookie` | 旧版单账号 Cookie（可选，兼容用）。填了会和上面的账号列表一起签到；新配置建议只填 `cookies`。 |
| `notify_umo` | 签到结果推送会话。保存的是 AstrBot 的 **unified_msg_origin（UMO）**，不是裸 Session ID；点配置项右侧按钮从会话列表选择即可自动填入。手动填写格式为 `平台适配器ID:消息类型:会话ID`，如 `aiocqhttp:FriendMessage:123456789`（私聊）、`aiocqhttp:GroupMessage:987654321`（群聊）。只填 QQ 号/群号也能用（仅有一个平台时会按「私聊 → 群聊」依次尝试自动补全），但多平台时必须填完整 UMO。 |
| `request_timeout` | 请求魔搭接口的超时时间，默认 30 秒。 |

> 第一次配置时，如果会话列表里找不到目标会话：先私聊机器人发一条消息（或在目标群里 @ 机器人一次），
> 让 AstrBot 记录下这个会话，再回到插件配置里选择；也可以展开输入框手动填写 UMO，
> 格式如 `aiocqhttp:FriendMessage:123456789`。
>
> v1.1.1 起，如果 `notify_umo` 只填了号码，插件会在推送时自动补成
> `平台ID:FriendMessage:号码` 与 `平台ID:GroupMessage:号码` 依次尝试；补全或失败的原因都会写进日志。

### 如何获取 Cookie / 令牌

方式一（推荐，多账号方便）：打开 <https://modelscope.cn/my/myaccesstoken> 生成访问令牌，
把 `ms-` 开头的令牌逐个填进「魔搭账号列表」（这个令牌本身就是网页会话 Cookie 的值）。

方式二（复制 Cookie）：

1. 浏览器登录 <https://www.modelscope.cn>。
2. 按 `F12` 打开开发者工具，切到「网络 / Network」。
3. 刷新页面，点开任意一个 `www.modelscope.cn` 的请求。
4. 在「请求头 / Request Headers」里找到 `Cookie`，复制它的**完整值**。
5. 粘贴到「魔搭账号列表」里，一个账号一条。

Cookie / 令牌会过期。签到失败提示 `HTTP 401` 时，重新登录魔搭并更新对应那条即可。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/ms签到` | 手动立即为**所有账号**各签到一次，汇总结果直接回复在当前会话（方便测试 Cookie 是否有效、临时补签）。 |

每日 06:00 的自动签到结果按配置推送到 `notify_umo`。

同一天重复执行 `/ms签到` 也会有回复：签到接口是幂等的，不会重复发魔粒，
插件会先读一次当日账单，再提示「今天已签到过，本次无新增到账」并给出今日合计。

## 实现说明

- 多账号：按配置顺序逐个签到，账号之间间隔 1 秒，避免请求过于密集；单个账号失败
  不影响其他账号，最后汇总成一条消息推送。
- 签到触发：`GET https://www.modelscope.cn/api/v1/users/login/info`。
  该请求本身就会触发当日签到，重复调用是幂等的。
- 成功确认：`GET https://www.modelscope.cn/openapi/v1/magicubes/transactions`
  查询当日 `EARN` 且 `rule_key` 为 `daily_active` / `aliyun_bindlogin` 的到账记录；
  到账可能有延迟，查不到时会再等 3 秒重查，最多查 3 次。
  账单日期按北京时间自然日比较；如果接口返回带时区的时间会先换算成北京时间，
  不带时区的时间按北京时间解释。
- 重复签到区分：触发签到前会先读一次当日账单，和触发后的账单对比，判断是
  「本次新到账」还是「今天已签到过」；读失败时只报今日合计，不影响签到本身。
- 余额展示：`GET https://www.modelscope.cn/openapi/v1/magicubes/balance`，仅用于成功文案，
  查询失败不影响签到成功的结论。
- 时区：固定按北京时间（UTC+8）计算「今天」和执行时间，不依赖宿主机时区。
- 想改执行时间，改 `main.py` 顶部的 `CRON_HOUR` / `CRON_MINUTE` 后重载插件即可。

参考实现：[L0NE-6/ModelScope-Daily](https://github.com/L0NE-6/ModelScope-Daily)（MIT）。
接口路径与账单字段依据该实现和社区资料整理；如果魔搭调整了字段，插件会推送
「未能确认今日签到到账」并把当天的账单样本写进 AstrBot 日志，方便排查。

## 注意事项

- 本插件只使用 AstrBot 已内置的 `aiohttp`、`apscheduler`，无需额外安装依赖。
- 签到接口属于非公开接口，魔搭可能随时调整；接口变化时请更新插件或自行修改 `main.py` 顶部的常量。
- 修改配置保存后，AstrBot 会自动重载插件，定时任务随之重建，无需手动重启。
- 多账号签到是串行的，账号越多耗时越长（每个账号 3～6 个请求，间隔 1 秒）；账号很多时请留意执行时间。

## 开源协议

本项目使用 [MIT License](LICENSE)。

接口与签到机制的参考实现：[L0NE-6/ModelScope-Daily](https://github.com/L0NE-6/ModelScope-Daily)（MIT），
特此致谢。
