# OCS AI 题库后端

> 一个跑在自己电脑上的小服务，给 **OCS 题库插件**提供 AI 答题接口：收到题目 → 上网搜资料 →
> DeepSeek 独立作答两遍 → 两遍一致才采用，不一致自动终审 → 结果缓存。

A tiny local HTTP service that answers exam questions for the OCS question-bank plugin by
combining web search with DeepSeek, double-checking every answer with two independent passes.
Pure Python standard library — no dependencies.

## 特点

- **只用标准库**，不用 `pip install` 任何东西，Python 3.8+ 直接跑。
- **两遍独立作答**：同一道题用两个不同角色提示（答题助手 / 复核阅卷老师）、不同温度各答一遍，
  归一化后一致才采用；不一致自动交第三轮「终审」。日志里会标 `[两遍一致]` 和 `[终审]`，
  标 `[终审]` 的就是模型自己都拿不准的题。代价是每题 token 翻倍、耗时 4–6 秒。
- **按答案内容匹配选项**：搜到的题目选项顺序常和卷子不同，所以匹配内容而不是照抄字母。
- **判断题返回「对/错」文字**（OCS 源码要求文字，返回字母一个选项都不会选）。
- **搜索失败不卡住**：自动退回「只靠模型知识作答」。
- **缓存**：同一道题第二次遇到直接读本地 SQLite，不再花钱调 API。

## 快速开始

需要 Python 3.8 或更高版本。

```bash
git clone https://github.com/haoyudai410-rgb/OCS-DeepseekAI.git
cd OCS-DeepseekAI
```

配置密钥：

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

然后编辑 `.env`，至少填上 `DEEPSEEK_API_KEY`：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） | 无 |
| `DEEPSEEK_BASE_URL` | 接口地址，换服务商时改这里 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-chat` |
| `PORT` | 本地监听端口 | `8899` |
| `THINKING` | 思维链开关。默认 `disabled` —— 开启会吃光输出预算，而且思维模式下温度被忽略，两遍作答就失去差异 | `disabled` |

同名环境变量会覆盖 `.env` 里的值，方便临时换端口调试。

启动：

- **Windows**：双击 `启动题库.bat`（自动找 python；找不到会提示你填路径）
- **命令行**（任何平台）：`python server.py`

窗口在 = 服务在，关掉窗口就停了。重复启动不会冲突：第二次会提示 `already running` 然后退出。

自检：

```bash
curl http://127.0.0.1:8899/health
# {"code": 1, "msg": "OCS AI 题库后端在运行"}
```

## 在 OCS 里怎么配

题库配置是**数组**，可以挂多个题库。要加新题库，**只往里追加一条，不要整段替换**，否则会覆盖掉已有的题库。

```json
{
  "name": "本地AI题库",
  "homepage": "http://127.0.0.1:8899",
  "url": "http://127.0.0.1:8899/",
  "method": "post",
  "contentType": "json",
  "type": "GM_xmlhttpRequest",
  "headers": { "Content-Type": "application/json" },
  "data": { "title": "${title}", "options": "${options}", "type": "${type}" },
  "handler": "return (res)=> res.code === 1 ? [undefined, res.answer] : [res.msg, undefined]"
}
```

同一份内容也放在 [`ocs-bank-config.json`](ocs-bank-config.json) 里，可以直接粘。

`type` 必须是 `GM_xmlhttpRequest`：默认的 `fetch` 会被 https 网页的混合内容策略挡死。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/` | 请求体 `{"title": "...", "options": ["...", "..."], "type": "single / multi / judge"}`，返回 `{"code": 1, "answer": "A"}` 或 `{"code": 0, "msg": "原因"}` |
| `GET` | `/health` | 存活检查 |
| `GET` | `/clear` | 清空答案缓存 |

多选题答案用 `#` 分隔（OCS 源码解析认得这个分隔符）。

## 文件

```
server.py             全部逻辑（就这一个文件）
启动题库.bat          Windows 一键启动
.env.example          配置模板，复制成 .env 再填
ocs-bank-config.json  粘到 OCS 里的题库配置
.env                  你的密钥 —— 已在 .gitignore 里，不会被提交
questions.db          答案缓存（运行时生成）
requests.log          每次请求的原始记录，排错用（运行时生成）
```

## 出问题时先看这三样

1. 黑窗口还在不在？不在 → 服务没跑，OCS 必然报「题库连接失败」。
2. 浏览器打开 <http://127.0.0.1:8899/health> 有没有 JSON？没有 → 服务没起来。
3. 黑窗口里每题会打一行 `[single] 题目… -> A 依据:… (资料2600字, 1552 tokens)`。
   `资料0字` 说明搜索引擎被限流，退回了纯模型作答；`[终审]` 说明模型自己拿不准，值得人工看一眼。

## 免责声明

- 本项目用于**学习 Python 标准库、HTTP 服务与 LLM 调用**，以及自测、复习等合法场景。
- 请遵守你所在学校的考试规定和当地法律法规。用它代替考试属于作弊，后果由使用者自负。
- 运行时会向搜索引擎和 DeepSeek 发送题目文本，请自行评估隐私风险。
- 答案由大模型生成，**可能出错**，不要当作事实依据。

## License

[MIT](LICENSE)
