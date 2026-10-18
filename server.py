r"""OCS 题库后端：收到题目 -> 上网搜答案 -> DeepSeek 独立作答两遍 -> 一致才放行，不一致交终审 -> 缓存。

运行：双击同目录的 启动题库.bat
配置读同目录 .env：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL / PORT
（同名环境变量可临时覆盖，方便换端口测试）
只用标准库。
"""

import hashlib
import json
import os
import pathlib
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).parent
DB = ROOT / "questions.db"
LOG = ROOT / "requests.log"

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LABEL = re.compile(r"^\s*[A-Za-z]\s*[.、．)）:：]\s*")
SEPARATOR = "#"  # 多选题分隔符，OCS 源码 resolvePlainAnswer 的剥离正则认得它
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# 引擎顺序：必应实测最稳，其次搜狗；360/百度容易被限流，放最后
ENGINES = (
    "https://cn.bing.com/search?q=",
    "https://www.sogou.com/web?query=",
    "https://www.so.com/s?q=",
    "https://www.baidu.com/s?wd=",
)
BLOCK_MARKERS = ("验证码", "访问异常", "安全验证", "异常流量")
COOLDOWN_SECONDS = 600  # 被判定为封禁的引擎，歇 10 分钟再试
_cooldown = {}


def engine_ready(engine):
    return time.time() >= _cooldown.get(engine, 0)


def mark_blocked(engine):
    _cooldown[engine] = time.time() + COOLDOWN_SECONDS

# OCS 的判断题判定（packages/core/src/core/worker/resolvers/judgement.ts）只认这些文字，
# 返回字母会导致它一个选项都不选。
CORRECT_WORDS = ("是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1")
INCORRECT_WORDS = ("非", "否", "错", "错误", "×", "x", "错的", "不对", "不正确的", "不正确", "不是", "不是的", "false", "f", "no", "0")

FORMAT_RULE = (
    "输出必须是 JSON，不要任何解释文字："
    '{"answer": "你的答案", "why": "一句话依据"}；'
    "单选题 answer 写字母如 A；多选题写连续字母如 AC；判断题写“对”或“错”；填空题写要填的内容。"
)

SYSTEM = (
    "你是答题助手。用户会给你一道题、本题的选项，以及从网上搜到的资料。"
    "规则：1) 资料里的题目选项措辞和顺序可能和本题不同，必须先看懂资料里给的答案内容，"
    "再按内容匹配到本题对应的选项字母；2) 资料里有题库/试题网站给出的明确答案时，优先采用它；"
    "3) 资料没用或搜不到时，凭你自己的知识判断；"
    "4) 判断题（选项是“对/错”“正确/错误”这类）直接答“对”或“错”，不要答字母。"
    "5) 多选题特别规则：这类课程题目里，只要选项本身没有明显错误，**通常都应该选上**，"
    "多选往往是全选。不要用“它更像民生问题/属于别的领域”这种语义分类的理由排除选项；"
    "只有选项含明显错误或绝对化表述（如“完全自动化”“可以讲条件”“唯一”“只要…就…”）才排除。"
    "6) 否定问法（“不属于/不包括/不正确的是/错误的是”）：要选那个**不符合题意**的选项，"
    "其余几个都是正确表述、是干扰项，别选“看起来最正确”的那个。"
    + FORMAT_RULE
)

VERIFY_SYSTEM = (
    "你是复核阅卷老师，正在独立完成一道题。你看不到任何其他人的答案，请完全独立判断。"
    "特别注意：1) 带绝对化措辞的选项（“可以讲条件”“完全自动化”“唯一”“只要…就…”）"
    "通常是错的；2) “不包括/不正确/错误的是”这类否定问法，要选那个**错误**的表述；"
    "3) 多选题里，只要选项本身没明显错误就倾向选上——课程题目的多选常常是四项全选，"
    "不要用“它更像别的领域的问题”这种理由漏掉正确项。"
    "同样先看资料里题库给出的答案，再自己判断。" + FORMAT_RULE
)

ARBITER_SYSTEM = (
    "你是终审阅卷老师。现在这道题有两个互相矛盾的候选答案，其中最多一个正确。"
    "要求：先自己独立把题做一遍得出结论，再和候选答案比对，"
    "不要因为某个候选先出现就默认它对，也不要为了折中而选另一个。"
    "证据优先级：网上资料里题库给出的明确答案 > 你的独立判断 > 两个候选里理由更充分的一个。" + FORMAT_RULE
)

PER_OPTION_SYSTEM = (
    "你是复核阅卷老师，专门负责检查多选题有没有漏选。"
    "做法：把每个选项当成一道独立的小判断题，逐个判断它本身是否正确，"
    "绝对不要只凭整体印象一次性给出答案。"
    "判断每个选项时，只要它本身表述没有明显错误就算正确，"
    "不要用“这项更像民生/经济领域”之类的理由判它为错。"
    "逐项过完之后，再把所有判断为“正确”的选项字母连起来作为 answer。" + FORMAT_RULE
)

MISS_CHECK_SYSTEM = (
    "你是复核阅卷老师，专门复查多选题的**漏选**。"
    "下面给你题目、选项、资料和一个候选答案。请只针对**没有被选中的那些选项**逐个判断："
    "它是否其实也是正确的？判断标准：只要该选项本身表述没有明显错误，就应该选上；"
    "这类课程题目的多选题经常是**四项全选**，不要因为“它更像民生问题、属于别的领域”"
    "就把它排除。只有选项本身含明显错误或绝对化表述时才保持不选。"
    "最后给出你认为完整的答案（字母连写，如 ACD）。" + FORMAT_RULE
)

# 否定问法（"不属于/不包括/错误的是"）：正确选项是那个"不符合题意"的
NEGATION_MARKERS = ("不属于", "不包括", "不正确", "错误的是", "不符合", "不是为了", "除外")

NEGATION_SYSTEM = (
    "这是一道**否定题**：题干问的是“不属于 / 不包括 / 不正确 / 错误的是”。"
    "关键：正确答案是那个**不符合题意**的选项，其余几个都是正确表述、是干扰项。"
    "做法：先分别判断每个选项本身是对的、还是错的，再选出那个错的；"
    "不要因为某个选项“看起来很正确、很切题”就选它。"
    "最后只输出那个不符合题意选项的字母。" + FORMAT_RULE
)


def is_negation(title):
    return any(marker in title for marker in NEGATION_MARKERS)


# ---------------------------------------------------------------- 联网搜索

def http_get(url, timeout=12):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def html_to_text(html):
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        html = html.replace(entity, char)
    return re.sub(r"\s+", " ", html)


SOURCES = 3        # 每题固定从三个不同来源抓
PER_SOURCE = 1100  # 每个来源最多摘这么多字
ENGINE_NAMES = {
    "https://cn.bing.com/search?q=": "必应",
    "https://www.sogou.com/web?query=": "搜狗",
    "https://www.so.com/s?q=": "360",
    "https://www.baidu.com/s?wd=": "百度",
}


def web_evidence(title, options, limit=3000):
    """搜题干，从**最多三个不同来源**分别抓“答案”/选项附近的片段，合并后交给模型。
    三个来源都捞不到东西（不足 350 字）时，再用短题干兜底一轮。"""
    full = re.sub(r"[（）()【】\[\]？?。，,]", " ", title).strip()
    keywords = ["答案"] + [o for o in options if len(o) >= 2][:4] + [full[:12]]
    seen, snippets, report = set(), [], []

    def harvest(text, cap):
        taken = 0
        for keyword in keywords:
            for match in list(re.finditer(re.escape(keyword), text))[:2]:
                piece = text[max(0, match.start() - 200):match.start() + 220].strip()
                fingerprint = piece[:60]
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                snippets.append(piece)
                taken += len(piece)
                if taken >= cap:
                    return taken
        return taken

    def run(query):
        used = 0
        for engine in ENGINES:
            if used >= SOURCES:
                break
            if not engine_ready(engine):
                continue
            name = ENGINE_NAMES.get(engine, engine)
            try:
                text = html_to_text(http_get(engine + urllib.parse.quote(query)))
            except Exception:
                report.append(f"{name}:失败")
                continue
            if len(text) < 400 or any(marker in text for marker in BLOCK_MARKERS):
                mark_blocked(engine)  # 反爬页，不能当资料，让它歇一会儿
                report.append(f"{name}:被封")
                continue
            used += 1
            report.append(f"{name}:{harvest(text, PER_SOURCE)}字")
            if sum(len(s) for s in snippets) >= limit:
                break

    run(full)
    if sum(len(s) for s in snippets) < 350:
        run(full[:18] if len(full) > 18 else full)
    total = sum(len(s) for s in snippets)
    print(f"[搜索] {' '.join(report)}  → 合计 {total} 字", flush=True)
    return " ".join(snippets)[:limit]


# ---------------------------------------------------------------- 配置 / 调用

def load_env():
    cfg = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "PORT", "THINKING"):
        if os.environ.get(name):
            cfg[name] = os.environ[name]
    cfg["THINKING"] = (cfg.get("THINKING") or "disabled").lower()
    cfg["DEEPSEEK_BASE_URL"] = (cfg.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    cfg["DEEPSEEK_MODEL"] = cfg.get("DEEPSEEK_MODEL") or "deepseek-chat"
    return cfg


def question_block(title, options, kind):
    if options:
        listed = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(options))
        return f"题目：{title}\n选项：\n{listed}\n题型：{kind}"
    return f"题目：{title}\n题型：{kind}"


def chat(system, user, cfg, temperature=0.0):
    """一次模型调用，返回 (答案, 依据, 用量)。

    deepseek-flash 默认开启思维链，思考会吃光输出预算导致 content 为空，
    而且思维模式下 temperature 会被忽略（两遍就失去差异）—— 所以默认关掉。
    """
    if not cfg.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，请检查 .env")

    thinking = (cfg.get("THINKING") or "disabled").lower()
    payload = {
        "model": cfg["DEEPSEEK_MODEL"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": 2000 if thinking == "enabled" else 800,
        "thinking": {"type": thinking},
        "response_format": {"type": "json_object"},
    }

    def post(body):
        request = urllib.request.Request(
            cfg["DEEPSEEK_BASE_URL"] + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["DEEPSEEK_API_KEY"]},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        data = post(payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and "thinking" in payload:  # 老模型不认这个参数，去掉重试一次
            payload.pop("thinking")
            data = post(payload)
        else:
            raise

    def parse(response):
        content = response["choices"][0]["message"].get("content") or ""
        usage = response.get("usage", {})
        try:
            parsed = json.loads(content)
            return str(parsed.get("answer", "")).strip(), str(parsed.get("why", "")).strip(), usage
        except (ValueError, AttributeError):
            return content.strip(), "", usage

    answer, why, usage = parse(data)
    if not answer:  # 空回复（例如思考过程吃光输出预算）→ 再试一次
        answer2, why2, usage2 = parse(post(payload))
        usage = add_usage(usage, usage2)
        if answer2:
            return answer2, why2, usage
    return answer, why, usage


def majority_answer(votes, options):
    """按选项投票：某个选项被**半数及以上**的结果选中就选它（偏向不漏选）。"""
    sets = [set(key.split(SEPARATOR)) for key in votes if key and not key.startswith("?")]
    if not sets:
        return ""
    picked = [
        LETTERS[i]
        for i in range(len(options))
        if sum(1 for chosen in sets if LETTERS[i] in chosen) * 2 >= len(sets)
    ]
    return SEPARATOR.join(picked)


def pick_most_common(keys):
    """单选/判断用：取出现次数最多的那个答案（并列时优先靠前的那遍）。"""
    counts = {}
    for index, key in enumerate(keys):
        if not key or key.startswith("?"):
            continue
        counts.setdefault(key, [0, index])
        counts[key][0] += 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1][0], -kv[1][1]))[0]


def solve(title, options, kind, evidence, cfg):
    """两遍独立作答；多选题再加一遍逐项核对并投票；仍然分歧才交终审。
    返回 (答案, 依据, 用量, 说明)。"""
    base = question_block(title, options, kind) + "\n\n网上搜到的资料：\n" + (evidence or "（没有搜到有用资料）")

    first, why1, usage1 = chat(SYSTEM, base, cfg, temperature=0.0)
    key1 = canonical(first, kind, options)
    if key1.startswith("?"):
        return first, why1, usage1, "单遍(答案无法归一化)"

    second, why2, usage2 = chat(VERIFY_SYSTEM, base, cfg, temperature=0.7)
    key2 = canonical(second, kind, options)
    usage = add_usage(usage1, usage2)

    if kind == "single" and not is_judgement(options) and is_negation(title):
        # 否定问法（"不属于/不包括"）：模型容易选"看起来最正确"的那个，专门再判一遍并投票
        third, why3, usage3 = chat(NEGATION_SYSTEM, base, cfg, temperature=0.0)
        usage = add_usage(usage, usage3)
        key3 = canonical(third, kind, options)
        picked = pick_most_common([key1, key2, key3])
        if picked:
            return picked, why3 or why1, usage, f"否定题三遍[{key1}/{key2}/{key3}]"

    if kind == "multiple":
        # 多选题的典型错误是"漏选"：先逐项核对，再专门复查没被选中的选项，最后四项投票
        third, why3, usage3 = chat(PER_OPTION_SYSTEM, base, cfg, temperature=0.0)
        miss_input = base + f"\n\n候选答案：{key1}"
        fourth, why4, usage4 = chat(MISS_CHECK_SYSTEM, miss_input, cfg, temperature=0.0)
        usage = add_usage(add_usage(usage, usage3), usage4)
        key3 = canonical(third, kind, options)
        key4 = canonical(fourth, kind, options)
        voted = majority_answer([key1, key2, key3, key4], options)
        note = f"多选四项投票[{key1}/{key2}/{key3}/{key4}]"
        if voted and len(voted.split(SEPARATOR)) == 1 and len(options) >= 3:
            # 多选题只选出一个 = 高度可疑的漏选（常发生在没搜到资料、模型保守时）。
            # 这时不能靠投票——四遍可能是同一个方向漏选，必须专门挑战一次。
            challenge = (
                base
                + f"\n\n注意：这是一道多选题，但前面几轮只选出了 {voted} 这一个选项。"
                "多选题的标准答案通常有 2-4 个正确选项，只选出一个往往是漏选。"
                "请重新逐个审查每个选项，指出还有哪些选项其实也正确。"
            )
            fifth, why5, usage5 = chat(MISS_CHECK_SYSTEM, challenge, cfg, temperature=0.0)
            usage = add_usage(usage, usage5)
            key5 = canonical(fifth, kind, options)
            if not key5.startswith("?"):
                merged = set(voted.split(SEPARATOR)) | {k for k in key5.split(SEPARATOR) if k}
                merged.discard("")
                if len(merged) > 1:
                    voted = SEPARATOR.join(sorted(merged))
                    note += f" → 单选项复查补为 {voted}[第5遍:{key5}]"
            note += f" | 复查依据:{why5[:40]}"
        if voted:
            return voted, why4 or why3 or why1, usage, note

    if key2 == key1:
        return first, why1, usage, "两遍一致"

    arbiter_input = (
        base
        + f"\n\n候选答案一：{first}（理由：{why1 or '无'}）"
        + f"\n候选答案二：{second}（理由：{why2 or '无'}）"
        + "\n\n请独立判断后给出最终答案。"
    )
    final, why_final, usage_final = chat(ARBITER_SYSTEM, arbiter_input, cfg, temperature=0.0)
    return final or first, why_final, add_usage(usage, usage_final), "终审"


def add_usage(*usages):
    total = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


# ---------------------------------------------------------------- 答案归一化

def _clean(text):
    return re.sub(r"[\s。．.,，、（）()【】\[\]！!？?]", "", (text or "")).lower()


def _polarity(text):
    """True=正确, False=错误, None=看不出来。"""
    cleaned = _clean(text)
    if cleaned in {_clean(w) for w in CORRECT_WORDS}:
        return True
    if cleaned in {_clean(w) for w in INCORRECT_WORDS}:
        return False
    return None


def is_judgement(options):
    """选项是两个“对/错”性质的选项 —— 无论 type 是 judgement 还是 single，都要按判断题处理。"""
    if len(options) != 2:
        return False
    polarities = [_polarity(o) for o in options]
    return True in polarities and False in polarities


def judgement_text(answer, options):
    """把答案换成选项原文（对/错），OCS 才选得中。"""
    letter = (answer or "").strip().upper()
    if len(letter) == 1 and letter in LETTERS:
        index = LETTERS.index(letter)
        if index < len(options):
            return options[index]
    want = _polarity(answer)
    if want is not None:
        for option in options:
            if _polarity(option) is want:
                return option
    return answer


def normalize(answer, kind):
    """单选题只保留第一个字母；多选题统一成 A#B 形式。"""
    text = (answer or "").upper()
    letters = [ch for ch in text if ch in LETTERS]
    if not letters:
        return (answer or "").strip().strip("。. ").strip()
    if kind == "multiple":
        return SEPARATOR.join(sorted(set(letters)))
    return letters[0]


def canonical(answer, kind, options):
    """把一个答案压成可比较的规范形式，用于判断两遍是否一致。"""
    text = (answer or "").strip()
    if kind == "judgement" or is_judgement(options):
        polarity = _polarity(text)
        if polarity is None:
            head = text.upper()[:1]
            if head in LETTERS and LETTERS.index(head) < len(options):
                polarity = _polarity(options[LETTERS.index(head)])
        if polarity is not None:
            return "T" if polarity else "F"
        return "?" + _clean(text)
    if kind == "multiple":
        if len(text) <= 12:  # 短答案基本就是字母形式（A / AC / A、C、D）
            letters = sorted({ch for ch in text.upper() if ch in LETTERS})
            if letters:
                return SEPARATOR.join(letters)
        cleaned = _clean(text)  # 长答案可能是选项原文，按内容反查选项序号
        picked = [LETTERS[i] for i, option in enumerate(options) if _clean(option) and _clean(option) in cleaned]
        if picked:
            return SEPARATOR.join(picked)
        return "?" + cleaned
    letters = [ch for ch in text.upper() if ch in LETTERS]
    if len(text) <= 2 and len(letters) == 1:
        return letters[0]
    cleaned = _clean(text)
    for index, option in enumerate(options):
        if _clean(option) and _clean(option) in cleaned:
            return LETTERS[index]
    return cleaned


# ---------------------------------------------------------------- 缓存

def cache_get(key):
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, answer TEXT, created TEXT)")
        row = conn.execute("SELECT answer FROM cache WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def cache_put(key, answer):
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, answer TEXT, created TEXT)")
        conn.execute("INSERT OR REPLACE INTO cache (key, answer, created) VALUES (?, ?, datetime('now'))", (key, answer))


# ---------------------------------------------------------------- HTTP

CONTROL_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>OCS AI 题库后端</title></head><body style="font:16px/1.8 system-ui;margin:40px;max-width:660px">
<h1 style="margin-bottom:4px">OCS AI 题库后端</h1>
<p style="color:#2a2">状态：运行中</p>
<p>答案缓存：<b>{count}</b> 条</p>
<p><a href="/clear" style="display:inline-block;padding:10px 20px;background:#c0392b;color:#fff;
border-radius:6px;text-decoration:none;font-size:17px">清空答案缓存</a></p>
<p style="color:#555">什么时候点它：<br>
· 某道题以前答错过，想让它重新算一遍<br>
· 想强制所有题重新搜索、重新判断（会重新花时间和钱）</p>
<p style="color:#999">正常答题不用管这个页面，它只是给你一个按钮。</p>
</body></html>"""


def cache_count():
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, answer TEXT, created TEXT)")
        return conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]


def cache_clear():
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, answer TEXT, created TEXT)")
        removed = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        conn.execute("DELETE FROM cache")
    return removed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    config = None

    def respond(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_params(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8", "replace")
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"--- {self.command} {self.path}\n{text}\n")
        try:
            return json.loads(text)
        except ValueError:
            return {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def respond_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/clear"):
            removed = cache_clear()
            print(f"[清缓存] 删除了 {removed} 条", flush=True)
            self.respond_html(f"<meta charset='utf-8'><div style='font:16px/1.8 system-ui;margin:40px'>"
                              f"<h2>缓存已清空</h2><p>删除了 {removed} 条记录。可以关掉这个页面继续答题了。</p>"
                              f"<p><a href='/'>返回</a></p></div>")
            return
        if self.path.startswith("/health"):
            self.respond({"code": 1, "msg": "OCS AI 题库后端在运行"})
            return
        self.respond_html(CONTROL_PAGE.replace("{count}", str(cache_count())))

    def do_POST(self):
        params = self.read_params()
        title = (params.get("title") or "").strip()
        kind = (params.get("type") or "single").strip() or "single"
        options = params.get("options") or []
        if isinstance(options, str):
            options = [line.strip() for line in options.splitlines() if line.strip()]
        options = [LABEL.sub("", o).strip() for o in options]

        if not title:
            self.respond({"code": 0, "msg": "没有收到题目"})
            return

        key = hashlib.sha256(f"v7|{kind}|{title}|{options}".encode("utf-8")).hexdigest()
        cached = cache_get(key)
        if cached:
            print(f"[缓存] {title[:32]} -> {cached}", flush=True)
            self.respond({"code": 1, "answer": cached})
            return

        evidence = web_evidence(title, options)
        try:
            raw, why, usage, note = solve(title, options, kind, evidence, self.config)
        except Exception as exc:
            print(f"[错误] {title[:32]} -> {exc}", flush=True)
            self.respond({"code": 0, "msg": f"AI 调用失败：{exc}"})
            return

        answer = judgement_text(raw, options) if (kind == "judgement" or is_judgement(options)) else normalize(raw, kind)
        if not answer:
            print(f"[空答案] {title[:32]} -> 模型没给出答案，没有缓存", flush=True)
            self.respond({"code": 0, "msg": "AI 这次没给出答案，请重新搜题一次"})
            return
        cache_put(key, answer)
        with LOG.open("a", encoding="utf-8") as fh:  # 答案 + 投票明细都记下来，方便事后复盘
            fh.write(f">>> 答案 = {answer}    [{note}]\n")
        print(f"[{kind}] {title[:32]} -> {answer}  [{note}]  依据: {why[:50]}  "
              f"(资料{len(evidence)}字, {usage.get('total_tokens', '?')} tokens)", flush=True)
        self.respond({"code": 1, "answer": answer})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    Handler.config = load_env()
    port = int(Handler.config.get("PORT") or 8899)
    print(f"OCS AI 题库后端已启动: http://127.0.0.1:{port}   模型: {Handler.config['DEEPSEEK_MODEL']}")
    print("每题：先联网搜答案，再独立作答两遍，两遍不一致交终审。保持本窗口开着。", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
