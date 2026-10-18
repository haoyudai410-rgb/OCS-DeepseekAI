# OCS AI 题库后端

[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](requirements.txt)

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

## 跑起来是什么样

这是真实运行输出（一道示例题：`下列哪个关键字是 Python 中用来定义函数的`）：

```
OCS AI 题库后端已启动: http://127.0.0.1:8899   模型: deepseek-flash
每题：先联网搜答案，再独立作答两遍，两遍不一致交终审。保持本窗口开着。
[搜索] 必应:220字 搜狗:1258字 360:1260字  → 合计 2738 字
[single] 下列哪个关键字是 Python 中用来定义函数的 -> A  [两遍一致]  依据: Python 使用 def 关键字定义函数，资料中明确给出答案为 A.def。  (资料2744字, 3253 tokens)
```

一行看懂：**三个搜索引擎一起找资料 → 两遍独立作答互相对照 → 一致才输出**，
`[两遍一致]` 说明这道题两轮答案相同；如果标 `[终审]`，就是模型自己拿不准、值得人工看一眼的题。
请求体 `{"title": "...", "options": [...], "type": "single"}` 返回 `{"code": 1, "answer": "A"}`。

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

> Windows 上如果敲 `python` 弹出的是 Microsoft Store（说明 PATH 里只有占位程序），
> 就把你真正的 `python.exe` 完整路径写进同目录的 `python-path.txt`（一行，例如 `C:\Python312\python.exe`），
> 然后继续双击 `启动题库.bat`。这个文件已被 `.gitignore` 忽略，不会传到仓库。

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
启动题库.bat          Windows 一键启动（自动找 Python）
python-path.txt       可选：本机 python.exe 完整路径，找不到 Python 时用（不进仓库）
.env.example          配置模板，复制成 .env 再填
requirements.txt      只是占位：本项目零第三方依赖
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

## 安全提醒

这个项目要花你自己的 API 额度，所以有几条值得提前知道：

1. **API Key 只存在本机 `.env` 里**，已被 `.gitignore` 挡在仓库之外。clone 本项目的人拿到的是没有密钥的空壳，不填自己的 key 就直接报错——不存在共享同一个 key 的情况。
2. **服务只监听 `127.0.0.1`**（见 `server.py` 最后一行），同一 wifi 下的其他人连不上这个端口，无法蹭用你正在跑的服务。
3. **不要把监听地址改成 `0.0.0.0`，也不要挂内网穿透**（ngrok / frp 等），除非你另外加了鉴权——那等于把你的额度公开给互联网。
4. **去 DeepSeek 控制台设一个月度消费上限**。这是唯一能兜住「万一」的硬性保险：真出问题顶多烧到上限，而不是无限扣费。
5. **怀疑 key 泄露的应急动作**：控制台里删掉旧 Key 重建一个，把新值填回 `.env`，重启服务即可，不用改任何代码。

推送前自查一句：`git status` 里不该出现 `.env`、`questions.db`、`requests.log`。

## 免责声明

- 本项目用于**学习 Python 标准库、HTTP 服务与 LLM 调用**，以及自测、复习等合法场景。
- 请遵守你所在学校的考试规定和当地法律法规。用它代替考试属于作弊，后果由使用者自负。
- 运行时会向搜索引擎和 DeepSeek 发送题目文本，请自行评估隐私风险。
- 答案由大模型生成，**可能出错**，不要当作事实依据。

## License

[MIT](LICENSE)
